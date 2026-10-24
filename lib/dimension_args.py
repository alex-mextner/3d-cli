"""dimension_args.py — the option grammar of `3d dimension`, in one stdlib-only place.

The command module (`commands/dimension.py`, imported on every `3d` call, so no heavy imports)
validates with these before it execs the renderer, and the renderer (`dimension_render.py`)
parses with the SAME functions — the two layers cannot drift apart.

Each parser raises ValueError with a message naming what was expected.
"""

from __future__ import annotations

import json
import math
import re
from typing import NamedTuple

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


def parse_define(raw: str) -> str:
    """`NAME=VALUE` — an OpenSCAD `-D` override; VALUE is passed to openscad untouched."""
    name, eq, value = raw.partition("=")
    if not eq or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not value:
        raise ValueError(f"-D {raw!r}: expected NAME=VALUE, e.g. standin=true")
    return raw


class PartSpec(NamedTuple):
    """A parsed `--part`: which part (`selector`), its new `name` and/or `role`."""

    selector: str
    name: str | None
    role: str | None


def parse_part(spec: str) -> PartSpec:
    """`SELECTOR=NAME[:ROLE]` — rename and/or set the role of one part.

    SELECTOR is a part name or a colour (#RGB / #RRGGBB, normalised to upper-case #RRGGBB); NAME may be
    empty when only the ROLE changes (`#2a62c9=:stand_in`); a NAME holds no ':'.
    """
    from parts import ROLES  # lazy: this module is imported on every `3d` call

    selector, eq, rest = spec.partition("=")
    selector = selector.strip()
    if not eq or not selector:
        raise ValueError(
            f"--part {spec!r}: expected SELECTOR=NAME[:ROLE], e.g. '#2a62c9=PCB:stand_in'"
        )
    if selector.startswith("#"):
        digits = selector[1:]
        if not re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", digits):
            raise ValueError(
                f"--part {spec!r}: {selector!r} is not a #RGB or #RRGGBB colour"
            )
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        selector = "#" + digits.upper()
    name, sep, tail = rest.rpartition(":")
    if not sep:
        name, tail = rest, ""
    role = tail.strip().lower().replace("-", "_")
    if sep and role not in ROLES:
        raise ValueError(
            f"--part {spec!r}: role {tail!r} is not one of {', '.join(ROLES)}"
        )
    new_name = name.strip() or None
    if new_name is None and not role:
        raise ValueError(f"--part {spec!r}: give a NAME, a :ROLE or both")
    return PartSpec(selector, new_name, role or None)


UP_AXES = ("Z", "Y", "X")
OVERALL_MODES = ("auto", "always", "never")


def parse_up(raw: str) -> str:
    """`Z`, `Y` or `X` (any case): the model's vertical axis."""
    value = raw.strip().upper()
    if value not in UP_AXES:
        raise ValueError(f"--up {raw!r}: expected one of {', '.join(UP_AXES)}")
    return value


def parse_overall(raw: str) -> str:
    value = raw.strip().lower()
    if value not in OVERALL_MODES:
        raise ValueError(
            f"--overall {raw!r}: expected one of {', '.join(OVERALL_MODES)}"
        )
    return value


def _point_text(raw: object, where: str) -> str:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError(f"{where}: a point is three numbers [x, y, z]")
    return ",".join(str(finite(str(v), where)) for v in raw)


def _load_dims_data(path: str) -> list[object]:
    """The raw list of a `--dims-from` file (JSON, or YAML when pyyaml is there)."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"--dims-from {path!r}: cannot read it: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml

            data = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - yaml raises its own error family or is missing
            raise ValueError(
                f"--dims-from {path!r}: not valid JSON or YAML: {exc}"
            ) from exc
    if isinstance(data, dict):
        data = data.get("dims")
    if not isinstance(data, list) or not data:
        raise ValueError(
            f"--dims-from {path!r}: expected a non-empty list of measurements"
        )
    return data


def parse_dims_file(path: str) -> list[str]:
    """Read a `--dims-from` spec (JSON, or YAML when pyyaml is there) as `--dim` strings.

    Shape: a list, or {"dims": [...]}, of {"name": ..., "from": [x,y,z], "to": [x,y,z]} and, for the
    mating gap between two parts, {"name": ..., "kind": "gap", "parts": [A, B], "from": ..., "to": ...}.
    Names must be unique (the check matches the image's labels by name).
    """
    data = _load_dims_data(path)
    out: list[str] = []
    for name, p1, p2, _gap in _dims_entries(data, path):
        out.append(f"{name}:{p1}:{p2}")
    return out


def parse_dims_gaps(path: str) -> dict[str, tuple[str, str]]:
    """The `kind: gap` entries of a `--dims-from` spec: measurement name -> its two named parts."""
    return {
        name: gap
        for name, _p1, _p2, gap in _dims_entries(_load_dims_data(path), path)
        if gap is not None
    }


def _dims_entries(
    data: list[object], path: str
) -> list[tuple[str, str, str, tuple[str, str] | None]]:
    """Validated (name, from-text, to-text, gap parts or None) of every entry.

    `kind` is `dim` (default: both ends on ONE part) or `gap` (the declared mating gap between two
    NAMED parts: `parts: [A, B]`). Anything else is refused.
    """
    out: list[tuple[str, str, str, tuple[str, str] | None]] = []
    seen: set[str] = set()
    for i, item in enumerate(data):
        where = f"--dims-from {path!r} entry {i + 1}"
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(f"{where}: needs a string `name`, `from` and `to`")
        name = item["name"].strip()
        if not name or ":" in name or name in seen:
            raise ValueError(
                f"{where}: name {name!r} is empty, contains ':' or repeats"
            )
        seen.add(name)
        kind = str(item.get("kind", "dim")).strip().lower()
        gap: tuple[str, str] | None = None
        if kind == "gap":
            named = item.get("parts")
            if (
                not isinstance(named, (list, tuple))
                or len(named) != 2
                or not all(isinstance(n, str) and n.strip() for n in named)
                or named[0].strip() == named[1].strip()
            ):
                raise ValueError(
                    f"{where}: a `kind: gap` needs `parts: [A, B]`, two different part names"
                )
            gap = (named[0].strip(), named[1].strip())
        elif kind != "dim":
            raise ValueError(f"{where}: kind {kind!r} is not `dim` or `gap`")
        out.append(
            (
                name,
                _point_text(item.get("from"), where),
                _point_text(item.get("to"), where),
                gap,
            )
        )
    return out
