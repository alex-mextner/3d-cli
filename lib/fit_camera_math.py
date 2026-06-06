from __future__ import annotations

import math
from typing import Sequence

MIN_EDGE_F1_PROOF = 0.35
MIN_EDGE_F1_OK = 0.50
MIN_AREA_IOU_PROOF = 0.65
MIN_BBOX_IOU_PROOF = 0.70
MAX_HAUSDORFF_P95_PROOF_PX = 32.0


def cam_from_params(p: Sequence[float], center: Sequence[float]) -> list[float]:
    if len(p) == 5:
        az, el, dist, panx, panz = p
        pany = 0.0
    else:
        az, el, dist, panx, pany, panz = p
    cx, cy, cz = center[0] + panx, center[1] + pany, center[2] + panz
    ar, er = math.radians(az), math.radians(el)
    ex = cx + dist * math.cos(er) * math.cos(ar)
    ey = cy + dist * math.cos(er) * math.sin(ar)
    ez = cz + dist * math.sin(er)
    return [ex, ey, ez, cx, cy, cz]


def stratified_samples(samples: Sequence[list[float]], budget: int) -> list[list[float]]:
    if budget <= 0:
        return []
    if budget >= len(samples):
        return list(samples)
    if budget == 1:
        return [samples[0]]
    last = len(samples) - 1
    indexes = [round(i * last / (budget - 1)) for i in range(budget)]
    return [samples[i] for i in indexes]


def fit_status_from_spatial_metrics(
    metrics: dict[str, float | bool | str | None],
) -> tuple[str, list[str]]:
    """Classify whether a fitted camera is reusable proof or diagnostic-only.

    Area IoU alone is not enough: a crop/scale cheat can overlap lots of filled area
    while missing the visible boundary. The status is intentionally conservative and
    follows the contour-first proof contract used by the roadmap.
    """
    warnings: list[str] = []

    def metric_float(name: str, default: float) -> float:
        value = metrics.get(name)
        return default if value is None else float(value)

    edge_f1 = metric_float("edge_f1@4", 0.0)
    area_iou = metric_float("area_iou", 0.0)
    bbox_iou = metric_float("bbox_iou", 0.0)
    p95 = metric_float("hausdorff_p95_px", 9999.0)
    chamfer = metric_float("edge_chamfer_px", 9999.0)
    warning = metrics.get("spatial_warning")
    if isinstance(warning, str) and warning:
        warnings.append(warning)
    if edge_f1 < MIN_EDGE_F1_OK:
        warnings.append(f"edge_f1@4={edge_f1:.3f} below ok threshold {MIN_EDGE_F1_OK:.2f}")
    if p95 > 18.0:
        warnings.append(f"hausdorff_p95_px={p95:.1f} is high")
    if area_iou < MIN_AREA_IOU_PROOF:
        warnings.append(f"area_iou={area_iou:.3f} below proof threshold {MIN_AREA_IOU_PROOF:.2f}")
    if bbox_iou < MIN_BBOX_IOU_PROOF:
        warnings.append(f"bbox_iou={bbox_iou:.3f} below proof threshold {MIN_BBOX_IOU_PROOF:.2f}")
    if bool(metrics.get("render_touches_border")) and not bool(metrics.get("reference_touches_border")):
        warnings.append("render touches border while reference does not")
    if (
        edge_f1 < MIN_EDGE_F1_PROOF
        or area_iou < MIN_AREA_IOU_PROOF
        or bbox_iou < MIN_BBOX_IOU_PROOF
        or p95 > MAX_HAUSDORFF_P95_PROOF_PX
    ):
        return "failed", warnings
    if warnings:
        return "warning", warnings
    if chamfer > 6.0:
        return "warning", [f"edge_chamfer_px={chamfer:.1f} is high"]
    return "ok", []
