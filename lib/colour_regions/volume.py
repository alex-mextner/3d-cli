"""volume.py — conformal colour volume of a set of mesh faces (a Voronoi cell of the skin).

WHAT: `shell_volume(s, sel, depth, pitch, out)` writes the closed volume of every point of space whose NEAREST skin
  face is one of the selected faces, within `depth` mm of it, as a PLY (one cell per connected piece of the selection).
  The colour boundary leaves the skin along the surface normal and starts exactly on the label border; a slicer step
  cuts the model with it like any other STL region (`shape: {type: stl}` of a `3d prep` colour spec).
METHOD: skin samples (spacing ~0.7 pitch) of the selected faces and of all other outer faces near them are binned on a
  voxel grid; two exact Euclidean distance transforms (scipy, sampling = pitch) give d_sel and d_oth;
  F = min(depth - d_sel, d_oth - d_sel) (> 0 inside the cell); a Gaussian of 0.8 voxel smooths the staircase; marching
  cubes (scikit-image) on F = LEVEL (~0); cells over `DECIMATE_ABOVE` faces are quadric-decimated to `DECIMATE_TO`
  (pymeshlab, topology preserving) when pymeshlab is installed, else kept at full resolution. The cell is NOT clipped
  to the inside of the model: it protrudes up to `depth` outside the skin and the slicer step intersects it with the
  model, so the skin stays the model's own triangles.
INVARIANTS:
  * Border placement error = the voxel quantisation of the two distance fields: <= ~0.5 pitch at worst.
  * The output file is read back the way a slicer step reads it (vertices merged): closed, consistently wound,
    positive volume, no inverted or hollow piece; otherwise the call fails (exit 1) instead of writing a bad cutter.
ASSUMPTIONS: millimetres; the cutter is only meaningful against the same mesh the faces belong to.
PAST BUGS: marching cubes leaves zero-area slivers (dropped, holes filled); an STL round trip (float32 vertex
  re-merge) opened seams and one piece came out inverted (negative volume), hence PLY output and an explicit
  orientation check; fast_simplification flipped and opened these meshes, hence pymeshlab for decimation.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from scipy import ndimage as ndi

from colour_regions import CMD
from colour_regions.surface import Array, Mask, Surf, load_mesh, sample_surface, submesh
from errors import GateFailure, UsageError

#: iso level off zero: F is exactly 0 on lattice nodes in flat fields (a vertex then coincides with its neighbour's and
#: merging it makes the mesh non-manifold)
LEVEL = 1.2347e-4
DECIMATE_ABOVE = 150_000
DECIMATE_TO = 120_000
MAX_VOXELS = 140e6
#: the voxel grid extends `depth` + this past the selected faces; faces within CANDIDATE_MARGIN_MM of the grid compete
GRID_MARGIN_MM = 1.5
CANDIDATE_MARGIN_MM = 3.0
#: connected pieces of a selection under this area (mm2) are too small to cut and are skipped
MIN_CELL_AREA_MM2 = 0.3


def _samples(s: Surf, idx: Array, spacing: float, seed: int) -> Array:
    return sample_surface(submesh(s.mesh, idx), spacing, seed)[0]


def _pymeshlab() -> Any | None:
    try:
        import pymeshlab
    except (ImportError, OSError):
        return None
    return pymeshlab


def _decimate(verts: Array, faces: Array, target: int) -> tuple[Array, Array]:
    """Topology-preserving quadric edge collapse (pymeshlab); unchanged when pymeshlab is not installed."""
    pm = _pymeshlab()
    if pm is None:
        print(
            f"  note: pymeshlab not installed: a cell of {len(faces)} faces stays at full resolution "
            "(pip install pymeshlab to decimate it)",
            file=sys.stderr,
        )
        return verts, faces
    ms = pm.MeshSet()
    ms.add_mesh(pm.Mesh(np.asarray(verts, float), np.asarray(faces, np.int32)))
    ms.meshing_decimation_quadric_edge_collapse(
        targetfacenum=target, preservetopology=True, qualitythr=0.5, optimalplacement=True, autoclean=True
    )
    o = ms.current_mesh()
    return o.vertex_matrix(), o.face_matrix()


def _cell(s: Surf, sel: Mask, depth: float, pitch: float, full_reach: bool, smooth_vox: float = 0.8) -> trimesh.Trimesh:
    """The colour cell of one connected piece of faces `sel`."""
    from skimage.measure import marching_cubes

    fi = np.nonzero(sel & s.outer)[0]
    v = s.mesh.vertices[s.mesh.faces[fi]].reshape(-1, 3)
    # The grid reaches `depth + GRID_MARGIN_MM` past the selected faces and the competing faces are the ones whose
    # centroid lies within CANDIDATE_MARGIN_MM of it: the set (and, as the samples are drawn per set, the very samples)
    # the Chekhov colour build was proven with. Faces whose TRIANGLE reaches into the grid although their centroid does
    # not (giant faces) are sampled as a second set, empty on a finely meshed skin. A competitor up to `depth` past a
    # cell point counts as well (a thin plate coloured from both sides: the far wall is `2 * depth` away), which the
    # legacy grid cannot see; with `full_reach` the grid grows by whole voxels (the lattice, hence every old distance
    # value, stays where it was) and the faces reaching into the growth join the second set.
    base_lo, base_hi = v.min(0) - (depth + GRID_MARGIN_MM), v.max(0) + (depth + GRID_MARGIN_MM)
    grow = math.ceil(depth / pitch) * pitch if full_reach else 0.0
    lo, hi = base_lo - grow, base_hi + grow
    others = s.outer & ~sel
    base = others & np.all((s.c > base_lo - CANDIDATE_MARGIN_MM) & (s.c < base_hi + CANDIDATE_MARGIN_MM), axis=1)
    tri_lo, tri_hi = s.tri_bounds
    reach = others & ~base & np.all((tri_hi > lo - CANDIDATE_MARGIN_MM) & (tri_lo < hi + CANDIDATE_MARGIN_MM), axis=1)
    near, extra = np.nonzero(base)[0], np.nonzero(reach)[0]
    shape = np.ceil((hi - lo) / pitch).astype(int) + 1
    if shape.prod() > MAX_VOXELS:
        raise UsageError(
            f"voxel grid {tuple(int(x) for x in shape)} = {shape.prod() / 1e6:.0f} M voxels at pitch {pitch}",
            command=CMD,
            remediation=["Raise the pitch (pitch_mm / --pitch) or split the selection into smaller pieces."],
        )
    p_sel = _samples(s, fi, 0.7 * pitch, 1)
    samples = [_samples(s, ids, 0.7 * pitch, seed) for ids, seed in ((near, 2), (extra, 3)) if len(ids)]
    p_oth = np.vstack(samples) if samples else np.empty((0, 3))

    def occupancy(pts: Array) -> Mask:
        occ = np.zeros(shape, bool)
        if len(pts):
            ij = np.floor((pts - lo) / pitch + 0.5).astype(int)
            ok = np.all((ij >= 0) & (ij < shape), axis=1)
            occ[tuple(ij[ok].T)] = True
        return occ

    d_sel = ndi.distance_transform_edt(~occupancy(p_sel), sampling=pitch).astype(np.float32)
    if len(p_oth):
        f = np.minimum(
            depth - d_sel,
            ndi.distance_transform_edt(~occupancy(p_oth), sampling=pitch).astype(np.float32) - d_sel,
        )
    else:
        f = depth - d_sel
    del d_sel
    if smooth_vox > 0:
        f = ndi.gaussian_filter(f, smooth_vox)
    f = np.pad(f, 1, constant_values=-1.0)
    verts, faces, _, _ = marching_cubes(f, LEVEL, spacing=(pitch, pitch, pitch))
    del f
    verts = verts + lo - pitch
    if len(faces) > DECIMATE_ABOVE:
        verts, faces = _decimate(verts, faces, DECIMATE_TO)
    m = trimesh.Trimesh(verts, faces, process=True)
    m.update_faces(m.nondegenerate_faces())
    m.remove_unreferenced_vertices()
    if not m.is_watertight:
        trimesh.repair.fill_holes(m)
    if m.is_watertight and m.volume < 0:
        # skimage's marching cubes winds outward for F > level; fix_normals() mis-orients meshes made of several
        # bodies, so it is not used
        m.invert()
    return m


def _repair(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """Drop pinch slivers (pieces of < 4 faces), drop faces on non-manifold edges, split non-manifold vertices, close
    the small holes that leaves (pymeshlab; without it only the sliver drop, the hole fill and the winding fix)."""
    # a connected piece of < 4 faces cannot enclose a volume: it is a pinch sliver of the float32 snap and makes
    # meshlab's close_holes spin forever, so it is dropped first
    pieces = m.split(only_watertight=False)
    keep = [c for c in pieces if len(c.faces) >= 4]
    if not keep:
        raise GateFailure(
            f"colour cell without a volume: all {len(pieces)} connected pieces have fewer than 4 faces", command=CMD
        )
    if len(keep) != len(pieces):
        m = trimesh.util.concatenate(keep)
    pm = _pymeshlab()
    if pm is None:
        trimesh.repair.fill_holes(m)
        trimesh.repair.fix_winding(m)
        return m
    ms = pm.MeshSet()
    ms.add_mesh(pm.Mesh(np.asarray(m.vertices, float), np.asarray(m.faces, np.int32)))
    ms.meshing_repair_non_manifold_edges(method=0)
    ms.meshing_repair_non_manifold_vertices(vertdispratio=0)
    ms.meshing_close_holes(maxholesize=30)
    o = ms.current_mesh()
    return trimesh.Trimesh(o.vertex_matrix(), o.face_matrix(), process=True)


def _drop_degenerate(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """Zero-area faces removed - unless that opens a CLOSED surface: such a face is kept (dropping it leaves a hole
    that a hole filler refills with the same face, an endless loop)."""
    nd = m.nondegenerate_faces()
    if nd.all():
        return m
    clean = trimesh.Trimesh(m.vertices, m.faces[nd], process=False)
    clean.remove_unreferenced_vertices()
    clean.merge_vertices()  # the file is read back with merged vertices: judge the closed surface the same way
    return clean if clean.is_watertight or not m.is_watertight else m


def closed_file(m: trimesh.Trimesh, out: Path) -> trimesh.Trimesh:
    """Export as PLY and verify the way a slicer step reads it (vertices merged): closed, consistently wound, positive
    volume. The file stores float32, so the vertices are snapped to float32 FIRST (two marching-cubes vertices a few
    1e-6 mm apart become one and pinch the surface; those pinches are repaired)."""
    m = trimesh.Trimesh(np.asarray(m.vertices, np.float32).astype(float), m.faces, process=True)
    r = m
    for _ in range(3):
        if not (m.is_watertight and m.is_winding_consistent):
            m = _repair(m)
        m = _drop_degenerate(m)
        trimesh.repair.fix_winding(m)
        m.export(str(out))
        r = load_mesh(out)
        if r.is_volume:
            neg = [round(float(c.volume), 2) for c in r.split(only_watertight=False) if c.volume < -0.01]
            if neg:
                raise GateFailure(
                    f"{out}: inverted or hollow pieces with volume {neg} mm3 (a colour cell must be solid)",
                    command=CMD,
                )
            return r
        m = r
    raise GateFailure(
        f"{out}: not a closed positive volume after repair "
        f"(watertight {r.is_watertight}, winding {r.is_winding_consistent}, volume {r.volume:.3f})",
        command=CMD,
        remediation=["Install pymeshlab (pip install pymeshlab) for the full repair, or raise the pitch."],
    )


def shell_volume(
    s: Surf,
    sel: Mask,
    depth: float,
    pitch: float,
    out: str | Path,
    min_area_mm2: float = MIN_CELL_AREA_MM2,
    full_reach: bool = False,
) -> trimesh.Trimesh:
    """One colour cell per connected piece of `sel` (outer body only, pieces under `min_area_mm2` skipped),
    concatenated into the PLY `out`; returns the mesh read back from it. `full_reach`: see `_cell`."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n, lab = s.components(sel & s.outer)
    parts = []
    for i in range(n):
        comp = lab == i
        if s.area[comp].sum() < min_area_mm2:
            continue
        parts.append(_cell(s, comp, depth, pitch, full_reach))
    if not parts:
        raise UsageError(
            f"no faces selected for {out_path}",
            command=CMD,
            remediation=[f"The selection has no connected piece of at least {min_area_mm2} mm2 on the main body."],
        )
    return closed_file(trimesh.util.concatenate(parts), out_path)

