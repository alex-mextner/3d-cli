"""Part colours of a segmented tool from its photo (numpy + OpenCV only, no models, deterministic).

`sample_part_colours(rgb, mask, fit=None, n_parts=None, max_colours=4)`:

1. the object is cut along its principal axis (the closed-form `fit_profile` axis, handle side first) at DISCONTINUITIES of the width
   profile (handle -> shaft -> head); `n_parts` forces the number of parts (strongest cuts first, equal splits if the profile has none);
2. per part, the colour comes from the ERODED mask interior (the boundary is blended with the background) with specular highlights
   removed (very light, nearly colourless pixels), as the densest cluster in CIE Lab (8-unit bins, median of the cluster): no random
   init, same input -> same output;
3. the part colours are merged into a palette of at most `max_colours` (colours within dE 12 are one colour).

`fit` (the dict of `fit_auto`) is optional: the axis always comes from the mask itself (it is the image-plane axis, which the fit's 3-D
axis only re-projects to); the fit's template name only refines `role_guess`. `role_guess` is a heuristic from order and template
(handle -> shaft -> head), not recognition. A colour is what the CAMERA saw (lighting and white balance included).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import BadInput
from .fit_profile import as_bool_mask, fit_profile

PROFILE_STATIONS = 48
NOISE_JUMP = 0.05  # forced n_parts: a cut still needs a real (>= 5 %) step; stair-step noise is not a discontinuity
MIN_JUMP = 0.2  # a cut needs a width change of >= 20 % of the max width between the stations before and after it
MIN_PART_STATIONS = 4  # parts are at least 4/47 ~ 8.5 % of the length
MAX_PARTS = 4
ERODE_FRAC = (
    0.2  # erosion radius as a fraction of the part's narrowest half-width (>= 2 px)
)
SPECULAR_L = 80.0  # Lab lightness above which a nearly colourless pixel is a highlight
SPECULAR_CHROMA = 14.0
BIN = 8.0  # Lab histogram bin of the dominant-colour search
CLUSTER_RADIUS = 14.0
MERGE_DE = 12.0


def _to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(
        rgb_u8.reshape(-1, 1, 3).astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB
    ).reshape(-1, 3)


def _to_srgb(lab: np.ndarray) -> list[int]:
    import cv2

    px = cv2.cvtColor(
        np.asarray(lab, np.float32).reshape(1, 1, 3), cv2.COLOR_LAB2RGB
    ).reshape(3)
    return [int(v) for v in np.clip(np.round(px * 255.0), 0, 255)]


def _hex(rgb: list[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def dominant_lab(lab: np.ndarray) -> np.ndarray | None:
    """Densest Lab cluster of an (N, 3) array without highlights; None when there are no pixels. Deterministic."""
    if lab.shape[0] == 0:
        return None
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    keep = ~((lab[:, 0] > SPECULAR_L) & (chroma < SPECULAR_CHROMA))
    if keep.sum() >= 5 and (~keep).mean() < 0.5:  # highlights are a MINORITY: a light-grey / white / chrome part is not glare
        lab = lab[keep]
    cell = np.floor(lab / BIN).astype(np.int64)
    keys, inverse, counts = np.unique(
        cell, axis=0, return_inverse=True, return_counts=True
    )
    best = int(
        np.argmax(counts)
    )  # ties: the first (lexicographically smallest) cell: deterministic
    centre = np.median(lab[inverse.reshape(-1) == best], axis=0)
    near = lab[np.linalg.norm(lab - centre, axis=1) <= CLUSTER_RADIUS]
    return np.median(near, axis=0) if near.shape[0] else centre


def _cuts(width: np.ndarray, n_parts: int | None) -> list[float]:
    """Cut positions t in (0, 1) at discontinuities of the (station) width profile."""
    k = width.size
    w = np.convolve(np.pad(width, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    wmax = max(float(w.max()), 1e-9)
    jumps = np.zeros(k - 1)
    for i in range(k - 1):  # between station i and i+1
        a, b = w[max(0, i - 2) : i + 1], w[i + 1 : i + 4]
        jumps[i] = abs(float(a.mean()) - float(b.mean())) / wmax
    order = sorted(range(k - 1), key=lambda i: (-jumps[i], i))
    limit = (n_parts or MAX_PARTS) - 1
    chosen: list[int] = []
    for i in order:
        if len(chosen) >= limit or jumps[i] < (MIN_JUMP if n_parts is None else NOISE_JUMP):
            break
        if i + 1 < MIN_PART_STATIONS or k - 1 - i < MIN_PART_STATIONS:
            continue
        if all(abs(i - j) >= MIN_PART_STATIONS for j in chosen):
            chosen.append(i)
    cuts = sorted((i + 0.5) / (k - 1) for i in chosen)
    while (
        n_parts is not None and len(cuts) < n_parts - 1
    ):  # no (more) discontinuities: split the longest part in half
        edges = [0.0, *cuts, 1.0]
        j = int(np.argmax(np.diff(edges)))
        cuts = sorted([*cuts, (edges[j] + edges[j + 1]) / 2])
    return cuts


def _roles(n: int, template: str | None) -> list[str]:
    if n == 1:
        return ["body"]
    if template in ("flat", "box"):
        return (
            ["handle"] + ["body"] * (n - 2) + ["jaw"]
            if template == "flat"
            else ["body"] * n
        )
    return ["handle", *(["shaft"] * (n - 2)), "head"] if n >= 3 else ["handle", "shaft"]


def _merge_palette(
    parts: list[dict[str, Any]], max_colours: int
) -> list[dict[str, Any]]:
    """Greedy palette: biggest areas first; a colour within dE MERGE_DE of an entry joins it; beyond max_colours join the nearest."""
    palette: list[dict[str, Any]] = []
    for p in sorted(
        (p for p in parts if p["lab"] is not None),
        key=lambda p: (-p["area_frac"], p["t0"]),
    ):
        lab = np.asarray(p["lab"])
        dists = [float(np.linalg.norm(lab - np.asarray(e["lab"]))) for e in palette]
        if dists and (min(dists) <= MERGE_DE or len(palette) >= max_colours):
            palette[int(np.argmin(dists))]["area_frac"] += p["area_frac"]
        else:
            palette.append(
                {
                    "colour_srgb": p["colour_srgb"],
                    "hex": p["hex"],
                    "lab": p["lab"],
                    "area_frac": p["area_frac"],
                }
            )
    return palette


def sample_part_colours(
    rgb: Any,
    mask: Any,
    fit: dict[str, Any] | None = None,
    n_parts: int | None = None,
    max_colours: int = 4,
) -> dict[str, Any]:
    """Per-part dominant colours of the masked object. See the module docstring.

    Returns `{"parts": [{"t0", "t1", "colour_srgb", "hex", "lab", "area_frac", "n_pixels", "role_guess"}], "palette": [...], "axis_deg"}`;
    `t` runs along the axis (0 = the wider end, "handle first"). A part with no usable pixel has `colour_srgb`/`hex`/`lab` = None.
    Raises BadInput for a non-uint8-RGB image, a shape mismatch, an empty/tiny mask, `n_parts` outside 1..4 or `max_colours` < 1.
    """
    import cv2

    img = np.asarray(rgb)
    m = as_bool_mask(mask)
    if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
        raise BadInput(
            f"rgb must be an (H, W, 3) uint8 image, got shape {img.shape} dtype {img.dtype}",
            remediation="convert 16-bit / float images to 8-bit sRGB first",
        )
    if img.shape[:2] != m.shape:
        raise BadInput(
            f"rgb {img.shape[:2]} and mask {m.shape} must have the same height and width"
        )
    if n_parts is not None and not 1 <= n_parts <= MAX_PARTS:
        raise BadInput(f"n_parts must be in 1..{MAX_PARTS}, got {n_parts}")
    if max_colours < 1:
        raise BadInput(f"max_colours must be >= 1, got {max_colours}")
    fp = fit_profile(m, 1.0, K=PROFILE_STATIONS)  # BadInput for empty/tiny masks
    ys, xs = np.nonzero(m)
    a = np.asarray(fp["axis_unit"])
    u = (np.c_[xs, ys] - np.asarray(fp["center_px"])) @ a
    t_all = (u - u.min()) / max(float(u.max() - u.min()), 1e-9)
    st = fp["stations"]
    width = np.array([q["hi_mm"] - q["lo_mm"] for q in st])
    cuts = _cuts(width, n_parts)
    edges = [0.0, *cuts, 1.0]

    x0, y0 = int(xs.min()), int(ys.min())
    crop = m[y0 : ys.max() + 1, x0 : xs.max() + 1].astype(np.uint8)
    inner_by_r: dict[int, np.ndarray] = {}

    def inner_mask(r: int) -> np.ndarray:  # eroded interior, per erosion radius (cached)
        if r not in inner_by_r:
            ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
            inner_by_r[r] = cv2.erode(crop, ker) > 0
        return inner_by_r[r]

    lab_all = _to_lab(img[ys, xs])
    tpl = fit.get("template") if isinstance(fit, dict) else None
    template = tpl.get("name") if isinstance(tpl, dict) else None
    roles = _roles(len(edges) - 1, template)
    total = float(len(xs))
    parts: list[dict[str, Any]] = []
    for j in range(len(edges) - 1):
        last = j == len(edges) - 2
        sel = (t_all >= edges[j]) & (
            (t_all <= edges[j + 1]) if last else (t_all < edges[j + 1])
        )
        ks = np.arange(len(st))
        in_part = (ks / (len(st) - 1) >= edges[j]) & (ks / (len(st) - 1) <= edges[j + 1])
        half = max(0.5 * float(np.median(width[in_part])) if in_part.any() else 1.0, 1.0)  # THIS part's half-width, not the tip's
        r = int(max(2, round(ERODE_FRAC * half)))
        use = sel & inner_mask(r)[ys - y0, xs - x0]
        lab = dominant_lab(
            lab_all[use] if use.any() else lab_all[sel]
        )  # a thin part has no eroded interior: use its pixels
        rgb_c = _to_srgb(lab) if lab is not None else None
        parts.append(
            {
                "t0": float(edges[j]),
                "t1": float(edges[j + 1]),
                "colour_srgb": rgb_c,
                "hex": _hex(rgb_c) if rgb_c else None,
                "lab": [float(v) for v in lab] if lab is not None else None,
                "area_frac": float(sel.sum()) / total,
                "n_pixels": int(sel.sum()),
                "role_guess": roles[j],
            }
        )
    return {
        "parts": parts,
        "palette": _merge_palette(parts, max_colours),
        "axis_deg": float(fp["axis_deg"]),
    }
