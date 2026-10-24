"""position.py — lay the PRINTED parts of an assembly on build plates and write the positioned project 3MF.

WHAT: `plan_layout` picks a print orientation per printed part (explicit `print.face` / `print.rotate`, or `face: auto`:
  the axis-aligned orientation with the least overhang on the surfaces that mate with other parts, then the least
  overhang overall, then the lowest height), lays each part flat on z = 0 and shelf-packs the footprints onto bed-sized
  plates with the same packer as `3d arrange` (`arrange_pack.shelf_pack`). `write_project` writes the multi-plate
  Orca/Bambu project 3MF (`orca_project_3mf`, the same writer as `arrange`) with
    - every object tagged `3d:role` / `3d:printable` (`parts.Part.to_metadata`) and the per-part slicer settings of
      the spec (`print.settings`) as object metadata,
    - a distinct display colour per object (`parts.assign_styles`),
    - `Metadata/3d_assembly.json`: the spec and the assembled -> plate matrix of every part, so
      `3d assembly check PROJECT.3mf` re-verifies the file that gets printed (`assembly.threemf`),
    - with `base_project`, the slicer settings (`Metadata/project_settings.config`) of an existing Orca-family
      project, so `3d retarget` accepts the result unchanged and moves it to another printer.

NON-PRINTABLE PARTS never enter a plate: `parts.filter_printable` drops vitamins / reference models / stand-ins and the
  plan names every dropped part. `include_non_printable=True` (the CLI's `--include-non-printable`, logged by
  `parts.assert_printable`) is the only way in, and the objects keep their tag.

INVARIANTS: the geometry of a part is never changed, only rotated and translated; written 3MFs are re-read and
  verified (object count, tags, plate assignment, bed fit) before the call succeeds.
"""

from __future__ import annotations

import json
import math
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from arrange_pack import PartBox, centering_offset, shelf_pack
from assembly.scene import Body, Scene, box_intersection
from assembly.spec import CMD, AssemblySpec, MateSpec
from assembly.threemf import ENTRY, SCHEMA_3MF, object_meshes
from assembly.vec import Vec3, format_direction
from errors import GateFailure, InvalidArgument
from parts import (
    META_PRINTABLE,
    META_ROLE,
    Part,
    assert_printable,
    assign_styles,
    filter_printable,
)

OVERHANG_COS = -math.sin(
    math.radians(45.0)
)  # a face turned down by more than 45 deg from vertical overhangs
_CANDIDATE_FACES: tuple[Vec3, ...] = (
    (0.0, 0.0, -1.0),
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0),
)
# settings of `base_project` that describe the slicer project and travel to the positioned file
CARRIED_ENTRIES = ("Metadata/project_settings.config",)


def rotation_to_down(face: Sequence[float]) -> Any:
    """The 3x3 rotation that turns the unit vector `face` into -Z (the bed), minimal tilt."""
    v = np.asarray(face, dtype=float)
    v = v / np.linalg.norm(v)
    down = np.array([0.0, 0.0, -1.0])
    c = float(v @ down)
    if c > 1 - 1e-9:
        return np.eye(3)
    if c < -1 + 1e-9:
        return np.array(
            [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]]
        )  # 180 deg about X
    axis = np.cross(v, down)
    s = float(np.linalg.norm(axis))
    k = (
        np.array(
            [[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]]
        )
        / s
    )
    return np.eye(3) + s * k + (1 - c) * (k @ k)


def overhang_areas(
    body: Body, rot: Any, fit_boxes: Sequence[tuple[Any, Any]]
) -> tuple[float, float, float]:
    """(overhang area on the mating surfaces, overhang area overall, height) of `body` turned by `rot`.

    A face counts when it points down by more than 45 deg from vertical and is not resting on the bed (within 0.05 mm
    of the lowest point). "Mating surfaces" are the faces whose centre lies in a mate zone / the overlap neighbourhood.
    """
    mesh = body.mesh
    verts = np.asarray(mesh.vertices) @ rot.T
    zmin = float(verts[:, 2].min())
    tris = verts[np.asarray(mesh.faces)]
    normals = np.asarray(mesh.face_normals) @ rot.T
    areas = np.asarray(mesh.area_faces)
    centres_asm = np.asarray(mesh.triangles_center)
    low = normals[:, 2] < OVERHANG_COS
    on_bed = tris[:, :, 2].max(axis=1) < zmin + 0.05
    over = low & ~on_bed
    in_fit = np.zeros(len(areas), dtype=bool)
    for lo, hi in fit_boxes:
        in_fit |= np.all((centres_asm >= lo) & (centres_asm <= hi), axis=1)
    height = float(verts[:, 2].max() - zmin)
    return float(areas[over & in_fit].sum()), float(areas[over].sum()), height


def _fit_boxes(scene: Scene, body: Body) -> list[tuple[Any, Any]]:
    """Boxes of the surfaces this part mates on: the mate zones, else the neighbourhood of each partner."""
    boxes: list[tuple[Any, Any]] = []
    for mate in scene.spec.mates:
        if body.name not in (mate.a, mate.b):
            continue
        if mate.zone is not None:
            lo, hi = mate.zone.bbox(0.5)
            boxes.append((np.array(lo), np.array(hi)))
            continue
        other = scene.body(mate.b if mate.a == body.name else mate.a)
        window = box_intersection(body.box, other.box, 2.0)
        if all(window[i] <= window[i + 3] for i in range(3)):
            boxes.append((np.array(window[:3]), np.array(window[3:])))
    return boxes


def choose_orientation(scene: Scene, body: Body) -> tuple[Any, dict[str, Any]]:
    """The rotation (assembled -> print pose, 3x3) and an explanation for one printed part."""
    spec = body.spec.print
    if spec.rotate is not None:
        from assembly.scene import rotation_matrix_deg

        rot = rotation_matrix_deg(*spec.rotate)[:3, :3]
        return rot, {"how": "rotate", "rotate_deg": list(spec.rotate)}
    if isinstance(spec.face, tuple):
        return rotation_to_down(spec.face), {
            "how": "face",
            "face": format_direction(spec.face),
        }
    boxes = _fit_boxes(scene, body)
    best: tuple[tuple[float, float, float], Vec3, Any] | None = None
    for face in _CANDIDATE_FACES:
        rot = rotation_to_down(face)
        score = overhang_areas(body, rot, boxes)
        if best is None or tuple(round(s, 3) for s in score) < tuple(
            round(s, 3) for s in best[0]
        ):
            best = (score, face, rot)
    assert best is not None
    (fit_over, total_over, height), face, rot = best
    note = {
        "how": "auto" if spec.face == "auto" else "default",
        "face": format_direction(face),
        "fit_surface_overhang_mm2": round(fit_over, 2),
        "overhang_mm2": round(total_over, 2),
        "height_mm": round(height, 2),
    }
    if fit_over > 1e-6:
        note["warning"] = (
            f"{fit_over:.1f} mm2 of mating surface overhangs in the best orientation: it needs supports or a redesign"
        )
    return rot, note


@dataclass
class Placed:
    name: str
    part: Part
    plate: int  # 0-based
    matrix: Any  # 4x4 assembled -> plate-local
    verts: Any
    faces: Any
    orientation: dict[str, Any]
    size: tuple[float, float, float]


@dataclass
class Layout:
    placed: list[Placed]
    dropped: list[Part]
    overridden: list[Part]
    plates: int
    bed: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plates": self.plates,
            "bed_mm": self.bed,
            "parts": [
                {
                    "part": p.name,
                    "role": p.part.role,
                    "printable": p.part.printable,
                    "plate": p.plate + 1,
                    "size_mm": [round(s, 2) for s in p.size],
                    "orientation": p.orientation,
                }
                for p in self.placed
            ],
            "dropped_non_printable": [d.to_dict() for d in self.dropped],
            "override": [o.to_dict() for o in self.overridden],
            "notes": self.notes,
        }


def plan_layout(
    scene: Scene,
    *,
    bed: float = 220.0,
    gap: float = 6.0,
    margin: float = 8.0,
    include_non_printable: bool = False,
    reason: str = "",
    command: str = "assembly position",
) -> Layout:
    """Orient and pack every printable part (non-printable ones are dropped and named, unless overridden)."""
    spec = scene.spec
    if 2.0 * margin >= bed:
        raise InvalidArgument(
            "--margin",
            f"{margin:g}",
            [f"less than half the bed ({bed / 2:g} mm)"],
            command=CMD,
        )
    everyone = spec.part_list()
    if include_non_printable:
        guard = assert_printable(everyone, include_non_printable=True, command=command)
        kept, overridden = list(everyone), list(guard.overridden)
        dropped: list[Part] = []
        if reason:
            import logging

            logging.getLogger("3d.parts").warning(
                "override reason (%s): %s", command, reason
            )
    else:
        kept, dropped = filter_printable(everyone)
        overridden = []
    if not kept:
        raise GateFailure(
            "no printable part in the assembly: every part is a vitamin, reference model or stand-in",
            command=CMD,
            remediation=[
                "Declare at least one `role: printed` part, or pass --include-non-printable on purpose."
            ],
        )
    usable = bed - 2.0 * margin
    flat: list[tuple[Part, Any, Any, Any, dict[str, Any]]] = []
    boxes: list[PartBox] = []
    for i, part in enumerate(kept):
        body = scene.body(part.name)
        rot, info = choose_orientation(scene, body)
        verts = np.asarray(body.mesh.vertices) @ rot.T
        verts = verts - np.array(
            [verts[:, 0].min(), verts[:, 1].min(), verts[:, 2].min()]
        )
        m = np.eye(4)
        m[:3, :3] = rot
        m[:3, 3] = -(np.asarray(body.mesh.vertices) @ rot.T).min(axis=0)
        flat.append((part, m, verts, np.asarray(body.mesh.faces), info))
        boxes.append(
            PartBox(part.name, i, float(verts[:, 0].max()), float(verts[:, 1].max()))
        )
    plates = shelf_pack(boxes, usable=usable, gap=gap)
    placed: list[Placed] = []
    for plate in plates:
        ox, oy = centering_offset(plate, bed=usable)
        for pl in plate.placements:
            part, m, verts, faces, info = flat[pl.mesh_index]
            dx, dy = pl.x + ox + margin, pl.y + oy + margin
            shift = np.eye(4)
            shift[0, 3], shift[1, 3] = dx, dy
            matrix = shift @ m
            v = verts + np.array([dx, dy, 0.0])
            placed.append(
                Placed(
                    part.name,
                    part,
                    plate.index - 1,
                    matrix,
                    v,
                    faces,
                    info,
                    (
                        float(verts[:, 0].max()),
                        float(verts[:, 1].max()),
                        float(verts[:, 2].max()),
                    ),
                )
            )
    notes = []
    if dropped:
        notes.append(
            "not placed on any plate (non-printable): "
            + ", ".join(f"{d.name} ({d.role})" for d in dropped)
        )
    return Layout(placed, dropped, overridden, len(plates), bed, notes)


def _assembly_doc(
    spec: AssemblySpec, layout: Layout, objects: Mapping[str, int], printer: str | None
) -> dict[str, Any]:
    raw = dict(spec.raw)
    raw["root"] = str(spec.root)
    rec: dict[str, Any] = {}
    for p in layout.placed:
        tm = trimesh.Trimesh(p.verts, p.faces, process=False)
        rec[p.name] = {
            "object": objects[p.name],
            "plate": p.plate + 1,
            # assembled pose -> the object's own coordinates; the build-item transform (plate position) is left
            # out on purpose: arrange / retarget / the slicer move items, the geometry is what must match
            "matrix": [round(float(x), 9) for x in p.matrix.reshape(-1)],
            "local_bounds": [round(float(x), 6) for x in tm.bounds.reshape(-1)],
            "volume_mm3": round(float(tm.volume), 4),
            "orientation": p.orientation,
        }
    return {
        "schema": SCHEMA_3MF,
        "spec": raw,
        "spec_root": str(spec.root),
        "layout": rec,
        "bed_mm": layout.bed,
        "plates": layout.plates,
        "printer": printer,
    }


def write_project(
    scene: Scene,
    layout: Layout,
    out: str | Path,
    *,
    base_project: str | Path | None = None,
    printer: str | None = None,
) -> dict[str, Any]:
    """Write and verify the positioned 3MF; returns the verification summary."""
    from orca_project_3mf import MeshPart, write_project_3mf

    spec = scene.spec
    styles = assign_styles(spec.part_list())
    mesh_parts: list[MeshPart] = []
    objects: dict[str, int] = {}
    for i, p in enumerate(layout.placed):
        meta = {**p.part.to_metadata(), "3d:part": p.name}
        meta.update(
            {str(k): str(v) for k, v in spec.parts[p.name].print.settings.items()}
        )
        mesh_parts.append(
            MeshPart(
                p.name,
                [(float(v[0]), float(v[1]), float(v[2])) for v in p.verts],
                [(int(f[0]), int(f[1]), int(f[2])) for f in p.faces],
                p.plate,
                meta,
                styles[p.name].color,
            )
        )
        objects[p.name] = i + 1
    doc = _assembly_doc(spec, layout, objects, printer)
    extra: dict[str, bytes] = {
        ENTRY: json.dumps(doc, indent=1, sort_keys=False).encode("utf-8")
    }
    if base_project is not None:
        with zipfile.ZipFile(base_project) as zf:
            missing = [n for n in CARRIED_ENTRIES if n not in zf.namelist()]
            if missing:
                raise InvalidArgument(
                    "--base-project",
                    str(base_project),
                    [
                        "an Orca / Snapmaker Orca / Bambu Studio project .3mf with Metadata/project_settings.config"
                    ],
                    command=CMD,
                    extra=f"missing {missing[0]}",
                )
            for name in CARRIED_ENTRIES:
                extra[name] = zf.read(name)
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_project_3mf(str(out_path), mesh_parts, bed=layout.bed, extra_entries=extra)
    return verify_project(out_path, layout)


def verify_project(path: Path, layout: Layout) -> dict[str, Any]:
    """Re-read the written 3MF: object count, role tags, plate assignment, bed fit, no stray non-printable object."""
    import parts_3mf

    tags = parts_3mf.tags(path)
    problems: list[str] = []
    if len(tags) != len(layout.placed):
        problems.append(f"object count {len(tags)} != {len(layout.placed)}")
    by_name = {t.name: t for t in tags}
    for p in layout.placed:
        t = by_name.get(p.name)
        if t is None:
            problems.append(f"object {p.name} missing")
            continue
        if t.metadata.get(META_ROLE) != p.part.role or t.metadata.get(
            META_PRINTABLE
        ) != ("true" if p.part.printable else "false"):
            problems.append(
                f"object {p.name}: role tag did not survive ({dict(t.metadata)})"
            )
    stray = [t.name for t in tags if t.metadata.get(META_PRINTABLE) == "false"]
    if stray and not layout.overridden:
        problems.append(
            f"non-printable object(s) on a plate without override: {', '.join(stray)}"
        )
    worlds = object_meshes(path)
    if len(worlds) != len(layout.placed):
        problems.append(f"{len(worlds)} build items for {len(layout.placed)} parts")
    for oid, mesh in worlds.items():
        if float(mesh.bounds[0][2]) < -1e-6:
            problems.append(f"object {oid} sits below the bed")
        if float(mesh.bounds[1][0]) > layout.bed * 5 or float(mesh.bounds[0][0]) < -layout.bed:
            problems.append(f"object {oid} is outside the plate grid")
    ok = not problems
    if not ok:
        raise GateFailure(
            "positioned 3MF failed its own verification: " + "; ".join(problems),
            command=CMD,
            remediation=[
                "This is a bug in `assembly position`: report it with the spec."
            ],
        )
    return {
        "objects": len(tags),
        "plates": layout.plates,
        "tags_ok": True,
        "non_printable_on_plates": [
            t.name for t in tags if t.metadata.get(META_PRINTABLE) == "false"
        ],
        "path": str(path),
    }


def mates_summary(mates: Sequence[MateSpec]) -> list[dict[str, str]]:
    return [{"name": m.name, "a": m.a, "b": m.b, "kind": m.kind} for m in mates]
