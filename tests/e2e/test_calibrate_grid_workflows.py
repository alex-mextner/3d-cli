"""`3d calibrate-grid` through bin/3d: a wall photo of the honeycomb panel becomes a camera + wall-plane homography."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")
pytest.importorskip("PIL")

import numpy as np  # noqa: E402
import vision_calib_fixtures as fx  # noqa: E402

from .workflow_helper import run_cli  # noqa: E402

LIGHT_ENV = {"THREED_VISION_PYTHON": sys.executable}


def test_calibrate_grid_json_recovers_the_camera_of_a_synthetic_wall_photo(
    tmp_path: Path,
) -> None:
    """A script calibrates a wall photo and reads the homography it needs to store with its photo."""
    photo = tmp_path / "wall.png"
    cam = fx.write_wall_photo(str(photo))
    r = run_cli(tmp_path, "calibrate-grid", str(photo), "--json", env_extra=LIGHT_ENV)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["ok"] is True and doc["reason"] is None
    assert doc["image"]["w"] == 1200 and doc["image"]["h"] == 900
    assert abs(doc["f_px"] / 900.0 - 1.0) < 0.03
    assert doc["reproj_median_px"] < 1.0 and doc["n_inliers"] > 250
    # photo px -> wall mm: the true centre pixel of the panel maps to a point on the 20.4382 x 23.6 mm lattice
    H = np.asarray(doc["homography_px_to_grid_mm"])
    assert H.shape == (3, 3)
    truth = cam["K"] @ np.column_stack([cam["R"][:, 0], cam["R"][:, 1], cam["t"]])
    px = np.array([[600.0, 450.0], [500.0, 400.0]])
    mm = np.column_stack([px, np.ones(2)]) @ H.T
    mm = mm[:, :2] / mm[:, 2:3]
    # lengths in the wall plane agree with the true camera's (the labelling origin is arbitrary, distances are not)
    ref = np.column_stack([px, np.ones(2)]) @ np.linalg.inv(truth).T
    ref = ref[:, :2] / ref[:, 2:3]
    assert np.linalg.norm(mm[0] - mm[1]) == pytest.approx(
        np.linalg.norm(ref[0] - ref[1]), rel=0.01
    )


def test_calibrate_grid_writes_calibration_and_overlay_artifacts(
    tmp_path: Path,
) -> None:
    photo = tmp_path / "wall.png"
    fx.write_wall_photo(str(photo))
    out = tmp_path / "calib"
    r = run_cli(
        tmp_path,
        "calibrate-grid",
        str(photo),
        "-o",
        str(out),
        "--f-px",
        "900",
        env_extra=LIGHT_ENV,
    )
    assert r.returncode == 0, r.stderr
    assert "grid found" in r.stdout and "reprojection median" in r.stdout
    doc = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
    assert doc["f_source"] == "given" and doc["f_px"] == 900.0
    assert (out / "overlay.jpg").stat().st_size > 5000


def test_calibrate_grid_without_a_grid_exits_3_and_still_prints_json(
    tmp_path: Path,
) -> None:
    blank = tmp_path / "blank.png"
    fx.write_png(str(blank), np.full((300, 400, 3), 200, np.uint8))
    r = run_cli(tmp_path, "calibrate-grid", str(blank), "--json", env_extra=LIGHT_ENV)
    assert r.returncode == 3
    doc = json.loads(r.stdout)
    assert (
        doc["ok"] is False and doc["reason"] and doc["homography_px_to_grid_mm"] is None
    )
    assert "no honeycomb grid found" in r.stderr


def test_calibrate_grid_bad_input_exits_2(tmp_path: Path) -> None:
    r = run_cli(
        tmp_path, "calibrate-grid", str(tmp_path / "missing.jpg"), env_extra=LIGHT_ENV
    )
    assert r.returncode == 2
    assert "image not found" in r.stderr
    both = run_cli(
        tmp_path,
        "calibrate-grid",
        "x.jpg",
        "--f-px",
        "700",
        "--f-ratio",
        "0.4",
        env_extra=LIGHT_ENV,
    )
    assert both.returncode == 2  # argparse: --f-px and --f-ratio are mutually exclusive
    assert run_cli(tmp_path, "calibrate-grid", "--help").returncode == 0
    no_args = run_cli(tmp_path, "calibrate-grid")
    assert no_args.returncode == 1 and "3d calibrate-grid" in no_args.stdout
