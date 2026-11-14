"""The K1 SE bed check when the camera is not where the reference was taken (2026-11-13 false "plate missing").

The K1 SE camera rides with the toolhead, so the same empty plate is seen from another place after every job: the
whole picture (background included) shifts and zooms by ~100 px. Against a reference from elsewhere that was
"70 % of the bed surface changed" and a plate-missing FAIL that refused the start. The frames in tests/data/bed
are real (scaled to 640 px): `k1se_idle_ref` is the reference taken at the idle Z 124.8, `k1se_idle_moved` the
same empty plate 10 minutes earlier from ~100 px away, `k1se_idle_far` from a view that cannot be aligned,
`k1se_old_ref` the older Z 125.0 reference (another day, another place). A plate taken off is the idle frame with
the bed surface replaced (no real K1 frame of a bare bed exists from the idle view); parts are cut from a real frame.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from printer_ops import bed as pbed
from printer_ops import preflight
from test_bed_state import K1_ROI, U1_OCCUPIED, FakeCamera, device, frame, u1_ref

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_pose as P  # noqa: E402  (needs numpy + OpenCV)
import bed_state as B  # noqa: E402

DATA = Path(__file__).parent / "data" / "bed"
IDLE_Z = 124.8


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    return tmp_path


def ref_of(name: str, roi: tuple[tuple[float, float], ...] = K1_ROI, z: float = IDLE_Z) -> B.Reference:
    img = frame(name)
    m = B.polygon_mask(roi, img.shape[:2])
    return B.Reference("plate", z, 0.0, "k1se.watch.bed.roi", img, m, m, DATA / name)


def classify(img: Any, ref: B.Reference) -> B.Verdict:
    return B.classify(img, [ref], ref.z, "k1se")


def part_patch() -> Any:
    """A 3D-printed figure lying on the K1's plate (2026-11-11), cut from a real frame."""
    return cv2.resize(cv2.imread(str(DATA / "k1se_part_patch.jpg")), (112, 71), interpolation=cv2.INTER_AREA)


def with_part(img: Any, x: int, y: int) -> Any:
    out = img.copy()
    patch = part_patch()
    out[y:y + patch.shape[0], x:x + patch.shape[1]] = patch
    return out


def moved_by(img: Any, dx: float, dy: float, zoom: float) -> Any:
    """`img` as a camera that moved sees it: shifted and zoomed about the frame centre."""
    h, w = img.shape[:2]
    m = np.array([[zoom, 0, dx + (1 - zoom) * w / 2], [0, zoom, dy + (1 - zoom) * h / 2]], np.float32)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


# ---- the 2026-11-13 failure -------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["k1se_idle_moved.jpg", "k1se_idle_ref.jpg"])
def test_empty_plate_against_an_old_reference_from_another_view_is_never_plate_missing(name: str) -> None:
    """Both preflight frames showed the plate on and empty; judged against the only reference (Z 125.0, taken
    from another place) the pixels read ~70 % changed, which used to be `plate missing` and FAIL."""
    v = classify(frame(name), ref_of("k1se_old_ref.jpg", z=125.0))
    assert v.metrics["surface_vs_plate"] > 0.6  # the pixel evidence the old verdict stood on is still there ...
    assert (v.bed, v.plate) == ("unknown", "unknown"), v.reason  # ... but it is no verdict
    assert "the camera is not where it was when the reference was taken" in v.reason
    assert "3d printer bed reference k1se --capture" in v.reason  # how to fix it


def test_the_preflight_line_for_that_verdict_is_a_warn_with_the_way_out_not_a_fail() -> None:
    v = classify(frame("k1se_idle_moved.jpg"), ref_of("k1se_old_ref.jpg", z=125.0))
    check = preflight.check_bed_verdict({"printer": "k1se", "bed": v.bed, "plate": v.plate, "reason": v.reason,
                                         "source": "camera now", "age_s": 0.0})
    assert check.status == preflight.WARN
    assert "bed reference k1se --capture" in check.detail and '--assume-empty "why"' in check.detail


def test_empty_plate_seen_from_a_camera_that_moved_passes_against_a_reference_from_the_idle_view() -> None:
    """20:37 frame, ~100 px from where the 20:38 reference was taken: aligned to it, clear with the plate."""
    v = classify(frame("k1se_idle_moved.jpg"), ref_of("k1se_idle_ref.jpg"))
    assert (v.bed, v.plate) == ("clear", "present"), v.reason
    assert v.metrics["pose_shift_px"] > 50 and "the frame was aligned to it" in v.reason


def test_empty_plate_at_the_idle_view_passes() -> None:
    v = classify(frame("k1se_idle_ref.jpg"), ref_of("k1se_idle_ref.jpg"))
    assert (v.bed, v.plate) == ("clear", "present"), v.reason
    assert v.metrics["pose_ncc"] > 0.99 and "pose_shift_px" not in v.metrics


def test_a_view_that_cannot_be_aligned_is_unknown_never_a_finding() -> None:
    """The 20:30 frame sees the plate from nearer and lower: the scene aligns, the bed does not."""
    v = classify(frame("k1se_idle_far.jpg"), ref_of("k1se_idle_ref.jpg"))
    assert (v.bed, v.plate) == ("unknown", "unknown"), v.reason
    assert "does not line up" in v.reason and "bed reference k1se --capture" in v.reason
    assert "watch.bed.roi is drawn on the frame" in v.reason  # the polygon has to follow the camera, too


# ---- what must still FAIL ----------------------------------------------------------------------------------

def plate_off(img: Any, ref: B.Reference) -> Any:
    """The idle frame with the plate surface replaced by a bright bare-metal-like surface."""
    out = img.copy()
    noise = np.random.default_rng(7).integers(-25, 25, size=out.shape)
    bare = np.clip(np.array([185, 180, 175]) + noise, 0, 255).astype(np.uint8)
    out[ref.surface] = bare[ref.surface]
    return out


def test_a_plate_that_was_taken_off_is_missing_from_the_idle_view() -> None:
    ref = ref_of("k1se_idle_ref.jpg")
    v = classify(plate_off(ref.image, ref), ref)
    assert v.plate == "missing", v.reason
    assert "plate off, or the bed covered" in v.reason
    assert "if the plate IS on, retake the reference" in v.reason  # the fix, in case the reference is the wrong one


@pytest.mark.parametrize(("dx", "dy", "zoom"), [(-40.0, 25.0, 0.93), (35.0, -20.0, 1.06)])
def test_a_plate_that_was_taken_off_is_still_missing_when_the_camera_moved(dx: float, dy: float, zoom: float) -> None:
    ref = ref_of("k1se_idle_ref.jpg")
    v = classify(moved_by(plate_off(ref.image, ref), dx, dy, zoom), ref)
    assert v.plate == "missing", v.reason
    assert v.metrics["pose_shift_px"] > 10 and "aligned" in v.reason


def test_a_part_on_the_plate_is_occupied_from_the_idle_view() -> None:
    ref = ref_of("k1se_idle_ref.jpg")
    v = classify(with_part(ref.image, 230, 170), ref)
    assert v.bed == "occupied" and v.plate == "present", v.reason


def test_a_part_on_the_plate_is_occupied_when_the_camera_moved_and_the_evidence_marks_it_on_the_frame() -> None:
    """The part is pasted on the moved frame at x 100..212, y 175..246: the red evidence is drawn there."""
    ref = ref_of("k1se_idle_ref.jpg")
    img = with_part(frame("k1se_idle_moved.jpg"), 100, 175)
    v = classify(img, ref)
    assert v.bed == "occupied" and v.plate == "present", v.reason
    assert v.changed is not None and v.roi is not None and v.changed.shape == img.shape[:2]
    ys, xs = np.nonzero(v.changed)
    assert 100 <= xs.mean() <= 212 and 175 <= ys.mean() <= 246, (xs.mean(), ys.mean())
    assert v.roi.shape == img.shape[:2] and v.roi.any()


def test_a_part_seen_from_a_view_that_cannot_be_aligned_is_not_a_finding_either() -> None:
    v = classify(with_part(frame("k1se_idle_far.jpg"), 100, 150), ref_of("k1se_idle_ref.jpg"))
    assert v.bed == "unknown" and "bed reference k1se --capture" in v.reason


def test_a_small_camera_move_is_aligned_too_not_judged_as_the_same_view() -> None:
    """A 15 px shift still correlates (0.4) and used to count as the same view: edges then read as changes."""
    ref = ref_of("k1se_idle_ref.jpg")
    pose = P.locate(ref.image, moved_by(ref.image, 15.0, 8.0, 1.0), ref.volume)
    assert pose.state == P.MOVED and 12 < pose.shift_px < 25
    v = classify(moved_by(plate_off(ref.image, ref), 15.0, 8.0, 1.0), ref)
    assert v.plate == "missing" and v.metrics["pose_shift_px"] > 12, v.reason


def test_a_camera_that_moved_so_that_part_of_the_bed_is_out_of_frame_is_unknown_never_clear() -> None:
    """A part in the 35 % of the bed region the moved frame no longer covers must not pass as a clear bed."""
    ref = ref_of("k1se_idle_ref.jpg")
    img = moved_by(ref.image, 250.0, 0.0, 1.0)
    pose = P.locate(ref.image, img, ref.volume)
    assert pose.state == P.LOST and pose.visible < P.MIN_VISIBLE, pose.why
    v = classify(img, ref)
    assert (v.bed, v.plate) != ("clear", "present") and v.bed != "occupied", v.reason


def test_a_bare_bed_reference_from_another_view_is_not_used_against_the_aligned_frame() -> None:
    """Aligned to the with-plate reference, the frame is not aligned to a --no-plate one taken elsewhere: that one
    would read every plate as present (its surface differs everywhere) or tie."""
    plate = ref_of("k1se_idle_ref.jpg")
    bare = dataclasses.replace(ref_of("k1se_idle_far.jpg"), kind="noplate")
    v = B.classify(plate_off(plate.image, plate), [plate, bare], IDLE_Z, "k1se")
    assert v.plate == "missing", v.reason
    assert "bare-bed one" not in v.reason


def test_a_zoom_about_the_bed_is_a_move_even_though_the_bed_centre_stays_put() -> None:
    """The toolhead moving along the camera axis zooms about the bed: its centre stays, its border moves ~6 px."""
    ref = ref_of("k1se_idle_ref.jpg")
    ys, xs = np.nonzero(ref.volume)
    cx, cy, zoom = float(xs.mean()), float(ys.mean()), 1.1
    m = np.array([[zoom, 0, cx * (1 - zoom)], [0, zoom, cy * (1 - zoom)]], np.float32)
    img = cv2.warpAffine(plate_off(ref.image, ref), m, (ref.image.shape[1], ref.image.shape[0]),
                         borderMode=cv2.BORDER_REPLICATE)
    v = classify(img, ref)
    assert v.plate == "missing" and v.metrics["pose_shift_px"] > P.SAME_PX, v.reason


def test_a_bare_bed_reference_taken_from_here_judges_the_frame_when_the_with_plate_one_was_not() -> None:
    """The with-plate reference is the old one from another place, the bare-bed one was taken from where the camera
    is now: the plate is off, and that is what the bare-bed reference says."""
    idle = ref_of("k1se_idle_ref.jpg")
    old = ref_of("k1se_old_ref.jpg", z=IDLE_Z)
    bare = dataclasses.replace(idle, kind="noplate", image=plate_off(idle.image, idle))
    v = B.classify(plate_off(idle.image, idle), [old, bare], IDLE_Z, "k1se")
    assert v.plate == "missing", v.reason
    assert v.info["pose"]["state"] == "same"
    assert "does not see the scene" not in v.reason


def test_an_empty_bed_region_is_reported_by_the_classifier_not_crashed_on() -> None:
    ref = ref_of("k1se_idle_ref.jpg")
    empty = np.zeros(ref.image.shape[:2], bool)
    assert P.locate(ref.image, frame("k1se_idle_moved.jpg"), empty).state == P.BLIND
    v = classify(frame("k1se_idle_moved.jpg"), dataclasses.replace(ref, volume=empty, surface=empty))
    assert v.bed == "unknown" and "empty or tiny" in v.reason


def test_a_whole_frame_roi_has_no_scene_to_locate_by_and_is_judged_as_before() -> None:
    ref = ref_of("k1se_idle_ref.jpg")
    whole = np.ones(ref.image.shape[:2], bool)
    assert P.locate(ref.image, frame("k1se_idle_moved.jpg"), whole).state == P.BLIND


@pytest.mark.parametrize("name", [*U1_OCCUPIED, "u1_spaghetti_printing.jpg", "u1_z200_light_1703.jpg",
                                  "u1_z200_light_2013.jpg"])
def test_a_fixed_camera_is_never_a_lost_camera_whatever_happened_on_the_bed(name: str) -> None:
    """The U1's camera never moves: parts, spaghetti and a night light must never be called a moved camera and
    turned into `unknown` by it (the start gate FAILs on these); at worst the locator cannot tell and the verdict
    is the pixels' as ever."""
    ref = u1_ref()
    assert P.locate(ref.image, frame(name), ref.volume).state in (P.SAME, P.BLIND)


# ---- the locator ---------------------------------------------------------------------------------------------

def test_locate_same_moved_lost_and_blind() -> None:
    ref = ref_of("k1se_idle_ref.jpg")
    assert P.locate(ref.image, ref.image, ref.volume).state == P.SAME
    moved = P.locate(ref.image, frame("k1se_idle_moved.jpg"), ref.volume)
    assert moved.state == P.MOVED and moved.h is not None and moved.covered is not None
    assert P.locate(ref.image, frame("k1se_old_ref.jpg"), ref.volume).state == P.LOST
    black = np.zeros_like(ref.image)
    assert P.locate(ref.image, black, ref.volume).state == P.BLIND  # nothing to locate: judged as before


def test_the_k1_in_a_dark_room_against_a_daylight_reference_is_the_same_view() -> None:
    """Another light is not another view: the daylight reference against the dark-room frame is not aligned."""
    ref = frame("k1_z125_clear_ref.jpg")
    roi = B.polygon_mask(K1_ROI, ref.shape[:2])
    assert P.locate(ref, frame("k1_z125_clear_later.jpg"), roi).state == P.SAME


# ---- end to end: the check on a stored reference ---------------------------------------------------------------

def test_check_against_the_only_old_reference_warns_and_a_new_reference_then_passes(xdg: Path) -> None:
    dev = device("k1se", watch={"bed": {"enabled": True, "roi": [list(p) for p in K1_ROI]}}, model="Creality K1 SE")
    cfg = pbed.BedConfig(enabled=True, roi=K1_ROI)
    B.capture_reference(dev, cfg, plate=True, frame=(DATA / "k1se_old_ref.jpg").read_bytes(), frame_z=125.0, now=1.0)
    cam = FakeCamera("k1se_idle_moved.jpg")
    v = B.one_shot(dev, cfg, max_age=None, camera=cam, printer_fn=lambda d: ("complete", IDLE_Z), sleep=lambda s: None)
    assert (v["bed"], v["plate"]) == ("unknown", "unknown") and "bed reference k1se --capture" in v["reason"]
    assert preflight.check_bed_verdict(v).status == preflight.WARN

    B.capture_reference(dev, cfg, plate=True, camera=FakeCamera("k1se_idle_ref.jpg"),
                        printer_fn=lambda d: ("complete", IDLE_Z), now=2.0)
    v = B.one_shot(dev, cfg, max_age=None, camera=cam, printer_fn=lambda d: ("complete", IDLE_Z), sleep=lambda s: None)
    assert (v["bed"], v["plate"]) == ("clear", "present"), v["reason"]
    assert preflight.check_bed_verdict(v).status == preflight.PASS


def test_a_clear_judged_from_a_moved_camera_never_becomes_the_reference(xdg: Path) -> None:
    """Stored from there, the reference would keep the ROI polygon where it was drawn: another place on the plate."""
    import bed_refs

    dev = device("k1se", watch={"bed": {"enabled": True, "roi": [list(p) for p in K1_ROI]}}, model="Creality K1 SE")
    cfg = pbed.BedConfig(enabled=True, roi=K1_ROI)
    B.capture_reference(dev, cfg, plate=True, frame=(DATA / "k1se_idle_ref.jpg").read_bytes(), frame_z=IDLE_Z, now=1.0)
    v = B.one_shot(dev, cfg, max_age=None, camera=FakeCamera("k1se_idle_moved.jpg"),
                   printer_fn=lambda d: ("complete", IDLE_Z), sleep=lambda s: None)
    assert (v["bed"], v["plate"], v["pose"]["state"]) == ("clear", "present", "moved")
    lit = {**v, "light": {"on_for_frame": True}}
    assert "not where the reference was taken" in (bed_refs.refresh_refusal(lit, 0.5) or "")
    assert bed_refs.refresh_refusal({**lit, "pose": {"state": "same"}}, 0.5) is None
    assert "not where the reference was taken" in (bed_refs.refresh_refusal({**lit, "pose": {"state": "lost"}}, 0.5) or "")
