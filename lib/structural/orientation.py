"""Print (build) orientation of a part: which model-frame axis the printer stacks layers along.

The orientation decides which direction is the weak, across-layer one. It is an input of the
anisotropy model, so the report always records where it came from (`source`).

Accepted specs (`--orientation`, or a part's `orientation:` in 3d.yaml):

  z-up | x-up | y-up   the model's +Z / +X / +Y axis points up on the bed (default z-up)
  flat                 the part lies on its largest face: the build axis is its thinnest bounding-box axis
  rotate=RX,RY,RZ      the part is rotated by these degrees (OpenSCAD order: X, then Y, then Z) before printing
  FILE.3mf             the rotation of the first build item of a slicer project 3MF
  auto                 the part's `orientation:` in 3d.yaml if it has one, else the 3MF build transform of the
                       input, else z-up

`flat-bottom` (a 3d.yaml value) is treated like `flat`. The axis is unsigned: layers are symmetric.
"""

from __future__ import annotations

import math
import pathlib
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from xml.etree import ElementTree

from errors import InputNotFound, InvalidArgument

NAMED = ("z-up", "x-up", "y-up", "flat", "auto")
_AXES = {"z-up": (0.0, 0.0, 1.0), "x-up": (1.0, 0.0, 0.0), "y-up": (0.0, 1.0, 0.0)}


@dataclass(frozen=True)
class Orientation:
    """Build axis (unit vector, part frame) and the provenance of that choice."""

    build_axis: tuple[float, float, float]
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "build_axis": [round(c, 6) for c in self.build_axis],
            "source": self.source,
        }


def _unit(v: Sequence[float]) -> tuple[float, float, float]:
    norm = math.sqrt(sum(c * c for c in v))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("zero-length build axis")
    return (v[0] / norm, v[1] / norm, v[2] / norm)


def _rotation_xyz(rx: float, ry: float, rz: float) -> list[list[float]]:
    """OpenSCAD rotate([rx, ry, rz]) matrix: R = Rz Ry Rx (degrees)."""
    cx, sx = math.cos(math.radians(rx)), math.sin(math.radians(rx))
    cy, sy = math.cos(math.radians(ry)), math.sin(math.radians(ry))
    cz, sz = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    return [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]


def from_rotation(rx: float, ry: float, rz: float) -> tuple[float, float, float]:
    """Build axis in the part frame when the part is printed after rotate([rx, ry, rz]): R^T applied to +Z."""
    r = _rotation_xyz(rx, ry, rz)
    return _unit((r[2][0], r[2][1], r[2][2]))


def from_3mf(path: pathlib.Path) -> Orientation:
    """Build axis from the first build item's transform of a 3MF (row-vector convention: v' = v R).

    The world Z of a model point is v . R[:, 2], so the build axis in the object frame is the third column.
    """
    if not path.is_file():
        raise InputNotFound(str(path), command="strength")
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".model")]
            if not names:
                raise InvalidArgument(
                    "--orientation",
                    str(path),
                    ["a 3MF with a 3D/*.model part"],
                    command="strength",
                )
            root = ElementTree.fromstring(archive.read(names[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise InvalidArgument(
            "--orientation",
            str(path),
            ["a valid 3MF archive"],
            command="strength",
            extra=f"Could not read it: {exc}",
        ) from exc
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "item":
            continue
        raw = element.get("transform")
        if raw is None:
            return Orientation(
                (0.0, 0.0, 1.0), f"3mf:{path.name} (identity build transform)"
            )
        nums = [float(x) for x in raw.split()]
        if len(nums) != 12:
            raise InvalidArgument(
                "--orientation",
                str(path),
                ["a 3MF item transform with 12 numbers"],
                command="strength",
            )
        return Orientation(
            _unit((nums[2], nums[5], nums[8])), f"3mf:{path.name} build transform"
        )
    return Orientation((0.0, 0.0, 1.0), f"3mf:{path.name} (no build item, z-up)")


def flat_axis(extents: Sequence[float]) -> tuple[float, float, float]:
    """Thinnest bounding-box axis: the part lies flat, so that axis is vertical."""
    idx = min(range(3), key=lambda i: extents[i])
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))[idx]


_ROTATE = re.compile(
    r"^rotate=\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)$"
)


def resolve(
    spec: str | Sequence[float] | None,
    *,
    extents: Sequence[float],
    part_orientation: str | Sequence[float] | None = None,
    input_3mf: pathlib.Path | None = None,
    label: str = "--orientation",
) -> Orientation:
    """Turn an orientation spec into a build axis for a body with bounding-box `extents`."""
    if spec is None or spec == "auto":
        if part_orientation is not None:
            inner = resolve(
                part_orientation, extents=extents, label="3d.yaml orientation"
            )
            return Orientation(
                inner.build_axis, f"3d.yaml part orientation ({inner.source})"
            )
        if input_3mf is not None:
            return Orientation(
                (0.0, 0.0, 1.0),
                f"3mf:{input_3mf.name} scene frame (build transform applied, z-up)",
            )
        return Orientation((0.0, 0.0, 1.0), "default z-up")
    if isinstance(spec, str):
        key = spec.strip()
        if key in _AXES:
            return Orientation(
                _AXES[key], f"flag {key}" if label == "--orientation" else key
            )
        if key in ("flat", "flat-bottom"):
            return Orientation(
                flat_axis(extents), "flat (thinnest bounding-box axis is vertical)"
            )
        m = _ROTATE.match(key)
        if m:
            rx, ry, rz = (float(g) for g in m.groups())
            return Orientation(
                from_rotation(rx, ry, rz), f"rotate=({rx:g},{ry:g},{rz:g}) deg"
            )
        if key.lower().endswith(".3mf"):
            return from_3mf(pathlib.Path(key))
        raise InvalidArgument(
            label, key, [*NAMED, "rotate=RX,RY,RZ", "FILE.3mf"], command="strength"
        )
    values = [float(v) for v in spec]
    if len(values) != 3 or not all(math.isfinite(v) for v in values):
        raise InvalidArgument(
            label, str(list(spec)), ["[rx, ry, rz] degrees"], command="strength"
        )
    return Orientation(
        from_rotation(*values),
        f"rotate=({values[0]:g},{values[1]:g},{values[2]:g}) deg",
    )
