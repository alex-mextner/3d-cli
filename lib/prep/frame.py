"""frame.py — load a model and move it into the print frame (Z up, on the bed, XY-centred).

WHAT: `load_mesh` reads STL/3MF/OBJ/PLY into one trimesh; `to_print_frame` shifts it so the lowest
  point is Z = 0 and the bounding box is centred on X = 0, Y = 0; `rotated` applies a rotation first.
WHY: every prep step (orientation, overhang, stability, regions) assumes this frame, so there is one
  place that establishes it. Also the content digest that keys the work-dir cache.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from errors import InputNotFound, InvalidArgument

MESH_SUFFIXES = (".stl", ".3mf", ".obj", ".ply", ".off", ".glb", ".gltf")


def load_mesh(path: str | Path, *, process: bool = True) -> trimesh.Trimesh:
    """One mesh from a model file; multi-object files (3MF plates) are merged with their transforms applied."""
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(str(p), command="prep")
    if p.suffix.lower() not in MESH_SUFFIXES:
        raise InvalidArgument(
            "model file", p.suffix or str(p), list(MESH_SUFFIXES), command="prep"
        )
    loaded: Any = trimesh.load(str(p), force="mesh", process=process)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise InvalidArgument(
            "model file", str(p), ["a file with at least one triangle"], command="prep"
        )
    return loaded


def to_print_frame(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Copy of `mesh` with the bbox centred on X/Y and its lowest point on Z = 0."""
    out = mesh.copy()
    lo, hi = out.bounds
    out.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return out


def rotated(mesh: trimesh.Trimesh, rotation: Any) -> trimesh.Trimesh:
    """Copy rotated by a 3x3 matrix about the origin, then put into the print frame."""
    rot = np.asarray(rotation, dtype=float)
    if rot.shape != (3, 3):
        raise InvalidArgument(
            "rotation", str(rot.shape), ["a 3x3 matrix"], command="prep"
        )
    out = mesh.copy()
    transform = np.eye(4)
    transform[:3, :3] = rot
    out.apply_transform(transform)
    return to_print_frame(out)


def file_digest(path: str | Path, *, length: int = 16) -> str:
    """Hex prefix of the SHA-256 of a file's bytes (the cache key of a model)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:length]


def mesh_digest(mesh: trimesh.Trimesh, *, length: int = 16) -> str:
    """Hex prefix of a hash of the vertex and face arrays (cache key of an in-memory mesh)."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(mesh.vertices, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(mesh.faces, dtype=np.int64).tobytes())
    return h.hexdigest()[:length]
