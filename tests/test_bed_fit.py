"""bed_fit: hull + Z-rotation fit of a footprint onto a rectangular bed (used by `3d retarget`)."""
from __future__ import annotations

import math

import pytest

from slicing.bed_fit import Bed, Point, bounds, closest_fit, convex_hull, fit_to_bed

BED = Bed(0, 0, 220, 220)


def rect(w: float, d: float, angle_deg: float = 0.0, at: Point = (0.0, 0.0)) -> list[Point]:
    """Corners of a w x d rectangle centred on `at`, spun `angle_deg` CCW."""
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return [
        (at[0] + x * c - y * s, at[1] + x * s + y * c)
        for x, y in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))
    ]


def test_convex_hull_drops_interior_and_collinear_points() -> None:
    cloud = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (5.0, 5.0), (5.0, 0.0), (2.0, 7.0)]
    assert sorted(convex_hull(cloud)) == [(0.0, 0.0), (0.0, 10.0), (10.0, 0.0), (10.0, 10.0)]


def test_fitting_part_is_only_centred_not_rotated() -> None:
    place = fit_to_bed(rect(100, 40, at=(500.0, -300.0)), BED, margin=5)
    assert place is not None
    assert place.angle_deg == 0.0
    placed = [place.apply(p) for p in rect(100, 40, at=(500.0, -300.0))]
    x0, y0, x1, y1 = bounds(placed)
    assert (x0 + x1) / 2 == pytest.approx(110) and (y0 + y1) / 2 == pytest.approx(110)
    assert place.size == pytest.approx((100, 40))


def test_too_long_along_x_is_turned_to_fit() -> None:
    long_thin = rect(240, 20)  # 240 > the 210 usable on a 220 bed with 5 mm margin
    place = fit_to_bed(long_thin, BED, margin=5)
    assert place is not None and place.angle_deg != 0.0
    x0, y0, x1, y1 = bounds([place.apply(p) for p in long_thin])
    assert x0 >= 5 - 1e-6 and x1 <= 215 + 1e-6 and y0 >= 5 - 1e-6 and y1 <= 215 + 1e-6


def test_slightly_spun_part_is_turned_back_only_as_far_as_needed() -> None:
    skewed = rect(205, 100, angle_deg=10)  # its bbox is wider than the 210 usable, the part itself is not
    place = fit_to_bed(skewed, BED, margin=5)
    assert place is not None
    assert place.size[0] <= 210 + 1e-6 and place.size[1] <= 210 + 1e-6
    assert 0 < abs(place.angle_deg) < 10  # a partial turn, not a full straighten to 0 or 10 degrees


def test_nothing_fits_returns_none_and_reports_the_overshoot() -> None:
    huge = rect(300, 300)
    assert fit_to_bed(huge, BED, margin=5) is None
    angle, size, slack = closest_fit(huge, BED, margin=5)
    assert slack < 0 and max(size) >= 300 - 1e-6 and 0 <= angle < 180


def test_margin_is_honoured_exactly() -> None:
    plate = rect(214, 214)
    assert fit_to_bed(plate, BED, margin=5) is None
    assert fit_to_bed(plate, BED, margin=3) is not None
    assert fit_to_bed(plate, BED, margin=110) is None  # no usable bed left at all


def test_empty_footprint_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        fit_to_bed([], BED, margin=5)


def test_the_smallest_rotation_that_fits_is_chosen_not_the_roomiest() -> None:
    part = rect(209, 150, angle_deg=4)  # bbox 219 wide: too big by 9 mm, fixed by straightening 4 degrees
    place = fit_to_bed(part, BED, margin=5)
    assert place is not None
    assert abs(place.angle_deg) == pytest.approx(4, abs=0.5)


def test_rotation_is_reported_as_the_short_way_round() -> None:
    place = fit_to_bed(rect(240, 20), BED, margin=5)  # 45 degrees either way; never more than a quarter turn
    assert place is not None and -90 <= place.angle_deg <= 90


def test_round_part_with_a_huge_hull_is_fast_and_exact() -> None:
    circle = [(100 * math.cos(2 * math.pi * i / 30000), 100 * math.sin(2 * math.pi * i / 30000)) for i in range(30000)]
    hull = convex_hull(circle)
    assert len(hull) > 10000
    place = fit_to_bed(hull, BED, margin=5)  # a 200 mm disc on a 210 mm usable bed
    assert place is not None and place.angle_deg == 0.0
    assert fit_to_bed(hull, BED, margin=11) is None  # 198 mm usable < 200 mm disc
    assert closest_fit(hull, BED, margin=11).slack == pytest.approx(-2, abs=0.01)


def test_closest_fit_reports_angle_size_and_slack_by_name() -> None:
    best = closest_fit(rect(300, 300), BED, margin=5)
    assert best.slack < 0 and best.size[0] >= 300 - 1e-6 and 0 <= abs(best.angle_deg) <= 90
