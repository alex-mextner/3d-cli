"""Unit tests for the pure, testable functions (no external tools needed)."""
from __future__ import annotations

import os

import extract_params
from cli.env import install_cmd, pypkg_for
from cli.imaging import score_metrics

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_score_metrics_perfect_overlap() -> None:
    m = score_metrics(inter=0.5, union=0.5, ae=0.0, area=1000)
    assert m["IoU"] == 1.0
    assert m["CLOSENESS"] == 1.0
    assert m["AE_NORM"] == 0.0


def test_score_metrics_half_overlap() -> None:
    m = score_metrics(inter=0.25, union=0.5, ae=100.0, area=1000)
    assert abs(m["IoU"] - 0.5) < 1e-9
    assert abs(m["AE_NORM"] - 0.1) < 1e-9


def test_score_metrics_blank_render_scores_zero() -> None:
    # union == 0 must never reward a blank frame.
    m = score_metrics(inter=0.0, union=0.0, ae=0.0, area=1000)
    assert m["IoU"] == 0.0
    assert m["CLOSENESS"] == 0.0


def test_score_metrics_zero_area() -> None:
    m = score_metrics(inter=0.0, union=0.0, ae=5.0, area=0)
    assert m["AE_NORM"] == 1.0


def test_pypkg_for_mapping() -> None:
    assert pypkg_for("PIL") == "pillow"
    assert pypkg_for("cv2") == "opencv-python-headless"
    assert pypkg_for("trimesh") == "trimesh"


def test_install_cmd_returns_a_string() -> None:
    # whatever the host OS, install_cmd must produce a non-empty hint, never crash.
    s = install_cmd("openscad")
    assert isinstance(s, str) and s


def test_extract_params_on_cube() -> None:
    rows = extract_params.extract(os.path.join(_REPO, "examples", "cube.scad"))
    names = {r["name"]: r for r in rows}
    assert {"width", "depth", "height", "wall"} <= set(names)
    assert names["width"]["value"] == "20"
    assert names["width"]["range"] == "10:40"
    assert names["wall"]["value"] == "2"
