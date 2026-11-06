"""convert_mesh.py — the mesh/colour worker behind `3d convert` (run via cli.pyrun).

WHAT: reads every input `3d convert` accepts, splits the faces into COLOUR groups and
writes one of three results:
  project3mf  an Orca/Bambu PROJECT .3mf (lib/orca_project_3mf.py) with ONE OBJECT PER
              COLOUR GROUP, each object carrying per-object `extruder` metadata
              (1-based, first-appearance colour order; uncoloured geometry prints on
              extruder 1) and its authored colour as the 3MF base material — the
              layout a multi-filament/AMS slicer prints as real colours;
  stl         one merged binary STL of every group (zero normals, like
              retarget_3mf.export_stl — slicers recompute them);
  usdz        a USDZ with one coloured mesh per colour group (lib/usdz.py, Y-up, mm).

Reading: dimension_model.load_raw (.scad through OpenSCAD's 3MF export, which keeps
every color() as a per-triangle base material; .3mf/.obj natively; .stl via trimesh).
.usdz is read with pxr (usd-core): every UsdGeom.Mesh becomes a group with its
displayColor; the stage is assumed Y-up millimetres (what 3d usdz/export/convert
themselves write), converted back to Z-up mm.

WHY: a plain `openscad -o out.3mf` keeps colours only as per-triangle materials inside
ONE object, which no slicer turns into per-extruder printing. Splitting per colour and
emitting one object per colour with `extruder` metadata is what makes the project
actually print multicolour.

INVARIANTS: the colour groups of ONE input keep their authored relative positions (the
groups of a scad assembly overlap exactly as designed — that overlap is what fuses the
colours); each input is laid flat (min z = 0) and centred on the bed, several inputs
side by side. Equal colours across inputs share one extruder slot.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Side-by-side spacing between several inputs on the plate (mm).
GAP_MM = 12.0
DEFAULT_BED_MM = 270.0  # square bed, same default as `3d arrange` (Snapmaker U1)
DEFAULT_COLOR = (0.78, 0.74, 0.66)  # the `3d export`/`3d usdz` default diffuse colour


@dataclass
class Group:
    """One colour bucket of one input: the mesh arrays and display-name candidates."""

    color: str | None  # authored "#RRGGBB"; None = uncoloured
    names: list[str] = field(default_factory=list)  # labels first, then object names
    verts: np.ndarray | None = None  # (N, 3) mm, Z-up
    faces: list[np.ndarray] = field(
        default_factory=list
    )  # (F, 3) index rows into verts


@dataclass
class Part:
    """A placed, re-indexed colour group ready to be written: one object / one mesh."""

    name: str
    color: str | None
    extruder: int
    verts: np.ndarray  # (N, 3) world mm (laid flat, positioned on the plate)
    faces: np.ndarray  # (F, 3) indices into verts


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="convert_mesh.py",
        description="mesh/colour-aware conversion worker for `3d convert`",
    )
    parser.add_argument("mode", choices=("project3mf", "stl", "usdz"))
    parser.add_argument("out", help="output path (its extension picks the writer)")
    parser.add_argument("inputs", nargs="+", help="input model files")
    parser.add_argument(
        "-D",
        "--define",
        dest="defines",
        action="append",
        default=[],
        metavar="K=V",
        help="OpenSCAD define for .scad inputs (repeatable)",
    )
    parser.add_argument(
        "--color",
        default=",".join(str(c) for c in DEFAULT_COLOR),
        metavar="R,G,B",
        help="diffuse colour 0..1 for uncoloured geometry (default 0.78,0.74,0.66)",
    )
    parser.add_argument(
        "--bed",
        type=float,
        default=DEFAULT_BED_MM,
        metavar="MM",
        help="square bed size for the .3mf project layout (default 270)",
    )
    return parser.parse_args(list(argv))


def _groups_of_model(path: str, defines: Sequence[str]) -> list[Group]:
    """The colour groups of a non-usdz input, via dimension_model.load_raw."""
    from dimension_model import load_raw

    lower = path.lower()
    raw = load_raw(path, list(defines) if lower.endswith(".scad") else [])
    buckets: dict[str | None, Group] = {}
    for index, group in enumerate(raw.groups):
        rows = np.nonzero(raw.face_group == index)[0]
        if not len(rows):
            continue
        bucket = buckets.setdefault(group.color, Group(group.color))
        bucket.faces.append(raw.faces[rows])
        if bucket.verts is None:
            bucket.verts = raw.vertices
        # an authored object name identifies the part better than a material label
        # ("lid" beats "Lid Plastic" beats "#CC2222"), so it comes first
        for name in (group.object_name, group.label):
            if name and name not in bucket.names:
                bucket.names.append(name)
    return [g for g in buckets.values() if g.verts is not None]


def _fan_triangulate(
    counts: Sequence[Any], indices: Sequence[Any]
) -> list[tuple[int, int, int]]:
    """Triangles of a USD mesh's polygon soup (fan-triangulate every face)."""
    faces: list[tuple[int, int, int]] = []
    start = 0
    for count in counts:
        count = int(count)
        if count >= 3:
            base = int(indices[start])
            faces.extend(
                (base, int(indices[start + k]), int(indices[start + k + 1]))
                for k in range(1, count - 1)
            )
        start += count
    return faces


def _usdz_group(prim: Any, *, y_up: bool, scale: float) -> Group | None:
    """One UsdGeom.Mesh as a colour group (its displayColor, Z-up millimetres), or None."""
    from pxr import UsdGeom  # type: ignore[import-untyped]  # usd-core

    from dimension_model import to_hex

    mesh = UsdGeom.Mesh(prim)
    points = mesh.GetPointsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    counts = mesh.GetFaceVertexCountsAttr().Get()
    if not points or not indices or not counts:
        return None
    verts = np.array([[p[0], p[1], p[2]] for p in points], dtype=float) * scale
    if y_up:  # Y-up stage -> Z-up model frame: (x, y, z) -> (x, -z, y)
        verts = verts[:, [0, 2, 1]] * np.array([1.0, -1.0, 1.0])
    faces = _fan_triangulate(counts, indices)
    if not faces:
        return None
    display = mesh.GetDisplayColorAttr().Get()
    rgb = tuple(display[0]) if display and len(display) else None
    color = to_hex(*rgb[:3]) if rgb else None
    return Group(
        color,
        [str(prim.GetName())],
        verts,
        [np.array(faces, dtype=np.int64).reshape(-1, 3)],
    )


def _groups_of_usdz(path: str) -> list[Group]:
    """The colour groups of a USDZ: one per UsdGeom.Mesh, displayColor as the colour."""
    try:
        from pxr import Usd, UsdGeom  # type: ignore[import-untyped]  # usd-core
    except ImportError as exc:  # pragma: no cover - tier resolution failure
        print(
            f"error: {path} is a USDZ but pxr (usd-core) is unavailable: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(127) from exc

    stage = Usd.Stage.Open(path)
    if stage is None:
        print(f"error: cannot open USDZ stage {path}", file=sys.stderr)
        raise SystemExit(1)
    y_up = UsdGeom.GetStageUpAxis(stage) == "Y"
    scale = 1000.0 * float(UsdGeom.GetStageMetersPerUnit(stage) or 0.001)
    groups = []
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            group = _usdz_group(prim, y_up=y_up, scale=scale)
            if group is not None:
                groups.append(group)
    return groups


def _groups(path: str, defines: Sequence[str]) -> list[Group]:
    if path.lower().endswith(".usdz"):
        return _groups_of_usdz(path)
    return _groups_of_model(path, defines)


def _bounds(groups: Sequence[Group]) -> tuple[np.ndarray, np.ndarray]:
    verts = np.vstack([g.verts for g in groups if g.verts is not None])  # type: ignore[misc]
    return verts.min(axis=0), verts.max(axis=0)


def _assembly_offsets(
    groups_per_input: Sequence[Sequence[Group]], bed: float
) -> list[tuple[float, float, float]]:
    """One (dx, dy, dz) per input: laid flat, centred on Y, inputs side by side on X."""
    spans = []
    for groups in groups_per_input:
        lo, hi = _bounds(groups)
        spans.append((float(hi[0] - lo[0]), float(hi[1] - lo[1]), lo))
    total = sum(w for w, _, _ in spans) + GAP_MM * max(0, len(spans) - 1)
    cursor = max(0.0, (bed - total) / 2.0) if total <= bed else 0.0
    offsets = []
    for width, depth, lo in spans:
        offsets.append(
            (cursor - float(lo[0]), (bed - depth) / 2.0 - float(lo[1]), -float(lo[2]))
        )
        cursor += width + GAP_MM
    return offsets


def _extruder_map(groups_per_input: Sequence[Sequence[Group]]) -> dict[str | None, int]:
    """Colour -> 1-based extruder slot, first-appearance order; uncoloured prints on 1.

    With no uncoloured geometry the first colour takes slot 1 (a fully coloured model
    must not waste a filament slot on geometry that does not exist).
    """
    slots: dict[str | None, int] = {}
    next_slot = 1
    if any(group.color is None for groups in groups_per_input for group in groups):
        slots[None] = next_slot
        next_slot += 1
    for groups in groups_per_input:
        for group in groups:
            if group.color is not None and group.color not in slots:
                slots[group.color] = next_slot
                next_slot += 1
    return slots


def _unique_names(parts: list[Part]) -> None:
    """Object/prim names must not collide (USD prims refuse duplicates); suffix -2, -3…"""
    seen: dict[str, int] = {}
    for part in parts:
        seen[part.name] = seen.get(part.name, 0) + 1
        if seen[part.name] > 1:
            part.name = f"{part.name} {seen[part.name]}"


def _parts(groups_per_input: Sequence[Sequence[Group]], bed: float) -> list[Part]:
    """Re-index every colour group to its own vertex list, name it, place it on the bed."""
    offsets = _assembly_offsets(groups_per_input, bed)
    slots = _extruder_map(groups_per_input)
    parts: list[Part] = []
    for groups, (dx, dy, dz) in zip(groups_per_input, offsets, strict=True):
        offset = np.array([dx, dy, dz])
        for number, group in enumerate(groups, 1):
            if group.verts is None or not group.faces:
                continue
            faces = np.vstack(group.faces)
            used = np.unique(faces)
            remap = np.full(len(group.verts), -1, dtype=np.int64)
            remap[used] = np.arange(len(used))
            name = group.names[0] if group.names else (group.color or f"part {number}")
            parts.append(
                Part(
                    name,
                    group.color,
                    slots.get(group.color, 1),
                    group.verts[used] + offset,
                    remap[faces],
                )
            )
    _unique_names(parts)
    return parts


def _write_stl(out: str, parts: Sequence[Part]) -> int:
    """One binary STL of every part (zero normals; slicers recompute them)."""
    packer = struct.Struct("<12fH")
    body = bytearray()
    count = 0
    for part in parts:
        verts = part.verts
        for a, b, c in part.faces:
            va, vb, vc = verts[a], verts[b], verts[c]
            body += packer.pack(0.0, 0.0, 0.0, *va, *vb, *vc, 0)
            count += 1
    with open(out, "wb") as handle:
        handle.write(b"3d convert".ljust(80, b"\0"))
        handle.write(struct.pack("<I", count))
        handle.write(body)
    return count


def _write_project(out: str, parts: Sequence[Part], bed: float) -> None:
    """The Orca project 3MF: one object per colour group, per-object extruder metadata."""
    from orca_project_3mf import MeshPart, write_project_3mf

    mesh_parts = [
        MeshPart(
            name=part.name,
            vertices=[(float(v[0]), float(v[1]), float(v[2])) for v in part.verts],
            triangles=[(int(f[0]), int(f[1]), int(f[2])) for f in part.faces],
            plate=0,
            metadata={"extruder": str(part.extruder)},
            color=part.color,
        )
        for part in parts
    ]
    write_project_3mf(out, mesh_parts, bed=bed)


def _write_usdz(
    out: str, parts: Sequence[Part], default_color: tuple[float, float, float]
) -> int:
    """A USDZ with one coloured mesh per part (lib/usdz.py handles the pxr packaging)."""
    from dimension_model import from_hex
    from usdz import UsdzPart, meshes_to_usdz

    with tempfile.TemporaryDirectory(prefix="3d-convert-usdz-") as tmp:
        usdz_parts = []
        for number, part in enumerate(parts):
            stl = os.path.join(tmp, f"part{number}.stl")
            _write_stl(stl, [part])
            color = from_hex(part.color) if part.color else default_color
            usdz_parts.append(UsdzPart(stl, color, part.name))
        return meshes_to_usdz(usdz_parts, out)


def _parse_color(raw: str) -> tuple[float, float, float]:
    fields = raw.split(",")
    try:
        rgb = tuple(float(v) for v in fields)
    except ValueError:
        rgb = ()
    if len(rgb) != 3:
        print(f"error: --color expects R,G,B in 0..1, got '{raw}'", file=sys.stderr)
        raise SystemExit(2)
    return tuple(min(max(c, 0.0), 1.0) for c in rgb)  # type: ignore[return-value]


def _describe(parts: Sequence[Part]) -> None:
    for number, part in enumerate(parts, 1):
        colour = part.color or "no colour"
        print(
            f"  object {number}: {part.name} — {colour}, extruder {part.extruder}, "
            f"{len(part.faces)} triangles"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    color = _parse_color(args.color)
    groups_per_input = [_groups(path, args.defines) for path in args.inputs]
    if not any(groups_per_input):
        print("error: no readable geometry in the inputs", file=sys.stderr)
        return 1
    parts = _parts(groups_per_input, args.bed)
    if not parts:
        print("error: no triangles in the inputs", file=sys.stderr)
        return 1
    _describe(parts)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    if args.mode == "project3mf":
        _write_project(args.out, parts, bed=args.bed)
        print(f"wrote {args.out}: {len(parts)} object(s), one plate")
    elif args.mode == "stl":
        triangles = _write_stl(args.out, parts)
        print(f"wrote {args.out}: {triangles} triangles from {len(parts)} group(s)")
    else:
        faces = _write_usdz(args.out, parts, color)
        print(f"wrote {args.out}: {faces} faces in {len(parts)} mesh(es)")
    return 0


if __name__ == "__main__":
    from dimension_model import RenderError
    from errors import ThreeDError

    try:
        code = main()
    except ThreeDError as exc:  # structured errors carry their own exit code
        print(f"error: {exc}", file=sys.stderr)
        code = exc.exit_code
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = exc.exit_code
    except (OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
