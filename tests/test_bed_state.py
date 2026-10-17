"""Tests for the printer bed state (issue #303): lib/bed_state.py (pixels, change gate, monitor,
references) and lib/printer_ops/bed.py (config, events, start gate, clear ack, watch child).

The frames in tests/data/bed are real camera frames scaled to 480 px: the Snapmaker U1 after a
cancelled job on 2026-10-14 (parked at Z 200, green parts standing on the bed until 14:00:47,
then taken off; 25 more minutes of the empty bed in drifting light), the U1's empty bed at Z 10
after a G28 on 2026-10-17 (#402), and the K1 SE empty bed in daylight and later in a dark room.
Every printer here is a loopback fake or a stub.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from errors import InvalidArgument
from printer_fakes import FakeMoonraker, u1_status
from printer_ops import bed as pbed
from printer_ops import preflight
from printer_ops.devices import Device

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_state as B  # noqa: E402  (needs numpy + OpenCV)

DATA = Path(__file__).parent / "data" / "bed"
K1_ROI = ((0.31, 0.48), (0.72, 0.27), (0.92, 0.55), (0.92, 0.97), (0.41, 0.97))
U1_CLEAR = ["u1_z200_clear_1403.jpg", "u1_z200_clear_1405.jpg", "u1_z200_clear_1412.jpg", "u1_z200_clear_1426.jpg"]
U1_OCCUPIED = ["u1_occupied_1351.jpg", "u1_occupied_1400.jpg"]


def frame(name: str) -> Any:
    return B.decode_small((DATA / name).read_bytes())


def device(name: str = "u1", *, watch: dict[str, Any] | None = None, trusted: tuple[str, ...] = ("STEPPER_BUZZ",),
           model: str | None = "Snapmaker U1", host: str = "127.0.0.1", port: int = 7125,
           access: str = "full") -> Device:
    return Device(name, "moonraker", host, port, model, Path("test.yaml"), trusted_macros=frozenset(trusted),
                  watch=watch or {}, access=access)


def u1_ref(kind: str = "plate", z: float = 200.0) -> B.Reference:
    from vision_geometry import bundled_calibration

    img = frame("u1_z200_clear_ref.jpg")
    cam = bundled_calibration("Snapmaker U1")
    vol, surf, _ = B.build_masks(device(), pbed.BedConfig(enabled=True), z, img.shape[:2])
    assert cam is not None and vol.any()
    return B.Reference(kind, z, 0.0, "calibration", img, vol, surf, DATA / "u1_z200_clear_ref.jpg")


def u1_z10_ref() -> B.Reference:
    """The U1 bed at Z 10 after a G28, empty, 2026-10-17 14:13 (a `bed status` evidence frame:
    its status text is burned in at the top left)."""
    img = frame("u1_z10_clear_ref.jpg")
    vol, surf, _ = B.build_masks(device(), pbed.BedConfig(enabled=True), 10.0, img.shape[:2])
    return B.Reference("plate", 10.0, 0.0, "calibration", img, vol, surf, DATA / "u1_z10_clear_ref.jpg")


def u1_match() -> B.ZMatch:
    match = B.z_match(device(), pbed.BedConfig(enabled=True))
    assert match.cam is not None
    return match


def k1_ref(kind: str = "plate", img: Any = None) -> B.Reference:
    img = frame("k1_z125_clear_ref.jpg") if img is None else img
    m = B.polygon_mask(K1_ROI, img.shape[:2])
    return B.Reference(kind, 125.0, 0.0, "roi", img, m, m, DATA / "k1_z125_clear_ref.jpg")


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    return tmp_path


# ---- the classifier on real frames ------------------------------------------------------------

@pytest.mark.parametrize("name", U1_OCCUPIED)
def test_u1_parts_on_the_bed_after_a_cancel_are_occupied(name: str) -> None:
    v = B.classify(frame(name), [u1_ref()], 200.0, "u1")
    assert v.bed == "occupied", v.reason
    assert v.metrics["blob_px"] > 1000


@pytest.mark.parametrize("name", U1_CLEAR)
def test_u1_bed_after_the_removal_is_clear_for_25_minutes_of_drifting_light(name: str) -> None:
    v = B.classify(frame(name), [u1_ref()], 200.0, "u1")
    assert v.bed == "clear", v.reason
    # the U1 parks the bed at Z 200; since the #262 refit the calibration sees the plate's front
    # edge at the bottom of the frame (~2% of it), so the plate is judged from that strip
    assert v.plate == "present", v.reason


def test_k1_empty_bed_in_a_dark_room_against_a_daylight_reference_is_clear_with_the_plate() -> None:
    v = B.classify(frame("k1_z125_clear_later.jpg"), [k1_ref()], 125.0, "k1se")
    assert (v.bed, v.plate) == ("clear", "present"), v.reason


def test_u1_spaghetti_frame_is_never_clear() -> None:
    """The 2026-10-14 spaghetti episode (a different Z, so live it would be `unknown`; pixel-wise
    against the Z 200 reference it must still not pass as an empty bed)."""
    assert B.classify(frame("u1_spaghetti_printing.jpg"), [u1_ref()], 200.0, "u1").bed == "occupied"


@pytest.mark.parametrize(("size", "expected"), [(24, "occupied"), (16, "occupied"), (12, "unknown")])
def test_k1_small_part_is_occupied_or_at_least_not_clear(size: int, expected: str) -> None:
    """A uniform square pasted on the real empty-bed frame (16 px ~ a 15 mm part on the K1 SE)."""
    img = frame("k1_z125_clear_ref.jpg").copy()
    img[180:180 + size, 300:300 + size] = (20, 20, 20)
    assert B.classify(img, [k1_ref()], 125.0, "k1se").bed == expected


def test_a_degenerate_roi_is_unknown_never_clear() -> None:
    """Review #303: a fat-fingered watch.bed.roi that erodes to nothing must not PASS the gate."""
    img = frame("k1_z125_clear_ref.jpg")
    m = B.polygon_mask(((0.5, 0.5), (0.51, 0.5), (0.5, 0.51)), img.shape[:2])
    v = B.classify(img, [B.Reference("plate", 125.0, 0.0, "roi", img, m, m, DATA / "x.jpg")], 125.0, "k1se")
    assert v.bed == "unknown" and "empty or tiny" in v.reason


def test_reference_from_a_saved_frame_needs_its_z_and_never_reads_the_printer(xdg: Path) -> None:
    from errors import UsageError

    def no_printer(d: Device) -> tuple[str, float | None]:
        raise AssertionError("must not read the printer")

    raw = (DATA / "u1_z200_clear_ref.jpg").read_bytes()
    ref = B.capture_reference(device(), pbed.BedConfig(enabled=True), plate=True, printer_fn=no_printer,
                              frame=raw, frame_z=199.7)
    assert ref["z"] == 199.7 and Path(ref["path"]).read_bytes() == raw
    v = B.classify(frame("u1_occupied_1400.jpg"), B.RefStore("u1").load(), 200.0, "u1")
    assert v.bed == "occupied"  # 0.3 mm from the PRINT_END park: within z_tol_mm
    with pytest.raises(UsageError, match="needs the Z"):
        B.capture_reference(device(), pbed.BedConfig(enabled=True), plate=True, frame=raw)


def test_plate_missing_with_only_the_with_plate_reference() -> None:
    """Plate off: the whole bed surface looks different (here: the U1 frame's pixels instead)."""
    ref = k1_ref()
    img = ref.image.copy()
    other = cv2.resize(frame("u1_occupied_1351.jpg"), (img.shape[1], img.shape[0]))
    img[ref.surface] = other[ref.surface]
    v = B.classify(img, [ref], 125.0, "k1se")
    assert v.plate == "missing" and v.bed == "unknown", v.reason
    assert "plate off, or the bed covered" in v.reason


def test_plate_judged_by_the_closer_of_the_two_references() -> None:
    plate = k1_ref()
    bare_img = plate.image.copy()
    other = cv2.resize(frame("u1_occupied_1351.jpg"), (bare_img.shape[1], bare_img.shape[0]))
    bare_img[plate.surface] = other[plate.surface]  # the bare magnetic sheet: another surface
    bare = k1_ref("noplate", bare_img)
    assert B.classify(bare_img, [plate, bare], 125.0, "k1se").plate == "missing"
    v = B.classify(plate.image, [plate, bare], 125.0, "k1se")
    assert (v.bed, v.plate) == ("clear", "present")


def test_no_reference_at_this_z_is_unknown_with_the_capture_command() -> None:
    v = B.classify(frame("u1_z200_clear_1403.jpg"), [u1_ref()], 150.0, "u1")
    assert (v.bed, v.plate) == ("unknown", "unknown")
    assert "no empty-bed reference at Z 150.0 (have: plate at Z 200.0)" in v.reason
    assert "3d printer bed reference u1 --capture" in v.reason


# ---- the U1 resting heights (#402): Z 200.3 after u1_night_v5, Z 275 after a home, Z 10 after G28 ----

@pytest.mark.parametrize(("name", "bed"), [(n, "clear") for n in U1_CLEAR] + [(n, "occupied") for n in U1_OCCUPIED])
def test_u1_park_at_z_200_3_is_judged_against_the_z_199_7_reference(name: str, bed: str) -> None:
    """u1_night_v5 (2026-10-17) ended at toolhead Z 200.3 (PRINT_END G0 Z200 plus that job's mesh
    and Z offset) and the reference was taken at Z 199.67: 0.63 mm, outside the old fixed 0.5 mm,
    so the bed stayed `unknown` with no event. The calibration says the picture moved 0.7 px."""
    match = u1_match()
    assert match(199.67, 200.3) and not match(200.0, 205.0)
    v = B.classify(frame(name), [u1_ref(z=199.67)], 200.3, "u1", match=match)
    assert v.bed == bed, v.reason
    assert 0.1 < v.metrics["bed_in_view"] < 0.2 and "low parts at the back may not show" in v.reason


def test_a_roi_polygon_printer_keeps_the_millimetre_tolerance() -> None:
    match = B.z_match(device("k1se", model=None), pbed.BedConfig(enabled=True, roi=K1_ROI))
    assert match.cam is None and match(125.0, 125.4) and not match(125.0, 125.6)


def test_u1_bed_at_z_275_is_out_of_the_camera_view_never_clear() -> None:
    """After a home the U1 bed sits at Z 275 (the Z endstop): the camera sees none of it, so no
    reference could tell a clear bed from a flat part; the verdict says so instead of asking for one."""
    v = B.classify(frame("u1_z200_clear_1403.jpg"), [u1_ref(z=199.67), u1_z10_ref()], 275.0, "u1", match=u1_match())
    assert (v.bed, v.plate) == ("unknown", "unknown")
    assert "out of the camera's view at Z 275.0" in v.reason and "sees the bed again at Z 210" in v.reason
    assert "--capture" not in v.reason and v.metrics["bed_in_view"] == 0.0


def test_no_reference_is_captured_where_the_bed_is_out_of_view(xdg: Path) -> None:
    from errors import UsageError

    with pytest.raises(UsageError, match="out of the camera's view at Z 275.0"):
        B.capture_reference(device(), pbed.BedConfig(enabled=True), plate=True,
                            camera=FakeCamera("u1_z200_clear_ref.jpg"), printer_fn=lambda d: ("idle", 275.0))
    assert B.list_references("u1") == []


def test_u1_at_z_10_after_a_home_is_judged_against_the_z_10_reference() -> None:
    from vision_geometry import prism_mask

    ref, match = u1_z10_ref(), u1_match()
    assert match.cam is not None and (match.in_view(10.0) or 0.0) > 0.9
    dim = np.clip(ref.image.astype(np.float32) * 0.8 + 10, 0, 255).astype(np.uint8)  # other room light
    assert B.classify(dim, [u1_ref(), ref], 10.0, "u1", match=match).bed == "clear"
    part = ref.image.copy()  # a 20 x 20 x 15 mm part in the middle of the bed, placed by the calibration
    box = [(125.0, 125.0), (145.0, 125.0), (145.0, 145.0), (125.0, 145.0)]
    part[prism_mask(match.cam, 10.0, part.shape[:2], [box], 0.0, 15.0) > 0] = (40, 160, 40)
    v = B.classify(part, [u1_ref(), ref], 10.0, "u1", match=match)
    assert v.bed == "occupied", v.reason
    # without a Z 10 reference: unknown with the capture command (the monitor warns, see below)
    assert B.classify(dim, [u1_ref()], 10.0, "u1", match=match).reason.startswith("no empty-bed reference at Z 10.0")


def test_a_bare_bed_reference_at_another_resolution_is_ignored() -> None:
    small = cv2.resize(frame("u1_z200_clear_ref.jpg"), (240, 135))
    m = np.ones(small.shape[:2], bool)
    noplate = B.Reference("noplate", 200.0, 0.0, "roi", small, m, m, DATA / "x.jpg")
    v = B.classify(frame("u1_z200_clear_1426.jpg"), [u1_ref(), noplate], 200.0, "u1")
    assert v.bed == "clear", v.reason


def test_a_frame_equally_far_from_both_plate_references_leaves_the_plate_unknown() -> None:
    ref = u1_ref()
    noplate = B.Reference("noplate", 200.0, 0.0, "calibration", ref.image, ref.volume, ref.surface, ref.path)
    plate, why, _ = B._judge_plate(ref.image, ref, noplate, 200.0, B.TH)
    assert plate == "unknown" and "plate unclear" in why


def test_a_malformed_reference_meta_is_skipped_in_the_listing(xdg: Path) -> None:
    d = pbed.refs_dir("u1")
    d.mkdir(parents=True)
    (d / "plate-z200.0.json").write_text('{"kind": "plate"}', encoding="utf-8")
    assert B.list_references("u1") == []


def test_frame_size_change_is_unknown() -> None:
    img = cv2.resize(frame("u1_z200_clear_1403.jpg"), (480, 300))
    assert "camera changed?" in B.classify(img, [u1_ref()], 200.0, "u1").reason


def test_decode_small_strips_k1_padding_and_targets_480_px() -> None:
    raw = (DATA / "k1_z125_clear_ref.jpg").read_bytes()
    padded = raw[:-2] + b"\x00" * 960 + b"\xff\xd9" + b"\x00" * 30
    assert B.decode_small(padded).shape == (270, 480, 3)
    assert B.jpeg_size(raw) == (480, 270)
    big = cv2.imencode(".jpg", cv2.resize(frame("u1_z200_clear_ref.jpg"), (1920, 1080)))[1].tobytes()
    assert B.decode_small(big).shape == (270, 480, 3)
    with pytest.raises(ValueError, match="not a decodable JPEG"):
        B.decode_small(b"\xff\xd8garbage")


# ---- the change gate: cheap while static ---------------------------------------------------------

def test_monitor_classifies_once_per_settled_change_and_never_while_static() -> None:
    calls: list[float | None] = []

    def classify(img: Any, z: float | None) -> B.Verdict:
        calls.append(z)
        return B.Verdict("clear", "unknown", "")

    mon = B.Monitor(classify)
    occupied, clear = frame("u1_occupied_1351.jpg"), frame("u1_z200_clear_1403.jpg")
    out = [mon.feed(occupied, 200.0) for _ in range(20)]
    assert mon.classified == 1 and sum(v is not None for v in out) == 1  # once at start, then static
    assert [mon.feed(clear, 200.0) for _ in range(3)] == [None, None, None]  # changed, settling
    assert mon.feed(clear, 200.0) is not None and mon.classified == 2
    for _ in range(50):
        mon.feed(clear, 200.0)
    assert mon.classified == 2 and calls == [200.0, 200.0]
    mon.feed(clear, 150.0)  # the bed moved in Z: judged again once settled
    for _ in range(3):
        mon.feed(clear, 150.0)
    assert mon.classified == 3


def test_monitor_does_not_judge_while_the_scene_keeps_moving() -> None:
    mon = B.Monitor(lambda img, z: B.Verdict("clear", "unknown", ""))
    a, b = frame("u1_occupied_1351.jpg"), frame("u1_z200_clear_1403.jpg")
    for i in range(30):  # a hand in the chamber: every frame differs from the last
        assert mon.feed(a if i % 2 else b, 200.0) is None
    assert mon.classified == 0


# ---- transitions / events ---------------------------------------------------------------------

@pytest.mark.parametrize(("prev", "new", "kinds"), [
    (("printing", "present"), ("occupied", "unknown"), ["bed_occupied"]),
    (("occupied", "present"), ("clear", "present"), ["bed_clear"]),
    (("clear", "present"), ("clear", "present"), []),
    (("clear", "present"), ("unknown", "unknown"), []),
    (("clear", "present"), ("unknown", "missing"), ["plate_missing"]),
    (("clear", "missing"), ("clear", "present"), ["plate_present"]),
    ((None, None), ("clear", "present"), ["bed_clear"]),
    (("printing", None), ("unknown", "unknown"), ["bed_unknown"]),  # #402: never silence after a job
    (("unknown", None), ("unknown", "unknown"), []),
    (("unknown", None), ("clear", "present"), ["bed_clear"]),
])
def test_transitions(prev: tuple[Any, Any], new: tuple[str, str], kinds: list[str]) -> None:
    assert pbed.transitions(*prev, *new) == kinds


def test_event_from_monitor_line() -> None:
    e = pbed.event_from("u1", {"kind": "bed_clear", "text": "clear", "ts": 5.0, "key": "u1:bed_clear:5",
                                "data": {"frame": "/x.jpg"}})
    assert (e.kind, e.severity, e.key, e.ts, e.data["frame"]) == ("bed_clear", "info", "u1:bed_clear:5", 5.0, "/x.jpg")
    assert pbed.event_from("u1", {"kind": "bed_unknown", "text": "cannot determine"}).severity == "warning"
    with pytest.raises(ValueError, match="unknown bed event kind"):
        pbed.event_from("u1", {"kind": "rm -rf"})


# ---- config -----------------------------------------------------------------------------------

def test_bed_config_defaults_off_and_validates() -> None:
    assert pbed.bed_config(device()) == pbed.BedConfig()
    cfg = pbed.bed_config(device(watch={"bed": {"enabled": True, "roi": [list(p) for p in K1_ROI]}}))
    assert cfg.enabled and cfg.roi == K1_ROI and cfg.clear_ack is None
    with pytest.raises(InvalidArgument, match="watch.bed"):
        pbed.bed_config(device(watch={"bed": {"enabled": True, "colour": 1}}))
    with pytest.raises(InvalidArgument, match="roi"):
        pbed.bed_config(device(watch={"bed": {"roi": [[0, 0], [2, 0], [1, 1]]}}))


def test_clear_ack_command_must_be_trusted() -> None:
    ack = {"enabled": True, "command": "STEPPER_BUZZ STEPPER=stepper_z"}
    assert pbed.bed_config(device(watch={"bed": {"clear_ack": ack}})).clear_ack == pbed.AckConfig(
        ("STEPPER_BUZZ STEPPER=stepper_z",), 600.0, 10.0)
    with pytest.raises(InvalidArgument, match="a command listed in u1.trusted_macros"):
        pbed.bed_config(device(watch={"bed": {"clear_ack": ack}}, trusted=()))
    lines = ["FORCE_MOVE STEPPER=stepper_z DISTANCE=0.5 VELOCITY=10", "FORCE_MOVE STEPPER=stepper_z DISTANCE=-0.5"]
    got = pbed.bed_config(device(watch={"bed": {"clear_ack": {"enabled": True, "command": lines}}},
                                 trusted=("FORCE_MOVE",))).clear_ack
    assert got is not None and got.command == tuple(lines)
    with pytest.raises(InvalidArgument, match="clear_ack.command"):
        pbed.bed_config(device(watch={"bed": {"clear_ack": {"enabled": True, "command": "STEPPER_BUZZ ; x"}}}))
    with pytest.raises(InvalidArgument, match="clear_ack.command"):
        pbed.bed_config(device(watch={"bed": {"clear_ack": {"enabled": True, "command": "STEPPER_BUZZ X\rM112"}}}))
    with pytest.raises(InvalidArgument, match="a Z stepper"):  # min_z_mm guards the bed, not the extruder
        pbed.bed_config(device(watch={"bed": {"clear_ack": {"enabled": True, "command": "STEPPER_BUZZ STEPPER=extruder"}}}))
    with pytest.raises(InvalidArgument, match="a Z stepper"):
        pbed.bed_config(device(trusted=("FORCE_MOVE",),
                               watch={"bed": {"clear_ack": {"enabled": True, "command": "FORCE_MOVE STEPPER=stepper_x DISTANCE=1 VELOCITY=5"}}}))
    off = {"enabled": False, "command": "NOT_TRUSTED"}
    assert pbed.bed_config(device(watch={"bed": {"clear_ack": off}})).clear_ack is None


# ---- the clear acknowledgement ----------------------------------------------------------------

class FakeAdapter:
    def __init__(self, state: str = "idle", z: float | None = 200.0) -> None:
        self.state = state
        self.z = z
        self.sent: list[str] = []

    def query(self, objects: str) -> dict[str, Any]:
        return {"toolhead": {"position": [0.0, 0.0, self.z, 0.0]}}

    def status(self) -> Any:
        return SimpleNamespace(state=self.state, raw_state=self.state)

    def run_gcode(self, script: str) -> None:
        self.sent.append(script)


ACK = pbed.AckConfig(("STEPPER_BUZZ STEPPER=stepper_z",), 600.0, 10.0)


def test_clear_ack_sends_once_when_quiet() -> None:
    a = FakeAdapter("complete")
    assert pbed.clear_ack(device(), ACK, z=200.0, last_ack=None, now=1000.0, adapter=a) == (
        True, "sent STEPPER_BUZZ STEPPER=stepper_z")
    assert a.sent == ["STEPPER_BUZZ STEPPER=stepper_z"]


@pytest.mark.parametrize(("kw", "state", "why"), [
    ({"z": 200.0, "last_ack": 900.0}, "idle", "debounced"),
    ({"z": 5.0, "last_ack": None}, "idle", "below min_z_mm"),
    ({"z": None, "last_ack": None}, "idle", "unknown or below"),
    ({"z": 200.0, "last_ack": None}, "printing", "printer is printing"),
])
def test_clear_ack_refuses(kw: dict[str, Any], state: str, why: str) -> None:
    a = FakeAdapter(state)
    sent, what = pbed.clear_ack(device(), ACK, now=1000.0, adapter=a, **kw)
    assert not sent and why in what and a.sent == []


def test_clear_ack_rereads_z_right_before_the_write() -> None:
    """The bed was jogged down after the verdict frame: the fresh Z refuses the write."""
    for z_now in (5.0, None, float("nan")):
        a = FakeAdapter("idle", z=z_now)
        sent, what = pbed.clear_ack(device(), ACK, z=200.0, last_ack=None, now=1000.0, adapter=a)
        assert not sent and "bed Z now" in what and a.sent == []


def test_clear_ack_refuses_read_only_and_untrusted_printers() -> None:
    a = FakeAdapter()
    assert "no writes" in pbed.clear_ack(device(access="read-only"), ACK, z=200.0, last_ack=None, now=1.0,
                                         adapter=a)[1]
    assert "not in u1.trusted_macros" in pbed.clear_ack(device(trusted=()), ACK, z=200.0, last_ack=None, now=1.0,
                                                        adapter=a)[1]
    assert a.sent == []


def test_clear_ack_through_moonraker_posts_one_gcode_script(xdg: Path) -> None:
    status = u1_status("complete")
    status["toolhead"] = {**status.get("toolhead", {}), "position": [100.0, 100.0, 200.0, 0.0]}
    moon = FakeMoonraker(status=status)
    with moon.serve():
        sent, _ = pbed.clear_ack(device(port=moon.port), ACK, z=200.0, last_ack=None, now=1.0)
    assert sent
    assert [(r.path, r.query) for r in moon.writes()] == [
        ("/printer/gcode/script", {"script": ["STEPPER_BUZZ STEPPER=stepper_z"]})]


# ---- the start gate ---------------------------------------------------------------------------

@pytest.mark.parametrize(("bed", "plate", "status"), [
    ("clear", "present", "PASS"), ("occupied", "present", "FAIL"), ("clear", "missing", "FAIL"),
    ("unknown", "missing", "FAIL"), ("clear", "unknown", "WARN"), ("unknown", "unknown", "WARN"),
    ("printing", "unknown", "WARN"),
])
def test_verdict_check(bed: str, plate: str, status: str) -> None:
    c = preflight.check_bed_verdict({"bed": bed, "plate": plate, "source": "watch", "age_s": 12.0, "reason": "r",
                            "frame": "/f.jpg"})
    assert (c.name, c.status) == ("bed", status)
    assert c.lines == ["frame: /f.jpg"] and "watch, 12 s ago" in c.detail


def test_bed_check_skips_printers_without_the_monitor() -> None:
    c = preflight.check_bed(device(), runner=lambda *a, **k: pytest.fail("must not run"))
    assert c.status == "SKIP" and "watch.bed.enabled" in c.detail


def test_bed_check_runs_the_tool_and_degrades_to_warn() -> None:
    dev = device(watch={"bed": {"enabled": True, "stale_s": 30}})
    seen: list[list[str]] = []

    def ok(argv: list[str], **kw: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"bed": "occupied", "plate": "present",
                                                                  "source": "camera now", "age_s": 0}), "")

    assert preflight.check_bed(dev, runner=ok).status == "FAIL"
    assert seen[0][-5:] == ["status", "u1", "--json", "--max-age", "30"]
    broken = preflight.check_bed(dev, runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    assert broken.status == "WARN" and "boom" in broken.detail

    def timeout(argv: list[str], **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 1)

    assert preflight.check_bed(dev, runner=timeout).status == "WARN"


# ---- references, one-shot status and the monitor loop with stubs --------------------------------

class FakeCamera:
    def __init__(self, name: str) -> None:
        self.jpeg = (DATA / name).read_bytes()
        self.grabs = 0

    def show(self, name: str) -> None:
        self.jpeg = (DATA / name).read_bytes()

    def keepalive(self) -> bool:
        return False

    def grab(self) -> Any:
        """Every grab is a new JPEG (a COM segment with a counter) of the same picture, like a
        camera that rewrites its snapshot."""
        self.grabs += 1
        com = b"\xff\xfe" + (2 + 8).to_bytes(2, "big") + self.grabs.to_bytes(8, "big")
        return SimpleNamespace(jpeg=self.jpeg[:2] + com + self.jpeg[2:])


class SimClock:
    """Time moves only in sleep(): every monitor poll takes exactly its interval."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def test_capture_then_one_shot_status(xdg: Path) -> None:
    dev = device()
    cfg = pbed.BedConfig(enabled=True)
    cam = FakeCamera("u1_z200_clear_ref.jpg")
    ref = B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 200.0), now=1.0)
    assert ref["kind"] == "plate" and ref["z"] == 200.0 and "camera calibration" in ref["roi_source"]
    assert Path(ref["path"]).name == "plate-z200.0.jpg" and Path(ref["path"]).is_file()
    assert [r["kind"] for r in B.list_references("u1")] == ["plate"]
    B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 200.2), now=2.0)
    assert len(B.list_references("u1")) == 1  # one per kind and Z

    cam.show("u1_occupied_1400.jpg")
    v = B.one_shot(dev, cfg, max_age=None, camera=cam, printer_fn=lambda d: ("complete", 200.0), sleep=lambda s: None)
    assert (v["bed"], v["source"]) == ("occupied", "camera now") and Path(v["frame"]).is_file()
    printing = B.one_shot(dev, cfg, max_age=None, camera=cam, printer_fn=lambda d: ("printing", 3.0))
    assert printing["bed"] == "printing" and cam.grabs == 4  # two for the frozen check; none while printing
    frozen = B.one_shot(dev, cfg, max_age=None, camera=FrozenCamera("u1_z200_clear_ref.jpg"),
                        printer_fn=lambda d: ("complete", 200.0), sleep=lambda s: None)
    assert frozen["bed"] == "unknown" and "frozen camera" in frozen["reason"]  # never a PASS
    parts = B.one_shot(dev, cfg, max_age=None, camera=FrozenCamera("u1_occupied_1400.jpg"),
                       printer_fn=lambda d: ("complete", 200.0), sleep=lambda s: None)
    assert parts["bed"] == "occupied"  # a still camera showing parts still blocks the start


def test_capture_refused_while_printing(xdg: Path) -> None:
    from errors import UsageError

    with pytest.raises(UsageError, match="is printing"):
        B.capture_reference(device(), pbed.BedConfig(enabled=True), plate=True, camera=FakeCamera("k1_z125_clear_ref.jpg"),
                            printer_fn=lambda d: ("printing", 1.0))


def test_monitor_loop_occupied_then_removal_emits_events_and_acks(xdg: Path) -> None:
    """After a job: parts on the bed -> bed_occupied; a hand takes them (frames change), the bed
    settles empty -> bed_clear with an evidence frame, then one ack."""
    dev = device(watch={"bed": {"enabled": True}})
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2,
                         clear_ack=pbed.AckConfig(("STEPPER_BUZZ STEPPER=stepper_z",), 600.0, 10.0))
    cam = FakeCamera("u1_z200_clear_ref.jpg")
    B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 200.0))
    script = ["printing"] * 3 + ["complete"] * 20
    shown = (["u1_occupied_1351.jpg"] * 11  # the job's last polls, then the parts standing on the bed
             + ["u1_occupied_1400.jpg", "u1_z200_clear_1405.jpg", "u1_occupied_1351.jpg"]  # a hand at work
             + ["u1_z200_clear_1412.jpg"] * 9)  # the bed left empty
    grabs_before = cam.grabs
    polls = {"i": 0}

    def printer_fn(d: Device) -> tuple[str, float | None]:
        i = min(polls["i"], len(script) - 1)
        cam.show(shown[i])
        polls["i"] += 1
        return script[i], 200.0

    acks: list[dict[str, Any]] = []

    def ack_fn(d: Device, ack: Any, **kw: Any) -> tuple[bool, str]:
        acks.append(kw)
        return True, "sent STEPPER_BUZZ STEPPER=stepper_z"

    out, clock = _Lines(), SimClock()
    mon = B.run_monitor(dev, cfg, duration=3 * 10.0 + 20 * 0.2 - 0.01, out=out, camera=cam, printer_fn=printer_fn,
                        ack_fn=ack_fn, clock=clock, sleep=clock.sleep)
    assert polls["i"] == len(script)
    events = [o["event"] for o in out.objs if "event" in o]
    assert [e["kind"] for e in events] == ["bed_occupied", "bed_clear"]
    assert Path(events[1]["data"]["frame"]).is_file() and events[1]["data"]["bed"] == "clear"
    assert len(acks) == 1 and acks[0]["z"] == 200.0
    assert mon.classified == 2  # once after the job, once after the removal settled
    state = pbed.read_state("u1")
    assert state is not None and state["bed"] == "clear" and state["last_ack"] == acks[0]["now"]
    assert cam.grabs - grabs_before == 20  # no frame grabbed during the 3 printing polls


def test_no_ack_on_a_first_clear_without_parts_taken_off(xdg: Path) -> None:
    """A fresh monitor on an already empty bed reports bed_clear but never buzzes."""
    dev = device(watch={"bed": {"enabled": True}})
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2,
                         clear_ack=pbed.AckConfig(("STEPPER_BUZZ STEPPER=stepper_z",), 600.0, 10.0))
    cam = FakeCamera("u1_z200_clear_ref.jpg")
    B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 200.0))
    acks: list[Any] = []

    def ack_fn(d: Device, ack: Any, **kw: Any) -> tuple[bool, str]:
        acks.append(kw)
        return True, "sent"

    out, clock = _Lines(), SimClock()
    B.run_monitor(dev, cfg, duration=2.0, out=out, camera=cam, printer_fn=lambda d: ("idle", 200.0),
                  ack_fn=ack_fn, clock=clock, sleep=clock.sleep)
    assert [o["event"]["kind"] for o in out.objs if "event" in o] == ["bed_clear"]
    assert acks == []


def test_a_second_monitor_of_the_same_printer_is_refused(xdg: Path) -> None:
    """One monitor per printer: a second one would double the clear ack and overwrite verdicts."""
    from errors import UsageError

    dev = device()
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2)
    cam = FakeCamera("u1_z200_clear_ref.jpg")
    B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 200.0))
    inner: list[str] = []

    def printer_fn(d: Device) -> tuple[str, float | None]:
        if not inner:
            with pytest.raises(UsageError, match="already running"):
                B.run_monitor(dev, cfg, duration=0.1, out=_Lines(), camera=cam, printer_fn=lambda d: ("idle", 200.0))
            inner.append("refused")
        return "idle", 200.0

    clock = SimClock()
    B.run_monitor(dev, cfg, duration=0.5, out=_Lines(), camera=cam, printer_fn=printer_fn,
                  clock=clock, sleep=clock.sleep)
    assert inner == ["refused"]
    B.run_monitor(dev, cfg, duration=0.1, out=_Lines(), camera=cam, printer_fn=lambda d: ("idle", 200.0),
                  clock=clock, sleep=clock.sleep)  # released after the first one ended


def test_an_undecodable_frame_is_a_clean_error(xdg: Path) -> None:
    from errors import UsageError
    from printer_ops.base import PrinterCommError

    dev, cfg = device(), pbed.BedConfig(enabled=True)
    with pytest.raises(UsageError, match="a finite number"):
        B.capture_reference(dev, cfg, plate=True, frame=b"x", frame_z=float("nan"))
    with pytest.raises(UsageError, match="--from frame is unusable"):
        B.capture_reference(dev, cfg, plate=True, frame=b"not a jpeg", frame_z=200.0)
    B.capture_reference(dev, cfg, plate=True, camera=FakeCamera("u1_z200_clear_ref.jpg"),
                        printer_fn=lambda d: ("idle", 200.0))
    hits = iter(range(10))
    bad = SimpleNamespace(keepalive=lambda: False,
                          grab=lambda: SimpleNamespace(jpeg=b"\xff\xd8broken" + bytes([next(hits)])))
    with pytest.raises(PrinterCommError, match="camera frame unusable"):
        B.one_shot(dev, cfg, max_age=None, camera=bad, printer_fn=lambda d: ("idle", 200.0), sleep=lambda s: None)


@pytest.mark.parametrize("argv", [["status", "u1", "--capture"], ["status", "u1", "--duration", "0"], ["status", "u1", "--no-plate"],
                                  ["reference", "u1", "--duration", "10"], ["monitor", "u1", "--max-age", "30"],
                                  ["reference", "u1", "--no-plate"]])
def test_options_outside_their_subcommand_are_refused(argv: list[str]) -> None:
    from errors import UsageError

    with pytest.raises(UsageError, match="does not take"):
        B.main(argv)


class _Lines:
    def __init__(self) -> None:
        self.objs: list[dict[str, Any]] = []

    def write(self, text: str) -> None:
        self.objs += [json.loads(line) for line in text.splitlines() if line.strip()]

    def flush(self) -> None:
        pass


def test_monitor_restart_does_not_repeat_the_last_event(xdg: Path) -> None:
    dev = device()
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2)
    cam = FakeCamera("u1_z200_clear_ref.jpg")
    B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 200.0))
    for expected in (["bed_clear"], []):
        out, clock = _Lines(), SimClock()
        B.run_monitor(dev, cfg, duration=2.0, out=out, camera=cam, printer_fn=lambda d: ("idle", 200.0),
                      clock=clock, sleep=clock.sleep)
        assert [o["event"]["kind"] for o in out.objs if "event" in o] == expected


def test_monitor_warns_once_when_the_bed_cannot_be_judged_after_a_job(xdg: Path) -> None:
    """#402: after the job the U1 bed sits at Z 275, out of the camera's view: one bed_unknown
    warning instead of silence; a restart does not repeat it; the bed back in view at the PRINT_END
    park (Z 200.3, reference at Z 199.67) with nothing on it is bed_clear."""
    dev = device(watch={"bed": {"enabled": True}})
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2)
    cam = FakeCamera("u1_z200_clear_ref.jpg")
    B.capture_reference(dev, cfg, plate=True, camera=cam, printer_fn=lambda d: ("idle", 199.67))
    script = [("printing", 50.0)] * 2 + [("complete", 275.0)] * 15
    polls = {"i": 0}

    def printer_fn(d: Device) -> tuple[str, float | None]:
        i = min(polls["i"], len(script) - 1)
        polls["i"] += 1
        return script[i]

    out, clock = _Lines(), SimClock()
    B.run_monitor(dev, cfg, duration=2 * 10.0 + 15 * 0.2 - 0.01, out=out, camera=cam, printer_fn=printer_fn,
                  clock=clock, sleep=clock.sleep)
    events = [o["event"] for o in out.objs if "event" in o]
    assert [e["kind"] for e in events] == ["bed_unknown"]
    assert events[0]["text"].startswith("u1: cannot determine the bed state after the job (camera, Z 275.0)")
    assert "out of the camera's view" in events[0]["text"] and Path(events[0]["data"]["frame"]).is_file()
    assert pbed.event_from("u1", events[0]).severity == "warning"
    for z, expected in ((275.0, []), (200.3, ["bed_clear"])):
        out = _Lines()
        B.run_monitor(dev, cfg, duration=2.0, out=out, camera=cam, printer_fn=lambda d, z=z: ("idle", z),
                      clock=clock, sleep=clock.sleep)
        assert [o["event"]["kind"] for o in out.objs if "event" in o] == expected


class FrozenCamera(FakeCamera):
    def grab(self) -> Any:
        """Byte-identical JPEGs every time, like a static or frozen snapshot."""
        self.grabs += 1
        return SimpleNamespace(jpeg=self.jpeg)


def test_monitor_judges_a_frozen_camera_once_at_start(xdg: Path) -> None:
    """Identical snapshot bytes still settle the monitor so the start verdict comes, but they never
    mark it current again: a frozen camera's verdict ages out of the start gate's --max-age."""
    dev = device()
    cfg = pbed.BedConfig(enabled=True, interval_s=0.2)
    B.capture_reference(dev, cfg, plate=True, camera=FakeCamera("u1_z200_clear_ref.jpg"),
                        printer_fn=lambda d: ("idle", 200.0))
    cam = FrozenCamera("u1_occupied_1351.jpg")
    out, clock = _Lines(), SimClock()
    mon = B.run_monitor(dev, cfg, duration=15.0, out=out, camera=cam, printer_fn=lambda d: ("idle", 200.0),
                        clock=clock, sleep=clock.sleep)
    assert [o["event"]["kind"] for o in out.objs if "event" in o] == ["bed_occupied"]
    assert mon.classified == 1
    state = pbed.read_state("u1")
    assert state is not None and state["bed"] == "occupied" and state["checked"] == state["ts"]


# ---- the watch child --------------------------------------------------------------------------

def test_bed_child_turns_lines_into_events_and_restarts() -> None:
    script = ("import json,sys;"
              "print(json.dumps({'event':{'kind':'bed_clear','text':'t','ts':1.0}}));"
              "print('not json');"
              "print(json.dumps({'state':{'bed':'clear'}}));sys.exit(3)")
    got: list[Any] = []
    states: list[tuple[str, dict[str, Any]]] = []
    stop = threading.Event()
    child = pbed.BedChild("u1", got.append, stop, on_state=lambda n, s: states.append((n, s)),
                          argv=lambda name: [sys.executable, "-c", script])
    child.start()
    for _ in range(100):
        if got and child.restarts:
            break
        threading.Event().wait(0.05)
    stop.set()
    child.close(5.0)
    assert got and got[0].kind == "bed_clear" and got[0].printer == "u1"
    assert states and states[0] == ("u1", {"bed": "clear"})
    assert child.restarts >= 1
