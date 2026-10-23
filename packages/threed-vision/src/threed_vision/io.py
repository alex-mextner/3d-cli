"""Image / mask / JSON helpers shared by every stage (numpy + OpenCV + Pillow only)."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BadInput, MissingDependency


def load_rgb(path: str | Path, max_side: int | None = None) -> np.ndarray:
    """Decode any photo (JPEG/PNG/HEIC) to an upright RGB uint8 array; EXIF orientation is applied."""
    from PIL import Image, ImageOps

    p = Path(path)
    if not p.is_file():
        raise BadInput(f"image not found: {p}")
    if p.suffix.lower() in (".heic", ".heif"):
        try:
            import pillow_heif
        except ImportError as exc:
            raise MissingDependency("pillow-heif is required to decode HEIC photos", "uv pip install pillow-heif") from exc
        pillow_heif.register_heif_opener()
    try:
        with Image.open(p) as im:
            rgb = np.array(ImageOps.exif_transpose(im).convert("RGB"))
    except (OSError, Image.DecompressionBombError) as exc:
        raise BadInput(f"cannot decode image {p}: {exc}") from exc
    if max_side and max(rgb.shape[:2]) > max_side:
        import cv2

        s = max_side / max(rgb.shape[:2])
        rgb = cv2.resize(
            rgb, (max(1, round(rgb.shape[1] * s)), max(1, round(rgb.shape[0] * s))), interpolation=cv2.INTER_AREA
        )
    return rgb


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_png(path: str | Path, arr: np.ndarray) -> None:
    """Write RGB/gray/uint16/bool arrays losslessly (bool -> 0/255)."""
    from PIL import Image

    a = np.asarray(arr)
    if a.dtype == bool:
        a = a.astype(np.uint8) * 255
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(a).save(path)


def load_mask(path: str | Path) -> np.ndarray:
    """Binary mask from a PNG (any non-zero pixel is foreground)."""
    from PIL import Image

    p = Path(path)
    if not p.is_file():
        raise BadInput(f"mask not found: {p}")
    try:
        with Image.open(p) as im:
            return np.array(im.convert("L")) > 0
    except OSError as exc:  # truncated / not an image (PIL's UnidentifiedImageError is an OSError)
        raise BadInput(f"cannot decode mask {p}: {exc}") from exc


def to_jsonable(obj: Any) -> Any:
    """Recursively convert numpy scalars/arrays (and non-finite floats -> null) to plain JSON types."""
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, np.generic):
        return to_jsonable(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Path):
        return str(obj)
    return obj


def write_json(path: str | Path, obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(to_jsonable(obj), indent=2) + "\n", encoding="utf-8")


def mask_bbox_xywh(mask: np.ndarray) -> list[int]:
    rows = np.flatnonzero(mask.any(axis=1))  # row / column projections: far cheaper than np.nonzero on a photo-sized mask
    if rows.size == 0:
        return [0, 0, 0, 0]
    cols = np.flatnonzero(mask.any(axis=0))
    return [int(cols[0]), int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)]


def mask_polygon(mask: np.ndarray, epsilon_px: float = 1.5) -> list[list[float]]:
    """Outer contour of the largest component, simplified, as [[x, y], ...] (empty for an empty mask)."""
    import cv2

    cs, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return []
    c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), epsilon_px, True)
    return [[float(x), float(y)] for x, y in c.reshape(-1, 2)]


def to_gray(rgb: np.ndarray, dtype: type = np.uint8) -> np.ndarray:
    """HxW or HxWx3 (RGB) image -> HxW grey array of `dtype` (uint8 for SIFT, float32 for band-pass filters)."""
    import cv2

    a = np.asarray(rgb)
    if a.ndim == 3:
        a = cv2.cvtColor(a[:, :, :3].astype(np.uint8), cv2.COLOR_RGB2GRAY)
    elif a.ndim != 2:
        raise BadInput("image must be HxW or HxWx3")
    return a.astype(dtype)


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, bool), np.asarray(b, bool)
    return float((a & b).sum() / max(int((a | b).sum()), 1))


def masks_to_labelmap(
    masks: list[np.ndarray], min_area: int = 150, max_frac: float = 0.25, shape: tuple[int, int] | None = None
) -> np.ndarray:
    """bool HxW masks -> uint16 label map (1..N); larger masks are painted first so small ones stay on top.

    `shape` (H, W) is required when `masks` may be empty so the result still has the image size."""
    if not masks and shape is None:
        raise BadInput("masks_to_labelmap needs shape=(H, W) when there are no masks")
    h, w = shape if shape is not None else masks[0].shape
    keep = sorted((m for m in masks if min_area <= m.sum() <= max_frac * h * w), key=lambda m: -int(m.sum()))
    lab = np.zeros((h, w), np.uint16)
    for i, m in enumerate(keep, 1):
        lab[m] = i
    return lab


def draw_overlay(rgb: np.ndarray, masks: list[np.ndarray], numbers: bool = True, alpha: float = 0.45) -> np.ndarray:
    """Colour-coded instance overlay (contour + optional id) on an RGB image; returns a new RGB array."""
    import cv2

    img = rgb.copy()
    rng = np.random.RandomState(3)
    pal = rng.randint(40, 255, size=(len(masks) + 1, 3)).astype(np.uint8)
    for i, m in enumerate(masks, 1):
        col = pal[i]
        img[m] = (img[m] * (1 - alpha) + col * alpha).astype(np.uint8)
    for i, m in enumerate(masks, 1):
        cs, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, cs, -1, tuple(int(c) for c in pal[i]), 2)
        if numbers and cs:
            x, y, _, _ = cv2.boundingRect(max(cs, key=cv2.contourArea))
            for colr, th in (((255, 255, 255), 2), ((0, 0, 0), 1)):
                cv2.putText(img, str(i), (x, max(12, y - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colr, th, cv2.LINE_AA)
    return img
