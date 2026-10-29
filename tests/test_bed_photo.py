"""bed_photo.py: the table photo with a lifted bed. NOTHING here touches a real printer: `FakePrinter` simulates
the Z moves of a Moonraker adapter and records every G-code script, the camera is a fake that serves a new frame
after each grab, XDG dirs are tmp."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import bed_photo
import bed_view
from errors import GateFailure, InvalidArgument
from printer_ops.bed import BedConfig
from printer_ops.devices import Device, ResumeProfile

RESUME = ResumeProfile(
    "SM_PRINT_PREEXTRUDE_FILAMENT", 30, 0.5, 12, 5, 3, 1.75, 170, 10000, 275.0, ()
)
CFG = BedConfig(enabled=True)


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    return tmp_path


def device(*, access: str = "full", resume: ResumeProfile | None = RESUME) -> Device:
    return Device(
        "u1",
        "moonraker",
        "127.0.0.1",
        7125,
        "Snapmaker U1",
        Path("t.yaml"),
        access=access,
        resume=resume,
    )


class FakePrinter:
    """The Moonraker adapter surface bed_view uses; `G1 Z..` moves the toolhead; every script is recorded."""

    def __init__(
        self, state: str = "complete", z: float = 200.0, *, homed: str = "xyz"
    ) -> None:
        self.raw, self.z, self.homed = state, z, homed
        self.offset = 0.0  # toolhead Z minus gcode Z (the gcode Z offset in force)
        self.scripts: list[str] = []
        self.on_script: Callable[[FakePrinter], None] | None = None

    def query(self, objects: str) -> dict[str, Any]:
        return {
            "webhooks": {"state": "ready"},
            "print_stats": {"state": self.raw, "filename": "mole.gcode"},
            "toolhead": {
                "position": [100.0, 100.0, self.z, 0.0],
                "homed_axes": self.homed,
            },
            "gcode_move": {"gcode_position": [100.0, 100.0, self.z - self.offset, 0.0]},
        }

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        self.scripts.append(script)
        for line in script.splitlines():
            if line.startswith("G1 Z"):
                self.z = float(line.split()[1][1:]) + self.offset
        if self.on_script is not None:
            self.on_script(self)

    def file_metadata(self, filename: str) -> dict[str, Any]:
        return {}


class FakeCamera:
    """A live camera: every grab is a different frame, unless `frozen`."""

    def __init__(self, *, frozen: bool = False) -> None:
        self.grabs, self.frozen = 0, frozen
        self.fail: BaseException | None = None
        self.fail_after = 1  # the first grab (the resting frame, before the move) works
        self.on_grab: Callable[[int], None] | None = None

    def keepalive(self) -> bool:
        return False

    def grab(self) -> Any:
        self.grabs += 1
        if self.on_grab is not None:
            self.on_grab(self.grabs)
        if self.fail is not None and self.grabs > self.fail_after:
            raise self.fail
        return SimpleNamespace(
            jpeg=b"\xff\xd8frame"
            + (b"0" if self.frozen else str(self.grabs).encode())
            + b"\xff\xd9"
        )


def moves(printer: FakePrinter) -> list[str]:
    return [
        ln
        for s in printer.scripts
        for ln in s.splitlines()
        if ln.startswith(("G0", "G1"))
    ]


def shoot(
    printer: FakePrinter,
    out: Path,
    *,
    camera: FakeCamera | None = None,
    lift: str | float = 70.0,
    dry_run: bool = False,
    view: bed_photo.ViewFn | None = None,
    part: float | None = 20.0,
) -> dict[str, Any]:
    return bed_photo.capture_table_photo(
        device(),
        CFG,
        adapter=printer,
        camera=camera or FakeCamera(),
        out=out,
        lift=lift,
        part_height_mm=part,
        view=view,
        dry_run=dry_run,
        settle_s=0.0,
        sleep=lambda s: None,
    )


def test_the_bed_is_lifted_within_limits_photographed_and_put_back(
    tmp_path: Path,
) -> None:
    printer = FakePrinter(z=200.0)
    out = tmp_path / "table.jpg"
    doc = shoot(printer, out, lift=70.0)
    assert moves(printer) == [
        "G1 Z70.000 F600",
        "G1 Z200.000 F600",
    ]  # up to the photo Z, then back
    assert (
        printer.z == 200.0
        and doc["moved"]
        and doc["restored"]
        and doc["restore_error"] is None
    )
    assert (
        doc["z_mm"] == 70.0
        and doc["fresh"] is True
        and out.read_bytes().startswith(b"\xff\xd8frame")
    )
    assert (
        doc["plan"]["floor"] == 35.0 and doc["plan"]["ceiling"] == 270.0
    )  # part 20 + 15 clearance .. z_max 275 - 5
    assert (
        bed_view.view_in_progress("u1", 10**12) is False
    )  # the displaced-Z marker is cleared


def test_dry_run_reads_the_printer_and_sends_nothing(tmp_path: Path) -> None:
    printer, camera = FakePrinter(z=200.0), FakeCamera()
    doc = shoot(printer, tmp_path / "t.jpg", camera=camera, dry_run=True)
    assert (
        printer.scripts == []
        and camera.grabs == 0
        and not (tmp_path / "t.jpg").exists()
    )
    assert (
        doc["dry_run"]
        and doc["plan"]["move"]
        and doc["plan"]["target_z"] == 70.0
        and doc["path"] is None
    )


@pytest.mark.parametrize("state", ["printing", "paused", "busy"])
def test_a_printer_that_is_not_idle_is_refused_and_nothing_moves(
    tmp_path: Path, state: str
) -> None:
    printer, camera = FakePrinter(state=state), FakeCamera()
    with pytest.raises(GateFailure) as exc:
        shoot(printer, tmp_path / "t.jpg", camera=camera)
    assert "belongs to the job" in exc.value.message
    assert printer.scripts == [] and camera.grabs == 0


def test_a_not_homed_printer_or_unknown_part_height_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GateFailure, match="not homed"):
        shoot(FakePrinter(homed=""), tmp_path / "t.jpg")
    printer = FakePrinter()
    with pytest.raises(
        GateFailure, match="height of what may stand on the bed is unknown"
    ):
        shoot(printer, tmp_path / "t.jpg", part=None)
    assert printer.scripts == []


@pytest.mark.parametrize("lift", [20.0, 272.0])
def test_a_lift_outside_floor_and_z_max_minus_5_is_refused(
    tmp_path: Path, lift: float
) -> None:
    printer = FakePrinter()
    with pytest.raises(GateFailure, match="outside the allowed Z 35..270"):
        shoot(printer, tmp_path / "t.jpg", lift=lift)
    assert printer.scripts == []


def test_the_bed_is_restored_even_when_the_camera_fails(tmp_path: Path) -> None:
    printer, camera = FakePrinter(z=200.0), FakeCamera()
    camera.fail = OSError("camera unplugged")
    with pytest.raises(OSError, match="unplugged"):
        shoot(printer, tmp_path / "t.jpg", camera=camera)
    assert printer.z == 200.0 and moves(printer)[-1] == "G1 Z200.000 F600"


def test_the_restore_is_guarded_a_print_started_meanwhile_gets_no_move(
    tmp_path: Path,
) -> None:
    printer = FakePrinter(z=200.0)

    def start_print(p: FakePrinter) -> None:
        if p.z == 70.0:
            p.raw = "printing"

    printer.on_script = start_print
    doc = shoot(printer, tmp_path / "t.jpg")
    assert moves(printer) == ["G1 Z70.000 F600"]  # no second move into a running job
    assert doc["restored"] is False and "belongs to the job" in doc["restore_error"]
    assert Path(
        doc["path"]
    ).is_file()  # the photo exists; the failure is reported, never swallowed


def test_a_frozen_camera_is_reported_as_not_fresh(tmp_path: Path) -> None:
    doc = shoot(FakePrinter(), tmp_path / "t.jpg", camera=FakeCamera(frozen=True))
    assert doc["fresh"] is False and doc["restored"]


def test_a_bed_already_at_the_height_is_not_moved(tmp_path: Path) -> None:
    printer = FakePrinter(z=70.0)
    doc = shoot(printer, tmp_path / "t.jpg", lift=70.0)
    assert printer.scripts == [] and doc["moved"] is False and doc["z_mm"] == 70.0


def test_auto_picks_the_z_that_shows_the_plate_nearest_to_where_the_bed_is() -> None:
    share = {
        35.0: 0.5,
        40.0: 0.9,
        45.0: 0.95,
        50.0: 0.97,
        100.0: 0.96,
        150.0: 0.4,
        200.0: 0.15,
        270.0: 0.1,
    }
    z, why = bed_photo.pick_photo_z(
        lambda z: share.get(z, 0.2), 150.0, floor=35.0, ceiling=270.0, threshold=0.85
    )
    assert (
        z == 100.0 and "96%" in why
    )  # views within 10 points of the best (97%) tie: the nearest to Z 150 wins
    none, reason = bed_photo.pick_photo_z(
        lambda z: 0.3, 150.0, floor=35.0, ceiling=270.0, threshold=0.85
    )
    assert none is None and "no allowed Z" in reason


def test_auto_without_a_camera_calibration_refuses_instead_of_guessing(
    tmp_path: Path,
) -> None:
    with pytest.raises(GateFailure, match="no camera calibration"):
        shoot(FakePrinter(), tmp_path / "t.jpg", lift="auto", view=None)
    printer = FakePrinter(z=200.0)
    doc = shoot(
        printer,
        tmp_path / "t.jpg",
        lift="auto",
        view=lambda z: 0.95 if z <= 100 else 0.2,
    )
    assert doc["plan"]["target_z"] == 100.0 and moves(printer)[0] == "G1 Z100.000 F600"


def test_parse_lift() -> None:
    assert bed_photo.parse_lift("auto") == "auto" and bed_photo.parse_lift("70") == 70.0
    for bad in ("0", "-5", "nan", "high"):
        with pytest.raises(InvalidArgument):
            bed_photo.parse_lift(bad)


def test_an_offset_changed_during_the_camera_request_refuses_the_move(
    tmp_path: Path,
) -> None:
    printer, camera = FakePrinter(z=200.0), FakeCamera()
    # the resting frame is requested after the plan: the offset changes while it is in flight
    camera.on_grab = lambda n: setattr(printer, "offset", -50.0)
    with pytest.raises(GateFailure, match="Z offset changed"):
        shoot(printer, tmp_path / "t.jpg", camera=camera, lift=70.0)
    assert (
        printer.scripts == [] and printer.z == 200.0
    )  # nothing was sent: floor 35 is safe
    assert bed_view.view_in_progress("u1", 10**12) is False  # no marker left behind


def test_a_view_running_elsewhere_blocks_the_capture_before_anything_is_read(
    tmp_path: Path,
) -> None:
    reads: list[int] = []

    def read(adapter: Any) -> bed_view.Motion:
        reads.append(1)
        return bed_view.read_motion(adapter)

    for lift, z in ((70.0, 200.0), (70.0, 70.0)):  # the move path and the no-move path
        printer = FakePrinter(z=z)
        with bed_view.view_lock("u1") as held:
            assert held
            with pytest.raises(GateFailure, match="another bed view"):
                bed_photo.capture_table_photo(
                    device(),
                    CFG,
                    adapter=printer,
                    camera=FakeCamera(),
                    out=tmp_path / "t.jpg",
                    lift=lift,
                    part_height_mm=20.0,
                    dry_run=False,
                    settle_s=0.0,
                    sleep=lambda s: None,
                    read=read,
                )
        assert reads == [] and printer.scripts == []


def test_a_pending_marker_is_undone_first_so_the_original_z_survives(
    tmp_path: Path,
) -> None:
    printer = FakePrinter(
        z=70.0
    )  # stranded by an interrupted view that found it at Z 200
    bed_view._write_pending(
        "u1",
        bed_view.Motion("complete", "complete", 200.0, 200.0, "xyz", True, None),
        time.time(),
    )
    doc = shoot(printer, tmp_path / "t.jpg", lift=70.0)
    assert moves(printer) == ["G1 Z200.000 F600", "G1 Z70.000 F600", "G1 Z200.000 F600"]
    assert printer.z == 200.0 and doc["restored"] and doc["moved"]
    assert bed_view.view_in_progress("u1", time.time()) is False


def test_a_pending_marker_that_cannot_be_undone_is_never_overwritten(
    tmp_path: Path,
) -> None:
    printer, camera = (
        FakePrinter(z=70.0, homed=""),
        FakeCamera(),
    )  # recovery refuses: not homed
    bed_view._write_pending(
        "u1",
        bed_view.Motion("complete", "complete", 200.0, 200.0, "xyz", True, None),
        time.time(),
    )
    with pytest.raises(GateFailure, match="interrupted bed view"):
        shoot(printer, tmp_path / "t.jpg", camera=camera, lift=70.0)
    assert printer.scripts == [] and camera.grabs == 0
    pending = bed_view._read_pending("u1")
    assert (
        pending is not None and pending["z"] == 200.0
    )  # the original Z is still on record
