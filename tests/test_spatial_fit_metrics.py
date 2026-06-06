from __future__ import annotations

import math

import numpy as np

from spatial_fit_metrics import binary_contour, boundary_sdf_loss, signed_boundary_distance, spatial_fit_metrics


def _square(x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    mask = np.zeros((32, 32), dtype=np.uint8)
    mask[y0:y1, x0:x1] = 1
    return mask


def test_binary_contour_returns_boundary_only() -> None:
    mask = _square(8, 8, 16, 16)
    contour = binary_contour(mask)
    assert int(contour.sum()) == 28
    assert not bool(contour[10, 10])
    assert bool(contour[8, 8])


def test_spatial_metrics_identical_masks_are_perfect() -> None:
    mask = _square(8, 8, 20, 20)
    metrics = spatial_fit_metrics(mask, mask)
    assert metrics.area_iou == 1.0
    assert metrics.edge_f1_at_2 == 1.0
    assert metrics.edge_chamfer_px == 0.0
    assert metrics.boundary_sdf_loss_px == 0.0
    assert metrics.hausdorff_p95_px == 0.0
    assert metrics.coverage_ratio == 1.0
    assert metrics.centroid_delta_px == 0.0
    assert metrics.spatial_warning is None


def test_spatial_metrics_warn_on_crop_scale_mismatch() -> None:
    reference = _square(8, 8, 20, 20)
    render = _square(0, 0, 31, 31)
    metrics = spatial_fit_metrics(render, reference)
    assert metrics.area_iou < 0.20
    assert metrics.coverage_ratio > 5.0
    assert metrics.render_touches_border
    assert not metrics.reference_touches_border
    assert metrics.spatial_warning is not None
    assert "coverage_ratio" in metrics.spatial_warning
    assert math.isfinite(metrics.edge_chamfer_px)


def test_spatial_metrics_empty_masks_are_finite_json_values() -> None:
    empty = np.zeros((32, 32), dtype=np.uint8)
    render = _square(8, 8, 20, 20)

    both_empty = spatial_fit_metrics(empty, empty)
    assert math.isfinite(both_empty.edge_chamfer_px)
    assert math.isfinite(both_empty.boundary_sdf_loss_px)
    assert math.isfinite(both_empty.hausdorff_p95_px)
    assert math.isfinite(both_empty.coverage_ratio)
    assert math.isfinite(both_empty.centroid_delta_px)
    assert both_empty.spatial_warning is not None
    assert "empty reference mask" in both_empty.spatial_warning
    assert "empty render mask" in both_empty.spatial_warning

    missing_ref = spatial_fit_metrics(render, empty)
    assert math.isfinite(missing_ref.edge_chamfer_px)
    assert math.isfinite(missing_ref.boundary_sdf_loss_px)
    assert math.isfinite(missing_ref.hausdorff_p95_px)
    assert math.isfinite(missing_ref.coverage_ratio)
    assert missing_ref.spatial_warning is not None
    assert "empty reference mask" in missing_ref.spatial_warning


def test_boundary_sdf_loss_increases_with_offset() -> None:
    reference = _square(8, 8, 20, 20)
    near = _square(10, 8, 22, 20)
    far = _square(14, 8, 26, 20)
    assert boundary_sdf_loss(near, reference) < boundary_sdf_loss(far, reference)


def test_signed_boundary_distance_has_expected_signs() -> None:
    mask = _square(8, 8, 20, 20)
    sdf = signed_boundary_distance(mask)
    assert sdf[14, 14] < 0.0
    assert sdf[4, 4] > 0.0
    assert sdf[8, 8] == 0.0
