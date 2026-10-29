"""Tests for the auto view of the printer bed (lib/bed_view.py, `3d printer bed status`'s move, the
`bed` preflight line and the one-shot `--on bed-free` subscription).

Background (2026-10-23): the Snapmaker U1 rests at Z 200 after a job, where its camera sees 15 % of the
plate; window light there read as "15.7 % of the view differs: occupied" and a print was started with
--force. The bed check now moves the bed in Z to where the whole plate is in view, judges there and puts
it back. NOTHING here touches a real printer: the printer is `FakePrinter` (it simulates the Z moves of a
Moonraker adapter and records every G-code script), the camera serves the saved frames of tests/data/bed
by the bed's Z, tg-ctl is a fake runner, XDG dirs are tmp.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from errors import InvalidArgument, UsageError
from printer_ops import bed as pbed
from printer_ops import preflight, subscriptions
from printer_ops.devices import Device, ResumeProfile
from printer_ops.subscriptions import (
    DeliveryState,
    Registry,
    SubscriptionSink,
    bed_free_event,
    bed_free_message,
    make,
)
from printer_ops.watch import INFO, WARNING, Event

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_state as B  # noqa: E402  (needs numpy + OpenCV)
import bed_view as V  # noqa: E402

DATA = Path(__file__).parent / "data" / "bed"
RESUME = ResumeProfile("SM_PRINT_PREEXTRUDE_FILAMENT", 30, 0.5, 12, 5, 3, 1.75, 170, 10000, 275.0, ())


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def device(*, access: str = "full", watch: dict[str, Any] | None = None, resume: ResumeProfile | None = RESUME) -> Device:
    return Device("u1", "moonraker", "127.0.0.1", 7125, "Snapmaker U1", Path("test.yaml"), access=access,
                  watch=watch or {}, resume=resume)


CFG = pbed.BedConfig(enabled=True, auto_view=True)


class FakePrinter:
    """The Moonraker adapter surface bed_view uses. A `G1 Z..` script moves the toolhead (offset =
    toolhead Z - gcode Z); every script is recorded. `on_script` runs after each script (a print that
    starts mid-check)."""

    def __init__(self, state: str = "complete", z: float = 200.0, *, offset: float = 0.0, homed: str = "xyz",
                 filename: str | None = "chekhov.gcode", object_height: float | None = None,
                 ready: bool = True) -> None:
        self.raw = state
        self.z = z
        self.offset = offset
        self.homed = homed
        self.filename = filename
        self.object_height = object_height
        self.ready = ready
        self.scripts: list[str] = []
        self.queries = 0
        self.on_script: Callable[[FakePrinter], None] | None = None
        self.metadata_error: Exception | None = None

    def query(self, objects: str) -> dict[str, Any]:
        self.queries += 1
        return {"webhooks": {"state": "ready" if self.ready else "shutdown"},
                "print_stats": {"state": self.raw, "filename": self.filename or ""},
                "toolhead": {"position": [100.0, 100.0, self.z, 0.0], "homed_axes": self.homed},
                "gcode_move": {"gcode_position": [100.0, 100.0, self.z - self.offset, 0.0]}}

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        self.scripts.append(script)
        for line in script.splitlines():
            if line.startswith("G1 Z"):
                self.z = float(line.split()[1][1:]) + self.offset
        if self.on_script is not None:
            self.on_script(self)

    def file_metadata(self, filename: str) -> dict[str, Any]:
        if self.metadata_error is not None:
            raise self.metadata_error
        return {} if self.object_height is None else {"object_height": self.object_height}


class ZCamera:
    """The U1 camera as the saved frames show it: the frame depends on where the bed is."""

    def __init__(self, printer: FakePrinter, frames: dict[float, str]) -> None:
        self.printer = printer
        self.frames = {z: (DATA / name).read_bytes() for z, name in frames.items()}
        self.grabs = 0
        self.on_grab: Callable[[], None] | None = None
        self.fail: BaseException | None = None

    def keepalive(self) -> bool:
        return False

    def grab(self) -> Any:
        self.grabs += 1
        if self.on_grab is not None:
            self.on_grab()
        if self.fail is not None:
            raise self.fail
        jpeg = self.frames[min(self.frames, key=lambda z: abs(z - self.printer.z))]
        com = b"\xff\xfe" + (2 + 8).to_bytes(2, "big") + self.grabs.to_bytes(8, "big")
        return SimpleNamespace(jpeg=jpeg[:2] + com + jpeg[2:])


# the incident: at Z 200 the sliver of the plate shows "parts" (window light), at Z 70 the whole empty plate
FRAMES = {200.0: "u1_occupied_1400.jpg", 70.0: "u1_z10_clear_ref.jpg"}


def capture(z: float, name: str) -> None:
    """An empty-bed reference at gcode Z `z` from a saved frame (the flow is under test, not the optics)."""
    B.capture_reference(device(), CFG, plate=True, frame=(DATA / name).read_bytes(), frame_z=z, now=1.0)


@pytest.fixture()
def refs() -> None:
    capture(200.0, "u1_z200_clear_ref.jpg")
    capture(70.0, "u1_z10_clear_ref.jpg")


def status(printer: FakePrinter, cam: ZCamera | None = None, *, mode: str = "auto", cfg: pbed.BedConfig = CFG,
           part_height: float | None = 20.0, dev: Device | None = None, max_age: float | None = None) -> dict[str, Any]:
    return B.status_view(dev or device(), cfg, mode=mode, max_age=max_age, part_height=part_height, adapter=printer,
                         camera=cam or ZCamera(printer, FRAMES), sleep=lambda s: None, settle_s=0.0)


def moves(printer: FakePrinter) -> list[str]:
    return [ln for s in printer.scripts for ln in s.splitlines() if ln.startswith(("G0", "G1"))]


# ---- the picker (pure) --------------------------------------------------------------------------

def lin(z: float) -> float:
    """A bed that leaves the frame linearly: whole at Z 0, gone at Z 200."""
    return max(0.0, 1.0 - z / 200.0)


def same_z(ref: float | None, z: float | None) -> bool:
    return ref is not None and z is not None and abs(ref - z) <= 1.0


def pick(view: Callable[[float], float | None], refs_: list[float], current: float | None = 180.0, *,
         floor: float = 10.0, ceiling: float = 270.0, threshold: float = 0.85) -> V.ViewPick:
    return V.pick_inspection_z(view, refs_, same_z, current, z_floor=floor, z_ceiling=ceiling, threshold=threshold)


def test_picker_takes_the_reference_z_that_shows_the_plate() -> None:
    p = pick(lin, [20.0, 100.0, 180.0])
    assert (p.status, p.z) == ("move", 20.0)
    assert p.visible == pytest.approx(0.9) and p.current_visible == pytest.approx(0.1)
    assert "Z 20.0" in p.reason


def test_picker_prefers_a_z_with_a_reference_over_a_better_view_without_one() -> None:
    p = pick(lin, [30.0], current=180.0)  # Z 0..30 all qualify, only 30 has a reference
    assert (p.status, p.z) == ("move", 30.0)


def test_picker_breaks_a_tie_by_the_nearest_z_to_the_current_one() -> None:
    p = pick(lin, [10.0, 25.0], current=180.0)  # 95 % and 87.5 %: within VIEW_TIE of each other
    assert p.z == 25.0
    flat = pick(lambda z: 1.0 if z <= 60 else 0.0, [10.0, 50.0], current=100.0)  # equal views: the nearer Z
    assert (flat.status, flat.z) == ("move", 50.0)


def test_picker_keeps_a_clearly_better_view_over_a_nearer_mediocre_one() -> None:
    # Z 5: 97.5 %, Z 28: 86 %: the nearer one is 11 points worse than the best, outside VIEW_TIE
    p = pick(lin, [5.0, 28.0], current=180.0, floor=0.0)
    assert p.z == 5.0


def test_picker_stays_when_the_current_z_already_shows_the_plate() -> None:
    p = pick(lin, [20.0, 100.0], current=20.0)
    assert (p.status, p.z, p.visible) == ("stay", 20.0, pytest.approx(0.9))


def test_picker_respects_the_clearance_floor_and_says_so() -> None:
    p = pick(lin, [20.0, 100.0], floor=60.0)
    assert p.status == "none" and p.z is None
    assert "Z 60..270" in p.reason and "tallest part" in p.reason


def test_picker_never_goes_above_the_ceiling() -> None:
    p = pick(lambda z: 1.0, [280.0, 290.0], current=100.0, ceiling=270.0)  # the references are above the ceiling
    assert p.status == "none"
    ok = pick(lambda z: 1.0, [270.0], current=100.0, ceiling=270.0)
    assert (ok.status, ok.z) == ("move", 270.0)


def test_picker_threshold_fallback_is_unknown_with_the_best_it_saw() -> None:
    p = pick(lambda z: 0.6, [50.0, 100.0])
    assert p.status == "none" and p.z is None
    assert "no Z shows that much" in p.reason and "60%" in p.reason and "85% needed" in p.reason


def test_picker_without_a_reference_at_a_qualifying_z_names_the_capture() -> None:
    p = pick(lin, [160.0, 180.0])
    assert p.status == "none"
    assert "no empty-bed reference stands there" in p.reason and "3d printer bed reference NAME --capture" in p.reason


def test_picker_without_a_view_estimate_does_not_guess() -> None:
    p = V.pick_inspection_z(lambda z: None, [50.0], same_z, 180.0, z_floor=10.0, z_ceiling=270.0, threshold=0.85)
    assert p.status == "none" and "cannot tell" in p.reason


def test_picker_on_the_u1_calibration_chooses_z_70_from_the_incident_references() -> None:
    """Real numbers: references at Z 70/150/175/199.7 on the bundled U1 calibration, the bed at the Z 200 rest."""
    match = B.z_match(device(), CFG)
    assert match.cam is not None
    p = V.pick_inspection_z(lambda z: match.in_view(z), [70.0, 150.0, 175.0, 199.7], match, 200.0,
                            z_floor=35.0, z_ceiling=270.0, threshold=0.85)
    assert (p.status, p.z) == ("move", 70.0)
    assert p.current_visible is not None and p.current_visible < 0.16  # the 15 % sliver of the incident
    assert p.visible is not None and p.visible > 0.88


# ---- the move: Z only, guarded, restored -----------------------------------------------------------

def test_the_incident_moves_to_z_70_judges_the_whole_plate_and_puts_the_bed_back(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    v = status(printer)
    assert (v["bed"], v["plate"]) == ("clear", "present")  # the sliver at Z 200 said occupied
    assert v["z"] == 70.0 and v["source"] == "camera now"
    assert v["view"]["action"] == "moved" and v["view"]["moved_from"] == 200.0 and v["view"]["restored"] is True
    assert v["view"]["z_used"] == 70.0 and v["view"]["visible"] < 0.16
    assert "judged at Z 70.0 after moving the bed from Z 200.0" in v["reason"] and "bed put back" in v["reason"]
    assert printer.z == 200.0  # left as found
    assert moves(printer) == ["G1 Z70.000 F600", "G1 Z200.000 F600"]
    assert all(s.splitlines() == [s.splitlines()[0], s.splitlines()[1], "M400"] and s.startswith("G90\nG1 Z")
               for s in printer.scripts)


def test_the_move_has_no_x_y_home_or_macro_in_it(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    status(printer)
    words = {w for s in printer.scripts for ln in s.splitlines() for w in ln.split()}
    assert not {w for w in words if w[0] in "XYE" and w[1:2].lstrip("-").isdigit()}
    assert {ln.split()[0] for s in printer.scripts for ln in s.splitlines()} == {"G90", "G1", "M400"}
    assert not any("G28" in s or "SAVE_GCODE_STATE" in s for s in printer.scripts)


def test_the_gcode_offset_is_kept_so_the_bed_returns_to_exactly_where_it_was(refs: None) -> None:
    printer = FakePrinter("complete", 199.7, offset=-0.3)  # toolhead 199.7 = gcode 200.0
    v = status(printer)
    assert moves(printer) == ["G1 Z70.300 F600", "G1 Z200.000 F600"]
    assert printer.z == pytest.approx(199.7) and v["z"] == pytest.approx(70.0)


@pytest.mark.parametrize("raw", ["printing", "paused"])
def test_nothing_moves_and_the_camera_is_not_asked_while_a_job_runs(refs: None, raw: str) -> None:
    printer = FakePrinter(raw, 150.0)
    cam = ZCamera(printer, FRAMES)
    for mode in ("auto", "force"):
        v = status(printer, cam, mode=mode)
        assert v["bed"] == "printing" and v["plate"] == "unknown"  # never clear
        assert f"{raw}: bed not moved, view not judged" in v["reason"]
        assert v["view"]["action"] == "not moved"
    assert printer.scripts == [] and cam.grabs == 0 and printer.z == 150.0


def test_an_error_or_unreadable_printer_is_never_moved(refs: None) -> None:
    for raw in ("error", "startup"):
        printer = FakePrinter(raw, 200.0)
        v = status(printer)
        assert printer.scripts == [] and "cannot move" in v["reason"]
    down = FakePrinter("complete", 200.0, ready=False)
    assert status(down)["view"]["action"] == "not moved" and down.scripts == []


def test_a_print_that_starts_during_the_check_gets_no_restore_move(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    cam = ZCamera(printer, FRAMES)
    cam.on_grab = lambda: setattr(printer, "raw", "printing")  # somebody starts a job while the frame is taken
    v = status(printer, cam)
    assert len(printer.scripts) == 1 and printer.z == 70.0  # the move only; NO second script while printing
    assert v["view"]["restored"] is False and "printing" in v["view"]["restore_error"]
    assert "BED NOT PUT BACK" in v["reason"]
    assert V._read_pending("u1") is not None  # the displaced Z stays recorded for after the job


def test_the_bed_is_restored_when_judging_raises(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    cam = ZCamera(printer, FRAMES)
    cam.fail = RuntimeError("camera exploded")
    with pytest.raises(RuntimeError, match="camera exploded"):
        status(printer, cam)
    assert printer.z == 200.0 and moves(printer) == ["G1 Z70.000 F600", "G1 Z200.000 F600"]
    assert V._read_pending("u1") is None


def test_the_bed_is_restored_on_keyboard_interrupt_and_sigterm_style_exits(refs: None) -> None:
    for exc in (KeyboardInterrupt(), SystemExit(143)):
        printer = FakePrinter("complete", 200.0)
        cam = ZCamera(printer, FRAMES)
        cam.fail = exc
        with pytest.raises(type(exc)):
            status(printer, cam)
        assert printer.z == 200.0


def test_a_failed_restore_is_reported_not_swallowed(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)

    def lose_homing(p: FakePrinter) -> None:
        if len(p.scripts) == 1:
            p.homed = ""  # the motors dropped out after the move (idle timeout, firmware)

    printer.on_script = lose_homing
    v = status(printer)
    assert v["bed"] == "clear" and v["view"]["restored"] is False and "not homed" in v["view"]["restore_error"]
    assert preflight.check_bed_verdict(v).status == "WARN"  # a clear bed left at Z 70 is never a PASS


def test_not_homed_means_no_move_and_the_old_verdict_with_the_reason(refs: None) -> None:
    printer = FakePrinter("complete", 200.0, homed="")
    v = status(printer)
    assert printer.scripts == []  # no G28, nothing
    assert v["view"]["action"] == "not moved" and "cannot move: not homed" in v["reason"]
    assert v["bed"] == "occupied" and v["z"] == 200.0  # the old (sliver) verdict stands, with the reason
    z_only = FakePrinter("complete", 200.0, homed="z")
    assert status(z_only)["view"]["action"] == "moved"  # a Z-only move needs Z homed, not XY


def test_a_read_only_printer_is_never_moved(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    v = status(printer, dev=device(access="read-only"))
    assert printer.scripts == [] and "read-only: no writes" in v["reason"]


def test_no_move_and_an_off_config_do_not_move_but_force_does(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    assert "--no-move" in status(printer, mode="never")["reason"] and printer.scripts == []
    off = pbed.BedConfig(enabled=True, auto_view=False)
    v = status(printer, mode="auto", cfg=off)
    assert printer.scripts == [] and "watch.bed.auto_view is off" in v["reason"]
    assert status(printer, mode="force", cfg=off)["view"]["action"] == "moved"


def test_a_good_view_at_the_current_z_is_judged_where_it_is(refs: None) -> None:
    printer = FakePrinter("complete", 70.0)
    v = status(printer)
    assert printer.scripts == [] and v["view"]["action"] == "none" and v["z"] == 70.0


def test_no_qualifying_z_keeps_the_old_verdict_and_says_why(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    strict = pbed.BedConfig(enabled=True, auto_view=True, auto_view_min=0.99)  # no Z shows 99 % of the plate
    v = status(printer, cfg=strict)
    assert printer.scripts == [] and v["bed"] == "occupied"
    assert v["view"]["action"] == "not moved" and "no Z shows that much" in v["reason"]


def test_no_reference_at_a_good_z_is_not_a_move_to_nowhere() -> None:
    capture(200.0, "u1_z200_clear_ref.jpg")  # only the sliver reference exists
    printer = FakePrinter("complete", 200.0)
    v = status(printer)
    assert printer.scripts == [] and "no empty-bed reference stands there" in v["reason"]


def test_the_height_of_what_may_stand_on_the_bed_sets_the_floor(refs: None) -> None:
    tall = FakePrinter("complete", 200.0, object_height=120.0)  # the last job: 120 mm -> floor Z 135
    v = status(tall, part_height=None)
    assert tall.scripts == [] and "Z 135..270" in v["reason"] and v["view"]["part_mm"] == 120.0
    assert "object_height of chekhov.gcode" in v["view"]["part_source"]
    low = FakePrinter("complete", 200.0, object_height=30.0)  # floor Z 45: Z 70 is fine
    assert status(low, part_height=None)["view"]["action"] == "moved"
    declared = FakePrinter("complete", 200.0, object_height=120.0)
    assert status(declared, part_height=0.0)["view"]["action"] == "moved"  # --part-height 0: the plate is empty
    cfg = pbed.BedConfig(enabled=True, auto_view=True, auto_view_part_mm=10.0)
    assert status(FakePrinter("complete", 200.0, object_height=120.0), part_height=None, cfg=cfg)["view"]["action"] == "moved"


def test_an_unknown_part_height_means_no_move(refs: None) -> None:
    for printer in (FakePrinter("complete", 200.0, object_height=None), FakePrinter("complete", 200.0, filename=None)):
        v = status(printer, part_height=None)
        assert printer.scripts == [] and "height of what may stand on the bed is unknown" in v["reason"]
    broken = FakePrinter("complete", 200.0, object_height=5.0)
    broken.metadata_error = OSError("404")
    assert status(broken, part_height=None)["view"]["action"] == "not moved" and broken.scripts == []


def test_a_device_without_an_axis_limit_is_not_moved(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    v = status(printer, dev=device(resume=None))
    assert printer.scripts == [] and "no resume.z_max_mm" in v["reason"]


def test_a_second_check_of_the_same_printer_waits_for_nobody(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    with V.view_lock("u1") as held:
        assert held
        v = status(printer)  # another process "holds" the lock
    assert printer.scripts == [] and "another view check of this printer is running" in v["reason"]


def test_an_interrupted_check_is_undone_by_the_next_one(refs: None) -> None:
    printer = FakePrinter("complete", 70.0)  # a killed check left the bed at Z 70
    V._write_pending("u1", V.Motion("complete", "complete", 200.0, 200.0, "xyz", True, None), 1000.0)
    v = V.auto_view(device(), CFG, mode="auto", adapter=printer, judge=lambda age: {"bed": "clear", "reason": "r"},
                    view=lambda z: 1.0, ref_zs=[], stands=same_z, clock=lambda: 1100.0)
    assert printer.z == 200.0 and moves(printer) == ["G1 Z200.000 F600"]
    assert "put the bed back to Z 200.0" in v["reason"] and V._read_pending("u1") is None
    stale = FakePrinter("complete", 70.0)
    V._write_pending("u1", V.Motion("complete", "complete", 200.0, 200.0, "xyz", True, None), 1000.0)
    V.auto_view(device(), CFG, mode="auto", adapter=stale, judge=lambda age: {"bed": "clear", "reason": "r"},
                view=lambda z: 1.0, ref_zs=[], stands=same_z, clock=lambda: 1000.0 + V.PENDING_MAX_AGE_S + 1)
    assert stale.scripts == [] and V._read_pending("u1") is None  # too old to trust: dropped, not obeyed
    busy = FakePrinter("printing", 70.0)
    V._write_pending("u1", V.Motion("complete", "complete", 200.0, 200.0, "xyz", True, None), 1000.0)
    V.auto_view(device(), CFG, mode="auto", adapter=busy, judge=lambda age: {"bed": "printing", "reason": "r"},
                view=lambda z: 1.0, ref_zs=[], stands=same_z, clock=lambda: 1100.0)
    assert busy.scripts == [] and V._read_pending("u1") is not None  # the job owns the bed; the marker waits


def test_guarded_z_move_refuses_everything_that_is_not_an_idle_homed_printer() -> None:
    for printer, why in ((FakePrinter("printing", 200.0), "printer is printing"),
                         (FakePrinter("paused", 200.0), "printer is paused"),
                         (FakePrinter("error", 200.0), "printer is error"),
                         (FakePrinter("complete", 200.0, homed="xy"), "not homed"),
                         (FakePrinter("complete", 200.0, ready=False), "Klipper is not ready")):
        with pytest.raises(V.MoveRefused, match=why):
            V.guarded_z_move(printer, to_gcode_z=70.0)
        assert printer.scripts == []
    ok = FakePrinter("standby", 200.0)  # Klipper's `standby` is the normalized idle
    V.guarded_z_move(ok, to_gcode_z=70.0, floor=40.0, ceiling=270.0, target_toolhead_z=70.0)
    assert ok.scripts == ["G90\nG1 Z70.000 F600\nM400"]
    with pytest.raises(V.MoveRefused, match="outside"):
        V.guarded_z_move(ok, to_gcode_z=20.0, floor=40.0, ceiling=270.0, target_toolhead_z=20.0)
    with pytest.raises(V.MoveRefused, match="outside"):
        V.guarded_z_move(ok, to_gcode_z=274.0, floor=40.0, ceiling=270.0, target_toolhead_z=274.0)
    with pytest.raises(V.MoveRefused, match="finite"):
        V.guarded_z_move(ok, to_gcode_z=float("nan"))
    assert len(ok.scripts) == 1


def test_guarded_z_move_refuses_a_gcode_z_that_would_land_elsewhere_under_the_current_offset() -> None:
    shifted = FakePrinter("complete", 70.0, offset=-190.0)
    with pytest.raises(V.MoveRefused, match="Z offset changed"):
        V.guarded_z_move(shifted, to_gcode_z=200.0, target_toolhead_z=200.0)  # planned under offset 0: lands at Z10
    assert shifted.scripts == []
    V.guarded_z_move(shifted, to_gcode_z=390.0, target_toolhead_z=200.0)  # recomputed under -190: lands at Z200
    assert shifted.z == 200.0


# ---- the preflight `bed` line uses the moved view --------------------------------------------------

def run_status_in_process(printer: FakePrinter, seen: list[list[str]]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A runner that does what `bed_state.py status` does, in this process, against the fakes."""
    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        mode = "never" if "--no-move" in argv else "force" if "--best-view" in argv else "auto"
        v = status(printer, mode=mode)
        return subprocess.CompletedProcess(argv, 0, json.dumps(v, default=str), "")

    return runner


def test_preflight_bed_line_judges_the_moved_view_and_names_the_z_used(refs: None) -> None:
    dev = device(watch={"bed": {"enabled": True, "stale_s": 30}, "vision": {"camera": {"kind": "moonraker-monitor"}}})
    printer = FakePrinter("complete", 200.0)
    seen: list[list[str]] = []
    c = preflight.check_bed(dev, runner=run_status_in_process(printer, seen))
    assert c.status == "PASS"
    assert "judged at Z 70.0 after moving the bed from Z 200.0 and back" in c.detail
    assert seen[0][-5:] == ["status", "u1", "--json", "--max-age", "30"] and "--no-move" not in seen[0]
    assert printer.z == 200.0
    sliver = FakePrinter("complete", 200.0)
    c2 = preflight.check_bed(dev, move=False, runner=run_status_in_process(sliver, seen))
    assert "--no-move" in seen[-1] and sliver.scripts == []
    assert c2.status == "FAIL"  # judged at the 15 % sliver, as before: the old verdict, never silently clear


def test_preflight_while_printing_is_a_warn_that_says_nothing_was_moved(refs: None) -> None:
    dev = device(watch={"bed": {"enabled": True}, "vision": {"camera": {"kind": "moonraker-monitor"}}})
    printer = FakePrinter("printing", 150.0)
    c = preflight.check_bed(dev, runner=run_status_in_process(printer, []))
    assert c.status == "WARN" and "printing: bed not moved, view not judged" in c.detail
    assert printer.scripts == []


def test_preflight_gives_a_move_the_time_it_needs() -> None:
    dev = device(watch={"bed": {"enabled": True, "auto_view": True}, "vision": {"camera": {"kind": "moonraker-monitor"}}})
    timeouts: list[float] = []

    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"bed": "clear", "plate": "present"}), "")

    preflight.check_bed(dev, runner=runner)
    preflight.check_bed(dev, move=False, runner=runner)
    off = device(watch={"bed": {"enabled": True}, "vision": {"camera": {"kind": "moonraker-monitor"}}})
    preflight.check_bed(off, runner=runner)  # auto_view off: no move, no extra time
    assert timeouts == [pbed.STATUS_MOVE_TIMEOUT_S, pbed.STATUS_TIMEOUT_S, pbed.STATUS_TIMEOUT_S]
    assert pbed.STATUS_MOVE_TIMEOUT_S > 2 * V.MOVE_TIMEOUT_S + pbed.AUTO_VIEW_SETTLE_S + 2 * B.FROZEN_CHECK_S


@pytest.mark.parametrize(("view", "status_", "text"), [
    ({"action": "moved", "z_used": 70.0, "moved_from": 200.0, "restored": True}, "PASS", "and back"),
    ({"action": "moved", "z_used": 70.0, "moved_from": 200.0, "restored": False, "restore_error": "x"}, "WARN",
     "BED NOT PUT BACK to Z 200.0: x"),
])
def test_verdict_check_reports_the_moved_view(view: dict[str, Any], status_: str, text: str) -> None:
    c = preflight.check_bed_verdict({"bed": "clear", "plate": "present", "source": "camera now", "age_s": 0.0,
                                     "reason": "r", "z": 70.0, "view": view})
    assert c.status == status_ and "judged at Z 70.0 after moving the bed from Z 200.0" in c.detail
    assert text in c.detail + " ".join(c.lines)


# ---- config + CLI ---------------------------------------------------------------------------------

def test_auto_view_is_off_unless_the_printer_entry_turns_it_on_and_validates() -> None:
    watch = {"vision": {"camera": {"kind": "moonraker-monitor"}}, "bed": {"enabled": True}}
    assert pbed.bed_config(device(watch=watch)).auto_view is False
    on = {**watch, "bed": {"enabled": True, "auto_view": True, "auto_view_min": 0.9, "auto_view_part_mm": 40}}
    cfg = pbed.bed_config(device(watch=on))
    assert (cfg.auto_view, cfg.auto_view_min, cfg.auto_view_part_mm) == (True, 0.9, 40.0)
    assert pbed.bed_config(device(watch=watch)).auto_view_part_mm is None
    assert pbed.bed_config(device(watch=watch)).auto_view_min == pbed.AUTO_VIEW_MIN == 0.85
    for bad in ({"auto_view": "yes"}, {"auto_view_min": 0}, {"auto_view_min": 1.5}, {"auto_view_part_mm": -1}):
        with pytest.raises(InvalidArgument):
            pbed.bed_config(device(watch={**watch, "bed": {"enabled": True, **bad}}))


def test_best_view_and_no_move_contradict_and_belong_to_status_only() -> None:
    with pytest.raises(UsageError, match="contradict"):
        B.main(["status", "u1", "--best-view", "--no-move"])
    for argv in (["monitor", "u1", "--best-view"], ["reference", "u1", "--no-move"],
                 ["reference", "u1", "--part-height", "5"]):
        with pytest.raises(UsageError, match="does not take"):
            B.main(argv)


def test_status_argv_modes() -> None:
    assert status_tail(pbed.status_argv("u1")) == ["status", "u1", "--json"]
    assert status_tail(pbed.status_argv("u1", move="never", max_age=30)) == [
        "status", "u1", "--json", "--max-age", "30", "--no-move"]
    assert "--no-move" in pbed.status_argv("u1", move="never") and "--best-view" in pbed.status_argv("u1", move="force")
    with pytest.raises(ValueError):
        pbed.status_argv("u1", move="sometimes")


def status_tail(argv: list[str]) -> list[str]:
    return argv[argv.index("status"):]


def test_the_fleet_u1_enables_the_auto_view_and_the_watch_never_moves() -> None:
    import yaml

    fleet = yaml.safe_load((Path(__file__).parent.parent / "fleet" / "printers.yaml").read_text(encoding="utf-8"))
    assert fleet["u1"]["watch"]["bed"]["auto_view"] is True
    assert "auto_view" not in (fleet["k1se"]["watch"].get("bed") or {})  # the K1 SE has no calibration to pick a Z by
    src = (Path(__file__).parent.parent / "lib" / "bed_state.py").read_text(encoding="utf-8")
    monitor = src[src.index("def _run_monitor"):src.index("def one_shot")]
    assert "run_gcode" not in monitor and "guarded_z_move" not in monitor and "status_view" not in monitor


def test_the_monitor_stands_aside_while_a_view_check_has_the_bed_away(refs: None) -> None:
    """Frames of a bed that is being moved are not its state: no verdict, no event, no ack meanwhile."""
    dev = device(watch={"bed": {"enabled": True}})
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2)
    cam = ZCamera(FakePrinter("complete", 200.0), {200.0: "u1_z200_clear_ref.jpg"})
    V._write_pending("u1", V.Motion("complete", "complete", 200.0, 200.0, "xyz", True, None), 1000.0)
    lines: list[str] = []

    class Out:
        def write(self, text: str) -> None:
            lines.append(text)

        def flush(self) -> None:
            pass

    now = {"t": 1000.0}

    def clock() -> float:
        return now["t"]

    def sleep(s: float) -> None:
        now["t"] += s

    mon = B.run_monitor(dev, cfg, duration=2.0, out=Out(), camera=cam, printer_fn=lambda d: ("complete", 200.0),
                        clock=clock, sleep=sleep)
    assert cam.grabs == 0 and mon.classified == 0 and lines == []
    assert not V.view_in_progress("u1", 1000.0 + V.PENDING_MAX_AGE_S + 1)  # a dead check's marker expires


# ---- the one-shot `--once --on bed-free` subscription ---------------------------------------------------

MAIN = {"pid": 433553, "sessionId": "01a12cd6-7b39", "cwd": "/tmp", "title": "MODEL: Finish 3D model", "busy": False}


class TgCtl:
    """tg-ctl omp-live: `status` lists `hosts`, `send` answers with the acceptance receipt."""

    def __init__(self, hosts: list[dict[str, Any]] | None = None) -> None:
        self.hosts = [MAIN] if hosts is None else hosts
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:3] == ["tg-ctl", "omp-live", "status"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.hosts), "")
        reply = {"ok": True, "accepted": True, "status": "accepted", "requestId": cmd[cmd.index("--request-id") + 1],
                 "sessionId": cmd[cmd.index("--session") + 1]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(reply), "")

    def sends(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["tg-ctl", "omp-live", "send"]]


VERDICT: dict[str, Any] = {"bed": "clear", "plate": "present", "z": 70.0, "source": "camera now", "age_s": 0.0,
           "reason": "0.4% of the bed view differs; judged at Z 70.0", "frame": "/cache/u1-status-clear.jpg",
           "view": {"action": "moved", "moved_from": 200.0, "z_used": 70.0, "restored": True}}


def job_end(frm: str = "printing", to: str = "complete", printer: str = "u1", n: int = 1) -> Event:
    return Event(printer, "state", INFO, f"{printer}: print COMPLETE", {"from": frm, "to": to, "reason": None},
                 key=f"{printer}:state:{frm}->{to}:{n}")


def bed_free_sink(tg: TgCtl, checks: list[str], verdict: dict[str, Any] | Exception = VERDICT, *,
                  threaded: bool = False, **kw: Any) -> SubscriptionSink:
    def check(printer: str) -> dict[str, Any]:
        checks.append(printer)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    return SubscriptionSink(runner=tg, bed_check=check, settle_s=0.0, sleep=lambda s: None, threaded=threaded, **kw)


def arm(session: str = "01a12cd6", printer: str = "u1") -> subscriptions.Subscription:
    return Registry().put(make(session=session, printers=[printer], on="bed-free", once=True, note="chekhov"))[0]


def test_bed_free_subscription_is_validated_and_never_an_ordinary_alert_filter() -> None:
    s = make(session="01a12cd6", printers=["u1"], on="bed-free", once=True)
    assert (s.on, s.once) == ("bed-free", True) and s.id != make(session="01a12cd6", printers=["u1"]).id
    assert not s.matches(Event("u1", "vision_spaghetti", WARNING, "t")) and not s.takes_over("u1")
    assert "once, when the printer is free: bed-free" in s.filters()
    assert s.to_dict()["on"] == "bed-free" and s.to_dict()["once"] is True
    assert "on" not in make(session="x").to_dict()
    for kw, field in (({"on": "bed-free"}, "--once"), ({"once": True}, "--once"),
                      ({"on": "weather", "once": True}, "--on"),
                      ({"on": "bed-free", "once": True, "printers": ["u1", "k1se"]}, "--printer"),
                      ({"on": "bed-free", "once": True}, "--printer"),
                      ({"on": "bed-free", "once": True, "printers": ["u1"], "kinds": ["state"]}, "--kinds")):
        args: dict[str, Any] = {"session": "x", **kw}
        if "printers" not in kw and field != "--printer":
            args["printers"] = ["u1"]
        with pytest.raises(InvalidArgument) as exc:
            make(**args)
        assert field in str(exc.value)


def test_the_registry_round_trips_a_bed_free_entry_next_to_ordinary_ones() -> None:
    arm()
    Registry().put(make(session="01a12cd6", printers=["u1"], kinds=["vision_*"]))
    again, new = Registry().put(make(session="01a12cd6", printers=["u1"], on="bed-free", once=True, note="2"))
    assert not new and again.note == "2"  # the same trigger updates in place
    loaded = Registry().load()
    assert [(s.on, s.once) for s in loaded] == [("bed-free", True), ("", False)]
    path = Registry().path
    assert "'on': bed-free" in path.read_text(encoding="utf-8")
    # a hand-written bare `on:` is the boolean True to YAML 1.1: it must still be the trigger
    path.write_text(path.read_text(encoding="utf-8").replace("'on': bed-free", "on: bed-free"), encoding="utf-8")
    assert [(s.on, s.once) for s in Registry().load()] == [("bed-free", True), ("", False)]


def test_job_end_runs_the_check_sends_one_event_with_the_frame_and_spends_the_subscription() -> None:
    s = arm()
    tg, checks = TgCtl(), list[str]()
    snk = bed_free_sink(tg, checks)
    assert snk.deliver([job_end()]) == []  # a trigger takes no alert: nothing for the temporary agent to skip
    assert checks == ["u1"]
    (send,) = tg.sends()
    text = send[-1]
    assert text.startswith(f"[3d printer bed-free -> subscriber {s.id}] u1: the job ended, the bed is free")
    assert "frames: /cache/u1-status-clear.jpg" in text and "bed clear, plate present" in text
    assert "judged at Z 70.0" in text and "bed moved from Z 200.0 and back" in text
    assert "Never move the bed, start or cancel a job" in text and "tg --photo" in text
    assert Registry().load() == []  # once: removed
    row = DeliveryState().read()["subscriptions"][s.id]
    assert row["delivered"] == 1 and row["last_kinds"] == ["bed_free"]
    assert snk.deliver([job_end(n=2)]) == [] and len(tg.sends()) == 1 and checks == ["u1"]  # nothing left to fire


@pytest.mark.parametrize(("frm", "to", "printer"), [
    ("idle", "complete", "u1"),           # not a job end
    ("printing", "printing", "u1"),
    ("printing", "paused", "u1"),         # a pause is not a free bed
    ("paused", "printing", "u1"),
    ("printing", "error", "u1"),          # an errored printer cannot be moved or judged
    ("printing", "complete", "k1se"),     # another printer
])
def test_other_state_changes_leave_the_subscription_armed(frm: str, to: str, printer: str) -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    bed_free_sink(tg, checks).deliver([job_end(frm, to, printer)])
    assert checks == [] and tg.sends() == [] and len(Registry().load()) == 1


@pytest.mark.parametrize(("frm", "to"), [("printing", "complete"), ("printing", "cancelled"), ("paused", "cancelled"),
                                         ("printing", "idle"), ("paused", "idle")])
def test_every_way_a_job_leaves_the_bed_free_fires(frm: str, to: str) -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    bed_free_sink(tg, checks).deliver([job_end(frm, to)])
    assert checks == ["u1"] and len(tg.sends()) == 1 and Registry().load() == []


def test_an_unknown_verdict_is_delivered_as_unknown_with_a_warning_never_as_clear() -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    printing = {"bed": "printing", "plate": "unknown", "z": 150.0, "source": "printer", "frame": None,
                "reason": "printing: bed not moved, view not judged (the camera is not asked)", "view": {}}
    bed_free_sink(tg, checks, printing).deliver([job_end()])
    (send,) = tg.sends()
    assert "bed printing, plate unknown" in send[-1] and "(warning)" in send[-1] and "(info)" not in send[-1]
    assert "frames: none attached" in send[-1]


def test_a_check_that_cannot_run_still_answers_once_and_says_why() -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    bed_free_sink(tg, checks, RuntimeError("camera frame unusable")).deliver([job_end()])
    (send,) = tg.sends()
    assert "bed unknown, plate unknown" in send[-1] and "the bed check could not run: camera frame unusable" in send[-1]
    assert Registry().load() == []


def test_a_dead_session_keeps_the_subscription_armed_and_is_counted() -> None:
    s = arm()
    tg, checks = TgCtl(hosts=[]), list[str]()
    bed_free_sink(tg, checks).deliver([job_end()])
    assert checks == ["u1"] and tg.sends() == [] and [x.id for x in Registry().load()] == [s.id]
    state = DeliveryState().read()
    assert state["counters"]["u1"]["subscriber_dead"] == 1 and "dead_since_ts" in state["subscriptions"][s.id]


def test_two_watchers_fire_a_subscription_once() -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    a, b = bed_free_sink(tg, checks), bed_free_sink(tg, checks)
    a.deliver([job_end()])
    b.deliver([job_end()])
    assert len(tg.sends()) == 1 and checks == ["u1"]


def test_the_check_runs_off_the_dispatcher_thread_and_close_waits_for_it() -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    snk = bed_free_sink(tg, checks, threaded=True)
    snk.deliver([job_end()])
    snk.close(10.0)
    assert checks == ["u1"] and len(tg.sends()) == 1 and Registry().load() == []


def test_dry_run_checks_nothing_and_keeps_the_subscription() -> None:
    arm()
    tg, checks = TgCtl(), list[str]()
    bed_free_sink(tg, checks, dry_run=True).deliver([job_end()])
    assert checks == [] and tg.sends() == [] and len(Registry().load()) == 1


def test_ordinary_subscribers_are_unaffected_by_an_armed_trigger() -> None:
    arm()
    Registry().put(make(session="01a12cd6", printers=["u1"], kinds=["state"], min_severity=INFO))
    tg, checks = TgCtl(), list[str]()
    snk = bed_free_sink(tg, checks)
    e = job_end()
    assert snk.deliver([e]) == [e]  # the ordinary `state` subscriber took the event
    assert len(tg.sends()) == 2 and checks == ["u1"]  # its alert + the one bed-free answer


def test_through_the_dispatcher_only_the_state_edge_fires_it() -> None:
    from printer_ops.watch_sinks import Dispatcher

    arm()
    tg, checks = TgCtl(), list[str]()
    snk = bed_free_sink(tg, checks)
    d = Dispatcher(dedup_s=0)
    d.add("u1", [snk])
    d.process([Event("u1", "temperature", WARNING, "u1: nozzle hot", {})])
    assert checks == []
    d.process([job_end()])
    assert checks == ["u1"] and len(tg.sends()) == 1


def test_bed_free_event_and_message_carry_the_verdict_and_the_frame() -> None:
    e = bed_free_event("u1", VERDICT, 1000.0)
    assert (e.kind, e.severity) == ("bed_free", INFO) and e.data["frame"] == "/cache/u1-status-clear.jpg"
    assert "bed clear, plate present (camera, Z 70.0, bed moved from Z 200.0 and back)" in e.text
    occupied = bed_free_event("u1", {**VERDICT, "bed": "occupied"}, 1000.0)
    assert occupied.severity == WARNING
    moved_view: dict[str, Any] = VERDICT["view"]
    stuck = bed_free_event("u1", {**VERDICT, "view": {**moved_view, "restored": False}}, 1000.0)
    assert stuck.severity == WARNING and "NOT put back" in stuck.text
    sub = make(session="x", printers=["u1"], on="bed-free", once=True)
    assert "subscription is removed" in bed_free_message(e, sub)


def test_cli_subscribe_once_on_bed_free_lists_and_unsubscribes(capsys: pytest.CaptureFixture[str],
                                                               xdg: Path) -> None:
    (xdg / "printer-devices.yaml").write_text(
        "u1:\n  adapter: moonraker\n  host: 127.0.0.1\n  model: Snapmaker U1\n  watch:\n"
        "    vision: {camera: {kind: moonraker-monitor}}\n    bed: {enabled: true}\n"
        "k1se:\n  adapter: moonraker\n  host: 127.0.0.2\n  model: Creality K1 SE\n", encoding="utf-8")
    assert subscriptions.run("subscribe", ["--session", "01a12cd6", "--printer", "u1", "--once", "--on", "bed-free"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("subscribed s") and "once, when the printer is free: bed-free" in out
    assert subscriptions.run("subscriptions", ["--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["on"] == "bed-free" and row["once"] is True and row["printers"] == ["u1"]
    assert subscriptions.run("subscriptions", []) == 0
    assert "once, when the printer is free: bed-free" in capsys.readouterr().out
    for argv, why in (
        (["--session", "x", "--printer", "u1", "--on", "bed-free"], "--once"),
        (["--session", "x", "--printer", "u1", "--once"], "--once"),
        (["--session", "x", "--once", "--on", "bed-free"], "--printer"),
        (["--session", "x", "--printer", "u1,k1se", "--once", "--on", "bed-free"], "--printer"),
        (["--session", "x", "--printer", "k1se", "--once", "--on", "bed-free"], "watch.bed.enabled"),
        (["--session", "x", "--printer", "u1", "--once", "--on", "rain"], "--on"),
    ):
        with pytest.raises(InvalidArgument) as exc:
            subscriptions.run("subscribe", argv)
        assert why in str(exc.value)
    sid = row["id"]
    assert subscriptions.run("unsubscribe", ["--id", sid]) == 0
    assert Registry().load() == []
