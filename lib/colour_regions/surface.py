"""surface.py — analysis of a triangle mesh on its own FACE GRAPH (no raster, no projection).

WHAT: `Surf(mesh)` wraps a trimesh and exposes what a colour region is derived from, all of it a property of the
  surface itself: `edge_angle` (crease strength per face), `dist_to_faces` (geodesic distance along the surface),
  `components`, `neighbours`, `smooth_faces`, `watershed` (marker-controlled priority flood on the face graph) and the
  shared-edge tables `fa` / `fa_len`.
WHY: a colour border that is a mesh edge between two label groups can be judged against the relief of the same mesh
  (`verify`), and snapped onto it (`snap`); a picture-space outline cannot.
INVARIANTS: `outer` is the largest connected body; every other body (an inward-facing cavity, an inserted part) is
  "neutral": never labelled, never competing in a colour shell.
ASSUMPTIONS: the mesh is loaded with merged vertices, so the face graph is connected across shared vertices' edges.
"""

from __future__ import annotations

import heapq
from pathlib import Path
from typing import Any

import numpy as np
import scipy.sparse as sps
import scipy.sparse.csgraph as csg
import trimesh
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from colour_regions import CMD
from errors import UsageError

Array = NDArray[Any]
Mask = NDArray[np.bool_]
#: seed of every surface sampling (a transfer or a colour volume must give the same result on every run)
SAMPLE_SEED = 7


def load_mesh(path: str | Path, *, process: bool = True) -> trimesh.Trimesh:
    """One trimesh from any file trimesh reads (scenes are concatenated)."""
    m = trimesh.load(str(path), force="mesh", process=process)
    if not isinstance(m, trimesh.Trimesh):
        raise UsageError(f"{path}: not a triangle mesh", command=CMD, remediation=["Pass STL, PLY, OBJ or 3MF holding triangles."])
    return m


def sample_surface(m: trimesh.Trimesh, spacing: float, seed: int = SAMPLE_SEED) -> tuple[Array, Array]:
    """Area-proportional surface samples every ~`spacing` mm plus every face centroid (tiny triangles are never
    missed) -> (points, face id of each point). The seed makes a transfer or a volume reproducible."""
    n = int(m.area / (spacing * spacing)) + 16
    pts, fid = trimesh.sample.sample_surface(m, n, seed=seed)
    return np.vstack([pts, m.triangles_center]), np.r_[fid, np.arange(len(m.faces))]


def submesh(m: trimesh.Trimesh, faces: Array) -> trimesh.Trimesh:
    """The faces `faces` (indices) of `m` as their own mesh, vertices untouched."""
    return trimesh.Trimesh(m.vertices, m.faces[faces], process=False)


class Surf:
    """Face-graph view of a mesh. Face order is the mesh's own (labels index faces)."""

    def __init__(self, path_or_mesh: str | Path | trimesh.Trimesh) -> None:
        m = path_or_mesh if isinstance(path_or_mesh, trimesh.Trimesh) else load_mesh(path_or_mesh)
        self.mesh = m
        self.nf, self.nv = len(m.faces), len(m.vertices)
        self.c: Array = np.asarray(m.triangles_center)
        self.n: Array = np.asarray(m.face_normals)
        self.area: Array = np.asarray(m.area_faces)
        fa = np.asarray(m.face_adjacency)
        self.fa: Array = fa  # (E, 2) faces sharing an edge
        edges = np.asarray(m.vertices)[np.asarray(m.face_adjacency_edges)]
        self.fa_len: Array = np.linalg.norm(np.diff(edges, axis=1)[:, 0], axis=1)  # length of that shared edge (mm)
        w = np.linalg.norm(self.c[fa[:, 0]] - self.c[fa[:, 1]], axis=1)
        self.adj = sps.coo_matrix(
            (np.r_[w, w], (np.r_[fa[:, 0], fa[:, 1]], np.r_[fa[:, 1], fa[:, 0]])), shape=(self.nf, self.nf)
        ).tocsr()
        _, comp = csg.connected_components(self.adj, directed=False)
        self.comp: Array = comp
        self.outer: Mask = comp == np.argmax(np.bincount(comp))
        self._tree: cKDTree | None = None
        self._nb: list[Array] | None = None
        self._tri_bounds: tuple[Array, Array] | None = None
        self.edge_len = float(np.median(m.edges_unique_length))

    @property
    def tri_bounds(self) -> tuple[Array, Array]:
        """Per-face (min corner, max corner) of the triangles."""
        if self._tri_bounds is None:
            tri = np.asarray(self.mesh.triangles)
            self._tri_bounds = (tri.min(axis=1), tri.max(axis=1))
        return self._tri_bounds

    @property
    def tree(self) -> cKDTree:
        """KD-tree of the face centroids."""
        if self._tree is None:
            self._tree = cKDTree(self.c)
        return self._tree

    def smooth_faces(self, x: Array, iters: int = 1) -> Array:
        """Mean over the face-graph neighbours (and the face itself)."""
        a = self.adj.copy()
        a.data[:] = 1.0
        d = np.asarray(a.sum(1)).ravel() + 1.0
        for _ in range(iters):
            x = (a @ x + x) / d
        return x

    def edge_angle(self, scale_mm: float, n_dir: int = 8) -> Array:
        """Angle (deg) between the area-weighted mean normals of the skin `scale_mm` to either side of each face
        centroid, max over `n_dir` tangent directions. A smooth slope gives ~0; a crease, ridge, groove, rim or the
        foot of a lobe gives the turning angle across it."""
        # smoothed face normals: area-weighted mean over the k-ring of faces within ~scale/2 (sparse powers, no per-face loop)
        k = max(1, round(scale_mm / 2.0 / self.edge_len / 0.87))
        a = self.adj.copy()
        a.data[:] = 1.0
        a = a + sps.identity(self.nf, format="csr")
        nn = self.n * self.area[:, None]
        for _ in range(k):
            nn = a @ nn
        nn /= np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-12)
        best = np.zeros(self.nf)
        t0 = np.cross(self.n, np.array([0.0, 0.0, 1.0]))
        bad = np.linalg.norm(t0, axis=1) < 1e-3
        t0[bad] = np.cross(self.n[bad], np.array([1.0, 0.0, 0.0]))
        # zero-area faces have no normal: their tangent stays 0 and the face reads as flat
        t0 /= np.maximum(np.linalg.norm(t0, axis=1, keepdims=True), 1e-12)
        t1 = np.cross(self.n, t0)
        for j in range(n_dir // 2):
            ang = np.pi * j / (n_dir // 2)
            t = np.cos(ang) * t0 + np.sin(ang) * t1
            _, ia = self.tree.query(self.c + t * scale_mm, workers=-1)
            _, ib = self.tree.query(self.c - t * scale_mm, workers=-1)
            deg = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", nn[ia], nn[ib]), -1, 1)))
            best = np.maximum(best, deg)
        return best

    def dist_to_faces(self, mask: Mask, limit: float | None = None) -> Array:
        """Geodesic (face-graph) distance to the face set `mask`; inf beyond `limit`."""
        idx = np.nonzero(mask)[0]
        return np.asarray(
            csg.dijkstra(self.adj, directed=False, indices=idx, min_only=True, limit=np.inf if limit is None else limit)
        )

    def neighbours(self) -> list[Array]:
        """Face-graph neighbours of every face."""
        if self._nb is None:
            self._nb = np.split(self.adj.indices, self.adj.indptr[1:-1])
        return self._nb

    def ring(self, members: Array, comp: Array, k: int) -> Array:
        """Faces of the main body touching the connected piece `k` (faces `members`, component ids `comp`) from outside."""
        nbs = self.neighbours()
        ring = np.unique(np.concatenate([nbs[i] for i in members]))
        return ring[(comp[ring] != k) & self.outer[ring]]

    def components(self, mask: Mask) -> tuple[int, Array]:
        """Connected components of the face set `mask` on the face graph -> (n, label per face, -1 outside)."""
        sub = self.adj[mask][:, mask]
        n, lab = csg.connected_components(sub, directed=False)
        out = np.full(self.nf, -1)
        out[mask] = lab
        return int(n), out

    def watershed(
        self, markers: Array, cost: Array, allowed: Mask | None = None, levels: float | None = None
    ) -> Array:
        """Marker-controlled watershed (priority flood): `markers` int per face (0 = free, > 0 = a label), `cost` per
        face (low floods first; with `levels` the cost is quantised to that step, and equal levels flood
        first-in-first-out, so on a plateau the fronts advance at equal speed and meet in the middle). Flooding is
        restricted to `allowed` (bool per face). Returns the label per face (0 where not reached)."""
        nb = self.neighbours()
        lab = markers.copy()
        if allowed is None:
            allowed = np.ones(self.nf, bool)
        c = np.floor(cost / levels) * levels if levels else cost
        heap: list[tuple[float, int, int, int]] = []
        cnt = 0
        for i in np.nonzero(markers > 0)[0]:
            for j in nb[i]:
                if lab[j] == 0 and allowed[j]:
                    heapq.heappush(heap, (float(c[j]), cnt, int(j), int(lab[i])))
                    cnt += 1
        while heap:
            _, _, j, label = heapq.heappop(heap)
            if lab[j] != 0:
                continue
            lab[j] = label
            for k in nb[j]:
                if lab[k] == 0 and allowed[k]:
                    heapq.heappush(heap, (float(c[k]), cnt, int(k), label))
                    cnt += 1
        return lab
