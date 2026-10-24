"""Synthetic mockup photos with a known patch under a colour cast, for the `mix target` tests (#415).

The scene is what a phone photo of a mockup looks like to the reader: a wall patch of a known sRGB
colour, a white trim at the level the reader maps white to (235), a dark shadow band and a white
glint inside the patch's box, and sensor noise. The camera is a per-channel gain in LINEAR light
(the colour cast and the exposure), then the sRGB encoding and 8-bit quantisation. Everything is
seeded, so the pixels are the same on every run.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from printops.color import linear_rgb

W, H = 320, 240
PATCH = (140, 90, 220, 170)  # the box to match, x0, y0, x1, y1
WHITE = (10, 10, 70, 60)  # the white trim
WHITE_LEVEL = "#EBEBEB"  # 235 grey, the level the reader maps white to


def _encode(lin: np.ndarray) -> np.ndarray:
    c = np.clip(lin, 0.0, 1.0)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


def _paint(
    scene: np.ndarray, box: tuple[int, int, int, int], rgb: tuple[float, ...]
) -> None:
    scene[box[1] : box[3], box[0] : box[2]] = rgb


def photo(
    path: Path,
    patch_hex: str,
    cast: tuple[float, float, float] = (1.0, 1.0, 1.0),
    seed: int = 7,
    noise: float = 0.004,
) -> Path:
    """Write the scene as `path` (PNG or JPEG by suffix); `cast` is the linear gain per channel."""
    patch = linear_rgb(patch_hex)
    scene = np.zeros((H, W, 3))
    _paint(scene, (0, 0, W, H), linear_rgb("#7A7468"))  # the wall
    _paint(scene, WHITE, linear_rgb(WHITE_LEVEL))
    _paint(scene, PATCH, patch)
    _paint(
        scene,
        (PATCH[0], PATCH[1], PATCH[2], PATCH[1] + 6),
        tuple(c * 0.45 for c in patch),
    )
    _paint(scene, (PATCH[2] - 8, PATCH[3] - 8, PATCH[2], PATCH[3]), (1.0, 1.0, 1.0))
    rng = np.random.default_rng(seed)
    shot = scene * np.array(cast) + rng.normal(0.0, noise, scene.shape)
    img = np.round(_encode(shot) * 255).astype(np.uint8)
    im = Image.fromarray(img, "RGB")
    if path.suffix.lower() in (".jpg", ".jpeg"):
        im.save(path, quality=95)
    else:
        im.save(path)
    return path
