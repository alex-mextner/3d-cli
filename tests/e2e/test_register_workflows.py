"""`3d register` through bin/3d: two photos -> homography, relative pose, baseline_ok, warped masks, repeatability IoU."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("PIL")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import vision_calib_fixtures as fx  # noqa: E402

from .workflow_helper import run_cli  # noqa: E402

LIGHT_ENV = {"THREED_VISION_PYTHON": sys.executable}


def _write_instances(folder: Path, masks: list[np.ndarray]) -> Path:
    (folder / "masks").mkdir(parents=True)
    entries = []
    for i, m in enumerate(masks, 1):
        fx.write_png(str(folder / "masks" / f"{i:03d}.png"), m.astype(np.uint8) * 255)
        entries.append({"id": i, "mask": f"masks/{i:03d}.png"})
    path = folder / "instances.json"
    path.write_text(json.dumps({"instances": entries}), encoding="utf-8")
    return path


def test_register_json_for_a_pair_with_a_real_baseline(tmp_path: Path) -> None:
    """Two photos taken from different positions: the JSON carries the homography and a usable relative pose."""
    a, b, H_true, R_true = fx.view_pair()
    fx.write_png(str(tmp_path / "a.png"), a)
    fx.write_png(str(tmp_path / "b.png"), b)
    r = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "a.png"),
        str(tmp_path / "b.png"),
        "--json",
        "--plane-depth-mm",
        "1000",
        env_extra=LIGHT_ENV,
    )
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["ok"] is True and doc["inliers"] > 100 and doc["baseline_ok"] is True
    H = np.asarray(doc["H_a_to_b"])
    p = np.array([[100.0, 100.0, 1.0], [700.0, 500.0, 1.0]])
    got, want = p @ H.T, p @ H_true.T
    assert np.abs(got[:, :2] / got[:, 2:3] - want[:, :2] / want[:, 2:3]).max() < 1.0
    assert doc["rel_rot_deg"] > 5.0 and np.linalg.norm(doc["t_mm"]) > 50.0
    assert "warped_masks" not in doc


def test_register_same_viewpoint_pair_reports_no_baseline(tmp_path: Path) -> None:
    """The carpet-pair situation: B is the same viewpoint at another zoom, so it adds no parallax (exit 0, baseline_ok false)."""
    a = fx.textured()
    zoom = np.array(
        [[0.77, 0, 310.0 - 0.77 * 400.0], [0, 0.77, 230.0 - 0.77 * 300.0], [0, 0, 1.0]]
    )
    fx.write_png(str(tmp_path / "a.png"), a)
    fx.write_png(
        str(tmp_path / "b.png"),
        cv2.warpPerspective(a, zoom, (620, 460), flags=cv2.INTER_AREA),
    )
    r = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "a.png"),
        str(tmp_path / "b.png"),
        "--json",
        env_extra=LIGHT_ENV,
    )
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert (
        doc["ok"] is True
        and doc["baseline_ok"] is False
        and "same_viewpoint" in doc["quality"]
    )
    text = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "a.png"),
        str(tmp_path / "b.png"),
        env_extra=LIGHT_ENV,
    )
    assert text.returncode == 0 and "baseline_ok=False" in text.stdout


def test_register_warps_masks_and_scores_repeatability(tmp_path: Path) -> None:
    """Segment both photos, register, and see which instance masks are not repeatable across the two photos."""
    a, b, H_true, _ = fx.view_pair()
    fx.write_png(str(tmp_path / "a.png"), a)
    fx.write_png(str(tmp_path / "b.png"), b)
    m1, m2 = np.zeros(a.shape[:2], np.uint8), np.zeros(a.shape[:2], np.uint8)
    m1[200:330, 150:300] = 1
    cv2.circle(m2, (520, 380), 70, 1, -1)

    def warp(m: np.ndarray) -> np.ndarray:
        return cv2.warpPerspective(m, H_true, (800, 600), flags=cv2.INTER_NEAREST) > 0

    b1, b2 = warp(m1), warp(m2)
    half = b2.copy()
    half[:, int(np.nonzero(b2.any(axis=0))[0].mean()) :] = (
        False  # B's segmentation of the disc is only its left half
    )
    seg_a = _write_instances(tmp_path / "segA", [m1.astype(bool), m2.astype(bool)])
    seg_b = _write_instances(tmp_path / "segB", [b1, half])
    out = tmp_path / "reg"
    r = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "a.png"),
        str(tmp_path / "b.png"),
        "--mask-a",
        str(seg_a),
        "--mask-b",
        str(seg_b),
        "-o",
        str(out),
        "--json",
        env_extra=LIGHT_ENV,
    )
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)["repeatability"]
    assert rep[0]["iou"] > 0.95 and rep[0]["unstable"] is False
    assert rep[1]["iou"] < 0.75 and rep[1]["unstable"] is True
    saved = json.loads((out / "registration.json").read_text(encoding="utf-8"))
    assert saved["repeatability"] == rep
    warped_png = cv2.imread(str(out / "warped" / "001.png"), cv2.IMREAD_GRAYSCALE)
    assert warped_png is not None
    warped = warped_png > 0
    assert (warped & b1).sum() / b1.sum() > 0.95
    assert (out / "warped" / "002.png").is_file()
    plain = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "a.png"),
        str(tmp_path / "b.png"),
        "--mask-a",
        str(seg_a),
        "--mask-b",
        str(seg_b),
        env_extra=LIGHT_ENV,
    )
    assert "UNSTABLE" in plain.stdout


def test_register_failure_modes(tmp_path: Path) -> None:
    """Featureless photos cannot be registered (exit 3 with the reason), a missing file is a plain input error (exit 2)."""
    blank = np.full((300, 400, 3), 128, np.uint8)
    fx.write_png(str(tmp_path / "blank.png"), blank)
    unreg = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "blank.png"),
        str(tmp_path / "blank.png"),
        env_extra=LIGHT_ENV,
    )
    assert unreg.returncode == 3 and "too_few_features" in unreg.stderr
    missing = run_cli(
        tmp_path,
        "register",
        str(tmp_path / "blank.png"),
        str(tmp_path / "nope.png"),
        env_extra=LIGHT_ENV,
    )
    assert missing.returncode == 2 and "image not found" in missing.stderr
    help_run = run_cli(tmp_path, "register", "--help")
    assert help_run.returncode == 0
    assert "baseline_ok" in help_run.stdout
    bare = run_cli(tmp_path, "register")
    assert bare.returncode == 1
    assert "3d register" in bare.stdout
    assert "too_few_features" in unreg.stderr
