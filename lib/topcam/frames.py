"""topcam.frames — turn raw YUYV stills into averaged images and PNG files.

WHAT: `to_rgb(frame, w, h)` (BT.601 limited-range YUYV → float RGB 0..255), `average(frames,
  mode)` (mean or median over N frames: the temporal noise of a locked camera drops by
  ~sqrt(N)), `luma`, and `write_png(path, array, depth=8|16)` without Pillow/OpenCV.
WHY: measurement stills stay uncompressed end to end; a 16-bit PNG keeps the sub-LSB detail
  an average of several 8-bit frames carries.
INVARIANTS: numpy is imported lazily (the module is reachable from command modules, which
  must stay stdlib-only at import time).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

AVERAGE_MODES = ("mean", "median", "none")


def _np() -> Any:
    import numpy

    return numpy


def yuyv_planes(
    frame: bytes, width: int, height: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(Y, U, V) as float32 W×H planes; chroma is shared by each horizontal pixel pair."""
    np_ = _np()
    if len(frame) != width * height * 2:
        raise ValueError(
            f"YUYV {width}x{height} needs {width * height * 2} bytes, got {len(frame)}"
        )
    a = (
        np_.frombuffer(frame, np_.uint8)
        .reshape(height, width // 2, 4)
        .astype(np_.float32)
    )
    y = a[:, :, [0, 2]].reshape(height, width)
    u = np_.repeat(a[:, :, 1], 2, axis=1)
    v = np_.repeat(a[:, :, 3], 2, axis=1)
    return y, u, v


def to_rgb(frame: bytes, width: int, height: int) -> np.ndarray:
    """Float32 H×W×3 RGB in 0..255 (BT.601, limited range as UVC cameras send it)."""
    np_ = _np()
    y, u, v = yuyv_planes(frame, width, height)
    c = (y - 16.0) * 1.164
    d = u - 128.0
    e = v - 128.0
    rgb = np_.stack([c + 1.596 * e, c - 0.392 * d - 0.813 * e, c + 2.017 * d], axis=-1)
    clipped: np.ndarray = np_.clip(rgb, 0.0, 255.0)
    return clipped


def luma(frame: bytes, width: int, height: int) -> np.ndarray:
    return yuyv_planes(frame, width, height)[0]


def average(images: list[np.ndarray], mode: str = "mean") -> np.ndarray:
    np_ = _np()
    if mode not in AVERAGE_MODES:
        raise ValueError(f"average mode must be one of {', '.join(AVERAGE_MODES)}")
    if not images:
        raise ValueError("no frames to average")
    if mode == "none" or len(images) == 1:
        return images[-1]
    stack = np_.stack(images, axis=0)
    out: np.ndarray = (
        np_.median(stack, axis=0) if mode == "median" else stack.mean(axis=0)
    )
    return out


def noise(images: list[np.ndarray]) -> float | None:
    """Mean per-pixel temporal standard deviation across frames (None for one frame)."""
    np_ = _np()
    if len(images) < 2:
        return None
    return float(np_.stack(images, axis=0).std(axis=0).mean())


def write_png(path: str | Path, image: np.ndarray, depth: int = 8) -> Path:
    """Grey (H×W) or RGB (H×W×3) float/uint image in 0..255 → PNG at 8 or 16 bits."""
    np_ = _np()
    if depth not in (8, 16):
        raise ValueError("depth must be 8 or 16")
    img = np_.asarray(image, dtype=np_.float64)
    if img.ndim == 2:
        color = 0
    elif img.ndim == 3 and img.shape[2] == 3:
        color = 2
    else:
        raise ValueError(f"image must be H×W or H×W×3, got shape {img.shape}")
    height, width = img.shape[:2]
    if depth == 8:
        data = np_.clip(np_.rint(img), 0, 255).astype(np_.uint8)
    else:
        data = np_.clip(np_.rint(img * 257.0), 0, 65535).astype(">u2")
    rows = data.reshape(height, -1).view(np_.uint8)
    raw = np_.concatenate([np_.zeros((height, 1), np_.uint8), rows], axis=1).tobytes()

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    out = Path(path)
    out.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    return out
