"""`gcode_thumbnails.ensure` bounds the picture it renders: a huge requested thumbnail must not
make it draw a gigantic image (the render allocates several arrays per pixel)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import gcode_thumbnails as gt


def _gcode(path: Path, spec: str) -> Path:
    path.write_text(
        "; HEADER_BLOCK_START\n; HEADER_BLOCK_END\nG90\nM83\nG1 X10 Y10\nG1 X30 Y10 E1\n"
        f"; CONFIG_BLOCK_START\n; thumbnails = {spec}\n; CONFIG_BLOCK_END\n",
        encoding="ascii",
    )
    return path


@pytest.mark.parametrize(
    ("spec", "rendered_side"),
    [("300x300/PNG", 600), ("1024x1024/PNG", 2048), ("4096x4096/PNG", 2048)],
)
def test_ensure_renders_at_most_render_max_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: str, rendered_side: int
) -> None:
    sides: list[int] = []

    def fake_render(path: str, size: int = 300) -> np.ndarray:
        sides.append(size)
        return np.full((8, 8, 4), 255, np.uint8)

    monkeypatch.setattr(gt, "render_gcode", fake_render)
    gt.ensure(str(_gcode(tmp_path / "a.gcode", spec)))
    assert sides == [rendered_side]
    assert gt.RENDER_MAX_SIDE == 2048
