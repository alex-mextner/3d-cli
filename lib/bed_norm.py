"""bed_norm.py — the lighting-normalised evidence channel of the bed verdict (issue #723, item 3).

WHY: `bed_state.difference` maps the current frame onto the reference colours with a per-channel
  gain/offset fit. That absorbs an exposure or white-balance change, not a NONLINEAR one (gamma, a
  light switched on or off, the camera going from daylight to night mode): a bright patch of the
  empty plate then reads as a changed region.

WHAT: when the two frames differ in light (the mean luminance of the bed ROI is off by more than
  `LIGHT_RATIO_MIN..LIGHT_RATIO_MAX`, or the reference and the frame were taken with a different
  printer light state), a pixel only counts as changed when TWO channels say so:
    colour      the existing aligned difference (a part has another colour than the empty plate);
    structure   the local texture: luminance minus its neighbourhood mean over its neighbourhood
                spread (`local_contrast`), compared with the reference. Gain, offset and any monotone
                tone curve leave it (nearly) alone; a part replaces the plate's texture with its own
                edges or with flat colour.
  A flat part has structure only at its rim: the rim is closed and filled (`_filled`) so the
  interior of a part is confirmed by its outline. Without a light mismatch nothing changes: the
  colour channel alone decides, as before.

Numpy + OpenCV (run through cli.pyrun with bed_state.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

# mean luminance of the frame over the reference's, inside the ROI: outside this range -> light mismatch
LIGHT_RATIO_MIN = 0.8
LIGHT_RATIO_MAX = 1.25
# grey levels added to both sides of a brightness ratio: a black corner must not make a ratio of noise
BRIGHTNESS_FLOOR = 8.0
# px of the 480-px frame: the neighbourhood of the local contrast
CONTRAST_SIGMA = 6.0
# noise floor of the contrast: this share of the ROI's mean luminance plus FLOOR_ABS grey levels
# (the spread of a flat area is noise)
FLOOR_REL = 0.04
FLOOR_ABS = 2.0
# |local contrast difference| above which the texture counts as changed. Measured on the K1 SE frames
# (tests/data/bed): an empty bed under gain 0.25-4 / gamma 0.5-2 stays under 0.35 at the 99.9th
# percentile (saturated frames reach 0.7), the rim of a 24-px part is 0.7 (median).
STRUCTURE_LEVEL = 0.5
# the rim of a flat part is closed with this square kernel before it is filled
CLOSE_PX = 9


@dataclass(frozen=True)
class LightStats:
    ratio: float  # the brightness ratio (frame / reference, inside the ROI) farthest from 1
    mismatch: bool
    why: str


def luma(img: np.ndarray) -> np.ndarray:
    """Luminance (float32) of a BGR frame."""
    f = img.astype(np.float32)
    return f[..., 0] * 0.114 + f[..., 1] * 0.587 + f[..., 2] * 0.299


def _worst_ratio(ref: np.ndarray, cur: np.ndarray, roi: np.ndarray) -> float:
    """The ratio of the frame's luminance to the reference's, at the dark, middle and bright end of the ROI,
    that is farthest from 1: a tone curve (gamma) leaves the mean alone and moves the ends."""
    qs = (10, 50, 90)
    a = np.percentile(luma(ref)[roi], qs) + BRIGHTNESS_FLOOR
    b = np.percentile(luma(cur)[roi], qs) + BRIGHTNESS_FLOOR
    ratios = b / a
    return float(ratios[int(np.argmax(np.abs(np.log(ratios))))])


def light_stats(
    ref: np.ndarray, cur: np.ndarray, roi: np.ndarray, light_differs: bool = False
) -> LightStats:
    """Do the frames differ in light? By the brightness of the ROI, or because the caller knows the light
    state of the reference and of the frame differ (`light_differs`)."""
    state = "light state differs" if light_differs else ""
    if not roi.any():
        return LightStats(1.0, light_differs, state)
    ratio = _worst_ratio(ref, cur, roi)
    off = not LIGHT_RATIO_MIN <= ratio <= LIGHT_RATIO_MAX
    return LightStats(
        ratio,
        off or light_differs,
        f"brightness x{ratio:.2f} of the reference" if off else state,
    )


def local_contrast(grey: np.ndarray, floor: float) -> np.ndarray:
    """(grey - local mean) / sqrt(local variance + floor^2): texture without brightness."""
    g = cv2.GaussianBlur(grey, (5, 5), 0)
    mu = cv2.GaussianBlur(g, (0, 0), CONTRAST_SIGMA)
    var = cv2.GaussianBlur((g - mu) ** 2, (0, 0), CONTRAST_SIGMA)
    return (g - mu) / np.sqrt(var + floor * floor)


def _shifted_min(cur: np.ndarray, ref: np.ndarray, shift: int) -> np.ndarray:
    """|cur - ref| where each pixel may match the reference within `shift` px (the registration tolerance
    of `bed_state.difference`)."""
    h, w = ref.shape
    padded = cv2.copyMakeBorder(ref, shift, shift, shift, shift, cv2.BORDER_REPLICATE)
    best = np.abs(cur - ref)
    for dy in range(-shift, shift + 1):
        for dx in range(-shift, shift + 1):
            if dx or dy:
                best = np.minimum(
                    best,
                    np.abs(
                        cur
                        - padded[
                            shift + dy : shift + dy + h, shift + dx : shift + dx + w
                        ]
                    ),
                )
    return best


def structure_changed(
    ref: np.ndarray, cur: np.ndarray, roi: np.ndarray, shift: int = 0
) -> np.ndarray:
    """Bool mask: where the local texture of `cur` differs from `ref` (see the module docstring)."""
    if not roi.any():
        return np.zeros(roi.shape, bool)
    gr, gc = luma(ref), luma(cur)
    floor = FLOOR_REL * float(gr[roi].mean()) + FLOOR_ABS
    floor_c = FLOOR_REL * float(gc[roi].mean()) + FLOOR_ABS
    nr, nc = local_contrast(gr, floor), local_contrast(gc, floor_c)
    diff = _shifted_min(nc, nr, shift) if shift > 0 else np.abs(nc - nr)
    return (diff > STRUCTURE_LEVEL) & roi


def _filled(mask: np.ndarray) -> np.ndarray:
    """`mask` with its small gaps closed and its enclosed holes filled: the rim of a flat part becomes the part."""
    closed = cv2.morphologyEx(
        mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((CLOSE_PX, CLOSE_PX), np.uint8)
    )
    h, w = closed.shape
    flood = np.pad(closed, 1).copy()
    cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 2)
    holes = flood[1:-1, 1:-1] == 0
    return (closed > 0) | holes


def confirm(changed: np.ndarray, structure: np.ndarray) -> np.ndarray:
    """The colour-changed pixels whose neighbourhood also changed in structure."""
    return changed & _filled(structure)


def describe(stats: LightStats) -> dict[str, Any]:
    return {"light_ratio": round(stats.ratio, 3), "light_mismatch": stats.mismatch}
