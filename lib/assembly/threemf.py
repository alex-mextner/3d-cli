"""threemf.py — the positioned-assembly 3MF: read a project 3MF back into an assembly spec + printed meshes.

WHAT: `3d assembly position` writes a multi-plate Orca/Bambu project 3MF whose objects are the PRINTED parts laid
  out on plates, plus one extra entry `Metadata/3d_assembly.json` (schema `3d.assembly-3mf/1`): the assembly spec as
  written, and per part the object id, the matrix that maps the assembled pose onto the object's own coordinates, and
  the bounds and volume it had when written. `read_positioned` reverses that: it loads each object (component
  transforms applied, the plate position of the build item left out), maps it back into the assembled frame with the
  inverse matrix and hands the check the geometry that is actually in the file, so `3d assembly check PROJECT.3mf`
  verifies the file that gets printed - also after `3d arrange` / `3d retarget` moved the items on the bed.
  Non-printable parts (vitamins, reference models, stand-ins) are NOT in the file by design: they come from the spec's
  mesh paths.

INVARIANTS:
  - A 3MF without the entry is refused with the command that makes one. A part whose object is missing, or whose
    own bounds / volume differ from the recorded ones (edited, re-meshed, scaled), is reported as `drift`; the check
    then uses the spec's mesh for that part, says so, and fails: the file is no longer what `position` wrote.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import trimesh

from assembly.spec import CMD, AssemblySpec, parse_spec
from errors import InputNotFound, InvalidArgument

ENTRY = "Metadata/3d_assembly.json"
SCHEMA_3MF = "3d.assembly-3mf/1"
_NS = {"c": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
_PNS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def matrix_from_3mf(text: str | None) -> Any:
    """A 3MF `transform` (12 numbers, column-major 4x3) -> a 4x4 row-vector-free matrix; None = identity."""
    if not text:
        return np.eye(4)
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        raise InvalidArgument("transform", text, ["12 numbers"], command=CMD)
    m = np.eye(4)
    for c in range(4):
        for r in range(3):
            m[r, c] = v[c * 3 + r]
    return m


def _mesh_of(obj: ET.Element) -> tuple[Any, Any] | None:
    mesh = obj.find("c:mesh", _NS)
    if mesh is None:
        return None
    verts = np.array(
        [
            [float(v.get(k, "0")) for k in ("x", "y", "z")]
            for v in mesh.findall("c:vertices/c:vertex", _NS)
        ]
    )
    faces = np.array(
        [
            [int(t.get(k, "0")) for k in ("v1", "v2", "v3")]
            for t in mesh.findall("c:triangles/c:triangle", _NS)
        ]
    )
    return verts, faces


def _resolve(
    zf: zipfile.ZipFile,
    objects: dict[str, ET.Element],
    object_id: str,
    models: dict[str, dict[str, ET.Element]],
) -> list[tuple[Any, Any]]:
    """The (vertices, faces) leaves of an object with component transforms applied (Orca sub-models included)."""
    obj = objects.get(object_id)
    if obj is None:
        return []
    own = _mesh_of(obj)
    if own is not None:
        return [own]
    out: list[tuple[Any, Any]] = []
    for comp in obj.findall("c:components/c:component", _NS):
        path = comp.get(f"{{{_PNS}}}path")
        pool = objects
        if path:
            name = path.lstrip("/")
            if name not in models:
                models[name] = _objects_of(zf.read(name))
            pool = models[name]
        m = matrix_from_3mf(comp.get("transform"))
        for verts, faces in _resolve(zf, pool, comp.get("objectid", ""), models):
            out.append(((m[:3, :3] @ verts.T).T + m[:3, 3], faces))
    return out


def _objects_of(xml: bytes) -> dict[str, ET.Element]:
    root = ET.fromstring(xml)
    return {o.get("id", ""): o for o in root.findall("c:resources/c:object", _NS)}


def object_meshes(path: str | Path, *, world: bool = True) -> dict[int, trimesh.Trimesh]:
    """object id -> mesh with components applied; `world=True` also applies the build-item transform (plate position).

    The assembly check uses `world=False`: the item transform is what `arrange` / `retarget` / the slicer move around,
    the object's own coordinates are what `assembly position` recorded.
    """
    out: dict[int, trimesh.Trimesh] = {}
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
        objects = {
            o.get("id", ""): o for o in root.findall("c:resources/c:object", _NS)
        }
        models: dict[str, dict[str, ET.Element]] = {}
        for item in root.findall("c:build/c:item", _NS):
            oid = item.get("objectid", "")
            world_m = matrix_from_3mf(item.get("transform")) if world else np.eye(4)
            leaves = _resolve(zf, objects, oid, models)
            if not leaves:
                continue
            verts_all, faces_all, offset = [], [], 0
            for verts, faces in leaves:
                verts_all.append((world_m[:3, :3] @ verts.T).T + world_m[:3, 3])
                faces_all.append(faces + offset)
                offset += len(verts)
            out[int(oid)] = trimesh.Trimesh(
                np.vstack(verts_all), np.vstack(faces_all), process=True
            )
    return out


def embedded(path: str | Path) -> dict[str, Any] | None:
    """The `Metadata/3d_assembly.json` document of a 3MF, or None."""
    try:
        with zipfile.ZipFile(path) as zf:
            if ENTRY not in zf.namelist():
                return None
            data: dict[str, Any] = json.loads(zf.read(ENTRY))
            return data
    except (zipfile.BadZipFile, json.JSONDecodeError):
        return None


@dataclass
class Positioned:
    spec: AssemblySpec
    meshes: dict[
        str, tuple[trimesh.Trimesh, str]
    ]  # part -> (assembled-frame mesh, source)
    drift: list[dict[str, Any]] = field(default_factory=list)
    layout: dict[str, Any] = field(default_factory=dict)


def read_positioned(path: str | Path) -> Positioned:
    """Load the spec and the printed geometry of a positioned 3MF, mapped back into the assembled frame."""
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(str(p), command=CMD)
    doc = embedded(p)
    if doc is None or doc.get("schema") != SCHEMA_3MF:
        raise InvalidArgument(
            "3mf",
            str(p),
            [
                f"a 3MF written by `3d assembly position` (entry {ENTRY}, schema {SCHEMA_3MF})"
            ],
            command=CMD,
            extra="Run `3d assembly position SPEC --3mf OUT.3mf --yes` to make one; "
            "a plain slicer project has no mates or poses.",
        )
    spec = parse_spec(
        doc["spec"], path=None, base_dir=Path(doc.get("spec_root") or p.parent)
    )
    worlds = object_meshes(p, world=False)
    meshes: dict[str, tuple[trimesh.Trimesh, str]] = {}
    drift: list[dict[str, Any]] = []
    for name, rec in doc.get("layout", {}).items():
        mesh = worlds.get(int(rec["object"]))
        if mesh is None:
            drift.append(
                {
                    "part": name,
                    "reason": f"object {rec['object']} is missing from the 3MF",
                }
            )
            continue
        matrix = np.array(rec["matrix"], dtype=float).reshape(4, 4)
        rb = np.array(rec["local_bounds"], dtype=float).reshape(2, 3)
        moved = float(np.abs(mesh.bounds - rb).max())
        dvol = abs(float(mesh.volume) - float(rec["volume_mm3"]))
        if moved <= 0.01 and dvol <= max(0.5, 0.001 * float(rec["volume_mm3"])):
            back = mesh.copy()
            back.apply_transform(np.linalg.inv(matrix))
            meshes[name] = (back, f"3mf:{p.name}#{rec['object']}")
        else:
            drift.append(
                {
                    "part": name,
                    "reason": f"object bounds moved by {moved:.3f} mm / volume changed by {dvol:.2f} mm3 since "
                    "`assembly position`: it was moved, re-oriented or edited after the layout was written; "
                    "the spec's mesh is checked for this part instead",
                    "bounds_shift_mm": round(moved, 4),
                    "volume_change_mm3": round(dvol, 4),
                }
            )
    return Positioned(spec, meshes, drift, doc.get("layout", {}))


def tagged_objects(path: str | Path) -> list[tuple[str, trimesh.Trimesh, dict[str, str]]]:
    """(name, world mesh, `3d:*` tags) of every object of a 3MF that carries part-role tags.

    Used by `arrange_pack` for tagged projects: it reads the objects with this stdlib XML parser (no lxml) so the
    tags and the object names are exact, whatever trimesh would have called the geometry.
    """
    import parts_3mf

    tags = {t.object_id: t for t in parts_3mf.tags(path)}
    out: list[tuple[str, trimesh.Trimesh, dict[str, str]]] = []
    for oid, mesh in sorted(object_meshes(path).items()):
        tag = tags.get(oid)
        name = tag.name if tag else f"object{oid}"
        meta = {k: v for k, v in (tag.metadata.items() if tag else []) if k.startswith("3d:")}
        out.append((name, mesh, meta))
    return out
