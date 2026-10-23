"""bed_fit.py — fit a footprint onto a rectangular bed by Z-rotation + translation.

WHAT: pure-stdlib 2D geometry for `3d retarget`: a convex hull of a point cloud, and a search
  for the smallest Z-rotation that lets that hull sit inside a rectangular bed with a margin,
  centred. No I/O, no slicer, no numpy.

WHY: a project 3MF carries absolute build positions for its source printer; moving it to a
  smaller bed needs a re-centre and, sometimes, a spin around Z. The fit lives apart from the
  3MF plumbing so it can be tested on plain point lists (including the "cannot fit" case).

Examples:
  hull = convex_hull(points)
  place = fit_to_bed(hull, Bed(0, 0, 220, 220), margin=5)   # None when it cannot fit
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

Point = tuple[float, float]

# Rotation angles tried, in degrees over a half turn (a rectangle fit is 180°-periodic).
_SCAN_STEP_DEG = 0.5
# A hull with more vertices than this is reduced to its extreme points per scan direction
# before the angle scan; that keeps the scan O(angles x 2 * 180 / step) for round parts
# (a cylinder's hull has thousands of vertices) and is exact at every scanned angle.
_MAX_EXACT_HULL = 400
_EPS = 1e-9


@dataclass(frozen=True)
class Bed:
    """Axis-aligned rectangular bed (mm): `[min_x, max_x] x [min_y, max_y]`."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def depth(self) -> float:
        return self.max_y - self.min_y

    @property
    def centre(self) -> Point:
        return ((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)


def rotate(point: Point, angle_deg: float, pivot: Point = (0.0, 0.0)) -> Point:
    """`point` turned CCW by `angle_deg` about `pivot`."""
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    x, y = point[0] - pivot[0], point[1] - pivot[1]
    return (x * c - y * s + pivot[0], x * s + y * c + pivot[1])


@dataclass(frozen=True)
class Placement:
    """Rotate by `angle_deg` (CCW, about `pivot`) then translate by `(dx, dy)`.

    `size` is the (width, depth) of the placed footprint's bounding box.
    """

    angle_deg: float
    pivot: Point
    dx: float
    dy: float
    size: tuple[float, float]

    def apply(self, point: Point) -> Point:
        x, y = rotate(point, self.angle_deg, self.pivot)
        return (x + self.dx, y + self.dy)


class Fit(NamedTuple):
    """One rotation tried: its angle, the footprint's bbox at it, and the room left.

    `slack` is the spare mm of usable bed in the tighter direction (negative = overshoot), so
    the largest margin that would still fit is `margin + slack / 2`.
    """

    angle_deg: float
    size: tuple[float, float]
    slack: float


def convex_hull(points: Sequence[Point]) -> list[Point]:
    """Andrew's monotone chain; counter-clockwise, no repeated end point."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def bounds(points: Sequence[Point]) -> tuple[float, float, float, float]:
    """`(min_x, min_y, max_x, max_y)` of a non-empty point list."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _rotated_size(points: Sequence[Point], angle_deg: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    xs = [x * c - y * s for x, y in points]
    ys = [x * s + y * c for x, y in points]
    return (max(xs) - min(xs), max(ys) - min(ys))


def _extreme_points(hull: Sequence[Point]) -> list[Point]:
    """For a CCW convex hull: the vertex extreme in each direction k * step over a full turn.

    The bbox of the hull rotated by any multiple of `step` is determined by these vertices,
    so the reduced set gives the exact size at every scanned angle. A pointer walks the hull
    once, so this is O(H + directions), not O(H x directions).
    """
    n = len(hull)
    directions = int(360 / _SCAN_STEP_DEG)
    first = max(range(n), key=lambda i: hull[i][0])  # direction 0°: +X
    keep: dict[int, None] = {first: None}
    idx = first
    for k in range(1, directions):
        dx = math.cos(math.radians(k * _SCAN_STEP_DEG))
        dy = math.sin(math.radians(k * _SCAN_STEP_DEG))
        while True:
            nxt = (idx + 1) % n
            if hull[nxt][0] * dx + hull[nxt][1] * dy > hull[idx][0] * dx + hull[idx][1] * dy + _EPS:
                idx = nxt
            else:
                break
        keep[idx] = None
    return [hull[i] for i in sorted(keep)]


def _candidate_angles(hull: Sequence[Point]) -> list[float]:
    """Half-turn grid plus, for a hull small enough to be exact, the edge-aligned angles
    (where a bbox side lies flush with a hull edge: the minima of the bbox area)."""
    angles = {round(i * _SCAN_STEP_DEG, 6) for i in range(int(180 / _SCAN_STEP_DEG))}
    if len(hull) <= _MAX_EXACT_HULL:
        n = len(hull)
        for i in range(n):
            a, b = hull[i], hull[(i + 1) % n]
            edge = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
            # Rotating by -edge lays this edge along +X; +90° lays it along Y.
            angles.add(round((-edge) % 180, 6))
            angles.add(round((90 - edge) % 180, 6))
    return sorted(angles)


def _fits(hull: Sequence[Point], bed: Bed, margin: float) -> list[Fit]:
    """Every candidate rotation (normalised to -90..90°) with its bbox and slack."""
    avail_w, avail_d = bed.width - 2 * margin, bed.depth - 2 * margin
    scan = hull if len(hull) <= _MAX_EXACT_HULL else _extreme_points(hull)
    out: list[Fit] = []
    for angle in _candidate_angles(hull):
        size = _rotated_size(scan, angle)
        turn = angle - 180.0 if angle > 90.0 else angle
        out.append(Fit(turn, size, min(avail_w - size[0], avail_d - size[1])))
    return out


def closest_fit(hull: Sequence[Point], bed: Bed, margin: float) -> Fit:
    """The Z-rotation that comes closest to fitting (the most slack; negative = overshoot)."""
    return max(_fits(hull, bed, margin), key=lambda f: (f.slack, -abs(f.angle_deg)))


def fit_to_bed(hull: Sequence[Point], bed: Bed, margin: float) -> Placement | None:
    """Rotate-on-Z by the SMALLEST angle that fits and centre `hull` on `bed` with `margin`.

    The original orientation is kept when it already fits. Returns None when no Z-rotation
    fits.
    """
    if not hull:
        raise ValueError("fit_to_bed needs a non-empty footprint")
    if bed.width - 2 * margin <= 0 or bed.depth - 2 * margin <= 0:
        return None
    fitting = [f for f in _fits(hull, bed, margin) if f.slack >= -_EPS]
    if not fitting:
        return None
    best = min(fitting, key=lambda f: (abs(f.angle_deg), -f.slack))

    min_x, min_y, max_x, max_y = bounds(hull)
    pivot = ((min_x + max_x) / 2, (min_y + max_y) / 2)
    rx0, ry0, rx1, ry1 = bounds([rotate(p, best.angle_deg, pivot) for p in hull])
    cx, cy = bed.centre
    return Placement(
        best.angle_deg,
        pivot,
        cx - (rx0 + rx1) / 2,
        cy - (ry0 + ry1) / 2,
        (rx1 - rx0, ry1 - ry0),
    )
