"""overlap.py — pairwise solid overlap: where two parts occupy the same space, and by how much.

WHAT: `pair_regions(a, b)` intersects two exact solids (manifold3d) and splits the result into connected overlap
  REGIONS, each with volume, location (centre + box, assembly frame), penetration depth, and the faces that cause
  it (area-weighted dominant normal per part, via manifold3d's face provenance).
  `min_gap(a, b)` is the exact minimum separation of two solids.

HOW (coarse to fine, one pair at a time):
  1. bounding boxes disjoint -> no overlap, nothing built;
  2. both solids are cropped to the overlap box (+ pad), so a 13k-triangle shell is judged on the few hundred
     triangles that can matter;
  3. only then the boolean runs; components below `eps_mm3` are noise.

PENETRATION DEPTH: the smallest width of the overlap region over the coordinate axes, the PCA axes of the region
  and the dominant face normals of both parts. For a rib pressed into a wall it is the interference (0.15 mm), for
  a seated face the thickness of the sliver (6 um, #140). It is an UPPER bound of the shortest separating move
  along those directions, not a general penetration-depth solver; the report names it `depth_mm`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import manifold3d as m3
import numpy as np
import trimesh

from assembly.scene import Box, box_intersection, boxes_overlap, crop, solid_box

Vec = tuple[float, float, float]


@dataclass
class Region:
    """One connected overlap volume between parts `a` and `b`."""

    a: str
    b: str
    volume_mm3: float
    center: Vec
    lo: Vec
    hi: Vec
    depth_mm: float
    depth_axis: Vec
    faces: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # part -> {normal, area_mm2}
    mesh: trimesh.Trimesh | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "a": self.a,
            "b": self.b,
            "volume_mm3": round(self.volume_mm3, 4),
            "center": [round(c, 3) for c in self.center],
            "bbox": {
                "min": [round(c, 3) for c in self.lo],
                "max": [round(c, 3) for c in self.hi],
            },
            "depth_mm": round(self.depth_mm, 4),
            "depth_axis": [round(c, 3) for c in self.depth_axis],
            "faces": self.faces,
        }


def _min_width(verts: Any, extra_dirs: list[Any]) -> tuple[float, Vec]:
    """Smallest extent of a point set over axes, PCA axes and `extra_dirs` -> (width, direction)."""
    dirs: list[Any] = [
        np.array(v, dtype=float) for v in ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    ]
    if len(verts) >= 4:
        centred = verts - verts.mean(axis=0)
        try:
            _, _, vt = np.linalg.svd(centred, full_matrices=False)
            dirs.extend(vt)
        except np.linalg.LinAlgError:
            pass
    dirs.extend(extra_dirs)
    best_w, best_d = float("inf"), dirs[0]
    for d in dirs:
        n = float(np.linalg.norm(d))
        if n < 1e-9:
            continue
        d = d / n
        proj = verts @ d
        w = float(proj.max() - proj.min())
        if w < best_w:
            best_w, best_d = w, d
    return best_w, (float(best_d[0]), float(best_d[1]), float(best_d[2]))


def _face_provenance(
    mesh: m3.Mesh, verts: Any, tris: Any, ids: dict[int, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per part: the dominant face normal and total area of the faces it contributes to the overlap region.

    manifold3d keeps the id of the input each output triangle came from; normals are clustered into ~8 degree
    cells and the heaviest cell is the offending face.
    """
    result: dict[str, dict[str, Any]] = {}
    dominant: dict[str, Any] = {}
    runs = list(mesh.run_index)
    for k, oid in enumerate(mesh.run_original_id):
        name = ids.get(int(oid))
        if name is None:
            continue
        t = tris[runs[k] // 3 : runs[k + 1] // 3]
        if len(t) == 0:
            continue
        p = verts[t]
        cross = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        area = np.linalg.norm(cross, axis=1) / 2.0
        unit = cross / np.maximum(np.linalg.norm(cross, axis=1, keepdims=True), 1e-12)
        cells: dict[tuple[int, int, int], list[Any]] = {}
        for u, a_ in zip(unit, area, strict=True):
            key = (int(round(u[0] * 4)), int(round(u[1] * 4)), int(round(u[2] * 4)))
            cell = cells.setdefault(key, [np.zeros(3), 0.0])
            cell[0] = cell[0] + u * a_
            cell[1] += float(a_)
        best = max(cells.values(), key=lambda c: c[1])
        vec = best[0] / max(best[1], 1e-12)
        vec = vec / max(float(np.linalg.norm(vec)), 1e-12)
        prev = result.get(name)
        total = float(area.sum()) + (prev["area_mm2"] if prev else 0.0)
        if prev is None or best[1] > prev["_cell"]:
            result[name] = {"normal": [round(float(c), 3) for c in vec], "area_mm2": total, "_cell": best[1]}
            dominant[name] = vec
        else:
            prev["area_mm2"] = total
    for entry in result.values():
        entry.pop("_cell", None)
        entry["area_mm2"] = round(entry["area_mm2"], 4)
    return result, dominant


def _touches(a: Box, b: Box, pad: float) -> bool:
    return boxes_overlap(a, b, pad)


def pair_regions(
    name_a: str,
    solid_a: m3.Manifold,
    name_b: str,
    solid_b: m3.Manifold,
    *,
    eps_mm3: float,
    pad_mm: float = 1.0,
    keep_mesh: bool = True,
) -> list[Region]:
    """The connected overlap regions of two solids (empty when disjoint or below `eps_mm3`)."""
    box_a, box_b = solid_box(solid_a), solid_box(solid_b)
    if not _touches(box_a, box_b, 0.0):
        return []
    window = box_intersection(box_a, box_b, pad_mm)
    ca, cb = crop(solid_a.as_original(), window), crop(solid_b.as_original(), window)
    if ca.is_empty() or cb.is_empty():
        return []
    # `crop` builds a new solid; re-tag the cropped pieces so every output face maps back to its part.
    ca_o, cb_o = ca.as_original(), cb.as_original()
    ids = {ca_o.original_id(): name_a, cb_o.original_id(): name_b}
    inter = ca_o ^ cb_o
    if inter.is_empty() or inter.volume() < eps_mm3:
        return []
    regions: list[Region] = []
    for comp in inter.decompose():
        vol = comp.volume()
        if vol < eps_mm3:
            continue
        mesh = comp.to_mesh()
        verts = np.asarray(mesh.vert_properties[:, :3], dtype=float)
        tris = np.asarray(mesh.tri_verts, dtype=np.int64)
        faces, dominant = _face_provenance(mesh, verts, tris, ids)
        width, axis = _min_width(verts, list(dominant.values()))
        tm = trimesh.Trimesh(verts, tris, process=False)
        center = tm.center_mass if tm.is_volume else verts.mean(axis=0)
        lo, hi = verts.min(axis=0), verts.max(axis=0)
        regions.append(
            Region(
                name_a,
                name_b,
                float(vol),
                (float(center[0]), float(center[1]), float(center[2])),
                (float(lo[0]), float(lo[1]), float(lo[2])),
                (float(hi[0]), float(hi[1]), float(hi[2])),
                width,
                axis,
                faces,
                tm if keep_mesh else None,
            )
        )
    regions.sort(key=lambda r: -r.volume_mm3)
    return regions


def min_gap(
    solid_a: m3.Manifold,
    solid_b: m3.Manifold,
    search_mm: float,
    window: Box | None = None,
) -> float:
    """Exact minimum separation (mm) of two solids, capped at `search_mm`; 0 for touching or overlapping solids.

    `window` restricts both solids to a box first (a mate's zone), which is also what makes it fast.
    """
    a, b = solid_a, solid_b
    if window is not None:
        a, b = crop(a, window), crop(b, window)
    if a.is_empty() or b.is_empty():
        return float(search_mm)
    box_a, box_b = solid_box(a), solid_box(b)
    if not boxes_overlap(box_a, box_b, search_mm):
        return float(search_mm)
    return float(min(a.min_gap(b, search_mm), search_mm))


def _window_mask(mesh: trimesh.Trimesh, window: Box, margin: float) -> Any:
    """Faces whose bounding box meets `window` (grown by `margin`)."""
    tri = mesh.triangles
    lo, hi = tri.min(axis=1), tri.max(axis=1)
    wl = np.array(window[:3]) - margin
    wh = np.array(window[3:]) + margin
    return np.all((hi >= wl) & (lo <= wh), axis=1)


def _surface_points(
    mesh: trimesh.Trimesh, window: Box, spacing: float, cap: int, seed: int
) -> tuple[Any, float]:
    """Surface samples (random, ~one per spacing^2, plus vertices) that lie inside `window`; returns (points, spacing)."""
    mask = _window_mask(mesh, window, 0.0)
    if not mask.any():
        return np.zeros((0, 3)), spacing
    sub = trimesh.Trimesh(mesh.vertices, mesh.faces[mask], process=False)
    area = float(sub.area)
    used = spacing
    n = int(area / (spacing * spacing)) + 1
    if n > cap:
        used = math.sqrt(area / cap)
        n = cap
    pts = trimesh.sample.sample_surface(sub, n, seed=seed)[0]
    pts = np.vstack([pts, sub.vertices])
    wl, wh = np.array(window[:3]), np.array(window[3:])
    keep = np.all((pts >= wl) & (pts <= wh), axis=1)
    return pts[keep], used


def sampled_gap(
    a: trimesh.Trimesh,
    b: trimesh.Trimesh,
    window: Box,
    search_mm: float,
    *,
    lift: Sequence[float] | None = None,
    spacing_mm: float = 0.1,
    cap: int = 40_000,
) -> tuple[float, float]:
    """Minimum separation of two surfaces measured only inside `window` -> (gap, sample spacing used).

    Surface points of each mesh inside the window (random samples every ~`spacing_mm`, plus every vertex) are
    measured against the OTHER whole mesh with exact point-to-triangle distances, in both directions, so the cut
    faces of a box crop never count (unlike cropping both solids). `lift` moves `a` rigidly first (the lateral
    measurement of an insertion mate: its seat face moves away, the side faces stay opposite each other). The result
    is exact for parallel faces and within ~spacing/2 for curved or skewed ones; it is capped at `search_mm`.
    """
    shift = np.zeros(3) if lift is None else np.asarray(lift, dtype=float)
    pa, used_a = _surface_points(a, window, spacing_mm, cap, 3)
    pb, used_b = _surface_points(b, window, spacing_mm, cap, 5)
    best = float(search_mm)
    best = min(best, _nearest(b, pa + shift))
    best = min(best, _nearest(a, pb - shift))
    return best, max(used_a, used_b)


def _nearest(mesh: trimesh.Trimesh, pts: Any, chunk: int = 4000) -> float:
    """Smallest exact point-to-surface distance of `pts` to `mesh`, in chunks (bounded memory)."""
    best = float("inf")
    with np.errstate(all="ignore"):  # degenerate triangles of an STL make trimesh divide by zero harmlessly
        for i in range(0, len(pts), chunk):
            d = trimesh.proximity.closest_point(mesh, pts[i : i + chunk])[1]
            best = min(best, float(np.min(d)))
    return best
