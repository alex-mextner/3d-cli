"""topcam.photo / photo_config: settings schema and merge, pose G-code, the printer link
(against the loopback FakeMoonraker), frame chunking, saving settings into the fleet file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from printer_fakes import FakeMoonraker
from topcam_fixtures import FakeCamera, frame, install_fake_camera, serve_node

from printer_ops.moonraker import MoonrakerAdapter
from topcam import photo_config, v4l2
from topcam.client import Capture, NodeCamera, StillSpec
from topcam.focus import parse_focus
from topcam.photo import (
    PhotoError,
    PrinterLink,
    capture_frames,
    pose_script,
    save_photo_settings,
)


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    return install_fake_camera(monkeypatch)


def test_photo_schema_accepts_the_documented_block() -> None:
    block = yaml.safe_load("""
camera: topcam
res: 2304x1536
focus: 30
exposure: 300
gain: 0
wb: 4500
ctrl: {sharpness: 0}
frames: 8
skip: 3
average: median
led: on
settle_ms: 300
printer_light: {on: "SET_PIN PIN=LED VALUE=1", off: "SET_PIN PIN=LED VALUE=0"}
pose: {x: 110, y: 220, z: 120, speed: 6000}
camera_height_mm: 300
px_per_mm: 10
scale_ref: scale-refs/steel-card.json
""")
    assert photo_config.validate(block) == []


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"iso": 100}, "photo.iso: unknown key"),
        ({"focus": "far"}, "photo.focus 'far': accepted auto | N"),
        ({"focus": True}, "photo.focus True: accepted auto | N"),
        ({"res": "big"}, "photo.res: 'big' is not WIDTHxHEIGHT"),
        ({"frames": 0}, "photo.frames: must be 1..32"),
        ({"frames": 33}, "photo.frames: must be 1..32"),
        ({"average": "max"}, "photo.average: 'max' is not one of"),
        ({"led": "blink"}, "photo.led: 'blink' is not on/off"),
        ({"ctrl": {"sharpness": "x"}}, "photo.ctrl: must map"),
        ({"printer_light": {"on": "X"}}, "photo.printer_light: must be"),
        ({"pose": {"x": 1, "a": 2}}, "photo.pose.a: unknown key"),
        ({"pose": {"speed": 100}}, "photo.pose: needs at least one of x, y, z"),
        ({"pose": {"x": 1, "speed": 0}}, "photo.pose.speed: must be > 0"),
        ({"px_per_mm": -1}, "photo.px_per_mm: -1 is not a positive number"),
        ([], "photo: must be a mapping"),
    ],
)
def test_photo_schema_problems(block: Any, message: str) -> None:
    assert any(message in p for p in photo_config.validate(block))


def test_merge_layers_and_origins() -> None:
    settings, origin = photo_config.merge(
        {"exposure": 300, "frames": 8, "led": "on"},
        {"exposure": 200, "focus": None, "camera": "usb"},
    )
    assert settings["exposure"] == 200 and origin["exposure"] == "cli"
    assert settings["frames"] == 8 and origin["frames"] == "fleet"
    assert settings["skip"] == 3 and origin["skip"] == "default"
    assert settings["led"] is True and "focus" not in settings
    assert settings["camera"] == "usb"


def test_pose_script_moves_z_away_first_and_closer_last() -> None:
    up = pose_script(
        {"x": 110, "y": 220, "z": 150, "speed": 6000}, [10.0, 10.0, 5.0, 0.0]
    )
    assert up.splitlines() == [
        "G90",
        "G1 Z150.000 F600",
        "G1 X110.000 Y220.000 F6000",
        "M400",
    ]
    down = pose_script({"x": 110, "y": 220, "z": 20}, [10.0, 10.0, 150.0, 0.0])
    assert down.splitlines() == [
        "G90",
        "G1 X110.000 Y220.000 F3000",
        "G1 Z20.000 F600",
        "M400",
    ]
    assert pose_script({"z": 50}, None).splitlines() == [
        "G90",
        "G1 Z50.000 F600",
        "M400",
    ]


def _moonraker(state: str = "standby", homed: str = "xyz") -> FakeMoonraker:
    fake = FakeMoonraker()
    fake.status["print_stats"]["state"] = state
    fake.status["toolhead"] = {
        "homed_axes": homed,
        "position": [100.0, 100.0, 5.0, 0.0],
        "axis_minimum": [-5.0, -0.5, -3.0, 0.0],
        "axis_maximum": [229.0, 227.0, 250.0, 0.0],
    }
    return fake


def _link(fake: FakeMoonraker, limits: Any = None) -> PrinterLink:
    return PrinterLink("k1se", MoonrakerAdapter("k1se", "127.0.0.1", fake.port), limits)


def test_link_moves_an_idle_homed_printer() -> None:
    fake = _moonraker()
    with fake.serve():
        moved = _link(fake).move(
            {"x": 110, "y": 220, "z": 150, "speed": 6000}, home=False
        )
    assert fake.scripts == ["G90\nG1 Z150.000 F600\nG1 X110.000 Y220.000 F6000\nM400"]
    assert moved["from"] == [100.0, 100.0, 5.0, 0.0]


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_link_refuses_a_busy_printer_and_sends_nothing(state: str) -> None:
    fake = _moonraker(state)
    with fake.serve():
        with pytest.raises(PhotoError, match=f"is {state}"):
            _link(fake).move({"z": 150}, home=False)
        with pytest.raises(PhotoError, match=f"is {state}"):
            _link(fake).light("SET_PIN PIN=LED VALUE=1")
    assert fake.scripts == [] and fake.writes() == []


def test_link_needs_homing_or_home_flag() -> None:
    fake = _moonraker(homed="xy")
    with fake.serve():
        with pytest.raises(PhotoError, match="not homed"):
            _link(fake).move({"z": 150}, home=False)
        assert fake.scripts == []
        link = _link(fake)
        link.move({"z": 150}, home=True)
    assert fake.scripts[0] == "G28"


def test_link_refuses_a_pose_out_of_range() -> None:
    fake = _moonraker()
    with fake.serve():
        with pytest.raises(PhotoError, match="z=300 is outside"):
            _link(fake).move({"z": 300}, home=False)
        with pytest.raises(
            PhotoError, match="x=235 is outside the printer's x range -5.0..229.0"
        ):
            _link(fake, ((-5.0, 229.0), (-0.5, 227.0))).move({"x": 235}, home=False)
    assert fake.scripts == []


def test_capture_frames_chunks_node_requests(fake_camera: FakeCamera) -> None:
    lock = v4l2.Lock(exposure=300)
    with serve_node(fake_camera) as (url, _):
        shot = capture_frames(NodeCamera(url), StillSpec(640, 480, 11, 2, lock), 11)
    assert len(shot.frames) == 11
    assert [c["count"] for c in fake_camera.captures] == [8, 3]
    assert shot.frames[0] == frame(640, 480, 2)


class _SweepCam:
    """Records each request; the first one 'sweeps' and reports focus 40 as best."""

    def __init__(self) -> None:
        self.specs: list[StillSpec] = []

    def still(self, spec: StillSpec) -> Capture:
        self.specs.append(spec)
        sweep = {"best": 40, "scores": {}} if spec.focus.mode == "sweep" else None
        return Capture(8, 2, [b"\0" * 32] * spec.frames, {}, 0, 0.1, {"backend": "stub"},
                       {"mode": spec.focus.text(), "sweep": sweep})


def test_capture_frames_keeps_the_focus_and_pins_the_sweep_result_for_later_chunks() -> None:
    cam = _SweepCam()
    spec = StillSpec(8, 2, 11, 0, focus=parse_focus("sweep:0-250:25"), focus_roi=(0.1, 0.1, 0.9, 0.9))
    shot = capture_frames(cam, spec, 11)  # type: ignore[arg-type]
    assert [s.focus.text() for s in cam.specs] == ["sweep:0-250:25", "40"]
    assert cam.specs[1].lock.focus == 40  # the USB backend locks from `lock`, not `focus`
    assert all(s.focus_roi == (0.1, 0.1, 0.9, 0.9) for s in cam.specs)
    assert len(shot.frames) == 11 and shot.focus["sweep"]["best"] == 40


def test_link_refuses_when_g28_left_the_printer_unhomed() -> None:
    fake = _moonraker(homed="")
    fake.home_on_g28 = False  # e.g. an endstop fault
    with fake.serve(), pytest.raises(PhotoError, match="still not homed after G28"):
        _link(fake).move({"z": 150}, home=True)
    assert fake.scripts == ["G28"]  # no pose move after a failed homing


def test_link_refuses_a_z_pose_without_a_known_position() -> None:
    fake = _moonraker()
    fake.status["toolhead"]["position"] = None
    with fake.serve(), pytest.raises(PhotoError, match="position unknown"):
        _link(fake).move({"z": 150}, home=False)
    assert fake.scripts == []


@pytest.mark.parametrize("state", ["error", None])
def test_link_fails_closed_on_an_error_or_unreadable_state(state: Any) -> None:
    fake = _moonraker()
    fake.status["print_stats"]["state"] = state
    with fake.serve(), pytest.raises(PhotoError, match="need an idle printer"):
        _link(fake).move({"z": 150}, home=False)
    assert fake.scripts == []


def test_link_refuses_a_pose_on_an_axis_without_a_known_range() -> None:
    fake = _moonraker()
    fake.status["toolhead"].pop("axis_minimum")
    with fake.serve(), pytest.raises(PhotoError, match="travel range of z unknown"):
        _link(fake).move({"z": 150}, home=False)
    assert fake.scripts == []


def test_capture_frames_follows_the_node_max_frames(fake_camera: FakeCamera) -> None:
    from topcam import node

    cfg = node.Config(device=fake_camera.path, stream_enabled=False, max_frames=4)
    with serve_node(fake_camera, cfg) as (url, _):
        shot = capture_frames(NodeCamera(url), StillSpec(640, 480, 6, 2, v4l2.Lock()), 6)
    assert len(shot.frames) == 6 and [c["count"] for c in fake_camera.captures] == [4, 2]


def test_light_off_is_sent_even_while_printing() -> None:
    fake = _moonraker("printing")
    with fake.serve():
        _link(fake).light_off("SET_PIN PIN=LED VALUE=0")
    assert fake.scripts == ["SET_PIN PIN=LED VALUE=0"]


FLEET = """# fleet header comment
u1:
  model: Snapmaker U1
  owner: own
  access: offline
  heads: [{label: T1, index: 0}]
  notes: keep me   # inline comment
k1se:
  model: Creality K1 SE
  owner: own
  access: offline
  heads: [{label: T1, index: 0}]
  photo:
    exposure: 100
    frames: 2
  notes: second
"""


def test_save_photo_settings_adds_and_replaces_blocks(tmp_path: Path) -> None:
    f = tmp_path / "printers.yaml"
    f.write_text(FLEET)
    save_photo_settings(
        f, "u1", {"camera": "topcam", "exposure": 250, "pose": {"x": 1.0, "z": 120.0}}
    )
    save_photo_settings(f, "k1se", {"exposure": 300, "led": True})
    text = f.read_text()
    assert (
        text.startswith("# fleet header comment\n")
        and "notes: keep me   # inline comment" in text
    )
    data = yaml.safe_load(text)
    assert data["u1"]["photo"] == {
        "camera": "topcam",
        "exposure": 250,
        "pose": {"x": 1.0, "z": 120.0},
    }
    assert data["k1se"]["photo"] == {"exposure": 300, "led": True}
    assert data["k1se"]["notes"] == "second"


def test_save_photo_settings_refuses_invalid_and_unknown(tmp_path: Path) -> None:
    f = tmp_path / "printers.yaml"
    f.write_text(FLEET)
    with pytest.raises(PhotoError, match="photo.focus"):
        save_photo_settings(f, "u1", {"focus": "far"})
    with pytest.raises(PhotoError, match="no printer 'mk4'"):
        save_photo_settings(f, "mk4", {"focus": 1})
    assert f.read_text() == FLEET
