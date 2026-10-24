"""`3d measure-ref` through bin/3d: the eye-iteration loop a user runs, with its gate, JSON and overlay."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

from .workflow_helper import run_cli

EYE = (0x28, 0x40, 0xA0)
SPEC = {
    "features": {
        "eye_R": {
            "inner_side": "left",
            "box": [20, 10, 140, 90],
            "reference": {"colour": "#2840a0", "tol": 20},
            "render": {"colour": "#2840a0", "tol": 20},
        }
    }
}


def _eye(path: Path, tilt: float, aspect: float = 2.5) -> Path:
    im = Image.new("RGB", (160, 100), (200, 200, 200))
    th = math.radians(tilt)
    pts = []
    for k in range(360):
        t = math.radians(k)
        a, b = 30 * math.cos(t), 30 / aspect * math.sin(t)
        pts.append((80 + a * math.cos(th) - b * math.sin(th), 50 - (a * math.sin(th) + b * math.cos(th))))
    ImageDraw.Draw(im).polygon(pts, fill=EYE)
    im.save(path)
    return path


def _setup(tmp_path: Path, render_tilt: float) -> None:
    _eye(tmp_path / "ref.png", 18)
    _eye(tmp_path / "render.png", render_tilt)
    (tmp_path / "eyes.json").write_text(json.dumps(SPEC))


def test_matching_render_prints_the_table_and_passes(tmp_path: Path) -> None:
    """A user compares a render that matches the reference: the table is printed and the gate passes."""
    _setup(tmp_path, 18)
    result = run_cli(tmp_path, "measure-ref", "ref.png", "render.png", "--spec", "eyes.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "eye_R: PASS" in result.stdout
    assert ">>> MEASURE-REF: PASS" in result.stdout
    for metric in ("tilt", "pca_tilt", "ar_axis", "corner_len"):
        assert metric in result.stdout


def test_wrong_tilt_exits_one_and_names_the_feature_and_metric(tmp_path: Path) -> None:
    """A user compares a render whose eye is 14 degrees too flat: exit 1, the feature and the tilt metric are named."""
    _setup(tmp_path, 4)  # 14 degrees flatter than the reference
    result = run_cli(tmp_path, "measure-ref", "ref.png", "render.png", "--spec", "eyes.json")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "eye_R: FAIL" in result.stdout
    assert "! tilt:" in result.stdout
    assert "MEASURE-REF: FAIL" in result.stderr
    assert ">>> MEASURE-REF: PASS" not in result.stdout


def test_tol_override_turns_the_same_pair_into_a_pass(tmp_path: Path) -> None:
    """A user loosens the tilt tolerances for a pair that failed the defaults: the same pair passes."""
    _setup(tmp_path, 4)
    result = run_cli(
        tmp_path, "measure-ref", "ref.png", "render.png", "--spec", "eyes.json", "--tol", "tilt=20", "--tol", "pca_tilt=20"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "eye_R: PASS" in result.stdout


def test_json_report_overlay_and_out_file_are_written(tmp_path: Path) -> None:
    """A user asks for the JSON report, the saved report file and the overlay sheet: all three are written and agree."""
    _setup(tmp_path, 18)
    result = run_cli(
        tmp_path, "measure-ref", "ref.png", "render.png", "--spec", "eyes.json", "--json", "-o", "report.json", "--overlay", "overlay.png"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    printed = json.loads(result.stdout)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert printed == saved
    assert printed["status"] == "PASS"
    tilt = printed["features"]["eye_R"]["metrics"]["tilt"]
    assert abs(tilt["reference"] - 18) < 1.5 and abs(tilt["delta"]) < 1.5
    with Image.open(tmp_path / "overlay.png") as overlay:
        assert overlay.width > 100 and overlay.height > 50


def test_usage_errors_exit_two(tmp_path: Path) -> None:
    """A user forgets the spec, passes a bad tolerance or a missing image: each is a usage error that says what is wrong."""
    _setup(tmp_path, 18)
    no_spec = run_cli(tmp_path, "measure-ref", "ref.png", "render.png")
    assert no_spec.returncode == 2
    bad_tol = run_cli(tmp_path, "measure-ref", "ref.png", "render.png", "--spec", "eyes.json", "--tol", "tilt=lots")
    assert bad_tol.returncode == 2 and "tilt" in bad_tol.stderr
    missing = run_cli(tmp_path, "measure-ref", "nope.png", "render.png", "--spec", "eyes.json")
    assert missing.returncode == 2
    assert "tilt" in bad_tol.stderr


def test_help_documents_every_option(tmp_path: Path) -> None:
    """A user reads `3d measure-ref --help` and the doc page: every option is in both."""
    result = run_cli(tmp_path, "measure-ref", "--help")
    assert result.returncode == 0
    for flag in ("--spec", "--tol", "--cand-scale", "--overlay", "--out", "--json"):
        assert flag in result.stdout
    doc = (Path(__file__).resolve().parents[2] / "docs" / "commands" / "measure-ref.md").read_text()
    for flag in ("--spec", "--tol", "--cand-scale", "--overlay", "--out", "--json"):
        assert flag in doc
