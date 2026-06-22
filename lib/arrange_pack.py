"""arrange_pack.py — split parts, shelf-pack them onto bed-sized plates, write per-plate 3MF.

WHAT: the heavy backend for `3d arrange`. Reads a .3mf or one-or-more .stl inputs, splits
  each input object into CONNECTED bodies (so each glyph / loose part packs independently),
  lays every part flat (drops its min-Z to 0), shelf-packs the XY footprints onto square
  plates that fit the printer bed, centers each plate, and writes ONE print-ready .3mf per
  plate (a trimesh.Scene of the placed parts as separate objects, flat on z=0, non-overlapping,
  within the bed). It then RELOADS each written 3MF to verify the object count and bed fit.

WHY: a 3MF that stores parts in their assembled positions dumps everything onto one plate,
  which overflows a 270x270 bed (Snapmaker U1). This arranges the parts across as many plates
  as needed so each plate is print-ready.

The shelf packer + the small geometry helpers (lay-flat box math, centering) are PURE STDLIB so
  they are unit-testable without trimesh/numpy. Only the mesh I/O (load / split / transform /
  3MF write+reload) uses trimesh. Keep that split: tests import the stdlib functions directly.

Exit codes (mirrors errors.py so the `arrange` command can translate them):
  0   -> success (plates written + verified)
  1   -> a single part is larger than the bed (cannot be placed) — names the part + its size
  2   -> usage / IO error (no inputs, unreadable file, empty mesh)
  127 -> a python runtime / trimesh is missing (raised by pyrun before this script runs)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # heavy types only for the checker; never imported at runtime top level
    import numpy as np
    import trimesh

_EPS = 1e-6


# --------------------------------------------------------------------------------------
# Pure data + shelf packing (STDLIB ONLY — unit-tested without trimesh/numpy)
# --------------------------------------------------------------------------------------
@dataclass
class PartBox:
    """A part's flat XY footprint plus the index that ties it back to its mesh."""

    name: str
    mesh_index: int
    width: float  # X extent (mm)
    depth: float  # Y extent (mm)


@dataclass
class Placement:
    """One part placed on a plate: the lower-left corner of its footprint, in bed mm."""

    name: str
    mesh_index: int
    width: float
    depth: float
    x: float  # lower-left corner X on the bed
    y: float  # lower-left corner Y on the bed


@dataclass
class Plate:
    """One build plate's worth of placements."""

    index: int  # 1-based plate number
    placements: list[Placement]


class OversizePart(Exception):
    """A single part is larger than the usable plate area, so it can never be placed."""

    def __init__(self, name: str, width: float, depth: float, usable: float) -> None:
        super().__init__(
            f"part {name!r} ({width:.1f} x {depth:.1f} mm) exceeds the usable plate "
            f"{usable:.1f} x {usable:.1f} mm (bed minus 2*margin)"
        )
        self.name = name
        self.width = width
        self.depth = depth
        self.usable = usable


def shelf_pack(parts: list[PartBox], *, usable: float, gap: float) -> list[Plate]:
    """Shelf / row pack footprints onto square plates of side `usable`, sorted height-desc.

    Classic shelf (next-fit-decreasing-height) packing: sort parts by depth descending, lay
    them left-to-right into the current shelf; when a part does not fit the remaining row
    width, open a new shelf below; when it does not fit the remaining plate height, start a
    NEW plate. A part bigger than the usable square (even alone) raises OversizePart.

    `gap` is the clearance left between adjacent footprints (and below each shelf). Footprints
    are placed with their lower-left corner at the returned (x, y); the caller centers the
    whole arrangement on the bed afterwards.
    """
    for p in parts:
        if p.width > usable + _EPS or p.depth > usable + _EPS:
            raise OversizePart(p.name, p.width, p.depth, usable)

    ordered = sorted(parts, key=lambda p: (-p.depth, -p.width, p.name))

    plates: list[Plate] = []
    cur: list[Placement] = []
    plate_index = 1
    cursor_x = 0.0  # left edge of the next part on the current shelf
    shelf_y = 0.0  # bottom edge of the current shelf
    shelf_h = 0.0  # tallest part on the current shelf (its depth)

    def flush_plate() -> None:
        nonlocal cur, plate_index, cursor_x, shelf_y, shelf_h
        if cur:
            plates.append(Plate(index=plate_index, placements=cur))
            plate_index += 1
        cur = []
        cursor_x = 0.0
        shelf_y = 0.0
        shelf_h = 0.0

    for p in ordered:
        # Does it fit on the current shelf (to the right of the cursor)?
        if cursor_x > _EPS and cursor_x + p.width > usable + _EPS:
            # No room in this row — drop to a new shelf below.
            shelf_y += shelf_h + gap
            cursor_x = 0.0
            shelf_h = 0.0
        # Does the new shelf fit on the current plate (height-wise)?
        if shelf_y + p.depth > usable + _EPS:
            flush_plate()
        cur.append(Placement(p.name, p.mesh_index, p.width, p.depth, cursor_x, shelf_y))
        cursor_x += p.width + gap
        shelf_h = max(shelf_h, p.depth)

    flush_plate()
    return plates


def plate_extent(plate: Plate) -> tuple[float, float]:
    """The bounding-box (width, depth) actually occupied by a plate's placements."""
    if not plate.placements:
        return (0.0, 0.0)
    max_x = max(pl.x + pl.width for pl in plate.placements)
    max_y = max(pl.y + pl.depth for pl in plate.placements)
    return (max_x, max_y)


def centering_offset(plate: Plate, *, bed: float) -> tuple[float, float]:
    """Offset that centers a plate's used bounding box on a square `bed`."""
    w, d = plate_extent(plate)
    return ((bed - w) / 2.0, (bed - d) / 2.0)


# --------------------------------------------------------------------------------------
# Mesh I/O (trimesh — lazy, only reached when actually arranging)
# --------------------------------------------------------------------------------------
def _load_meshes(inputs: list[str]) -> list[tuple[str, "trimesh.Trimesh"]]:
    """Load inputs (.3mf scene or .stl files) and split each into connected bodies.

    Returns a list of (name, mesh) where each mesh is a single connected body laid out in
    its source coordinates (not yet flattened). A .3mf scene contributes one entry per
    object-per-connected-body; each .stl contributes one entry per connected body.
    """
    import trimesh

    out: list[tuple[str, trimesh.Trimesh]] = []
    for path in inputs:
        if not os.path.isfile(path):
            _die(f"input not found: {path}", 2)
        loaded = trimesh.load(path, force="scene")
        # Normalize to a dict of {object_name: Trimesh}.
        geoms: dict[str, Any]
        if isinstance(loaded, trimesh.Scene):
            geoms = dict(loaded.geometry)
        else:
            geoms = {os.path.splitext(os.path.basename(path))[0]: loaded}
        stem = os.path.splitext(os.path.basename(path))[0]
        for obj_name, geom in geoms.items():
            if not hasattr(geom, "vertices") or len(geom.vertices) == 0:
                continue
            base = stem if (not obj_name or obj_name == "geometry") else os.path.splitext(obj_name)[0]
            bodies = _split_bodies(geom)
            for bi, body in enumerate(bodies):
                label = base if len(bodies) == 1 else f"{base}_{bi + 1}"
                out.append((label, body))
    if not out:
        _die("no usable geometry found in the inputs", 2)
    return out


def _split_bodies(mesh: "trimesh.Trimesh") -> list["trimesh.Trimesh"]:
    """Split a mesh into connected components; fall back to the whole mesh on failure."""
    try:
        parts = mesh.split(only_watertight=False)
    except Exception:
        parts = []
    bodies = [p for p in parts if hasattr(p, "vertices") and len(p.vertices) > 0]
    return bodies if bodies else [mesh]


def _lay_flat(mesh: "trimesh.Trimesh") -> tuple["trimesh.Trimesh", float, float]:
    """Translate a copy so its min corner sits at the XY origin and min-Z = 0.

    Returns (flattened_mesh, width_x, depth_y). The part is NOT reoriented (no minimal-bbox
    rotation) — it just drops to the plate; orientation is the slicer's / user's call.
    """
    m = mesh.copy()
    lo = m.bounds[0]
    m.apply_translation([-lo[0], -lo[1], -lo[2]])
    ext = m.extents
    return m, float(ext[0]), float(ext[1])


def _np_translation(dx: float, dy: float) -> "np.ndarray":
    import numpy as np

    t = np.eye(4)
    t[0, 3] = dx
    t[1, 3] = dy
    return t


# --------------------------------------------------------------------------------------
# 3MF write + reload verification
# --------------------------------------------------------------------------------------
def _write_plate(
    plate: Plate,
    flat_by_index: dict[int, "trimesh.Trimesh"],
    *,
    bed: float,
    out_path: str,
) -> dict[str, Any]:
    """Place a plate's parts (centered) into a Scene and export it as a 3MF.

    Returns a verification summary {path, objects, reloaded_objects, max_x, max_y, within_bed}.
    """
    import trimesh

    ox, oy = centering_offset(plate, bed=bed)
    scene = trimesh.Scene()
    for pl in plate.placements:
        body = flat_by_index[pl.mesh_index].copy()
        body.apply_transform(_np_translation(pl.x + ox, pl.y + oy))
        scene.add_geometry(body, geom_name=f"{pl.name}")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    scene.export(out_path, file_type="3mf")
    return _verify_plate(out_path, expected_objects=len(plate.placements), bed=bed)


def _verify_plate(path: str, *, expected_objects: int, bed: float) -> dict[str, Any]:
    """Reload a written 3MF and confirm object count + that all geometry is within the bed."""
    import trimesh

    reloaded = trimesh.load(path, force="scene")
    objects = len(reloaded.geometry) if isinstance(reloaded, trimesh.Scene) else 1
    bnds = reloaded.bounds  # (2,3): min, max in scene coords
    max_x = float(bnds[1][0])
    max_y = float(bnds[1][1])
    min_x = float(bnds[0][0])
    min_y = float(bnds[0][1])
    within = (
        min_x >= -_EPS
        and min_y >= -_EPS
        and max_x <= bed + 1e-3
        and max_y <= bed + 1e-3
    )
    return {
        "path": path,
        "objects": expected_objects,
        "reloaded_objects": objects,
        "max_x": round(max_x, 3),
        "max_y": round(max_y, 3),
        "within_bed": within,
    }


# --------------------------------------------------------------------------------------
# Orchestration + CLI
# --------------------------------------------------------------------------------------
def _die(msg: str, code: int) -> None:
    sys.stderr.write(f"arrange: {msg}\n")
    raise SystemExit(code)


def arrange(
    inputs: list[str],
    *,
    bed: float,
    gap: float,
    margin: float,
    out_prefix: str,
    emit_json: bool,
) -> int:
    """Full pipeline: load+split -> lay flat -> pack -> write per-plate 3MF -> verify."""
    usable = bed - 2.0 * margin
    if usable <= 0:
        _die(f"usable plate area is non-positive: bed {bed} - 2*margin {margin}", 2)

    named = _load_meshes(inputs)
    flat_by_index: dict[int, Any] = {}
    boxes: list[PartBox] = []
    for idx, (name, mesh) in enumerate(named):
        flat, w, d = _lay_flat(mesh)
        flat_by_index[idx] = flat
        boxes.append(PartBox(name=name, mesh_index=idx, width=w, depth=d))

    try:
        plates = shelf_pack(boxes, usable=usable, gap=gap)
    except OversizePart as exc:
        _die(str(exc), 1)
        return 1  # unreachable; for the type-checker

    summaries: list[dict[str, Any]] = []
    for plate in plates:
        out_path = f"{out_prefix}_plate{plate.index}.3mf"
        summaries.append(_write_plate(plate, flat_by_index, bed=bed, out_path=out_path))

    _report(boxes, plates, summaries, bed=bed, usable=usable, gap=gap, emit_json=emit_json)
    # If any reloaded plate count mismatches or a part escaped the bed, fail loudly.
    bad = [s for s in summaries if not s["within_bed"] or s["reloaded_objects"] != s["objects"]]
    return 1 if bad else 0


def _report(
    boxes: list[PartBox],
    plates: list[Plate],
    summaries: list[dict[str, Any]],
    *,
    bed: float,
    usable: float,
    gap: float,
    emit_json: bool,
) -> None:
    if emit_json:
        payload = {
            "bed": bed,
            "usable": usable,
            "gap": gap,
            "parts": [asdict(b) for b in boxes],
            "plates": [
                {"index": p.index, "placements": [asdict(pl) for pl in p.placements]}
                for p in plates
            ],
            "verification": summaries,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print(f"ARRANGE bed {bed:g}x{bed:g} mm  usable {usable:g}x{usable:g} mm  gap {gap:g} mm")
    print(f"parts: {len(boxes)}   plates: {len(plates)}")
    for p in plates:
        s = next(x for x in summaries if x["path"].endswith(f"_plate{p.index}.3mf"))
        verdict = "OK" if s["within_bed"] and s["reloaded_objects"] == s["objects"] else "FAIL"
        ext_w, ext_d = plate_extent(p)
        print(
            f"  plate {p.index}: {len(p.placements)} part(s)  "
            f"used {ext_w:g}x{ext_d:g} mm  max-corner ({s['max_x']:g}, {s['max_y']:g}) mm  "
            f"objects(reload) {s['reloaded_objects']}/{s['objects']}  -> {verdict}"
        )
        for pl in p.placements:
            print(
                f"      {pl.name:<24} {pl.width:7.1f} x {pl.depth:7.1f} mm  "
                f"@ ({pl.x:.1f}, {pl.y:.1f})"
            )
        print(f"    -> {s['path']}")
    print(f"STATUS: {'PASS' if all(s['within_bed'] for s in summaries) else 'FAIL'}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="arrange_pack", add_help=False)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--bed", type=float, default=270.0)
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--margin", type=float, default=8.0)
    ap.add_argument("-o", "--out", dest="out", default="")
    ap.add_argument("--json", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    out_prefix = args.out
    if not out_prefix:
        first = args.inputs[0]
        out_prefix = os.path.splitext(first)[0] + "_arranged"
    try:
        return arrange(
            args.inputs,
            bed=args.bed,
            gap=args.gap,
            margin=args.margin,
            out_prefix=out_prefix,
            emit_json=args.json,
        )
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
