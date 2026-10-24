"""backdrop_mask.py — subject mask of a studio render / product shot: everything that is not the smooth backdrop.

ACCESSED VIA: `3d preprocess <image> --backdrop` (lib/preprocess_reference.py calls `backdrop_mask`).

WHAT: the backdrop is the connected region of low-saturation, mid-value, texture-free pixels that touches the frame
  border; the subject is everything else (largest component, holes filled). Unlike grabCut or a salient-object
  model it keeps a subject whose parts are very dark, very light or saturated (black fur, white paws, a pink nose),
  which a grey studio render has in abundance and which those models take for background or shadow.

WHY: `3d compare` / `3d score` need a reference silhouette. On neutral-grey studio renders grabCut dropped the pink
  ears and pads and a floor shadow leaked into the mask; "differs from the smooth grey backdrop" does neither.
  A floor shadow is mid-grey and low-saturation, so it counts as backdrop, not subject.

ASSUMPTIONS: the backdrop is smooth and neutral (saturation <= max_saturation, value inside value_range, local
  gray standard deviation < texture) and visible along the frame border. Kernel sizes are for a 427 px frame and
  scale with the image. numpy + scipy only (no OpenCV).
"""

from __future__ import annotations

from typing import cast

import numpy as np
from errors import UsageError
from numpy.typing import NDArray

REFERENCE_SIZE = 427  # the frame the kernel sizes below were tuned on
WINDOW = 7  # texture window, px at the reference size
BORDER_BAND = (
    8  # backdrop seed band along the frame edge: tile seams leave a noisy 1-2 px frame
)
BoolArray = NDArray[np.bool_]


def _disc(radius: int) -> BoolArray:
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    return cast(BoolArray, (xx * xx + yy * yy) <= radius * radius + radius)


def _scaled(px: float, scale: float, minimum: int = 1) -> int:
    return max(minimum, round(px * scale))


def _closing(mask: BoolArray, radius: int) -> BoolArray:
    from scipy import ndimage as ndi

    padded = np.pad(
        mask, radius + 1, mode="edge"
    )  # the frame edge is not a boundary of the subject
    closed = ndi.binary_closing(padded, structure=_disc(radius))
    return cast(BoolArray, closed[radius + 1 : -radius - 1, radius + 1 : -radius - 1])


def _opening(mask: BoolArray, radius: int) -> BoolArray:
    from scipy import ndimage as ndi

    padded = np.pad(mask, radius + 1, mode="edge")
    opened = ndi.binary_opening(padded, structure=_disc(radius))
    return cast(BoolArray, opened[radius + 1 : -radius - 1, radius + 1 : -radius - 1])


def _largest(mask: BoolArray) -> BoolArray:
    from scipy import ndimage as ndi

    labels, n = ndi.label(mask, structure=np.ones((3, 3)))
    if n == 0:
        return mask
    counts = np.bincount(labels.ravel())
    counts[0] = 0
    return cast(BoolArray, labels == int(counts.argmax()))


def backdrop_mask(
    rgb: NDArray[np.uint8],
    *,
    max_saturation: float = 0.14,
    value_range: tuple[float, float] = (0.38, 0.80),
    texture: float = 7.0,
) -> NDArray[np.uint8]:
    """HxWx3 uint8 RGB -> HxW uint8 mask, 255 = subject."""
    from scipy import ndimage as ndi

    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise UsageError(
            f"backdrop mask needs an HxWx3 RGB image, got shape {rgb.shape}",
            command="preprocess",
        )
    scale = min(rgb.shape[:2]) / REFERENCE_SIZE
    f = rgb.astype(np.float32) / 255.0
    hi, lo = f.max(axis=2), f.min(axis=2)
    saturation = np.where(hi > 0, (hi - lo) / np.maximum(hi, 1e-6), 0.0)
    gray = (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]) * 255.0
    win = _scaled(WINDOW, scale) | 1
    mean = ndi.uniform_filter(gray, win, mode="reflect")
    std = np.sqrt(
        np.maximum(
            ndi.uniform_filter(gray * gray, win, mode="reflect") - mean * mean, 0.0
        )
    )
    backdrop = (
        (std < texture)
        & (saturation < max_saturation)
        & (hi > value_range[0])
        & (hi < value_range[1])
    )
    backdrop = _opening(backdrop, 1)
    labels, _ = ndi.label(backdrop, structure=np.ones((3, 3)))
    band = _scaled(BORDER_BAND, scale, 2)
    edge = np.concatenate(
        [
            labels[:band].ravel(),
            labels[-band:].ravel(),
            labels[:, :band].ravel(),
            labels[:, -band:].ravel(),
        ]
    )
    seeds = np.unique(edge)
    seeds = seeds[seeds != 0]
    if len(seeds) == 0:
        raise UsageError(
            "no backdrop along the frame border: nothing there is smooth, low-saturation and mid-value",
            command="preprocess",
            remediation=[
                "Drop --backdrop for a photo with a cluttered background (grabCut / rembg tiers).",
                "Or widen the backdrop with --backdrop-saturation S / --backdrop-value LO,HI.",
            ],
        )
    subject = ~np.isin(labels, seeds)
    subject = _closing(
        subject, _scaled(9, scale)
    )  # pale shaded parts leak into the backdrop class: refill the bites
    subject = cast(
        BoolArray,
        ndi.binary_erosion(subject, structure=_disc(_scaled(2, scale)), border_value=1),
    )  # the edge halo of the texture window is not subject
    subject = _opening(subject, _scaled(2, scale))
    subject = _largest(subject)
    subject = cast(BoolArray, ndi.binary_fill_holes(subject))
    if not subject.any():
        raise UsageError(
            "the whole frame is backdrop: no subject found",
            command="preprocess",
            remediation=[
                "Check the image, or narrow the backdrop with --backdrop-saturation S / --backdrop-value LO,HI."
            ],
        )
    return subject.astype(np.uint8) * 255
