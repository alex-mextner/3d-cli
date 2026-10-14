"""Synthetic insert/socket fixtures for `3d fit-check` tests: a box insert standing on the
floor of a rectangular pocket in a plate, all in one assembly frame (mm)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

PLATE = (40.0, 30.0)          # plate footprint, centred on the origin
FLOOR = 2.0                   # pocket floor height = insert bottom


def box(sx: float, sy: float, sz: float, z0: float = 0.0, dx: float = 0.0) -> Any:
    from manifold3d import Manifold
    return Manifold.cube((sx, sy, sz), center=True).translate((dx, 0.0, z0 + sz / 2))


def write_manifold(path: Path, m: Any) -> Path:
    import trimesh
    mesh = m.to_mesh()
    trimesh.Trimesh(vertices=mesh.vert_properties[:, :3], faces=mesh.tri_verts, process=False).export(path)
    return path


def write_insert(path: Path, size: tuple[float, float, float] = (20.0, 10.0, 5.0), dx: float = 0.0) -> Path:
    """a box insert standing on the pocket floor"""
    return write_manifold(path, box(*size, z0=FLOOR, dx=dx))


def write_plate(path: Path, pocket: tuple[float, float] = (20.4, 10.4), depth: float = 4.0, dx: float = 0.0) -> Path:
    """a plate whose top is `depth` above the pocket floor, with a rectangular pocket"""
    plate = box(*PLATE, FLOOR + depth, dx=dx)
    pocket_cut = box(pocket[0], pocket[1], depth + 1.0, z0=FLOOR, dx=dx)
    return write_manifold(path, plate - pocket_cut)
