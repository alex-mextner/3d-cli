"""The bed verdict stays honest under light, exposure and registration drift (issue #723).

2026-11-04: the idle Snapmaker U1 sat at Z 200, where its camera sees 15 % of the plate, and
`print-preflight` said "objects on the bed 14.3 %" on an empty, clean plate. The owner's evidence
frames showed why: at that Z the bed ROI is mostly the machine's back wall and the window behind the
printer; the window light had changed, a cable had moved and the picture sat a pixel or two off the
reference from October. Only the annotated evidence image was kept, so
`u1_z200_light_1703.jpg` and `u1_z200_light_2013.jpg` are rebuilt from it: the red "changed"
overlay (0.45 * pixel + (0, 0, 140), 4x4 blocks) was inverted, the cyan outline and the caption
were inpainted, and the result was scaled to 480 px like the other fixtures. The old classifier
reads both as occupied (14.1 % / 17.1 % changed, a 823 / 925 px region) against
`u1_z200_clear_ref_1016.jpg`, the reference that was in force. Those frames carry a real change
(a moved room object at the edge of the ROI) that no pixel difference can call "not the bed", so
the honest verdict at a 15 % view is UNKNOWN with the way out, never occupied and never clear.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from printer_ops import bed as pbed
from test_bed_state import DATA, device, frame, k1_ref, u1_match

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_state as B  # noqa: E402  (needs numpy + OpenCV)

OLD = dataclasses.replace(
    B.TH, shift_px=0, open_px=3, occupied_blob_low_px=B.TH.occupied_blob_px
)  # the classifier before #723


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def u1_ref_1016() -> B.Reference:
    """The Z 199.67 reference of 2026-10-16, the one the false verdict was judged against."""
    img = frame("u1_z200_clear_ref_1016.jpg")
    vol, surf, _ = B.build_masks(
        device(), pbed.BedConfig(enabled=True), 199.67, img.shape[:2]
    )
    return B.Reference(
        "plate", 199.67, 0.0, "calibration", img, vol, surf, DATA / "x.jpg"
    )


LIGHT_DRIFT = ["u1_z200_light_1703.jpg", "u1_z200_light_2013.jpg"]


@pytest.mark.parametrize("name", LIGHT_DRIFT)
def test_the_empty_plate_under_new_window_light_is_not_called_occupied(
    name: str,
) -> None:
    v = B.classify(frame(name), [u1_ref_1016()], 200.2, "u1", match=u1_match())
    assert v.bed == "unknown", v.reason
    assert v.metrics["changed"] < B.TH.occupied_min
    assert v.metrics["blob_px"] < B.TH.occupied_blob_low_px
    # the verdict says what to do instead of guessing
    assert "`3d printer bed check u1`" in v.reason
    assert "the room behind the bed" in v.reason


@pytest.mark.parametrize("name", LIGHT_DRIFT)
def test_the_same_frames_failed_the_old_classifier(name: str) -> None:
    """Teeth: with the pre-#723 settings the same frames are 'occupied' (the incident)."""
    v = B.classify(frame(name), [u1_ref_1016()], 200.2, "u1", OLD, u1_match())
    assert v.bed == "occupied", v.reason
    assert 0.12 < v.metrics["changed"] < 0.20


def paste(img: Any, roi: Any, size: int, colour: tuple[int, int, int]) -> Any:
    """`size` x `size` px block of `colour` centred in the ROI's middle rows."""
    out = img.copy()
    ys, xs = np.nonzero(roi)
    cy, cx = int(np.percentile(ys, 40)), int(np.percentile(xs, 60))
    out[cy : cy + size, cx : cx + size] = colour
    return out


def test_a_real_part_at_a_low_view_is_still_occupied() -> None:
    """Hiding real objects is the one thing the tolerance must not do."""
    ref = B.Reference(
        "plate",
        200.0,
        0.0,
        "calibration",
        frame("u1_z200_clear_ref.jpg"),
        *B.build_masks(device(), pbed.BedConfig(enabled=True), 200.0, (270, 480))[:2],
        DATA / "x.jpg",
    )
    base = frame("u1_z200_clear_1403.jpg")
    big = paste(base, ref.volume, 56, (30, 90, 30))
    v = B.classify(big, [ref], 200.0, "u1", match=u1_match())
    assert v.bed == "occupied", v.reason
    # smaller than the low-view minimum: not clear, and the way out is named
    small = paste(base, ref.volume, 16, (30, 90, 30))
    v = B.classify(small, [ref], 200.0, "u1", match=u1_match())
    assert v.bed in ("unknown", "occupied"), v.reason
    if v.bed == "unknown":
        assert "bed check u1" in v.reason


@pytest.mark.parametrize(("size", "bed"), [(24, "occupied"), (16, "occupied")])
def test_a_small_part_at_a_full_view_is_found_as_before(size: int, bed: str) -> None:
    """The K1 SE sees the whole bed: the tolerance for shifts did not cost its sensitivity."""
    img = frame("k1_z125_clear_ref.jpg").copy()
    img[180 : 180 + size, 300 : 300 + size] = (20, 20, 20)
    assert B.classify(img, [k1_ref()], 125.0, "k1se").bed == bed


def test_a_scene_shifted_by_two_pixels_is_not_a_part() -> None:
    """The bed parked 0.3 mm off its reference Z, a camera nudge: every edge of the scene moves a
    pixel or two. That is a line, not a region."""
    shifted = np.roll(frame("u1_z200_clear_ref.jpg"), (2, 2), axis=(0, 1))
    refs = [
        B.Reference(
            "plate",
            200.0,
            0.0,
            "calibration",
            frame("u1_z200_clear_ref.jpg"),
            *B.build_masks(device(), pbed.BedConfig(enabled=True), 200.0, (270, 480))[
                :2
            ],
            DATA / "x.jpg",
        )
    ]
    assert B.classify(shifted, refs, 200.0, "u1", match=u1_match()).bed == "clear"
    assert B.classify(shifted, refs, 200.0, "u1", OLD, u1_match()).bed == "occupied"


@pytest.mark.parametrize(("gain", "offset"), [(0.7, 0), (1.25, -10), (0.55, 12)])
def test_a_lighting_change_on_an_empty_bed_is_clear(gain: float, offset: int) -> None:
    """Exposure / room light: the whole frame darker or brighter."""
    img = np.clip(
        frame("k1_z125_clear_ref.jpg").astype(np.float32) * gain + offset, 0, 255
    ).astype(np.uint8)
    v = B.classify(img, [k1_ref()], 125.0, "k1se")
    assert v.bed == "clear", v.reason


def test_a_lighting_change_on_a_u1_bed_with_parts_still_shows_the_parts() -> None:
    ref = B.Reference(
        "plate",
        200.0,
        0.0,
        "calibration",
        frame("u1_z200_clear_ref.jpg"),
        *B.build_masks(device(), pbed.BedConfig(enabled=True), 200.0, (270, 480))[:2],
        DATA / "x.jpg",
    )
    dim = np.clip(
        frame("u1_occupied_1400.jpg").astype(np.float32) * 0.7, 0, 255
    ).astype(np.uint8)
    assert B.classify(dim, [ref], 200.0, "u1", match=u1_match()).bed == "occupied"


# ---- evidence ---------------------------------------------------------------------------------


def test_evidence_keeps_the_camera_frame_next_to_the_annotated_one(
    tmp_path: Path,
) -> None:
    raw = (DATA / "u1_z200_clear_1403.jpg").read_bytes()
    v = B.classify(frame("u1_z200_clear_1403.jpg"), [], 200.0, "u1")
    path = B.save_evidence("u1", raw, v, "status-unknown", 1_700_000_000.0)
    assert Path(path).is_file() and path.endswith("status-unknown.jpg")
    assert B.raw_frame_path(path).read_bytes() == raw
    assert B.raw_frame_path(path).name == "u1-20231114T221320Z-status-unknown.raw.jpg"


def test_pruning_old_evidence_removes_the_raw_frame_with_it() -> None:
    raw = (DATA / "u1_z200_clear_1403.jpg").read_bytes()
    v = B.classify(frame("u1_z200_clear_1403.jpg"), [], 200.0, "u1")
    paths = [
        B.save_evidence("u1", raw, v, "x", 1_700_000_000.0 + i, keep=2)
        for i in range(4)
    ]
    assert [Path(p).is_file() for p in paths] == [False, False, True, True]
    assert [B.raw_frame_path(p).is_file() for p in paths] == [False, False, True, True]


def test_the_new_thresholds_are_per_printer_overridable() -> None:
    cfg = pbed.bed_config(
        device(
            watch={
                "bed": {
                    "enabled": True,
                    "thresholds": {"shift_px": 2, "occupied_blob_low_px": 900},
                }
            }
        )
    )
    th = B.thresholds_for(cfg)
    assert (th.shift_px, th.occupied_blob_low_px) == (2, 900)
    assert json.dumps(sorted(pbed.THRESHOLD_KEYS)) == json.dumps(
        sorted(f.name for f in dataclasses.fields(B.Thresholds))
    )
