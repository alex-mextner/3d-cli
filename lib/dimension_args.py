"""dimension_args.py — the option grammar of `3d dimension`, in one stdlib-only place.

The command module (`commands/dimension.py`, imported on every `3d` call, so no heavy imports)
validates with these before it execs the renderer, and the renderer (`dimension_render.py`)
parses with the SAME functions — the two layers cannot drift apart.

Each parser raises ValueError with a message naming what was expected.
"""

from __future__ import annotations

import math

Point = tuple[float, float, float]
MIN_WIDTH_PX = 640
MAX_WIDTH_PX = 8192  # a typo like 128000 would allocate a ~20 GB canvas


def finite(raw: str, what: str) -> float:
    """A finite float: `inf` and `nan` parse as floats but are never valid sizes."""
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{what}: {raw!r} is not a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{what}: {raw!r} is not a finite number")
    return value


def positive(raw: str, what: str) -> float:
    value = finite(raw, what)
    if value <= 0:
        raise ValueError(f"{what}: {raw!r} must be > 0")
    return value


def parse_bed(raw: str) -> tuple[float, float]:
    """`WIDTHxDEPTH` in mm, e.g. 220x220."""
    parts = raw.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"--bed {raw!r}: expected WIDTHxDEPTH in mm, e.g. 220x220")
    return positive(parts[0], "--bed width"), positive(parts[1], "--bed depth")


def parse_point(raw: str, spec: str) -> Point:
    nums = raw.split(",")
    if len(nums) != 3:
        raise ValueError(
            f"--dim {spec!r}: point {raw!r} needs three comma-separated numbers"
        )
    x, y, z = (finite(n, f"--dim {spec!r}") for n in nums)
    return x, y, z


def parse_dim(spec: str) -> tuple[str, Point, Point]:
    """`NAME:x,y,z:x,y,z` — a labelled distance between two model-space points."""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"--dim {spec!r}: expected name:x,y,z:x,y,z")
    name = parts[0].strip()
    if not name:
        raise ValueError(f"--dim {spec!r}: empty name")
    return name, parse_point(parts[1], spec), parse_point(parts[2], spec)


def parse_width(raw: str) -> int:
    if not raw.isdigit() or not MIN_WIDTH_PX <= int(raw) <= MAX_WIDTH_PX:
        raise ValueError(
            f"--width {raw!r}: expected an integer {MIN_WIDTH_PX}..{MAX_WIDTH_PX}"
        )
    return int(raw)
