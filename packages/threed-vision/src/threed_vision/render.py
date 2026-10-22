"""Independent hard rasteriser (numpy + OpenCV) used to SCORE fits: the soft torch rasteriser that optimises a fit must
never grade it. Also builds the fit's world mesh and the review overlays.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .loft import local_mesh


def project(V: np.ndarray, K: Any, R: Any, t: Any) -> np.ndarray:
    """Pinhole projection of world points (N,3) mm -> pixels (N,2); depth is clamped to >= 1 mm (points behind the camera stay finite)."""
    Xc = np.asarray(V, np.float64) @ np.asarray(R, np.float64).T + np.asarray(t, np.float64)
    z = np.maximum(Xc[:, 2:3], 1.0)
    K = np.asarray(K, np.float64)
    return Xc[:, :2] / z * np.array([K[0, 0], K[1, 1]]) + K[:2, 2]


def hard_mask(
    V: np.ndarray, F: np.ndarray, K: Any, R: Any, t: Any, size: tuple[int, int], roi: tuple[int, int, int, int] | None = None
) -> np.ndarray:
    """Binary silhouette of a triangle mesh. `size` = (width, height) of the full image; `roi` = (x0, y0, x1, y1) renders only that
    window (returned array is the window). A degenerate candidate (NaN/inf vertices) yields an EMPTY mask (IoU 0), never an exception;
    coordinates are clipped before cv2.fillConvexPoly, which is extremely slow on huge polygons."""
    import cv2

    x0, y0, x1, y1 = roi if roi is not None else (0, 0, size[0], size[1])
    img = np.zeros((max(y1 - y0, 0), max(x1 - x0, 0)), np.uint8)
    V = np.asarray(V, np.float64)
    if img.size == 0 or not np.isfinite(V).all():
        return img > 0
    uv = project(V, K, R, t) - np.array([x0, y0], np.float64)
    if not np.isfinite(uv).all():
        return img > 0
    big = 3 * max(size)
    uv = np.clip(uv, -2 * big, 3 * big)
    tri = np.round(uv[np.asarray(F)] * 16).astype(np.int32)
    for q in tri:
        cv2.fillConvexPoly(img, q, 1, shift=4)
    return img > 0


def hull_mask(V: np.ndarray, K_st: int, M: int, K: Any, R: Any, t: Any, size: tuple[int, int]) -> np.ndarray:
    """Silhouette of a K-station loft as the union of the K-1 convex hulls of consecutive projected rings (same guards as `hard_mask`).

    The silhouette of the closed mesh equals this union for the loft template (consecutive rings are homothetic, see soft_raster), but
    it needs ~K-1 fills instead of ~2*M*(K-1)+2*M, so it is the cost function of the CMA-ES search; SCORING uses `hard_mask` on the mesh.
    """
    import cv2

    img = np.zeros((size[1], size[0]), np.uint8)
    V = np.asarray(V, np.float64)
    if not np.isfinite(V).all():
        return img > 0
    uv = project(V[: K_st * M], K, R, t)
    if not np.isfinite(uv).all():
        return img > 0
    big = 3 * max(size)
    pts = np.round(np.clip(uv, -2 * big, 3 * big) * 16).astype(np.int32)
    for k in range(K_st - 1):
        cv2.fillConvexPoly(img, cv2.convexHull(pts[k * M : (k + 2) * M]), 1, shift=4)
    return img > 0


def view_iou(mask: np.ndarray, V: np.ndarray, F: np.ndarray, K: Any, R: Any, t: Any) -> float:
    """IoU of the mesh silhouette against a full-resolution view mask, rendered only inside bbox(mask) U bbox(model)."""
    mask = np.asarray(mask, bool)
    H, W = mask.shape
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return 0.0
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    V = np.asarray(V, np.float64)
    if not np.isfinite(V).all():
        return 0.0
    uv = project(V, K, R, t)
    if not np.isfinite(uv).all():
        return 0.0
    x0, y0 = max(min(x0, int(np.floor(uv[:, 0].min()))), 0), max(min(y0, int(np.floor(uv[:, 1].min()))), 0)
    x1, y1 = min(max(x1, int(np.ceil(uv[:, 0].max())) + 1), W), min(max(y1, int(np.ceil(uv[:, 1].max())) + 1), H)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    h = hard_mask(V, F, K, R, t, (W, H), roi=(x0, y0, x1, y1))
    m = mask[y0:y1, x0:x1]
    # model pixels outside the ROI would be clipped; the ROI contains the model bbox so none are lost except off-image ones
    return float((h & m).sum() / max(int((h | m).sum()), 1))


def mesh_from_fit(fit: dict[str, Any], ring: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(V, F) of a fit in WORLD coordinates (mm): V = R0 @ local + t_mm. `ring` raises the cross-section resolution (export)."""
    V, F = local_mesh(fit, ring)
    R0 = np.asarray(fit["pose"]["R0"], np.float64)
    return V @ R0.T + np.asarray(fit["pose"]["t_mm"], np.float64), F


def overlay(
    base: np.ndarray | None,
    model_mask: np.ndarray,
    view_mask: np.ndarray | None = None,
    *,
    thickness: int = 2,
) -> np.ndarray:
    """RGB review image: view mask contour GREEN, model silhouette contour RED, on `base` (RGB) or black. Returns a new array."""
    import cv2

    h, w = model_mask.shape
    img = np.zeros((h, w, 3), np.uint8) if base is None else np.asarray(base, np.uint8).copy()
    for m, col in ((view_mask, (0, 255, 0)), (model_mask, (255, 0, 0))):
        if m is None:
            continue
        cs, _ = cv2.findContours(np.asarray(m, np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, cs, -1, col, thickness)
    return img
