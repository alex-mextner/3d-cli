"""selfint.py — self-intersection check: pymeshlab when present, else a numpy KD-tree edge/triangle test.

WHAT: `count_self_intersections(V, F)` reports how many faces (pymeshlab) or piercing edge/face pairs
  (built-in) intersect each other without sharing a vertex. The built-in test pairs edge midpoints with
  face centroids through a KD-tree radius query and runs a vectorised Moeller-Trumbore ray/triangle test
  on the surviving pairs.

WHY: a relief or grab can push a surface through another one (toes, a tail against a leg). The gate has to
  say so on a 400k-face mesh in seconds (research: pymeshlab 0.8 s, this test 2.7 s, Open3D 62 s) and with
  core dependencies only. The built-in test finds transversal piercings (an edge through a face); it does
  not see coplanar overlaps. Counts of the two engines have different units and are never compared.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]

CHUNK = 2_000_000


def undirected_edges(F: IntArray) -> IntArray:
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    e.sort(axis=1)
    return np.asarray(np.unique(e, axis=0))


def piercing_pairs(V: Array, F: IntArray) -> tuple[int, int]:
    """(piercing edge/face pairs, candidate pairs tested). Pairs that share a vertex are skipped."""
    from scipy.spatial import cKDTree

    E = undirected_edges(F)
    mid = 0.5 * (V[E[:, 0]] + V[E[:, 1]])
    centroid = V[F].mean(axis=1)
    edge_len = np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1)
    face_radius = np.linalg.norm(V[F] - centroid[:, None], axis=2).max(axis=1)
    radius = float(edge_len.max() / 2 + face_radius.max()) * 1.001
    pairs = cKDTree(mid).sparse_distance_matrix(
        cKDTree(centroid), radius, output_type="ndarray"
    )
    ei, fi = pairs["i"].astype(np.int64), pairs["j"].astype(np.int64)
    shares = (E[ei][:, :, None] == F[fi][:, None, :]).any(axis=(1, 2))
    ei, fi = ei[~shares], fi[~shares]
    hits = 0
    for start in range(0, len(ei), CHUNK):
        a, b = ei[start : start + CHUNK], fi[start : start + CHUNK]
        p0, direction = V[E[a, 0]], V[E[a, 1]] - V[E[a, 0]]
        t0 = V[F[b, 0]]
        e1, e2 = V[F[b, 1]] - t0, V[F[b, 2]] - t0
        h = np.cross(direction, e2)
        det = (e1 * h).sum(axis=1)
        ok = np.abs(det) > 1e-14
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = p0 - t0
        u = (s * h).sum(axis=1) * inv
        q = np.cross(s, e1)
        v = (direction * q).sum(axis=1) * inv
        t = (e2 * q).sum(axis=1) * inv
        hits += int((ok & (u > 0) & (v > 0) & (u + v < 1) & (t > 0) & (t < 1)).sum())
    return hits, int(len(ei))


def count_self_intersections(V: Array, F: IntArray) -> dict[str, Any]:
    try:
        import pymeshlab

        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(vertex_matrix=V, face_matrix=F.astype(np.int32)))
        ms.apply_filter("compute_selection_by_self_intersections_per_face")
        return {
            "engine": "pymeshlab",
            "unit": "faces",
            "count": int(ms.current_mesh().selected_face_number()),
        }
    except ImportError:
        pass
    hits, candidates = piercing_pairs(V, F)
    return {
        "engine": "kdtree-moller-trumbore",
        "unit": "piercing edge/face pairs",
        "count": hits,
        "candidate_pairs": candidates,
    }
