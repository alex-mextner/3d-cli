"""Closed-form silhouette fit (numpy + OpenCV, no optimiser): oriented rectangle + width profile along the principal axis.

`fit_profile(mask, mm_per_px)` answers in milliseconds: length, max width, per-station lateral extents, a `kind`
(box / cylinder_capsule / lathe_profile / loft_flat / ring_lathe) and the IoU of the profile polygon against the mask.
It cannot follow bent or open shapes (a straight-axis profile), and it says nothing about thickness or 3-D pose.
"""
from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from .errors import BadInput

MIN_PIXELS = 8  # below this a mask has no measurable axis/width (a 1-pixel mask is noise, not an object)


def as_bool_mask(mask: Any) -> np.ndarray:
    """Validate + normalise a mask to a 2-D bool array (bool; ints: != 0; floats: > 0.5)."""
    a = np.asarray(mask)
    if a.ndim != 2:
        raise BadInput(f"mask must be 2-D (H, W), got shape {a.shape}")
    if a.dtype == bool:
        return a
    if a.dtype.kind == "f":
        if not np.isfinite(a).all():
            raise BadInput("mask contains NaN/inf")
        return a > 0.5
    return a != 0


def _frame(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    c = pts.mean(0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    a = vt[0]
    return c, a, np.array([-a[1], a[0]])


def _profile(pts: np.ndarray, c: np.ndarray, a: np.ndarray, n: np.ndarray, nbins: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    u = (pts - c) @ a
    v = (pts - c) @ n
    edges = np.linspace(u.min(), u.max() + 1e-6, nbins + 1)
    b = np.clip(np.digitize(u, edges) - 1, 0, nbins - 1)
    lo = np.full(nbins, np.nan)
    hi = np.full(nbins, np.nan)
    order = np.argsort(b, kind="stable")
    bs, vs = b[order], v[order]
    idx = np.searchsorted(bs, np.arange(nbins + 1))
    for k in range(nbins):
        seg = vs[idx[k] : idx[k + 1]]
        if len(seg):
            lo[k], hi[k] = seg.min(), seg.max()
    good = ~np.isnan(lo)
    x = np.arange(nbins)
    return edges, np.interp(x, x[good], lo[good]), np.interp(x, x[good], hi[good])


def _smooth(x: np.ndarray) -> np.ndarray:
    return np.convolve(np.pad(x, 2, mode="edge"), np.ones(3) / 3, mode="same")[2:-2]


def _recon_iou(mask: np.ndarray, c: np.ndarray, a: np.ndarray, n: np.ndarray, edges: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> float:
    import cv2

    uc = (edges[:-1] + edges[1:]) / 2
    top = c + np.outer(uc, a) + np.outer(hi, n)
    bot = c + np.outer(uc, a) + np.outer(lo, n)
    pad = 8
    poly = np.vstack([top, bot[::-1]]) + pad
    h, w = mask.shape
    img = np.zeros((max(h + 2 * pad, int(poly[:, 1].max()) + 2), max(w + 2 * pad, int(poly[:, 0].max()) + 2)), np.uint8)
    cv2.fillPoly(img, [np.round(poly).astype(np.int32)], 1)
    m = np.zeros(img.shape, bool)
    m[pad : pad + h, pad : pad + w] = mask
    return float(((img > 0) & m).sum() / max(int(((img > 0) | m).sum()), 1))


def fit_profile(mask: Any, mm_per_px: float = 1.0, K: int = 12, nbins: int = 96, axis_angle_deg: float | None = None) -> dict[str, Any]:
    """Closed-form profile of one silhouette.

    Coordinates `center_px` / `extent_center_px` are in the INPUT mask's pixel frame. `axis_angle_deg` overrides the PCA axis
    (PCA is ill-defined for near-square silhouettes). Raises BadInput for empty/tiny masks or a non-positive scale; never returns NaN.
    """
    import cv2

    t0 = time.perf_counter()
    if not (isinstance(mm_per_px, (int, float)) and math.isfinite(mm_per_px) and mm_per_px > 0):
        raise BadInput(f"mm_per_px must be a positive finite number, got {mm_per_px!r}")
    if K < 2 or nbins < 4:
        raise BadInput(f"need K >= 2 stations and nbins >= 4, got K={K}, nbins={nbins}")
    full = as_bool_mask(mask)
    rows, cols = np.flatnonzero(full.any(1)), np.flatnonzero(full.any(0))  # bbox first: np.nonzero on a 3 MP frame is the slow part
    if rows.size == 0:
        raise BadInput(f"mask has 0 foreground pixels; need >= {MIN_PIXELS} to fit a profile")
    off = np.array([cols[0], rows[0]], np.float64)
    sub = full[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]
    ys, xs = np.nonzero(sub)
    if xs.size < MIN_PIXELS:
        raise BadInput(f"mask has {xs.size} foreground pixel(s); need >= {MIN_PIXELS} to fit a profile")
    pts = np.c_[xs, ys].astype(np.float64)

    c, a, n = _frame(pts)
    if axis_angle_deg is not None:
        th = math.radians(axis_angle_deg)
        a = np.array([math.cos(th), math.sin(th)])
        n = np.array([-a[1], a[0]])
    edges, lo, hi = _profile(pts, c, a, n, nbins)
    # orient the axis so the wider half comes first ("handle first"); flips the lateral sign consistently
    wid = hi - lo
    if wid[: nbins // 2].mean() < wid[nbins // 2 :].mean():
        a, n = -a, -n
        edges, lo, hi = _profile(pts, c, a, n, nbins)
    lo_s, hi_s = _smooth(lo), _smooth(hi)
    iou = _recon_iou(sub, c, a, n, edges, lo_s, hi_s)

    L = float(edges[-1] - edges[0])
    (_, (rw0, rh0), _) = cv2.minAreaRect(pts.astype(np.float32))
    rw, rh = sorted((rw0, rh0), reverse=True)
    area = float(sub.sum())
    rect_fill = area / max(rw * rh, 1.0)
    padded = np.pad(sub, 1).astype(np.uint8)
    cnts, hier = cv2.findContours(padded, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = [i for i, h in enumerate(hier[0]) if h[3] >= 0 and cv2.contourArea(cnts[i]) > 0.02 * area] if hier is not None else []
    hull = cv2.convexHull(np.vstack(cnts))
    solidity = area / max(cv2.contourArea(hull), 1.0)
    elong = rw / max(rh, 1.0)
    width = (hi_s - lo_s)
    if holes and elong < 1.3:
        kind = "ring_lathe"  # tape roll / washer
    elif rect_fill > 0.88 and solidity > 0.93:
        kind = "box"
    elif elong > 3.0 and width.std() / max(width.mean(), 1e-6) < 0.18:
        kind = "cylinder_capsule"
    elif solidity > 0.80 or elong > 2.2:
        kind = "lathe_profile"  # axisymmetric handle+shaft style: radius = half-width
    else:
        kind = "loft_flat"  # flat articulated (pliers/scissors): extrude the silhouette
    s = mm_per_px
    ks = np.linspace(0, nbins - 1, K)
    xk = np.arange(nbins)
    out: dict[str, Any] = {
        "kind": kind,
        "length_mm": L * s,
        "max_width_mm": float(width.max()) * s,
        "rect_mm": [rw * s, rh * s],
        "rect_fill": float(rect_fill),
        "solidity": float(solidity),
        "elongation": float(elong),
        "center_px": (c + off).tolist(),
        "lateral_offset_mm": float((lo_s + hi_s).mean() / 2) * s,
        "extent_center_px": (c + a * (edges[0] + edges[-1]) / 2 + n * float((lo_s + hi_s).mean() / 2) + off).tolist(),
        "axis_unit": a.tolist(),
        "axis_deg": float(np.degrees(np.arctan2(a[1], a[0]))),
        "stations": [
            {"t": float(k / (nbins - 1)), "lo_mm": float(np.interp(k, xk, lo_s) * s), "hi_mm": float(np.interp(k, xk, hi_s) * s)}
            for k in ks
        ],
        "iou_recon": iou,
        "pixels": int(area),
    }
    if kind == "ring_lathe" and holes:
        hole_area = cv2.contourArea(cnts[holes[0]])
        out["ring_mm"] = {"r_outer": float(np.sqrt(area / np.pi + hole_area / np.pi) * s), "r_inner": float(np.sqrt(hole_area / np.pi) * s)}
    out["time_ms"] = (time.perf_counter() - t0) * 1e3
    return out
