from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import ndimage


@dataclass(frozen=True)
class SpatialFitMetrics:
    area_iou: float
    edge_f1_at_2: float
    edge_f1_at_4: float
    edge_f1_at_8: float
    edge_chamfer_px: float
    boundary_sdf_loss_px: float
    hausdorff_p95_px: float
    bbox_iou: float
    coverage_ratio: float
    centroid_delta_px: float
    render_touches_border: bool
    reference_touches_border: bool
    spatial_warning: str | None

    def as_dict(self) -> dict[str, float | bool | str | None]:
        return {
            "area_iou": self.area_iou,
            "edge_f1@2": self.edge_f1_at_2,
            "edge_f1@4": self.edge_f1_at_4,
            "edge_f1@8": self.edge_f1_at_8,
            "edge_chamfer_px": self.edge_chamfer_px,
            "boundary_sdf_loss_px": self.boundary_sdf_loss_px,
            "hausdorff_p95_px": self.hausdorff_p95_px,
            "bbox_iou": self.bbox_iou,
            "coverage_ratio": self.coverage_ratio,
            "centroid_delta_px": self.centroid_delta_px,
            "render_touches_border": self.render_touches_border,
            "reference_touches_border": self.reference_touches_border,
            "spatial_warning": self.spatial_warning,
        }


def binary_contour(mask: Any) -> Any:
    """Return one-pixel binary boundary for a filled binary mask."""
    m = np.asarray(mask).astype(bool)
    if not m.any():
        return np.zeros_like(m, dtype=bool)
    eroded = ndimage.binary_erosion(m, structure=np.ones((3, 3), dtype=bool), border_value=0)
    return np.logical_and(m, np.logical_not(eroded))


def mask_iou(render_mask: Any, ref_mask: Any) -> float:
    render = np.asarray(render_mask).astype(bool)
    ref = np.asarray(ref_mask).astype(bool)
    union = np.logical_or(render, ref).sum()
    if union == 0:
        return 0.0
    inter = np.logical_and(render, ref).sum()
    return float(inter) / float(union)


def _bbox(mask: Any) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(np.asarray(mask).astype(bool))
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _bbox_iou(a: tuple[int, int, int, int] | None, b: tuple[int, int, int, int] | None) -> float:
    if a is None or b is None:
        return 0.0
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = area_a + area_b - inter
    return float(inter) / float(union) if union else 0.0


def _centroid(mask: Any) -> tuple[float, float] | None:
    ys, xs = np.nonzero(np.asarray(mask).astype(bool))
    if xs.size == 0:
        return None
    return float(xs.mean()), float(ys.mean())


def _centroid_delta(a: Any, b: Any) -> float:
    shape = np.asarray(a).shape
    ca = _centroid(a)
    cb = _centroid(b)
    if ca is None or cb is None:
        return _image_diag(shape)
    return float(np.hypot(ca[0] - cb[0], ca[1] - cb[1]))


def _touches_border(mask: Any) -> bool:
    m = np.asarray(mask).astype(bool)
    if not m.any():
        return False
    return bool(m[0, :].any() or m[-1, :].any() or m[:, 0].any() or m[:, -1].any())


def _edge_hit_f1(render_edge: Any, ref_edge: Any, radius_px: float) -> float:
    render = np.asarray(render_edge).astype(bool)
    ref = np.asarray(ref_edge).astype(bool)
    if not render.any() or not ref.any():
        return 0.0
    dist_to_ref = ndimage.distance_transform_edt(np.logical_not(ref))
    dist_to_render = ndimage.distance_transform_edt(np.logical_not(render))
    precision = float((dist_to_ref[render] <= radius_px).mean())
    recall = float((dist_to_render[ref] <= radius_px).mean())
    if precision + recall == 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _edge_distances(render_edge: Any, ref_edge: Any) -> tuple[Any, Any]:
    render = np.asarray(render_edge).astype(bool)
    ref = np.asarray(ref_edge).astype(bool)
    if not render.any() or not ref.any():
        miss = np.array([_image_diag(render.shape)], dtype=np.float64)
        return miss, miss
    dist_to_ref = ndimage.distance_transform_edt(np.logical_not(ref))
    dist_to_render = ndimage.distance_transform_edt(np.logical_not(render))
    return dist_to_ref[render], dist_to_render[ref]


def _image_diag(shape: tuple[int, ...]) -> float:
    if len(shape) < 2:
        return 1.0
    return float(np.hypot(float(shape[0]), float(shape[1])))


def signed_boundary_distance(mask: Any) -> Any:
    """Return a signed pixel distance field to the mask boundary.

    Positive values are outside the filled mask, negative values are inside it, and
    boundary pixels are zero. The sign makes the field useful for pose diagnostics:
    moving a rendered boundary in the right direction should reduce the absolute
    sampled distance locally.
    """
    m = np.asarray(mask).astype(bool)
    edge = binary_contour(m)
    if not edge.any():
        fill = -float("inf") if m.any() else float("inf")
        return np.full(m.shape, fill, dtype=np.float64)
    unsigned = ndimage.distance_transform_edt(np.logical_not(edge)).astype(np.float64)
    unsigned[edge] = 0.0
    return np.where(m, -unsigned, unsigned)


def boundary_sdf_loss(render_mask: Any, ref_mask: Any) -> float:
    """Symmetric boundary-to-signed-distance-field loss in pixels."""
    render = np.asarray(render_mask).astype(bool)
    ref = np.asarray(ref_mask).astype(bool)
    render_edge = binary_contour(render)
    ref_edge = binary_contour(ref)
    if not render_edge.any() or not ref_edge.any():
        return _image_diag(render.shape)
    ref_sdf = signed_boundary_distance(ref)
    render_sdf = signed_boundary_distance(render)
    render_to_ref = np.abs(ref_sdf[render_edge])
    ref_to_render = np.abs(render_sdf[ref_edge])
    return float((render_to_ref.mean() + ref_to_render.mean()) / 2.0)


def spatial_fit_metrics(render_mask: Any, ref_mask: Any) -> SpatialFitMetrics:
    render = np.asarray(render_mask).astype(bool)
    ref = np.asarray(ref_mask).astype(bool)
    render_edge = binary_contour(render)
    ref_edge = binary_contour(ref)
    d_render_to_ref, d_ref_to_render = _edge_distances(render_edge, ref_edge)
    all_dist = np.concatenate([d_render_to_ref, d_ref_to_render])

    render_area = float(render.sum())
    ref_area = float(ref.sum())
    coverage_ratio = render_area / ref_area if ref_area else 1.0 if render_area == 0 else render_area
    render_border = _touches_border(render)
    ref_border = _touches_border(ref)
    edge_chamfer = float(np.mean(all_dist))
    p95 = float(np.percentile(all_dist, 95))
    bbox_iou = _bbox_iou(_bbox(render), _bbox(ref))
    warning = _spatial_warning(
        coverage_ratio=coverage_ratio,
        bbox_iou=bbox_iou,
        edge_chamfer=edge_chamfer,
        p95=p95,
        render_border=render_border,
        ref_border=ref_border,
        render_empty=render_area == 0,
        ref_empty=ref_area == 0,
    )

    return SpatialFitMetrics(
        area_iou=mask_iou(render, ref),
        edge_f1_at_2=_edge_hit_f1(render_edge, ref_edge, 2.0),
        edge_f1_at_4=_edge_hit_f1(render_edge, ref_edge, 4.0),
        edge_f1_at_8=_edge_hit_f1(render_edge, ref_edge, 8.0),
        edge_chamfer_px=edge_chamfer,
        boundary_sdf_loss_px=boundary_sdf_loss(render, ref),
        hausdorff_p95_px=p95,
        bbox_iou=bbox_iou,
        coverage_ratio=coverage_ratio,
        centroid_delta_px=_centroid_delta(render, ref),
        render_touches_border=render_border,
        reference_touches_border=ref_border,
        spatial_warning=warning,
    )


def _spatial_warning(
    *,
    coverage_ratio: float,
    bbox_iou: float,
    edge_chamfer: float,
    p95: float,
    render_border: bool,
    ref_border: bool,
    render_empty: bool,
    ref_empty: bool,
) -> str | None:
    problems: list[str] = []
    if ref_empty:
        problems.append("empty reference mask")
    if render_empty:
        problems.append("empty render mask")
    if coverage_ratio < 0.70 or coverage_ratio > 1.35:
        problems.append(f"coverage_ratio={coverage_ratio:.2f}")
    if bbox_iou < 0.70:
        problems.append(f"bbox_iou={bbox_iou:.2f}")
    if edge_chamfer > 6.0:
        problems.append(f"edge_chamfer_px={edge_chamfer:.1f}")
    if p95 > 18.0:
        problems.append(f"hausdorff_p95_px={p95:.1f}")
    if render_border and not ref_border:
        problems.append("render touches border while reference does not")
    if not problems:
        return None
    return "spatial mismatch risk: " + "; ".join(problems)
