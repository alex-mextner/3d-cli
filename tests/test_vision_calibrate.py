"""threed_vision.calibrate: honeycomb grid detector + pose solver, scale fallbacks, intrinsics.

CI-runnable: synthetic panels with a KNOWN camera and a recorded-centres fixture. Skipped when the private wall photo is
absent: the real IMG_4937 comparison against the reference calibration (`HSW_PHOTOS_DIR`, `HSW_REFERENCE_CALIBRATION`).
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402
import vision_calib_fixtures as fx  # noqa: E402

from threed_vision import calibrate as C  # noqa: E402
from threed_vision import io  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402

DATA = ROOT / "tests" / "data" / "vision"
PHOTOS = Path(
    os.environ.get(
        "HSW_PHOTOS_DIR", "/home/ultra/dev/garage-band/projects/HSW/docs/photos"
    )
)
REFERENCE = Path(
    os.environ.get(
        "HSW_REFERENCE_CALIBRATION",
        str(PHOTOS.parent.parent / "refit" / "photo_calibration.json"),
    )
)
SIZE = (1200, 900)
CENTRE = (11 * fx.PITCH[0], 8.25 * fx.PITCH[1])


# ---------------------------------------------------------------- synthetic panel, known camera
@pytest.mark.parametrize(
    ("f", "tilt_x", "tilt_y", "roll", "depth"),
    [
        (900.0, 25.0, 12.0, 4.0, 900.0),
        (900.0, -20.0, -15.0, -3.0, 1300.0),
        (700.0, 35.0, 0.0, 0.0, 800.0),
    ],
)
def test_detect_grid_recovers_known_camera(
    f: float, tilt_x: float, tilt_y: float, roll: float, depth: float
) -> None:
    cam = fx.camera(f, SIZE, tilt_x, tilt_y, roll, depth, CENTRE)
    res = C.detect_grid(fx.render(cam, SIZE))
    assert res["ok"], res["reason"]
    assert res["f_source"] == "estimated"
    assert abs(res["f_px"] / f - 1.0) < 0.03
    med, worst = fx.lattice_projection_error(res, cam)
    assert med < 1.0 and worst < 2.0  # recovered camera vs the known lattice, px
    assert res["reproj_median_px"] < 1.0
    assert res["n_inliers"] > 250
    # the rendered panel is upright: the recovered roll is the camera roll
    assert abs(res["roll_deg"] - roll) < 1.5


def test_detect_grid_with_known_focal_is_tight() -> None:
    cam = fx.camera(900.0, SIZE, 25.0, 12.0, 4.0, 900.0, CENTRE)
    res = C.detect_grid(fx.render(cam, SIZE), f_px=900.0)
    assert res["ok"] and res["f_source"] == "given" and res["f_px"] == 900.0
    med, worst = fx.lattice_projection_error(res, cam)
    assert med < 0.5 and worst < 1.5


def test_detect_grid_result_contract_and_homography_conventions() -> None:
    cam = fx.camera(900.0, SIZE, 25.0, 12.0, 4.0, 900.0, CENTRE)
    res = C.detect_grid(fx.render(cam, SIZE))
    assert res["ok"]
    for key in (
        "K",
        "f_px",
        "R",
        "t_mm",
        "homography_grid_mm_to_px",
        "homography_px_to_grid_mm",
        "reproj_median_px",
        "reproj_p95_px",
        "n_cells",
        "n_inliers",
        "inlier_cells_px",
        "inlier_cells",
        "quality",
        "timing_s",
    ):
        assert key in res
    R, t = np.asarray(res["R"]), np.asarray(res["t_mm"])
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-6) and np.linalg.det(R) > 0
    assert R[2, 2] < 0  # grid z points out of the wall towards the camera
    assert t[2] > 0
    Hg, Hp = (
        np.asarray(res["homography_grid_mm_to_px"]),
        np.asarray(res["homography_px_to_grid_mm"]),
    )
    assert np.allclose(Hg @ Hp / (Hg @ Hp)[2, 2], np.eye(3), atol=1e-8)
    K = np.asarray(res["K"])
    assert np.allclose(
        Hg / Hg[2, 2],
        (K @ np.column_stack([R[:, 0], R[:, 1], t]))
        / (K @ np.column_stack([R[:, 0], R[:, 1], t]))[2, 2],
        atol=1e-6,
    )
    # cells are labelled like hsw-editor grid.ts: centre = ((col + .5) * 20.4382, (row + .5 + .5 * (col & 1)) * 23.6)
    lab = np.asarray(res["inlier_cells"])
    assert lab.min(axis=0).tolist() == [0, 0]
    assert len({tuple(r) for r in lab.tolist()}) == len(lab)
    mm = np.column_stack(
        [(lab[:, 0] + 0.5) * 20.4382, (lab[:, 1] + 0.5 + 0.5 * (lab[:, 0] & 1)) * 23.6]
    )
    back = C.px_to_grid_mm(Hp, res["inlier_cells_px"])
    assert np.median(np.linalg.norm(back - mm, axis=1)) < 0.6  # mm
    assert (
        np.linalg.norm(
            C.grid_mm_to_px(Hg, mm) - np.asarray(res["inlier_cells_px"]), axis=1
        ).max()
        < 3.0
    )
    # the whole result survives JSON with NaN forbidden
    json.dumps(io.to_jsonable(res), allow_nan=False)


def test_detect_grid_handles_clutter_outside_the_panel() -> None:
    cam = fx.camera(900.0, SIZE, 25.0, 12.0, 4.0, 900.0, CENTRE)
    img = fx.render(cam, SIZE).copy()
    rng = np.random.default_rng(5)
    for _ in range(
        60
    ):  # dark blobs ("screws") and bright specks anywhere, also inside the panel
        x, y = int(rng.integers(5, SIZE[0] - 5)), int(rng.integers(5, SIZE[1] - 5))
        img[y - 2 : y + 3, x - 2 : x + 3] = int(rng.choice([20, 240]))
    res = C.detect_grid(img)
    assert res["ok"]
    assert abs(res["f_px"] / 900.0 - 1.0) < 0.04
    assert fx.lattice_projection_error(res, cam)[0] < 1.0


def test_detect_grid_without_grid_never_crashes_or_returns_nan() -> None:
    rng = np.random.default_rng(1)
    blank = np.full((300, 400, 3), 200, np.uint8)
    noise = rng.integers(0, 255, (300, 400, 3), dtype=np.uint8)
    for img in (blank, noise):
        res = C.detect_grid(img)
        assert res["ok"] is False and res["reason"]
        assert res["K"] is None and res["homography_px_to_grid_mm"] is None
        assert res["n_inliers"] == 0
        json.dumps(io.to_jsonable(res), allow_nan=False)


def test_detect_grid_rejects_bad_images() -> None:
    with pytest.raises(BadInput):
        C.detect_grid(np.zeros((0, 0, 3), np.uint8))


def test_nearly_frontal_grid_falls_back_to_prior_focal() -> None:
    cam = fx.camera(900.0, SIZE, 1.0, 1.0, 0.0, 900.0, CENTRE)
    res = C.detect_grid(fx.render(cam, SIZE))
    assert res["ok"]
    assert (
        res["f_source"] == "default" and "f_unobservable_prior_used" in res["quality"]
    )
    assert res["f_px"] == pytest.approx(C.DEFAULT_F_RATIO * math.hypot(*SIZE))
    # the scale at the wall is still right: f only scales depth, mm/px comes from the lattice itself
    true_mm_per_px = 900.0 / 900.0  # depth / f at the centre of a fronto-parallel wall
    assert res["mm_per_px_centre"] == pytest.approx(true_mm_per_px, rel=0.03)


# ---------------------------------------------------------------- solver from recorded centres
def _recorded() -> tuple[np.ndarray, tuple[int, int]]:
    doc = json.loads((DATA / "IMG_4937_cell_centres.json").read_text(encoding="utf-8"))
    return np.asarray(doc["points_px"], float), (
        int(doc["image_size"][0]),
        int(doc["image_size"][1]),
    )


def test_solver_from_recorded_cell_centres_of_img_4937() -> None:
    pts, size = _recorded()
    res = C.solve_grid_pose(pts, image_size=size)
    assert res["ok"]
    assert (
        res["n_cells"] == len(pts) and res["n_inliers"] > 400
    )  # whole panel, outliers (screws, holders) rejected
    assert res["reproj_median_px"] < 0.6 and res["reproj_p95_px"] < 1.6
    assert (
        600 < res["f_px"] < 900
    )  # weakly determined at 9 degrees of tilt: see f_rel_uncertainty / quality
    assert res["tilt_deg"] == pytest.approx(9.4, abs=1.5)
    assert "f_weak" in res["quality"] and res["f_rel_uncertainty"] > 0.05
    again = C.solve_grid_pose(pts[::-1], image_size=size)  # input order must not matter
    assert again["f_px"] == pytest.approx(res["f_px"], rel=1e-3)
    given = C.solve_grid_pose(pts, image_size=size, f_px=720.8221)
    assert given["f_source"] == "given" and given["reproj_median_px"] < 0.6


def test_solver_rejects_too_few_and_random_points() -> None:
    assert (
        C.solve_grid_pose(np.zeros((0, 2)), image_size=(100, 100))["reason"]
        == "too_few_cells"
    )
    rng = np.random.default_rng(3)
    res = C.solve_grid_pose(rng.uniform(0, 1000, (400, 2)), image_size=(1000, 1000))
    assert res["ok"] is False
    with pytest.raises(BadInput):
        C.solve_grid_pose(np.zeros((20, 2)), image_size=(0, 10))


# ---------------------------------------------------------------- real photo (private, skipped when absent)
def _reference_vs_mine(res: dict, ref: dict) -> tuple[np.ndarray, np.ndarray]:
    """Project the SAME lattice cells with my camera and with the reference camera; returns (pixel distances, ref-vs-detected).

    The lattice origin is ambiguous, so each of my cells is mapped through the reference homography into the reference
    object plane, the reference lattice phase is estimated there (circular mean of the fractional lattice coordinates) and
    the cell snaps to its nearest reference lattice node: the nearest-cell labelling."""
    pc, pr = C.DEFAULT_GRID_PITCH_MM
    f = float(ref["focal_px"])
    w, h = ref["image_size_px"]
    K = C.default_intrinsics(w, h, f)
    R, t = (
        np.asarray(ref["camera_R_object_to_cv"], float),
        np.asarray(ref["camera_t_object_to_cv_mm"], float),
    )
    Hr = K @ np.column_stack([R[:, 0], R[:, 1], t])
    P = np.asarray(res["inlier_cells_px"], float)
    q = C.apply_homography(np.linalg.inv(Hr), P)
    m = q[:, 0] / pc
    n = q[:, 1] / pr - 0.5 * m
    am = np.angle(np.exp(2j * np.pi * m).mean()) / (2 * np.pi)
    an = np.angle(np.exp(2j * np.pi * n).mean()) / (2 * np.pi)
    node = np.column_stack([np.rint(m - am) + am, np.rint(n - an) + an])
    assert len({tuple(r_) for r_ in np.rint(node - [am, an]).tolist()}) == len(
        node
    )  # one reference node per cell
    ref_px = C.apply_homography(
        Hr, np.column_stack([node[:, 0] * pc, (node[:, 1] + 0.5 * node[:, 0]) * pr])
    )
    lab = np.asarray(res["inlier_cells"])
    mm = np.column_stack(
        [(lab[:, 0] + 0.5) * pc, (lab[:, 1] + 0.5 + 0.5 * (lab[:, 0] & 1)) * pr]
    )
    mine_px = C.apply_homography(np.asarray(res["homography_grid_mm_to_px"]), mm)
    return np.linalg.norm(mine_px - ref_px, axis=1), np.linalg.norm(ref_px - P, axis=1)


@pytest.mark.skipif(
    not (PHOTOS / "IMG_4937.jpeg").is_file() or not REFERENCE.is_file(),
    reason="private HSW wall photo / reference calibration not present",
)
def test_img_4937_matches_reference_calibration() -> None:
    res = C.detect_grid(io.load_rgb(PHOTOS / "IMG_4937.jpeg"))
    ref = json.loads(REFERENCE.read_text(encoding="utf-8"))
    assert res["ok"] and res["n_inliers"] >= 283
    assert res["reproj_median_px"] < 0.6 and res["reproj_p95_px"] < 1.5
    dist, ref_vs_detected = _reference_vs_mine(res, ref)
    print(
        f"IMG_4937: mine vs reference projection median {np.median(dist):.3f} px, max {dist.max():.3f} px; "
        f"reference vs detected median {np.median(ref_vs_detected):.3f} px; f {res['f_px']:.1f} vs {ref['focal_px']:.1f}"
    )
    assert np.median(dist) < 1.0 and dist.max() < 1.0
    # f is weakly determined at 9 degrees of tilt (the detector scale alone moves it by +-10 %, see f_rel_uncertainty), so the
    # bound is 8 % rather than the 5 % hoped for; measured 4.96 %
    assert abs(res["f_px"] / ref["focal_px"] - 1.0) < 0.08
    assert abs(res["f_px"] / ref["focal_px"] - 1.0) < res["f_rel_uncertainty"]


@pytest.mark.skipif(
    not (PHOTOS / "IMG_4935.jpeg").is_file(), reason="private photo not present"
)
def test_img_4935_without_panel_is_not_a_grid() -> None:
    res = C.detect_grid(io.load_rgb(PHOTOS / "IMG_4935.jpeg"))
    assert res["ok"] is False


@pytest.mark.skipif(
    not (PHOTOS / "IMG_4936.jpeg").is_file(), reason="private photo not present"
)
def test_img_4936_partial_panel_in_the_corner() -> None:
    img = io.load_rgb(PHOTOS / "IMG_4936.jpeg")
    res = C.detect_grid(img)
    assert res["ok"] and res["n_inliers"] >= 40
    x, y, w, h = res["roi_xywh"]
    assert (
        x > 0.75 * img.shape[1] and y < 0.15 * img.shape[0]
    )  # the panel sticks into the top-right corner
    assert (
        res["f_source"] == "default" and "f_unobservable_prior_used" in res["quality"]
    )  # tiny footprint: f not observable
    assert res["reproj_median_px"] < 0.8


# ---------------------------------------------------------------- intrinsics / scale fallbacks / helpers
def test_default_intrinsics() -> None:
    K = C.default_intrinsics(1152, 1536)
    f = 0.375 * math.hypot(1152, 1536)
    assert K.tolist() == [[f, 0, 576], [0, f, 768], [0, 0, 1]]
    assert C.default_intrinsics(100, 80, f_px=50.0)[0, 0] == 50.0
    assert C.default_intrinsics(100, 80, f_ratio=1.0)[0, 0] == pytest.approx(
        math.hypot(100, 80)
    )
    for bad in ((0, 10), (10, -1)):
        with pytest.raises(BadInput):
            C.default_intrinsics(*bad)
    with pytest.raises(BadInput):
        C.default_intrinsics(10, 10, f_px=-3.0)


def test_scale_from_points() -> None:
    res = C.scale_from_points((100, 100), (400, 500), 250.0, image_size=(2000, 1500))
    assert res["length_px"] == pytest.approx(500.0)
    assert res["mm_per_px"] == pytest.approx(0.5)
    assert res["f_px"] == pytest.approx(0.375 * 2500)
    assert res["plane_depth_mm"] == pytest.approx(0.5 * 0.375 * 2500)
    assert res["source"] == "points" and res["f_source"] == "default"
    assert (
        "fronto_parallel_assumed" in res["quality"]
        and "f_default_ratio" in res["quality"]
    )
    assert np.asarray(res["K"]).shape == (3, 3)
    given = C.scale_from_points(
        (0, 0), (10, 0), 5.0, image_size=(1000, 1000), f_px=80.0
    )
    assert (
        given["f_px"] == 80.0
        and given["f_source"] == "given"
        and "short_reference" in given["quality"]
    )
    for p1, p2, d in (
        ((1, 1), (1, 1), 10.0),
        ((0, 0), (5, 5), 0.0),
        ((0, 0), (5, float("nan")), 3.0),
    ):
        with pytest.raises(BadInput):
            C.scale_from_points(p1, p2, d, image_size=(100, 100))


def test_scale_from_reference_rotated_bar() -> None:
    import cv2

    mask = np.zeros((600, 800), np.uint8)
    box = cv2.boxPoints(((400.0, 300.0), (400.0, 30.0), 25.0)).astype(
        np.int32
    )  # a 400 x 30 px bar at 25 degrees
    cv2.fillConvexPoly(mask, box, 1)
    res = C.scale_from_reference(
        mask.astype(bool), 200.0, image_size=(800, 600), f_px=600.0
    )
    assert res["length_px"] == pytest.approx(400.0, abs=3.0)
    assert res["mm_per_px"] == pytest.approx(0.5, rel=0.01)
    assert res["plane_depth_mm"] == pytest.approx(300.0, rel=0.01)
    assert res["source"] == "ref" and "mask_touches_border" not in res["quality"]
    border = np.zeros((600, 800), bool)
    border[100:110, 0:300] = True
    assert (
        "mask_touches_border"
        in C.scale_from_reference(border, 100.0, image_size=(800, 600))["quality"]
    )
    with pytest.raises(BadInput):
        C.scale_from_reference(np.zeros((600, 800), bool), 10.0, image_size=(800, 600))
    with pytest.raises(BadInput):
        C.scale_from_reference(
            border, 10.0, image_size=(640, 480)
        )  # mask / image size mismatch


def test_scale_from_grid_matches_the_homography_and_accepts_the_hsw_json_schema() -> (
    None
):
    cam = fx.camera(900.0, SIZE, 25.0, 12.0, 4.0, 900.0, CENTRE)
    res = C.detect_grid(fx.render(cam, SIZE))
    from_result = C.scale_from_grid(json.loads(json.dumps(io.to_jsonable(res))))
    assert from_result["source"] == "grid"
    Ht = cam["K"] @ np.column_stack([cam["R"][:, 0], cam["R"][:, 1], cam["t"]])
    truth = C.local_mm_per_px(np.linalg.inv(Ht), from_result["at_px"])
    assert from_result["mm_per_px"] == pytest.approx(truth, rel=0.01)
    assert from_result["plane_depth_mm"] == pytest.approx(
        (
            cam["R"][:, :2] @ C.px_to_grid_mm(np.linalg.inv(Ht), from_result["at_px"])
            + cam["t"]
        )[2],
        rel=0.05,  # depth scales with the (2 %-uncertain) estimated focal length
    )
    hsw = {
        "focal_px": 900.0,
        "image_size_px": list(SIZE),
        "camera_R_object_to_cv": cam["R"].tolist(),
        "camera_t_object_to_cv_mm": cam["t"].tolist(),
    }
    from_hsw = C.scale_from_grid(hsw, at_px=from_result["at_px"])
    assert (
        from_hsw["mm_per_px"] == pytest.approx(truth, rel=1e-9)
        and from_hsw["f_source"] == "calibration"
    )
    with pytest.raises(BadInput):
        C.scale_from_grid({"nonsense": 1})
    with pytest.raises(BadInput):
        C.scale_from_grid(C.detect_grid(np.full((200, 200, 3), 128, np.uint8)))


def test_homography_helpers() -> None:
    H = np.array([[2.0, 0, 10], [0, 2.0, -5], [0, 0, 1]])
    assert C.apply_homography(H, [1.0, 1.0]).tolist() == [12.0, -3.0]
    assert C.apply_homography(H, [[0, 0], [1, 2]]).tolist() == [
        [10.0, -5.0],
        [12.0, -1.0],
    ]
    assert C.local_mm_per_px(H, (3.0, 4.0)) == pytest.approx(2.0)
    persp = np.array([[1.0, 0, 0], [0, 1.0, 0], [0.001, 0, 1]])
    assert C.local_mm_per_px(persp, (0.0, 0.0)) == pytest.approx(1.0)
    assert C.local_mm_per_px(persp, (500.0, 0.0)) == pytest.approx(
        math.sqrt(1 / 1.5**3), rel=1e-6
    )  # Jacobian det = 1/w^3 (d(x/w)/dx = 1/w^2, d(y/w)/dy = 1/w), w = 1.5
