"""collage.py — before / after / displacement collage PNG, rendered by the numpy rasteriser (no GUI, no GL).

WHAT: three rows (before, after, displacement heat map) by three columns (front, left, top), each a
  headlight-shaded orthographic view of the mesh on one shared scale, written as a PNG.

WHY: a numeric report says how far vertices moved; a reviewer still has to SEE the fur, the grab or the
  fit before accepting it. The heat map shows where the spec actually changed the surface.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from errors import MissingDependency

from . import raster

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]
VIEWS = ("front", "left", "top")


def _heat(t: Array) -> NDArray[np.uint8]:
    """Blue -> cyan -> yellow -> red ramp for t in [0, 1]."""
    t = np.clip(t, 0.0, 1.0)
    r = np.clip(4 * t - 2, 0, 1)
    g = np.clip(np.minimum(4 * t, 4 - 4 * t), 0, 1)
    b = np.clip(2 - 4 * t, 0, 1)
    return np.asarray(np.stack([r, g, b], axis=-1) * 255, dtype=np.uint8)


def render_collage(
    path: str, V0: Array, F0: IntArray, V1: Array, F1: IntArray, tile: int = 360
) -> None:
    try:
        from PIL import Image
    except ImportError as err:
        raise MissingDependency(
            "Pillow",
            install="pip install pillow   (or: uv sync --extra preprocess)",
            degrades="--collage output",
            command="mesh deform",
        ) from err
    both = np.concatenate([V0, V1])
    # displacement per vertex of the after mesh versus the nearest before vertex (same index when topology is unchanged)
    if len(V0) == len(V1):
        disp = np.linalg.norm(V1 - V0, axis=1)
    else:
        from scipy.spatial import cKDTree

        disp = np.asarray(cKDTree(V0).query(V1, workers=1)[0])
    top = float(disp.max()) if len(disp) else 0.0
    rows: list[NDArray[np.uint8]] = []
    for kind in ("before", "after", "heat"):
        tiles = []
        for view in VIEWS:
            cam = raster.auto_ortho(view, both, tile, tile)
            V, F = (V0, F0) if kind == "before" else (V1, F1)
            grey = raster.shade(cam, V, F)
            img = np.stack([grey] * 3, axis=-1)
            if kind == "heat" and top > 0:
                uv, z = cam.project(V1)
                _, _, fbuf = raster.rasterize(uv, z, F1, tile, tile, want_depth=True)
                assert fbuf is not None
                hit = fbuf >= 0
                face_disp = disp[F1].mean(axis=1)
                img = img.copy()
                heat = _heat(face_disp[fbuf[hit]] / top)
                img[hit] = (0.35 * img[hit] + 0.65 * heat).astype(np.uint8)
            tiles.append(img)
        rows.append(np.concatenate(tiles, axis=1))
    sheet = np.concatenate(rows, axis=0)
    Image.fromarray(sheet).save(path)
