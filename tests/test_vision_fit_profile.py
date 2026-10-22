"""Closed-form `threed_vision.fit_profile`: invariants, recorded real silhouettes, hostile inputs (numpy + OpenCV only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vision_fit_synth import recorded_mask  # noqa: E402

from threed_vision.errors import BadInput  # noqa: E402
from threed_vision.fit_profile import MIN_PIXELS, fit_profile  # noqa: E402
from threed_vision.io import to_jsonable  # noqa: E402

DATA = Path(__file__).resolve().parent / "data" / "vision" / "carpet_masks.npz"
SCALE = 1.263  # mm/px of the carpet photo (caliper 239.4 mm)


def rotated_rect(
    w: float,
    h: float,
    angle_deg: float,
    canvas: tuple[int, int] = (400, 400),
    centre: tuple[float, float] | None = None,
) -> np.ndarray:
    cx, cy = centre or (canvas[0] / 2, canvas[1] / 2)
    box = cv2.boxPoints(((cx, cy), (w, h), angle_deg))
    img = np.zeros((canvas[1], canvas[0]), np.uint8)
    cv2.fillConvexPoly(img, np.round(box).astype(np.int32), 1)
    return img > 0


def capsule(
    length: float, radius: float, angle_deg: float, canvas: tuple[int, int] = (500, 500)
) -> np.ndarray:
    a = np.radians(angle_deg)
    d = np.array([np.cos(a), np.sin(a)]) * (length / 2 - radius)
    c = np.array(canvas) / 2
    img = np.zeros((canvas[1], canvas[0]), np.uint8)
    cv2.line(
        img,
        tuple(np.round(c - d).astype(int)),
        tuple(np.round(c + d).astype(int)),
        1,
        int(round(2 * radius)),
    )
    cv2.circle(img, tuple(np.round(c - d).astype(int)), int(radius), 1, -1)
    cv2.circle(img, tuple(np.round(c + d).astype(int)), int(radius), 1, -1)
    return img > 0


# ------------------------------------------------------------------ recorded carpet silhouettes
@pytest.mark.parametrize(
    ("name", "kind", "iou_range", "length_mm", "width_mm"),
    [
        ("screwdriver", "lathe_profile", (0.95, 1.0), 255.3, 38.4),
        ("caliper", "lathe_profile", (0.95, 1.0), 239.4, 79.0),
        ("multimeter", "box", (0.97, 1.0), 140.3, 82.2),
        ("tape_measure", "lathe_profile", (0.97, 1.0), 110.1, 105.8),
        ("drill", "loft_flat", (0.80, 0.93), 127.7, 133.4),
        ("bit_set", "loft_flat", (0.95, 1.0), 202.9, 102.7),
        # an OPEN pliers is not a straight-axis profile: the closed form must say so with a low IoU, not pretend
        ("pliers_open", "lathe_profile", (0.70, 0.85), 229.3, 72.5),
    ],
)
def test_recorded_carpet_masks(
    name: str,
    kind: str,
    iou_range: tuple[float, float],
    length_mm: float,
    width_mm: float,
) -> None:
    fp = fit_profile(recorded_mask(name), SCALE)
    assert fp["kind"] == kind
    assert iou_range[0] <= fp["iou_recon"] <= iou_range[1]
    assert fp["length_mm"] == pytest.approx(length_mm, rel=0.01)
    assert fp["max_width_mm"] == pytest.approx(width_mm, rel=0.02)


def test_recorded_fixtures_stay_small() -> None:
    assert DATA.stat().st_size <= 30_000


# ------------------------------------------------------------------ invariants
@pytest.mark.parametrize("angle", [0, 33, 90, 145, 179])
def test_rotation_invariance_of_profile(angle: int) -> None:
    ref = fit_profile(capsule(240, 18, 0))
    fp = fit_profile(capsule(240, 18, angle))
    assert fp["length_mm"] == pytest.approx(ref["length_mm"], rel=0.015)
    assert fp["max_width_mm"] == pytest.approx(ref["max_width_mm"], rel=0.06)
    assert fp["kind"] == ref["kind"]  # the (crude) classifier gives the same answer at every angle
    assert fp["iou_recon"] > 0.95


def test_translation_moves_centre_not_profile() -> None:
    a = rotated_rect(200, 40, 20, centre=(150, 100))
    b = rotated_rect(200, 40, 20, canvas=(600, 500), centre=(400, 300))
    fa, fb = fit_profile(a), fit_profile(b)
    assert fb["length_mm"] == pytest.approx(fa["length_mm"], rel=1e-3)
    assert fb["max_width_mm"] == pytest.approx(fa["max_width_mm"], rel=1e-3)
    assert np.allclose(
        np.array(fb["center_px"]) - np.array(fa["center_px"]), [250, 200], atol=1.0
    )  # coordinates are in the INPUT frame


def test_scale_invariance_lengths_follow_mm_per_px() -> None:
    m = capsule(200, 15, 40)
    f1, f2 = fit_profile(m, 1.0), fit_profile(m, 2.5)
    assert f2["length_mm"] == pytest.approx(2.5 * f1["length_mm"], rel=1e-9)
    assert f2["max_width_mm"] == pytest.approx(2.5 * f1["max_width_mm"], rel=1e-9)
    assert f2["iou_recon"] == f1["iou_recon"]
    big = fit_profile(capsule(400, 30, 40), 1.0)  # the same shape drawn twice as large
    assert big["length_mm"] == pytest.approx(2 * f1["length_mm"], rel=0.02)


@pytest.mark.parametrize("angle", [0, 25, 70])
def test_filled_rectangle_is_a_box(angle: int) -> None:
    fp = fit_profile(rotated_rect(120, 70, angle))
    assert fp["kind"] == "box"
    assert fp["length_mm"] == pytest.approx(120, rel=0.04)
    assert fp["max_width_mm"] == pytest.approx(70, rel=0.06)


def test_ring_is_a_ring_lathe_with_radii() -> None:
    img = np.zeros((300, 300), np.uint8)
    cv2.circle(img, (150, 150), 80, 1, -1)
    cv2.circle(img, (150, 150), 45, 0, -1)
    fp = fit_profile(img > 0, 0.5)
    assert fp["kind"] == "ring_lathe"
    assert fp["ring_mm"]["r_outer"] == pytest.approx(40.0, rel=0.03)
    assert fp["ring_mm"]["r_inner"] == pytest.approx(22.5, rel=0.03)


def test_axis_angle_override_is_honoured() -> None:
    fp = fit_profile(rotated_rect(100, 98, 0), axis_angle_deg=90.0)
    assert abs(abs(fp["axis_deg"]) - 90.0) < 1e-6


def test_stations_schema() -> None:
    fp = fit_profile(capsule(240, 18, 10), 1.0, K=12)
    assert len(fp["stations"]) == 12
    assert fp["stations"][0]["t"] == 0.0 and fp["stations"][-1]["t"] == 1.0
    assert all(s["hi_mm"] >= s["lo_mm"] for s in fp["stations"])


# ------------------------------------------------------------------ hostile inputs: BadInput or finite, never NaN
def test_empty_and_tiny_masks_are_bad_input() -> None:
    with pytest.raises(BadInput):
        fit_profile(np.zeros((50, 50), bool))
    one = np.zeros((50, 50), bool)
    one[10, 10] = True
    with pytest.raises(BadInput, match="foreground pixel"):
        fit_profile(one)
    few = np.zeros((50, 50), bool)
    few.ravel()[: MIN_PIXELS - 1] = True
    with pytest.raises(BadInput):
        fit_profile(few)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_bad_scale_is_bad_input(bad: float) -> None:
    with pytest.raises(BadInput, match="mm_per_px"):
        fit_profile(capsule(100, 10, 0), bad)


def test_bad_shapes_and_params_are_bad_input() -> None:
    with pytest.raises(BadInput):
        fit_profile(np.ones((4, 4, 3), bool))
    with pytest.raises(BadInput):
        fit_profile(capsule(100, 10, 0), K=1)
    with pytest.raises(BadInput):
        fit_profile(np.full((20, 20), np.nan))


@pytest.mark.parametrize(
    "mask",
    [
        np.ones((60, 60), bool),  # whole frame
        np.pad(np.ones((1, 40), bool), ((20, 20), (5, 5))),  # one pixel thick line
        np.pad(np.ones((40, 1), bool), ((5, 5), (20, 20))),
        np.eye(30, dtype=bool),  # diagonal line
        np.pad(np.ones((3, 3), bool), 10),  # 9 pixels
    ],
    ids=["full", "hline", "vline", "diag", "3x3"],
)
def test_degenerate_masks_never_produce_nan(mask: np.ndarray) -> None:
    fp = fit_profile(mask, 1.263)
    json.dumps(to_jsonable(fp), allow_nan=False)  # raises on NaN/inf
    assert (
        fp["length_mm"] >= 0
        and fp["max_width_mm"] >= 0
        and 0.0 <= fp["iou_recon"] <= 1.0
    )


def test_huge_scale_stays_finite() -> None:
    fp = fit_profile(capsule(100, 10, 0), 1e30)
    json.dumps(to_jsonable(fp), allow_nan=False)


def test_soft_float_mask_is_thresholded_and_uint8_255_accepted() -> None:
    m = capsule(160, 14, 30)
    assert (
        fit_profile(m.astype(np.float32) * 0.9)["length_mm"]
        == fit_profile(m)["length_mm"]
    )
    assert (
        fit_profile(m.astype(np.uint8) * 255)["length_mm"]
        == fit_profile(m)["length_mm"]
    )
