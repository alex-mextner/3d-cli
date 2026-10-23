"""threed_vision.register: SIFT + RANSAC plane homography, relative pose, baseline_ok, mask warping, repeatability IoU.

CI-runnable on synthetic textured images related by a KNOWN plane-induced homography; the real carpet pair is a
skip-if-absent check of the carpet pair measured in the spike (1853 matches, same viewpoint, no parallax).
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402
import vision_calib_fixtures as fx  # noqa: E402

from threed_vision import calibrate as C  # noqa: E402
from threed_vision import io  # noqa: E402
from threed_vision import register as G  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402

PHOTOS = Path(
    os.environ.get(
        "HSW_PHOTOS_DIR", "/home/ultra/dev/garage-band/projects/HSW/docs/photos"
    )
)
SIZE = (800, 600)


textured = fx.textured
_view_pair = fx.view_pair


def test_register_recovers_known_plane_homography_and_pose() -> None:
    a, b, H_true, R_true = _view_pair()
    res = G.register(a, b)
    assert res["ok"] and res["inliers"] > 100
    gx, gy = np.meshgrid(np.linspace(80, 720, 9), np.linspace(60, 540, 7))
    grid = np.column_stack([gx.ravel(), gy.ravel()])
    err = np.linalg.norm(
        C.apply_homography(res["H_a_to_b"], grid) - C.apply_homography(H_true, grid),
        axis=1,
    )
    assert err.max() < 1.0, err.max()
    assert res["median_reproj_px"] < 0.5
    R = np.asarray(res["rel_R"])
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-6)
    d_rot = math.degrees(
        math.acos(float(np.clip((np.trace(R.T @ R_true) - 1) / 2, -1, 1)))
    )
    assert d_rot < 1.5  # decomposition picked the physical solution among OpenCV's four
    assert res["rel_rot_deg"] == pytest.approx(
        math.degrees(math.acos((np.trace(R_true) - 1) / 2)), abs=1.5
    )
    assert res["translation_ratio"] == pytest.approx(
        math.sqrt(0.15**2 + 0.02**2 + 0.05**2), rel=0.35
    )
    assert res["baseline_ok"] is True
    assert res["t_mm"] is None
    scaled = G.register(a, b, plane_depth_mm=1000.0)
    assert np.allclose(scaled["t_mm"], np.asarray(scaled["t_over_depth"]) * 1000.0)
    json.dumps(
        io.to_jsonable({k: v for k, v in res.items() if k != "warped_masks"}),
        allow_nan=False,
    )


def test_same_viewpoint_pair_has_no_baseline() -> None:
    """Pure zoom/shift of one viewpoint (the carpet pair situation): rotation ~0, no decomposable translation."""
    a = textured()
    s = 0.77  # zoom about the principal point of each photo, the carpet-pair situation
    zoom = np.array([[s, 0, 310.0 - s * 400.0], [0, s, 230.0 - s * 300.0], [0, 0, 1.0]])
    b = cv2.warpPerspective(a, zoom, (620, 460), flags=cv2.INTER_AREA)
    res = G.register(a, b)
    assert res["ok"]
    assert res["rel_rot_deg"] < 3.0 and res["translation_ratio"] < 0.05
    assert res["baseline_ok"] is False and "same_viewpoint" in res["quality"]
    assert np.allclose(
        np.asarray(res["rel_R"]) @ np.asarray(res["rel_R"]).T, np.eye(3), atol=1e-6
    )


def test_masks_are_warped_and_scored_per_instance() -> None:
    a, b, H_true, _ = _view_pair()
    m1 = np.zeros(a.shape[:2], bool)
    m1[200:330, 150:300] = True
    disc = np.zeros(a.shape[:2], np.uint8)
    cv2.circle(disc, (520, 380), 70, 1, -1)
    m2 = disc.astype(bool)

    def warp(m: np.ndarray) -> np.ndarray:
        return (
            cv2.warpPerspective(
                m.astype(np.uint8), H_true, SIZE, flags=cv2.INTER_NEAREST
            )
            > 0
        )

    b1, b2 = warp(m1), warp(m2)
    unstable_b2 = (
        b2.copy()
    )  # B segmented the second object as its left half only: an unstable segmentation
    unstable_b2[:, int(np.nonzero(b2.any(axis=0))[0].mean()) :] = False
    res = G.register(a, b, [m1, m2], masks_b=[b1, unstable_b2, np.zeros_like(b1)])
    assert len(res["warped_masks"]) == 2 and res["warped_masks"][0].shape == b1.shape
    first, second = res["repeatability"]
    assert (
        first["index"] == 0
        and first["best_b_index"] == 0
        and first["iou"] > 0.95
        and first["unstable"] is False
    )
    assert first["inside_b_frac"] > 0.99
    assert second["iou"] < 0.75 and second["unstable"] is True
    nob = G.register(a, b, np.stack([m1, m2]))  # 3-D array input, no B masks: warp only
    assert [r["iou"] for r in nob["repeatability"]] == [None, None]
    assert (nob["warped_masks"][0] & b1).sum() / b1.sum() > 0.95


def test_register_reports_failures_instead_of_raising() -> None:
    blank = np.full((300, 400, 3), 128, np.uint8)
    res = G.register(blank, blank)
    assert (
        res["ok"] is False
        and res["reason"] == "too_few_features"
        and res["baseline_ok"] is False
    )
    assert res["rel_rot_deg"] is None
    unrelated = G.register(textured(seed=1), textured(seed=2))
    assert unrelated["ok"] is False and unrelated["reason"] in {
        "too_few_matches",
        "no_homography",
        "too_few_inliers",
    }
    json.dumps(io.to_jsonable(res), allow_nan=False)


def test_register_validates_arguments() -> None:
    a = textured()
    with pytest.raises(BadInput):
        G.register(a, a, [np.zeros((10, 10), bool)])  # mask size differs from its image
    with pytest.raises(BadInput):
        G.register(np.zeros((4, 4, 3), np.uint8), a)


@pytest.mark.skipif(
    not (PHOTOS / "tools-on-carpet-2026-10-21.heic").is_file()
    or not (PHOTOS / "IMG_4934.jpeg").is_file(),
    reason="private carpet photos not present",
)
def test_real_carpet_pair_is_the_same_viewpoint() -> None:
    pytest.importorskip("pillow_heif")  # HEIC decoding
    res = G.register(
        io.load_rgb(PHOTOS / "tools-on-carpet-2026-10-21.heic"),
        io.load_rgb(PHOTOS / "IMG_4934.jpeg"),
    )
    print(
        f"carpet pair: {res['matches']} matches, {res['inliers']} inliers, median {res['median_reproj_px']:.3f} px, "
        f"rot {res['rel_rot_deg']:.2f} deg, translation ratio {res['translation_ratio']:.3f}, {res['timing_s']['total_s']:.2f} s"
    )
    assert res["ok"] and res["inliers"] > 1500 and res["median_reproj_px"] < 0.2
    # the spike reported 0.94 deg: that was the trace of OpenCV's non-orthonormal R (diag 1.0000, 1.0001, 0.9996); the
    # pair differs by a pure zoom about the principal point, i.e. no rotation at all
    assert res["rel_rot_deg"] < 0.5
    assert res["baseline_ok"] is False


def test_bbox_cropped_ious_equal_the_full_frame_definition() -> None:
    rng = np.random.default_rng(4)
    masks = []
    for _ in range(30):
        m = np.zeros((300, 400), bool)
        y, x = int(rng.integers(0, 250)), int(rng.integers(0, 350))
        m[y : y + int(rng.integers(5, 60)), x : x + int(rng.integers(5, 60))] = True
        masks.append(m)
    boxes = [G._bbox(m) for m in masks]
    for w in masks[:5]:
        got = G._ious(w, G._bbox(w), masks, boxes)
        want = [float((w & b).sum() / max(int((w | b).sum()), 1)) for b in masks]
        assert got == pytest.approx(want)
    assert G._bbox(np.zeros((4, 4), bool))[4] == 0


def test_same_size_zoom_is_not_mistaken_for_a_baseline() -> None:
    """Both photos 800x600 (a phone's 1x/2x lens): K_a == K_b, a pure zoom decomposes as t || normal, no parallax."""
    a = fx.textured()
    s = 1.6
    zoom = np.array([[s, 0, 400.0 - s * 400.0], [0, s, 300.0 - s * 300.0], [0, 0, 1.0]])
    b = cv2.warpPerspective(a, zoom, (800, 600), flags=cv2.INTER_CUBIC)
    res = G.register(a, b)
    assert res["ok"] and res["rel_rot_deg"] < 3.0
    assert res["baseline_ok"] is False and "same_viewpoint" in res["quality"]
    assert res["lateral_translation_ratio"] < 0.05


def test_lateral_translation_still_counts_as_a_baseline() -> None:
    a, b, _, _ = fx.view_pair(yaw_deg=0.5, pitch_deg=0.0, t=(0.25, 0.0, 0.0))
    res = G.register(a, b)
    assert res["ok"] and res["baseline_ok"] is True and res["lateral_translation_ratio"] > 0.1
