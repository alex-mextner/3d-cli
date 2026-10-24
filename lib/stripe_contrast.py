"""stripe_contrast.py — how visible are the layer stripes of a stack of single-colour layers, in CIEDE2000.

WHAT: `layer_stripe_de(colours, heights, ...)` takes the colour and the real height of consecutive layers of one
  vertical wall, blurs that colour-vs-height profile with the eye's line-spread (Gaussian, sigma = `arcmin` at
  `view_mm`, in LINEAR light: opaque fine stripes mix by area) and returns, per layer, the worst CIEDE2000 distance
  between the blurred profile and its local mean (the mean over one pattern period). A grating nobody can resolve
  gives ~0; a bold one gives 10-25.
WHY: the layer-by-layer pattern checks of gcode_mix_gate.py (no neighbours equal, each layer <= 0.10 mm) passed
  GKGKWK (green / black / green / black / white / black), a 6-layer high-contrast cycle that prints as visible bands.
  Contrast has to be measured over the whole period at the real layer heights. Same model as `stripe_view` of the
  babushkin-dom Full Spectrum tools (docs/full-spectrum/tools/colour.py), ported to heights that may differ per layer.
  Reference values at 30 cm / 0.08 mm layers: GKGKWK 22.9, GWGWGK 15.1, GWGK 10.7, GKW 2.6, GW 0.2, GK 0.1.
INVARIANTS: pure function, no project constants, numpy + printops.color (hex, linear light, CIELAB, CIEDE2000); the wall is vertical (a band is as wide as the layer is
  high); a segment too short for the blur plus one period is assumed to repeat itself (the model's own assumption);
  layers whose blur window leaves the segment are None, never 0.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from printops.color import delta_e2000, linear_rgb, linear_to_lab

STEP_MM = 0.004  # sampling step of the height profile

Vec = npt.NDArray[np.float64]


def _lab(linear: Vec) -> tuple[float, float, float]:
    return linear_to_lab((float(linear[0]), float(linear[1]), float(linear[2])))


def layer_stripe_de(
    colours: Sequence[str],
    heights: Sequence[float],
    *,
    period_layers: int,
    view_mm: float = 300.0,
    arcmin: float = 1.0,
) -> list[float | None]:
    """Worst stripe dE00 per layer of a vertical wall printed one colour per layer.

    `colours[i]` (`#RRGGBB`) and `heights[i]` (mm) describe layer i, bottom to top; `period_layers` is the
    pattern's length (the mean is taken over that many layers' height). A layer whose blur window is not fully
    inside the stack is None; a stack too short for any window is treated as one repeating period and every layer
    gets the stack's worst value."""
    n = len(colours)
    if n != len(heights) or n == 0:
        raise ValueError("colours and heights must be the same non-zero length")
    if min(heights) <= 0 or view_mm <= 0 or period_layers < 1:
        raise ValueError("heights, view_mm and period_layers must be positive")
    sigma = view_mm * math.tan(math.radians(arcmin / 60))
    edges = np.concatenate([[0.0], np.cumsum(heights)])
    xs = np.arange(STEP_MM / 2, edges[-1], STEP_MM)
    layer_at = np.minimum(np.searchsorted(edges, xs, side="right") - 1, n - 1)
    lin = np.array([linear_rgb(c) for c in colours], dtype=np.float64)
    prof = lin[layer_at]
    half = int(math.ceil(4 * sigma / STEP_MM))
    kern = np.exp(-0.5 * ((np.arange(-half, half + 1) * STEP_MM) / sigma) ** 2)
    kern /= kern.sum()
    win = max(1, round(period_layers * float(np.median(heights)) / STEP_MM))

    def worst(p: Vec) -> tuple[npt.NDArray[np.intp], Vec]:
        """(sample index in `p`, worst dE) of the blur-vs-mean deviation where both windows are inside `p`."""
        if len(p) < len(kern):
            return np.empty(0, dtype=np.intp), np.empty(0)
        blur = np.stack([np.convolve(p[:, c], kern, mode="valid") for c in range(3)], 1)
        if len(blur) < win:
            return np.empty(0, dtype=np.intp), np.empty(0)
        mean = np.stack(
            [
                np.convolve(blur[:, c], np.full(win, 1.0 / win), mode="valid")
                for c in range(3)
            ],
            1,
        )
        centre = blur[win // 2 : win // 2 + len(mean)]
        idx = np.arange(len(mean), dtype=np.intp)
        des = np.array(
            [delta_e2000(_lab(centre[j]), _lab(mean[j])) for j in idx]
        )
        return idx + win // 2 + half, des

    where, des = worst(prof)
    if len(where) == 0:
        reps = int(math.ceil((2 * half + win + 1) / len(prof))) + 1
        _, des = worst(np.tile(prof, (reps, 1)))
        return [float(des.max())] * n if len(des) else [None] * n
    out: list[float | None] = [None] * n
    for x, d in zip(where, des, strict=True):
        k = int(layer_at[int(x)])
        prev = out[k]
        out[k] = float(d) if prev is None else max(prev, float(d))
    return out
