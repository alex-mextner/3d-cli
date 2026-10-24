"""`3d preprocess --backdrop` through bin/3d: a studio render with black, white and pink parts gets one solid subject mask."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from .workflow_helper import run_cli

pytest.importorskip("cv2")  # `3d preprocess` needs the optional OpenCV extra

SIZE = 300


def _studio(path: Path) -> np.ndarray:
    img = Image.new("RGB", (SIZE, SIZE), (178, 173, 170))
    d = ImageDraw.Draw(img)
    d.ellipse([70, 40, 230, 250], fill=(24, 24, 24))
    d.ellipse([110, 150, 190, 240], fill=(252, 252, 252))
    d.ellipse([130, 80, 160, 105], fill=(240, 150, 160))
    img.save(path)
    truth = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(truth).ellipse([70, 40, 230, 250], fill=255)
    return np.asarray(truth) > 0


def test_backdrop_mask_keeps_the_dark_white_and_pink_parts(tmp_path: Path) -> None:
    truth = _studio(tmp_path / "studio.png")
    result = run_cli(tmp_path, "preprocess", "studio.png", "--backdrop", "--force-fallback", "-o", "out")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MASK_TIER=backdrop" in result.stdout
    mask = np.asarray(Image.open(tmp_path / "out" / "mask.png").convert("L")) > 127
    assert (mask & truth).sum() / (mask | truth).sum() > 0.96
    assert mask[92, 145] and mask[200, 150]  # the pink patch and the white patch are subject


def test_no_backdrop_at_the_border_exits_two_with_the_remedy(tmp_path: Path) -> None:
    Image.new("RGB", (SIZE, SIZE), (230, 40, 40)).save(tmp_path / "red.png")
    result = run_cli(tmp_path, "preprocess", "red.png", "--backdrop", "--force-fallback", "-o", "out")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no backdrop along the frame border" in result.stderr and "--backdrop-value" in result.stderr


def test_backdrop_value_range_must_be_two_numbers(tmp_path: Path) -> None:
    _studio(tmp_path / "studio.png")
    result = run_cli(tmp_path, "preprocess", "studio.png", "--backdrop", "--backdrop-value", "dark", "--force-fallback", "-o", "out")
    assert result.returncode == 2 and "--backdrop-value must be LO,HI" in result.stderr


def test_backdrop_options_are_documented_in_help_and_docs(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "preprocess", "--help")
    doc = (Path(__file__).resolve().parents[2] / "docs" / "commands" / "preprocess.md").read_text()
    for flag in ("--backdrop", "--backdrop-saturation S", "--backdrop-value LO,HI"):
        assert flag in result.stdout and flag in doc
