"""Synthetic meshes and label files for the `3d colour-regions` tests (sphere, subdivided box).

Everything is analytic: a 10 mm sphere split at z = 0 has two 628 mm2 halves; a 12 mm box has twelve 12 mm crease edges.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray

from colour_regions.labels import mesh_digest, write_labels


def sphere(radius: float = 10.0, subdivisions: int = 4, jitter: float = 0.0, seed: int = 3) -> trimesh.Trimesh:
    """An icosphere; `jitter` moves every vertex by that many mm in a random direction (a re-meshed, perturbed copy)."""
    m = trimesh.creation.icosphere(subdivisions=subdivisions, radius=radius)
    if jitter:
        rng = np.random.default_rng(seed)
        v = np.asarray(m.vertices) + rng.normal(0.0, jitter, m.vertices.shape)
        m = trimesh.Trimesh(v, m.faces, process=True)
    return m


def fine_box(side: float = 12.0, edge: float = 0.5) -> trimesh.Trimesh:
    """A cube split into small triangles, vertices merged: flat faces, twelve sharp 90 degree edges."""
    b = trimesh.creation.box((side, side, side))
    v, f = trimesh.remesh.subdivide_to_size(np.asarray(b.vertices), np.asarray(b.faces), edge)
    return trimesh.Trimesh(v, f, process=True)


def centroid_z(m: trimesh.Trimesh) -> NDArray[np.float64]:
    return np.asarray(m.triangles_center)[:, 2]


def save_mesh(m: trimesh.Trimesh, path: Path) -> Path:
    m.export(str(path))
    return path


def save_labels(
    path: Path, face_label: NDArray[np.integer[Any]], names: list[str], m: trimesh.Trimesh | None = None, mesh_key: str | None = None
) -> Path:
    """npz label file; `m` adds its mesh_digest, `mesh_key` the relative path of its mesh."""
    return write_labels(path, face_label, names, digest=mesh_digest(m.triangles) if m is not None else None, mesh=mesh_key)


def write_rules(path: Path, rules: list[dict[str, Any]], **top: float) -> Path:
    path.write_text(json.dumps({"rules": rules, **top}), encoding="utf-8")
    return path


#: two coloured groups meeting on the sphere's equator band, default colour in between
SPHERE_RULES: list[dict[str, Any]] = [
    {"label": "top", "group": "top", "assign": {"key": "W"}, "depth_mm": 1.5, "pitch_mm": 0.4},
    {"label": "bottom", "group": "bottom", "assign": {"mix": "WR"}, "depth_mm": 1.5, "pitch_mm": 0.4, "min_speck_mm3": 0.1},
    {"label": "belt", "group": "default", "assign": None},
]


def sphere_labels(m: trimesh.Trimesh, gap: float = 3.0) -> tuple[NDArray[np.int16], list[str]]:
    """top (z > gap), bottom (z < -gap), belt in between (default colour)."""
    z = centroid_z(m)
    fl = np.full(len(z), 2, np.int16)
    fl[z > gap] = 0
    fl[z < -gap] = 1
    return fl, ["top", "bottom", "belt"]


def write_project(
    stem: Path, ball: trimesh.Trimesh, work: Path, shift: list[float], top_assign: dict[str, Any], bottom_assign: dict[str, Any]
) -> Path:
    """A built project as `verify --parts` reads it: STEM.manifest.json + STEM.parts/N_part.stl in the project frame
    (`shift`): a default part under everything (1), the `top` patch (2) and the `bottom` patch (3) of a `build` over it."""
    from colour_regions.surface import load_mesh

    pdir = Path(str(stem) + ".parts")
    pdir.mkdir(parents=True)
    meshes = (ball, load_mesh(work / "patches" / "top.ply"), load_mesh(work / "patches" / "bottom.ply"))
    for k, mesh in enumerate(meshes, start=1):
        moved = mesh.copy()
        moved.apply_translation(shift)
        moved.export(str(pdir / f"{k}_part.stl"))
    man = {
        "shift": shift,
        "filaments": [{"slot": 1, "key": "K"}, {"slot": 2, "key": "W"}, {"slot": 3, "key": "R"}],
        "parts": [{"assign": {"slot": 1}}, {"assign": top_assign}, {"assign": bottom_assign}],
    }
    Path(str(stem) + ".manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return stem
