"""Synthetic top-camera photos with known geometry for rectification tests.

`render_scene` draws ArUco markers lying on the bed (positions in bed mm) as a camera at a
known perspective sees them (`BED_TO_IMAGE`: bed mm → image px), on a grey bed with noise.
Tests then check that calib_rectify recovers the geometry it was never told.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# bed mm → image px: ~6.5 px/mm, a slight tilt and rotation, bed +Y up in the image
_A = np.array([[6.6, 0.25, 300.0], [0.35, -6.4, 1470.0], [0.00012, -0.00009, 1.0]])
BED_TO_IMAGE = _A
SIZE = (2304, 1536)
FIDUCIALS: list[dict[str, Any]] = [
    {"id": 0, "center_mm": [12.0, 12.0], "size_mm": 10.0},
    {"id": 1, "center_mm": [208.0, 12.0], "size_mm": 10.0},
    {"id": 2, "center_mm": [208.0, 208.0], "size_mm": 10.0},
    {"id": 3, "center_mm": [12.0, 208.0], "size_mm": 10.0},
]
CARD = {"40": [0.0, 0.0], "41": [100.0, 0.0], "42": [100.0, 60.0], "43": [0.0, 60.0]}


def corners_mm(
    center: Sequence[float],
    size: float,
    rot_deg: float = 0.0,
    scale: tuple[float, float] = (1.0, 1.0),
) -> np.ndarray:
    h = size / 2
    pts = np.array([[-h, h], [h, h], [h, -h], [-h, -h]])
    a = math.radians(rot_deg)
    pts = pts @ np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]).T
    return pts * np.array(scale) + np.array(center)


def _project(pts_mm: np.ndarray) -> np.ndarray:
    hom = np.hstack([pts_mm, np.ones((len(pts_mm), 1))]) @ BED_TO_IMAGE.T
    return hom[:, :2] / hom[:, 2:3]


def _paste(img: np.ndarray, ident: int, quad_px: np.ndarray) -> None:
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    k = 40
    marker = cv2.aruco.generateImageMarker(d, ident, 6 * k)
    pad = np.full((8 * k, 8 * k), 255, np.uint8)
    pad[k : 7 * k, k : 7 * k] = marker
    src = np.array([[k, k], [7 * k, k], [7 * k, 7 * k], [k, 7 * k]], np.float32)
    m = cv2.getPerspectiveTransform(src, quad_px.astype(np.float32))
    warped = cv2.warpPerspective(pad, m, SIZE, flags=cv2.INTER_AREA, borderValue=0)
    mask = cv2.warpPerspective(np.full_like(pad, 255), m, SIZE, flags=cv2.INTER_AREA)
    alpha = mask.astype(np.float32) / 255.0
    img[:] = (img * (1 - alpha) + warped * alpha).astype(np.uint8)


def render_scene(
    path: Path,
    *,
    card_at: tuple[float, float] | None = None,
    card_rot: float = 15.0,
    printer_scale: tuple[float, float] = (1.0, 1.0),
    seed: int = 1,
    drop: tuple[int, ...] = (),
) -> Path:
    """Write a 16-bit RGB PNG (as `3d camera still --depth 16` does). The card markers are
    placed at true positions; a printer with `printer_scale` (true mm per commanded mm) put
    the fiducials, so in bed (commanded) mm the card looks 1/scale as long."""
    img = np.full((SIZE[1], SIZE[0]), 170, np.uint8)
    for f in FIDUCIALS:
        if f["id"] in drop:
            continue
        _paste(
            img, int(f["id"]), _project(corners_mm(tuple(f["center_mm"]), f["size_mm"]))
        )
    if card_at is not None:
        a = math.radians(card_rot)
        rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        inv = np.array([1 / printer_scale[0], 1 / printer_scale[1]])
        for ident, true_xy in CARD.items():
            # marker corners in true card mm → rotate → place → to bed (commanded) mm
            true_corners = corners_mm(tuple(true_xy), 10.0) @ rot.T
            bed = true_corners * inv + np.array(card_at)
            _paste(img, int(ident), _project(bed))
    rng = np.random.default_rng(seed)
    noisy = np.clip(
        cv2.GaussianBlur(img.astype(np.float32), (0, 0), 0.8)
        + rng.normal(0, 2.0, img.shape),
        0,
        255,
    )
    rgb16 = np.repeat((noisy * 257).astype(np.uint16)[:, :, None], 3, axis=2)
    cv2.imwrite(str(path), rgb16)
    return path


def write_card(path: Path, thickness: float = 0.0) -> Path:
    doc: dict[str, Any] = {
        "schema": "scale-ref/1",
        "name": "test card",
        "dictionary": "DICT_4X4_50",
        "marker_size_mm": 10.0,
        "markers": CARD,
        "thickness_mm": thickness,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def to_yuyv(png: Path) -> bytes:
    """The PNG as the camera would send it: YUYV, limited range, neutral chroma."""
    raw = cv2.imread(str(png), cv2.IMREAD_UNCHANGED)
    assert raw is not None, png
    img = raw.astype(np.float64)
    grey = (img[:, :, 0] if img.ndim == 3 else img) / (
        257.0 if img.max() > 255 else 1.0
    )
    y = np.clip(np.rint(grey / 1.164 + 16), 0, 255).astype(np.uint8)
    out = np.full((y.shape[0], y.shape[1] * 2), 128, np.uint8)
    out[:, 0::2] = y
    return out.tobytes()
