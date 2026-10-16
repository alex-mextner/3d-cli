"""Unit tests for lib/vision_geometry.py: projection, calibration fit, masks, yaml round-trip."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
import yaml

import vision_geometry as vg

# A low front-corner wide-angle camera looking across a 270 mm bed, like the Snapmaker U1.
TRUE = vg.CameraModel(
    width=1920,
    height=1080,
    fx=820.0,
    fy=815.0,
    cx=950.0,
    cy=560.0,
    dist=(-0.28, 0.09, 0.001, -0.002, -0.012),
    rvec=(0.0, 0.0, 0.0),
    tvec=(0.0, 0.0, 0.0),
)


def _look_at(eye: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    fwd = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(fwd, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    rot = np.stack([right, down, fwd])  # world -> camera rows
    return vg._rodrigues_inv(rot), -rot @ eye


RVEC, TVEC = _look_at(np.array([-40.0, -60.0, 60.0]), np.array([150.0, 150.0, -20.0]))
CAM = dataclasses.replace(TRUE, rvec=tuple(RVEC), tvec=tuple(TVEC))
FOOTPRINTS = [
    [(20.0, 20.0), (90.0, 20.0), (90.0, 140.0), (20.0, 140.0)],
    [(150.0, 60.0), (220.0, 60.0), (185.0, 120.0)],
]


def _corr(cam: vg.CameraModel, n: int = 40, seed: int = 0) -> list[vg.Correspondence]:
    rng = np.random.default_rng(seed)
    out: list[vg.Correspondence] = []
    while len(out) < n:
        x, y = rng.uniform(0, 270, 2)
        z = float(rng.choice([2.0, 30.0, 80.0]))
        h = float(rng.uniform(0, z))
        u, v = cam.project(np.array([[x, y, h]]), z)[0]
        if 0 <= u < cam.width and 0 <= v < cam.height:
            out.append(vg.Correspondence(float(u), float(v), float(x), float(y), h, z))
    return out


def test_rodrigues_round_trip() -> None:
    for r in ([0.1, -0.4, 0.2], [2.0, 0.5, -1.0], [0.0, 0.0, 0.0]):
        rot = vg._rodrigues(np.array(r))
        assert np.allclose(rot @ rot.T, np.eye(3), atol=1e-12)
        assert np.allclose(vg._rodrigues_inv(rot), r, atol=1e-9)


def test_bed_moves_z_shifts_points_down() -> None:
    p = CAM.project(np.array([[100.0, 100.0, 10.0]]), 10.0)
    q = CAM.project(np.array([[100.0, 100.0, 0.0]]), 0.0)
    assert np.allclose(p, q)  # the newest layer always sits at the nozzle plane
    fixed = dataclasses.replace(CAM, bed_moves_z=False)
    assert np.allclose(fixed.project(np.array([[100.0, 100.0, 0.0]]), 50.0), q)


def test_project_matches_opencv_when_available() -> None:
    cv2 = pytest.importorskip("cv2")
    pts = np.array([[10.0, 20.0, 0.0], [200.0, 250.0, 5.0], [135.0, 135.0, 30.0]])
    k = np.array([[CAM.fx, 0, CAM.cx], [0, CAM.fy, CAM.cy], [0, 0, 1]])
    ref, _ = cv2.projectPoints(
        pts, np.array(CAM.rvec), np.array(CAM.tvec), k, np.array(CAM.dist)
    )
    assert np.allclose(CAM.project(pts, 0.0), ref.reshape(-1, 2), atol=1e-6)


def test_points_behind_camera_are_nan() -> None:
    eye = CAM.camera_center()
    behind = eye + (eye - np.array([150.0, 150.0, -20.0]))
    assert np.isnan(CAM.project(behind[None, :], 0.0)).all()


@pytest.mark.parametrize("model", ["pinhole", "fisheye"])
def test_fit_recovers_synthetic_camera(model: str) -> None:
    fisheye = dataclasses.replace(
        CAM, model="fisheye", dist=(0.05, -0.02, 0.01, -0.004)
    )
    truth = CAM if model == "pinhole" else fisheye
    corr = _corr(truth)
    fit, rms = vg.fit_camera(corr, 1920, 1080, model=model)
    assert rms < 1e-3
    assert fit.model == model
    assert math.isclose(fit.fx, truth.fx, rel_tol=1e-3)
    assert np.allclose(fit.camera_center(), truth.camera_center(), atol=0.1)
    probe = np.array([[0.0, 0.0, 0.0], [270.0, 270.0, 0.0], [135.0, 50.0, 40.0]])
    assert np.allclose(fit.project(probe, 60.0), truth.project(probe, 60.0), atol=0.05)


def test_fit_with_noise_reports_rms_and_residuals() -> None:
    rng = np.random.default_rng(3)
    corr = [
        vg.Correspondence(
            c.u + rng.normal(0, 1.0), c.v + rng.normal(0, 1.0), c.x, c.y, c.h, c.z
        )
        for c in _corr(CAM, n=60, seed=1)
    ]
    fit, rms = vg.fit_camera(corr, 1920, 1080)
    assert 0.5 < rms < 1.5
    assert vg.residuals(fit, corr).shape == (60,)
    refit, rms2 = vg.fit_camera(corr, 1920, 1080, init=fit)
    assert rms2 <= rms + 1e-6


def test_fit_needs_enough_points() -> None:
    with pytest.raises(ValueError, match="at least 8"):
        vg.fit_camera(_corr(CAM, n=5), 1920, 1080)


def test_yaml_round_trip() -> None:
    d = CAM.to_yaml()
    again = vg.CameraModel.from_yaml(yaml.safe_load(yaml.safe_dump(d)))
    assert again == CAM
    with pytest.raises(ValueError, match="distortion"):
        vg.CameraModel.from_yaml({**d, "model": "fisheye"})


def test_fill_matches_area() -> None:
    mask = np.zeros((100, 100), dtype=bool)
    vg._fill(mask, np.array([[10.0, 10.0], [60.0, 10.0], [60.0, 40.0], [10.0, 40.0]]))
    assert mask.sum() == 50 * 30
    tri = np.zeros((100, 100), dtype=bool)
    vg._fill(
        tri, np.array([[-50.0, -50.0], [150.0, -50.0], [-50.0, 150.0]])
    )  # clipped to the image
    assert abs(tri.sum() - 100 * 100 / 2) < 150


def test_masks_shapes_and_coverage() -> None:
    shape = (540, 960)
    bed = vg.bed_mask(CAM, 10.0, shape)
    assert (
        bed.shape == shape and bed.dtype == np.uint8 and set(np.unique(bed)) <= {0, 255}
    )
    assert 0.05 < (bed > 0).mean() < 0.95
    sil = vg.silhouette_mask(CAM, 10.0, shape, FOOTPRINTS)
    vol = vg.volume_mask(CAM, 10.0, shape, FOOTPRINTS)
    band = vg.top_band_mask(CAM, 10.0, shape, FOOTPRINTS, band_mm=3)
    assert (sil > 0).sum() > 0 and (band > 0).sum() > 0
    assert not ((sil > 0) & (vol == 0)).any()  # the part is inside the allowed volume
    tight = vg.silhouette_mask(CAM, 10.0, shape, FOOTPRINTS, pad_mm=0)
    outside = (
        (band > 0) & (tight == 0)
    ).sum()  # only edge pixels may differ (chord sampling)
    assert outside <= 0.01 * (band > 0).sum()
    assert (band > 0).sum() < (sil > 0).sum()
    # a taller print covers more pixels; the dropping bed moves down in the image
    tall = vg.silhouette_mask(CAM, 40.0, shape, FOOTPRINTS)
    assert (tall > 0).sum() > (sil > 0).sum()
    low = vg.bed_mask(CAM, 100.0, shape)
    assert np.nonzero(low)[0].mean() > np.nonzero(bed)[0].mean()
    # the mask scales with the frame: half resolution ~ quarter pixel count
    full = vg.silhouette_mask(CAM, 10.0, (1080, 1920), FOOTPRINTS)
    assert abs((full > 0).sum() / 4 - (sil > 0).sum()) / (sil > 0).sum() < 0.03


def test_finished_objects_leave_the_top_band() -> None:
    shape = (540, 960)
    both = vg.top_band_mask(CAM, 30.0, shape, FOOTPRINTS, heights=[60.0, 60.0])
    one = vg.top_band_mask(CAM, 30.0, shape, FOOTPRINTS, heights=[60.0, 12.0])
    assert 0 < (one > 0).sum() < (both > 0).sum()
    short = vg.silhouette_mask(CAM, 30.0, shape, FOOTPRINTS, heights=[60.0, 12.0])
    assert (short > 0).sum() < (
        vg.silhouette_mask(CAM, 30.0, shape, FOOTPRINTS) > 0
    ).sum()
    with pytest.raises(ValueError, match="heights"):
        vg.silhouette_mask(CAM, 30.0, shape, FOOTPRINTS, heights=[1.0])


def test_overlay_draws_when_opencv_available() -> None:
    pytest.importorskip("cv2")
    img = np.zeros((540, 960, 3), dtype=np.uint8)
    out = vg.overlay(img, CAM, 10.0, FOOTPRINTS)
    assert out.shape == img.shape and out.any() and not img.any()


def test_load_calibration_prefers_own_file_then_bundled_model(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = tmp_path / "3d-cli"
    (cfg / "vision").mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: 127.0.0.1, model: Snapmaker U1}\n"
        "k1: {adapter: creality-stock, host: 127.0.0.2, model: Creality K1 SE}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    bundled = vg.load_calibration("u1")
    assert bundled is not None and bundled.width == 1920 and bundled.bed_moves_z
    assert vg.load_calibration("k1") is None
    assert vg.load_calibration("nope") is None
    (cfg / "vision" / "k1-camera.yaml").write_text(
        yaml.safe_dump({"camera": CAM.to_yaml()}), encoding="utf-8"
    )
    assert vg.load_calibration("k1") == CAM


def test_bundled_u1_calibration_projects_bed_into_frame() -> None:
    cam = vg.bundled_calibration("Snapmaker U1")
    assert cam is not None
    corners = np.array([[x, y, 0.0] for x in (0, 270) for y in (0, 270)])
    uv = cam.project(corners, 0.5)
    assert np.isfinite(uv).all()
