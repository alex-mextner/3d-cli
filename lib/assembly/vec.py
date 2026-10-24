"""vec.py — parse and validate the vectors an assembly spec or a CLI flag declares (stdlib only).

WHAT: a direction is `+X|-Y|Z|...` (the signed axes), a 3-number list, or `x,y,z`; a point is a 3-number list.
  Everything is validated BEFORE any geometry is touched: a zero, non-finite or malformed vector is an
  `InvalidArgument` that lists the accepted forms (this is the #553 requirement "invalid vectors fail first").

ACCESSED VIA: `assembly.spec` (the YAML spec), `fit_check --open` (declared insertion axis, #553) and the
  `3d assembly` flags.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from errors import InvalidArgument

Vec3 = tuple[float, float, float]

AXIS_NAMES: dict[str, Vec3] = {
    "+X": (1.0, 0.0, 0.0),
    "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0),
    "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0),
    "-Z": (0.0, 0.0, -1.0),
}
_ALIASES = {"X": "+X", "Y": "+Y", "Z": "+Z"}
ACCEPTED_DIRECTIONS = [
    "+X",
    "-X",
    "+Y",
    "-Y",
    "+Z",
    "-Z",
    "[x, y, z] (any non-zero vector)",
    "x,y,z",
]


def _finite_numbers(
    raw: Sequence[Any], what: str, command: str, size: int = 3, accepted: Sequence[str] | None = None
) -> Vec3:
    if len(raw) != size:
        raise InvalidArgument(
            what, repr(list(raw)), list(accepted or [f"{size} numbers"]), command=command
        )
    out: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            raise InvalidArgument(
                what, repr(list(raw)), list(accepted or [f"{size} finite numbers"]), command=command
            )
        try:
            value = float(item)
        except ValueError as exc:
            raise InvalidArgument(
                what, repr(list(raw)), list(accepted or [f"{size} finite numbers"]), command=command
            ) from exc
        if not math.isfinite(value):
            raise InvalidArgument(
                what, repr(list(raw)), list(accepted or [f"{size} finite numbers"]), command=command
            )
        out.append(value)
    return (out[0], out[1], out[2])


def parse_point(value: Any, what: str, *, command: str = "assembly") -> Vec3:
    """A 3-number point (list/tuple or `x,y,z`)."""
    if isinstance(value, str):
        return _finite_numbers([p.strip() for p in value.split(",")], what, command)
    if isinstance(value, Sequence):
        return _finite_numbers(value, what, command)
    raise InvalidArgument(what, repr(value), ["[x, y, z]", "x,y,z"], command=command)


def parse_direction(value: Any, what: str, *, command: str = "assembly") -> Vec3:
    """A unit direction from `+X`..`-Z`, `[x, y, z]` or `x,y,z`; zero / non-finite / malformed is refused."""
    if isinstance(value, str):
        key = value.strip().upper()
        key = _ALIASES.get(key, key)
        if key in AXIS_NAMES:
            return AXIS_NAMES[key]
        if "," not in value:
            raise InvalidArgument(what, value, ACCEPTED_DIRECTIONS, command=command)
        inner = value.strip().strip("[]()")
        raw = _finite_numbers([p.strip() for p in inner.split(",")], what, command, 3, ACCEPTED_DIRECTIONS)
    elif isinstance(value, Sequence):
        raw = _finite_numbers(value, what, command, 3, ACCEPTED_DIRECTIONS)
    else:
        raise InvalidArgument(what, repr(value), ACCEPTED_DIRECTIONS, command=command)
    norm = math.sqrt(sum(c * c for c in raw))
    if norm < 1e-9:
        raise InvalidArgument(
            what,
            repr(list(raw)),
            ACCEPTED_DIRECTIONS,
            command=command,
            extra="A zero vector has no direction.",
        )
    return (raw[0] / norm, raw[1] / norm, raw[2] / norm)


def format_direction(vec: Vec3) -> str:
    """`+Y` for a signed axis, else `[x, y, z]` rounded to 4 decimals."""
    for name, axis in AXIS_NAMES.items():
        if all(abs(a - b) < 1e-9 for a, b in zip(vec, axis, strict=True)):
            return name
    return "[" + ", ".join(f"{round(c, 4) + 0.0:g}" for c in vec) + "]"


def rotation_taking(src: Vec3, dst: Vec3) -> tuple[Vec3, Vec3, Vec3]:
    """The 3x3 rotation (rows) that turns the unit vector `src` into `dst` with the smallest turn."""
    cx = src[1] * dst[2] - src[2] * dst[1]
    cy = src[2] * dst[0] - src[0] * dst[2]
    cz = src[0] * dst[1] - src[1] * dst[0]
    c = src[0] * dst[0] + src[1] * dst[1] + src[2] * dst[2]
    if c > 1 - 1e-12:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    if c < -1 + 1e-12:  # opposite: half a turn about any axis normal to src
        ax = (1.0, 0.0, 0.0) if abs(src[0]) < 0.9 else (0.0, 1.0, 0.0)
        nx = src[1] * ax[2] - src[2] * ax[1]
        ny = src[2] * ax[0] - src[0] * ax[2]
        nz = src[0] * ax[1] - src[1] * ax[0]
        n = math.sqrt(nx * nx + ny * ny + nz * nz)
        ux, uy, uz = nx / n, ny / n, nz / n
        return (
            (2 * ux * ux - 1, 2 * ux * uy, 2 * ux * uz),
            (2 * ux * uy, 2 * uy * uy - 1, 2 * uy * uz),
            (2 * ux * uz, 2 * uy * uz, 2 * uz * uz - 1),
        )
    k = 1.0 / (1.0 + c)  # Rodrigues for unit vectors: I + [v]x + [v]x^2 / (1 + c)
    return (
        (1 - k * (cy * cy + cz * cz), -cz + k * cx * cy, cy + k * cx * cz),
        (cz + k * cx * cy, 1 - k * (cx * cx + cz * cz), -cx + k * cy * cz),
        (-cy + k * cx * cz, cx + k * cy * cz, 1 - k * (cx * cx + cy * cy)),
    )
