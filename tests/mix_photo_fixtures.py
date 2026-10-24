"""Synthetic photos of a mix tester card, drawn from the tester's own manifest and card PNG (#416).

The scene is the card PNG as `3d mix tester` wrote it (paper at the level the reader maps white to,
235 of 255) with every swatch lying in its frame in a TRUE colour the reader is never told, its
border darker (a rounded edge). A camera then sees it: a homography (perspective), a colour cast and
an uneven light as per-channel gains in linear light, blur and sensor noise, the sRGB encoding.
`top_camera_frame` is what `3d camera still --depth 16` writes (16-bit PNG, nearly square on);
`phone_photo` is a hand-held JPEG with strong perspective, a cast, a light gradient and an EXIF
orientation. Everything is seeded.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from printops.color import linear_rgb
from slicing.mix_card import PX_PER_MM

PAPER = 235 / 255  # the paper's sRGB level, the one `mix target` maps white to
TABLE = 0.12  # linear level of the table around the card


def true_colour(pred_hex: str) -> str:
    """What the printed swatch really looks like: a deterministic shift off the prediction."""
    lin = np.array(linear_rgb(pred_hex)) * np.array([0.93, 1.03, 0.88])
    c = np.clip(lin, 0.0, 1.0)
    enc = np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{round(v * 255):02X}" for v in enc)


def _decode(a: np.ndarray) -> np.ndarray:
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _encode(lin: np.ndarray) -> np.ndarray:
    c = np.clip(lin, 0.0, 1.0)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


def scene(
    card_png: Path,
    manifest: dict[str, Any],
    true_hex: dict[int, str],
    *,
    half_shadow: int | None = None,
    missing: tuple[int, ...] = (),
) -> np.ndarray:
    """The card in linear light at 10 px/mm with the swatches on it (float32 RGB)."""
    img = np.asarray(Image.open(card_png).convert("RGB"), dtype=np.float32) / 255.0
    lin = _decode(img) * np.float32(_decode(np.array(PAPER)))
    lay = manifest["readout"]
    h_mm = lay["card_mm"][1]

    def px(box: list[float]) -> tuple[int, int, int, int]:
        return (
            round(box[0] * PX_PER_MM),
            round((h_mm - box[3]) * PX_PER_MM),
            round(box[2] * PX_PER_MM),
            round((h_mm - box[1]) * PX_PER_MM),
        )

    for cell in lay["cells"]:
        n = cell["number"]
        if n in missing:
            continue
        x0, y0, x1, y1 = px(cell["face_box"])
        lin[y0:y1, x0:x1] = np.array(linear_rgb(true_hex[n]), dtype=np.float32)
        edge = 8  # 0.8 mm of rounded, darker border
        for sl in (
            (slice(y0, y0 + edge), slice(x0, x1)),
            (slice(y1 - edge, y1), slice(x0, x1)),
            (slice(y0, y1), slice(x0, x0 + edge)),
            (slice(y0, y1), slice(x1 - edge, x1)),
        ):
            lin[sl] *= 0.45
        if n == half_shadow:
            lin[y0:y1, x0 + (x1 - x0) // 2 : x1] *= 0.5
    return lin.astype(np.float32)


def shoot(
    lin: np.ndarray,
    quad: Sequence[tuple[float, float]],
    size: tuple[int, int],
    *,
    cast: tuple[float, float, float],
    gradient: tuple[float, float] = (0.0, 0.0),
    noise: float = 0.003,
    blur: float = 0.8,
    seed: int = 3,
) -> np.ndarray:
    """Linear-light camera image (h, w, 3), `quad` = where the card's TL, TR, BR, BL land (px)."""
    h, w = lin.shape[:2]
    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    m = cv2.getPerspectiveTransform(src, np.array(quad, np.float32))
    warped = cv2.warpPerspective(
        lin,
        m,
        size,
        flags=cv2.INTER_AREA,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(TABLE,) * 3,
    )
    if blur:
        warped = cv2.GaussianBlur(warped, (0, 0), blur)
    ys, xs = np.mgrid[0 : size[1], 0 : size[0]].astype(np.float32)
    light = (
        1.0
        + gradient[0] * (xs / size[0] - 0.5) * 2
        + gradient[1] * (ys / size[1] - 0.5) * 2
    )
    shot = warped * light[:, :, None] * np.array(cast, np.float32)
    rng = np.random.default_rng(seed)
    return shot + rng.normal(0.0, noise, shot.shape).astype(np.float32)


def top_camera_frame(
    path: Path, lin: np.ndarray, *, cast: tuple[float, float, float] = (1.0, 0.9, 0.78)
) -> Path:
    """A 16-bit RGB PNG, the camera nearly square above the card (a slight tilt)."""
    quad = [(212, 132), (1318, 150), (1300, 1480), (196, 1456)]
    shot = shoot(
        lin, quad, (1536, 1600), cast=cast, gradient=(0.04, 0.05), noise=0.002, blur=0.6
    )
    rgb16 = np.round(_encode(shot) * 65535).astype(np.uint16)
    ok = cv2.imwrite(str(path), rgb16[:, :, ::-1])
    assert ok
    return path


def phone_photo(
    path: Path,
    lin: np.ndarray,
    *,
    cast: tuple[float, float, float] = (1.0, 0.8, 0.62),
    gradient: tuple[float, float] = (0.18, 0.12),
) -> Path:
    """A hand-held JPEG: strong perspective, a warm cast, a light gradient, EXIF orientation 6."""
    quad = [(520, 380), (2160, 250), (2310, 2930), (330, 2760)]
    shot = shoot(lin, quad, (2560, 3200), cast=cast, gradient=gradient)
    upright = np.round(_encode(shot) * 255).astype(np.uint8)
    stored = np.rot90(
        upright, 1
    )  # the sensor's rows; EXIF orientation 6 turns it upright again
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(stored)).save(
        buf, "JPEG", quality=92, exif=exif
    )
    path.write_bytes(buf.getvalue())
    return path


def truths(manifest: dict[str, Any]) -> dict[int, str]:
    """The true colour of every swatch: the prediction shifted, the pure spools as printed."""
    entries = manifest["candidates"] + manifest["references"]
    return {e["number"]: true_colour(e["hex"]) for e in entries}
