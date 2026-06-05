from __future__ import annotations

from collections.abc import Iterable
from typing import Any, SupportsInt, TypedDict, cast


class MaskMetadata(TypedDict):
    coverage_pct: float
    bbox_xywh: tuple[int, int, int, int] | None
    centroid_xy: tuple[float, float] | None


def mask_metadata(mask: object) -> MaskMetadata:
    """Return deterministic geometry metadata for a binary subject mask."""
    fast = _numpy_like_mask_metadata(mask)
    if fast is not None:
        return fast

    return _iterable_mask_metadata(mask)


def _numpy_like_mask_metadata(mask: object) -> MaskMetadata | None:
    try:
        subject = cast(Any, mask) > 0
        total = int(subject.size)
        count = int(subject.sum())
        coverage = 100.0 * count / total if total > 0 else 0.0
        if count == 0:
            return {"coverage_pct": coverage, "bbox_xywh": None, "centroid_xy": None}

        coords = subject.nonzero()
        if len(coords) < 2:
            return None
        ys = coords[-2]
        xs = coords[-1]
        x0 = int(xs.min())
        y0 = int(ys.min())
        x1 = int(xs.max())
        y1 = int(ys.max())
        bbox = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
        centroid = (float(xs.mean()), float(ys.mean()))
        return {"coverage_pct": coverage, "bbox_xywh": bbox, "centroid_xy": centroid}
    except (AttributeError, TypeError, ValueError):
        return None


def _iterable_mask_metadata(mask: object) -> MaskMetadata:
    total = 0
    count = 0
    min_x: int | None = None
    min_y: int | None = None
    max_x: int | None = None
    max_y: int | None = None
    sum_x = 0
    sum_y = 0

    rows = cast(Iterable[Iterable[object]], mask)
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            total += 1
            if int(cast(SupportsInt, value)) <= 0:
                continue
            count += 1
            sum_x += x
            sum_y += y
            min_x = x if min_x is None else min(min_x, x)
            min_y = y if min_y is None else min(min_y, y)
            max_x = x if max_x is None else max(max_x, x)
            max_y = y if max_y is None else max(max_y, y)

    coverage = 100.0 * count / total if total > 0 else 0.0
    if count == 0 or min_x is None or min_y is None or max_x is None or max_y is None:
        return {"coverage_pct": coverage, "bbox_xywh": None, "centroid_xy": None}

    bbox = (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
    centroid = (sum_x / count, sum_y / count)
    return {"coverage_pct": coverage, "bbox_xywh": bbox, "centroid_xy": centroid}
