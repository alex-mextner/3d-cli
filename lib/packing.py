"""Deterministic 2D bed layout plans for `3d pack`.

This is a bounded skeleton, not a nesting optimizer: it validates explicit part
dimensions and places expanded part quantities left-to-right in rows, top-to-bottom.
The simple shelf layout is deterministic so downstream commands and tests can depend
on stable coordinates while a fuller nesting solver is developed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from errors import UsageError

_EPS = 1e-9


@dataclass(slots=True)
class PartSpec:
    """One requested part footprint in millimeters."""

    name: str
    width: float
    depth: float
    quantity: int = 1

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise UsageError(
                "part name cannot be empty",
                command="pack",
                remediation=["Use --part name=WxD[:qty], for example --part bracket=60x40:2."],
            )
        self.width = _positive_float(self.width, "part width")
        self.depth = _positive_float(self.depth, "part depth")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise UsageError(
                f"part {self.name!r}: quantity must be a positive integer, got {self.quantity!r}",
                command="pack",
                remediation=["Use --part name=WxD[:qty], for example --part bracket=60x40:2."],
            )


@dataclass(frozen=True, slots=True)
class Placement:
    """One physical copy placed on the bed."""

    name: str
    index: int
    x: float
    y: float
    width: float
    depth: float
    rotated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "depth": self.depth,
            "rotated": self.rotated,
        }


@dataclass(frozen=True, slots=True)
class BedLayout:
    """A deterministic layout plan for a rectangular print bed."""

    bed_width: float
    bed_depth: float
    gap: float
    placements: list[Placement]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bed": {"width": self.bed_width, "depth": self.bed_depth},
            "gap": self.gap,
            "placements": [p.to_dict() for p in self.placements],
        }


def _positive_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UsageError(
            f"{label} must be a number, got {value!r}",
            command="pack",
            remediation=["Dimensions are millimeters and must be positive numbers."],
        ) from None
    if not math.isfinite(number):
        raise UsageError(
            f"{label} must be finite, got {number}",
            command="pack",
            remediation=["Use finite millimeter values; inf and nan are not valid dimensions."],
        )
    if number <= 0:
        raise UsageError(
            f"{label} must be positive, got {number:g}",
            command="pack",
            remediation=["Dimensions are millimeters and must be greater than zero."],
        )
    return number


def _non_negative_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UsageError(
            f"{label} must be a number, got {value!r}",
            command="pack",
            remediation=["Gap is millimeters and must be zero or greater."],
        ) from None
    if not math.isfinite(number):
        raise UsageError(
            f"{label} must be finite, got {number}",
            command="pack",
            remediation=["Use a finite millimeter clearance; inf and nan are not valid gaps."],
        )
    if number < 0:
        raise UsageError(
            f"{label} must be zero or greater, got {number:g}",
            command="pack",
            remediation=["Use --gap 0 for touching bounding boxes, or a positive clearance."],
        )
    return number


def _fits(width: float, depth: float, bed_width: float, bed_depth: float) -> bool:
    return _lte(width, bed_width) and _lte(depth, bed_depth)


def _lte(left: float, right: float) -> bool:
    return left <= right or math.isclose(left, right, rel_tol=_EPS, abs_tol=_EPS)


def _bed_orientation(
    part: PartSpec,
    *,
    bed_width: float,
    bed_depth: float,
    allow_rotate: bool,
) -> tuple[float, float, bool]:
    if _fits(part.width, part.depth, bed_width, bed_depth):
        return part.width, part.depth, False
    if allow_rotate and _fits(part.depth, part.width, bed_width, bed_depth):
        return part.depth, part.width, True
    raise UsageError(
        f"part {part.name!r} ({part.width:g}x{part.depth:g} mm) does not fit bed "
        f"{bed_width:g}x{bed_depth:g} mm",
        command="pack",
        remediation=["Reduce the part dimensions, choose a larger bed, or allow rotation."],
    )


def _row_orientation(
    part: PartSpec,
    *,
    bed_width: float,
    bed_depth: float,
    row_y: float,
    row_height: float,
    x: float,
    allow_rotate: bool,
) -> tuple[float, float, bool] | None:
    candidates = [(part.width, part.depth, False)]
    if allow_rotate:
        candidates.append((part.depth, part.width, True))

    for width, depth, rotated in candidates:
        if (
            _lte(x + width, bed_width)
            and _lte(row_y + max(row_height, depth), bed_depth)
            and _fits(width, depth, bed_width, bed_depth)
        ):
            return width, depth, rotated
    return None


def plan_bed_layout(
    parts: list[PartSpec],
    *,
    bed_width: float,
    bed_depth: float,
    gap: float = 0.0,
    allow_rotate: bool = True,
) -> BedLayout:
    """Place parts on a rectangular bed with a deterministic shelf layout."""
    bed_width = _positive_float(bed_width, "bed width")
    bed_depth = _positive_float(bed_depth, "bed depth")
    gap = _non_negative_float(gap, "gap")
    if not parts:
        raise UsageError(
            "at least one --part is required",
            command="pack",
            remediation=["Add a part footprint, for example --part bracket=60x40."],
        )

    placements: list[Placement] = []
    cursor_x = 0.0
    row_y = 0.0
    row_height = 0.0

    for part in parts:
        _bed_orientation(part, bed_width=bed_width, bed_depth=bed_depth, allow_rotate=allow_rotate)
        for index in range(1, part.quantity + 1):
            x = cursor_x + gap if cursor_x > 0 else 0.0
            oriented = _row_orientation(
                part,
                bed_width=bed_width,
                bed_depth=bed_depth,
                row_y=row_y,
                row_height=row_height,
                x=x,
                allow_rotate=allow_rotate,
            )
            if oriented is None:
                row_y += row_height + gap if row_height > 0 else 0.0
                cursor_x = 0.0
                row_height = 0.0
                x = 0.0
                oriented = _row_orientation(
                    part,
                    bed_width=bed_width,
                    bed_depth=bed_depth,
                    row_y=row_y,
                    row_height=row_height,
                    x=x,
                    allow_rotate=allow_rotate,
                )
            if oriented is None:
                raise UsageError(
                    f"part {part.name!r} copy {index} cannot be placed on bed "
                    f"{bed_width:g}x{bed_depth:g} mm with gap {gap:g} mm",
                    command="pack",
                    remediation=[
                        "Use a larger bed, reduce quantities, lower --gap, or split the job into multiple plates."
                    ],
                )

            width, depth, rotated = oriented
            placements.append(
                Placement(
                    name=part.name,
                    index=index,
                    x=x,
                    y=row_y,
                    width=width,
                    depth=depth,
                    rotated=rotated,
                )
            )
            cursor_x = x + width
            row_height = max(row_height, depth)

    return BedLayout(bed_width=bed_width, bed_depth=bed_depth, gap=gap, placements=placements)
