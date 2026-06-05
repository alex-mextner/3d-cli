from __future__ import annotations

import pytest

from mask_geometry import mask_metadata


def test_mask_metadata_reports_bbox_centroid_and_coverage() -> None:
    mask = [[0 for _ in range(12)] for _ in range(10)]
    for y in range(2, 6):
        for x in range(3, 9):
            mask[y][x] = 255

    meta = mask_metadata(mask)

    assert meta["coverage_pct"] == pytest.approx(20.0)
    assert meta["bbox_xywh"] == (3, 2, 6, 4)
    assert meta["centroid_xy"] == pytest.approx((5.5, 3.5))


def test_mask_metadata_handles_empty_subject_mask() -> None:
    meta = mask_metadata([[0 for _ in range(9)] for _ in range(8)])

    assert meta["coverage_pct"] == pytest.approx(0.0)
    assert meta["bbox_xywh"] is None
    assert meta["centroid_xy"] is None


def test_mask_metadata_handles_empty_image() -> None:
    meta = mask_metadata([])

    assert meta["coverage_pct"] == pytest.approx(0.0)
    assert meta["bbox_xywh"] is None
    assert meta["centroid_xy"] is None


def test_mask_metadata_uses_numpy_like_arrays_without_optional_image_deps() -> None:
    np = pytest.importorskip("numpy")
    mask = np.zeros((10, 12), dtype=np.uint8)
    mask[2:6, 3:9] = 255

    meta = mask_metadata(mask)

    assert meta["coverage_pct"] == pytest.approx(20.0)
    assert meta["bbox_xywh"] == (3, 2, 6, 4)
    assert meta["centroid_xy"] == pytest.approx((5.5, 3.5))
