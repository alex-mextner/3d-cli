"""scene.py — load the parts of an assembly spec as posed meshes and exact solids (trimesh + manifold3d).

WHAT: turns `spec.AssemblySpec` into a `Scene`: one `Body` per part with its mesh moved into the ASSEMBLED pose
  (float64) and a lazily built manifold3d solid. Poses follow OpenSCAD: inside one op `rotate` (deg, X then Y then Z)
  is applied before `translate`; a list of ops is applied in order.

WHY: every check (overlap, clearance, sweeps, ergonomics) needs the same posed solids; building them once and
  freeing them after a pair keeps memory flat ("one pair at a time").

INVARIANTS:
  - Input mesh files are never modified; poses are applied to in-memory copies.
  - A mesh over `settings.max_faces` or one manifold3d rejects is refused with a structured error naming the part
    (never silently decimated or "repaired" into a different solid).
  - Solids are float32 on the way in (manifold3d's mesh constructor) and double inside; the sweep and boolean
    results are good to ~1e-5 mm at the coordinates of a desktop printer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import manifold3d as m3
import numpy as np
import trimesh

from assembly.spec import CMD, AssemblySpec, PartSpec, Pose, mesh_path
from errors import InputNotFound, InvalidArgument

Matrix = Any  # np.ndarray (4, 4)
Box = tuple[
    float, float, float, float, float, float
]  # minx, miny, minz, maxx, maxy, maxz


def rotation_matrix_deg(rx: float, ry: float, rz: float) -> Matrix:
    """OpenSCAD `rotate([rx, ry, rz])`: Rz(rz) @ Ry(ry) @ Rx(rx), degrees, homogeneous 4x4."""
    ax, ay, az = (math.radians(v) for v in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = (
        math.cos(ax),
        math.sin(ax),
        math.cos(ay),
        math.sin(ay),
        math.cos(az),
        math.sin(az),
    )
    rot_x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    rot_y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rot_z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    out = np.eye(4)
    out[:3, :3] = rot_z @ rot_y @ rot_x
    return out


def translation_matrix(t: Iterable[float]) -> Matrix:
    out = np.eye(4)
    out[:3, 3] = list(t)
    return out


def axis_rotation(
    axis: Iterable[float], degrees: float, origin: Iterable[float] = (0.0, 0.0, 0.0)
) -> Matrix:
    """Rotation of `degrees` about the unit `axis` through `origin` (Rodrigues)."""
    a = np.asarray(list(axis), dtype=float)
    a = a / np.linalg.norm(a)
    th = math.radians(degrees)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    rot = np.eye(3) + math.sin(th) * k + (1 - math.cos(th)) * (k @ k)
    o = np.asarray(list(origin), dtype=float)
    out = np.eye(4)
    out[:3, :3] = rot
    out[:3, 3] = o - rot @ o
    return out


def pose_matrix(pose: Pose) -> Matrix:
    """The 4x4 matrix of a spec pose (ops applied in order to the mesh)."""
    total = np.eye(4)
    for kind, nums in pose:
        if kind == "translate":
            op = translation_matrix(nums)
        elif kind == "rotate":
            op = rotation_matrix_deg(*nums)
        elif kind == "scale":
            op = np.diag([nums[0], nums[1], nums[2], 1.0])
        else:
            op = np.array(nums, dtype=float).reshape(4, 4)
        total = op @ total
    return total


def solid_from_mesh(
    mesh: trimesh.Trimesh, label: str, *, max_faces: int | None = None
) -> m3.Manifold:
    """An exact manifold3d solid of a mesh; refuses oversize or non-manifold input with the part named."""
    if max_faces is not None and len(mesh.faces) > max_faces:
        raise InvalidArgument(
            f"{label}.mesh",
            f"{len(mesh.faces)} faces",
            [f"at most {max_faces} faces"],
            command=CMD,
            extra="Decimate the mesh (`3d mesh decimate`) or raise settings.max_faces; "
            "the checker never simplifies a part on its own.",
        )
    verts = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.uint32)
    solid = m3.Manifold(m3.Mesh(verts, faces))
    if solid.status() != m3.Error.NoError or solid.is_empty():
        raise InvalidArgument(
            f"{label}.mesh",
            f"not a valid solid ({solid.status().name})",
            ["a watertight, consistently wound mesh"],
            command=CMD,
            extra="Repair it first: `3d mesh repair`.",
        )
    return solid


def box_solid(lo: Iterable[float], hi: Iterable[float]) -> m3.Manifold:
    a, b = np.asarray(list(lo), dtype=float), np.asarray(list(hi), dtype=float)
    return m3.Manifold.cube(tuple(np.maximum(b - a, 1e-6)), False).translate(tuple(a))


def solid_box(solid: m3.Manifold) -> Box:
    bb = solid.bounding_box()
    return (bb[0], bb[1], bb[2], bb[3], bb[4], bb[5])


def boxes_overlap(a: Box, b: Box, pad: float = 0.0) -> bool:
    return all(a[i] <= b[i + 3] + pad and b[i] <= a[i + 3] + pad for i in range(3))


def box_intersection(a: Box, b: Box, pad: float = 0.0) -> Box:
    lo = [max(a[i], b[i]) - pad for i in range(3)]
    hi = [min(a[i + 3], b[i + 3]) + pad for i in range(3)]
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def crop(solid: m3.Manifold, box: Box) -> m3.Manifold:
    """The part of `solid` inside `box` (coarse-to-fine: pairs are judged on the overlap box only)."""
    return solid ^ box_solid(box[:3], box[3:])


@dataclass
class Body:
    """One part in the assembled pose: its spec, mesh (float64) and a lazily built solid."""

    spec: PartSpec
    mesh: trimesh.Trimesh
    source: str  # where the geometry came from: a file path or `3mf:<object>`
    max_faces: int | None = None
    _solid: m3.Manifold | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def box(self) -> Box:
        b = self.mesh.bounds
        return (b[0][0], b[0][1], b[0][2], b[1][0], b[1][1], b[1][2])

    def solid(self) -> m3.Manifold:
        if self._solid is None:
            self._solid = solid_from_mesh(
                self.mesh, f"parts.{self.name}", max_faces=self.max_faces
            )
        return self._solid

    def release(self) -> None:
        """Drop the solid (rebuilt on demand): keeps one pair's working set in memory at a time."""
        self._solid = None


@dataclass
class Scene:
    spec: AssemblySpec
    bodies: dict[str, Body]

    def body(self, name: str) -> Body:
        try:
            return self.bodies[name]
        except KeyError:
            raise InvalidArgument(
                "part", name, sorted(self.bodies), command=CMD
            ) from None

    def pairs(self) -> list[tuple[Body, Body]]:
        names = sorted(self.bodies)
        return [
            (self.bodies[a], self.bodies[b])
            for i, a in enumerate(names)
            for b in names[i + 1 :]
        ]


def load_mesh_file(path: Path, label: str) -> trimesh.Trimesh:
    if not path.is_file():
        raise InputNotFound(str(path), command=CMD)
    try:
        loaded = trimesh.load(str(path), force="mesh", process=True)
    except Exception as exc:
        raise InvalidArgument(
            f"{label}.mesh",
            str(path),
            ["a readable .stl / .obj / .ply / .3mf mesh"],
            command=CMD,
            extra=str(exc),
        ) from exc
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise InvalidArgument(
            f"{label}.mesh", str(path), ["a mesh with triangles"], command=CMD
        )
    return loaded


def load_scene(
    spec: AssemblySpec,
    *,
    meshes: Mapping[str, tuple[trimesh.Trimesh, str]] | None = None,
    only: Iterable[str] | None = None,
) -> Scene:
    """Load every part (or `only` those) posed in the assembled frame.

    `meshes` supplies already-posed geometry for some parts (name -> (mesh, source)), e.g. the printed objects
    of a positioned 3MF mapped back into the assembled frame; the rest come from the spec's mesh paths.
    """
    wanted = set(only) if only is not None else None
    bodies: dict[str, Body] = {}
    for name, part in spec.parts.items():
        if wanted is not None and name not in wanted:
            continue
        if meshes is not None and name in meshes:
            mesh, source = meshes[name]
            mesh = mesh.copy()
        else:
            path = mesh_path(spec, part)
            mesh = load_mesh_file(path, f"parts.{name}")
            mesh.apply_transform(pose_matrix(part.pose))
            source = str(path)
        bodies[name] = Body(part, mesh, source, spec.settings.max_faces)
    return Scene(spec, bodies)
