"""meshfix.py — diagnose a triangle mesh and repair it into one watertight volume, behind a stable backend seam.

WHAT: `diagnose(mesh)` -> `Diagnosis` (edge valence, winding, non-manifold edges/vertices, degenerate faces,
  debris shells, volume); `repair(mesh, backend=...)` -> `RepairResult` (repaired mesh, before/after
  diagnosis, what was done, how far the surface moved). Repair engines are `MeshFixBackend` objects in a
  registry: `BuiltinBackend` ('builtin', numpy/scipy/trimesh/manifold3d) and `ExternalBackend`
  ('meshfix-lib', the separate public `mesh_repair` library, used automatically once it is installed).
WHY: the purchased Chekhov STL needed hours of ad-hoc repair (61 debris shells, 87 non-manifold + 37 open
  edges, 8 pinch vertices) because the only working tool was an external GUI-grade library. The defects are
  local, so the repair is local too: only defective faces are touched, detail is never resampled.
HOW (builtin): per round -> make winding consistent, drop debris shells, cut the faces around every
  non-manifold edge / pinch vertex plus min(round - 1, `ring`) face rings (so the hole is a clean disc), fill
  every boundary loop of up to `max_hole_edges` edges with its minimum-area triangulation (no vertices
  added), merge/dedupe, and
  iterate up to `max_rounds`; finally orient closed bodies outward and cross-check with
  `manifold3d.Manifold.status()`. Open edges alone are filled in place, never ring-cut: cutting a
  well-formed boundary would only enlarge the hole. A patch is flat-ish, so a cut across curved relief
  moves the surface (reported as `max_deviation_mm`, WARN above 0.2 mm); `ring` trades hole size for it.
INVARIANTS:
  * everything is vectorised (edge valence by `np.unique` on packed sorted edges; non-manifold vertices by
    fan connectivity with `scipy.sparse.csgraph`) — 411k faces diagnose in seconds, no per-vertex loops;
  * a clean mesh is returned as a copy with identical arrays (`actions == ["already clean"]`);
  * a merely bad model is a FAIL verdict with reasons; only an unknown backend name or an unusable option
    raises (`errors.InvalidArgument`);
  * `max_deviation_mm` is the directed Hausdorff distance from the ORIGINAL surface (minus the debris
    shells that were dropped on purpose) to the repaired one, evaluated on the faces that changed. Filling a
    hole adds surface where the original had none, so it does not count as deviation.
ASSUMPTIONS: input vertices are already merged (`frame.load_mesh` does it); builtin keeps every non-debris
  body (it never silently deletes a second large body — that would show up as a huge deviation anyway).
PAST BUGS: ring-cutting around open edges as well as non-manifold ones ate coarse meshes (a cube with one
  missing triangle lost most of its faces); open edges are therefore filled directly.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Protocol, runtime_checkable

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

from errors import InvalidArgument, MissingDependency
from prep.contracts import Verdict

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]
Report = dict[str, float | int | bool | str]

#: faces with less area than this (mm^2) are degenerate (slivers no slicer can use)
_MIN_AREA_MM2 = 1e-9
#: a repaired surface that moved more than this (mm) is a WARN: the repair was not invisible
_DEVIATION_WARN_MM = 0.2
#: spacing of the surface samples used for the deviation estimate (mm^2 of surface per sample), and caps
_SAMPLE_AREA_MM2 = 0.01
_SAMPLES_MIN, _SAMPLES_MAX = 1_000, 40_000
#: vertices closer than 10^-_MERGE_DIGITS mm are the same vertex when comparing two meshes
_MERGE_DIGITS = 4
_HASH_MULTIPLIERS = np.array(
    [0x9E3779B97F4A7C15, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9], dtype=np.uint64
)


# --------------------------------------------------------------------------------------------- options


@dataclass(frozen=True)
class MeshFixOptions:
    """Knobs of the builtin backend (the external `mesh_repair` library runs with its own defaults).

    `max_hole_edges`: boundary loops longer than this are left open. `debris_max_faces` / `debris_max_volume_mm3`:
    a shell below both next to a bigger body is debris. `ring`: the most face rings the builtin backend cuts
    around a non-manifold edge or pinch vertex; it escalates from 0 by one ring per round, so the cut is
    never larger than the defect needs. `max_rounds`: repair iterations.
    """

    max_hole_edges: int = 300
    debris_max_faces: int = 500
    debris_max_volume_mm3: float = 50.0
    ring: int = 2
    max_rounds: int = 6

    def __post_init__(self) -> None:
        if self.max_hole_edges < 3:
            raise InvalidArgument(
                "max_hole_edges",
                str(self.max_hole_edges),
                ["an integer >= 3"],
                command="prep",
            )
        if self.ring < 0:
            raise InvalidArgument(
                "ring", str(self.ring), ["an integer >= 0"], command="prep"
            )
        if self.max_rounds < 1:
            raise InvalidArgument(
                "max_rounds", str(self.max_rounds), ["an integer >= 1"], command="prep"
            )
        if self.debris_max_faces < 0 or self.debris_max_volume_mm3 < 0:
            raise InvalidArgument(
                "debris limits",
                f"{self.debris_max_faces} faces / {self.debris_max_volume_mm3} mm3",
                ["non-negative limits"],
                command="prep",
            )


#: the documented defaults, shared as the default argument of `diagnose` and `repair`
DEFAULT_OPTIONS = MeshFixOptions()

# --------------------------------------------------------------------------------------------- analysis


@dataclass
class _Analysis:
    """Topology of a face set. Array members are aligned with the faces that have three distinct vertices."""

    keep: NDArray[np.bool_]  # per INPUT face: three distinct vertex indices
    faces: IntArray  # the kept faces
    edge_inv: IntArray  # (F, 3) unique-edge id of each face edge (edge k joins corner k and k + 1)
    valence: IntArray  # (E,) faces per undirected edge
    inconsistent_edges: int  # valence-2 edges whose two faces run the same direction
    pair_faces: IntArray  # (P, 2) the two faces of every valence-2 edge
    pair_bad: NDArray[
        np.bool_
    ]  # (P,) that pair runs the same direction (winding conflict)
    n_vertices: int
    labels: IntArray  # (F,) component (faces connected across valence-2 edges)
    comp_faces: IntArray
    comp_volume: Array  # signed
    debris: NDArray[np.bool_]  # (C,)
    degenerate: int  # index-degenerate + zero-area faces
    volume: float

    @property
    def boundary_edges(self) -> int:
        return int((self.valence == 1).sum())

    @property
    def nonmanifold_edges(self) -> int:
        return int((self.valence >= 3).sum())

    @property
    def watertight(self) -> bool:
        return len(self.faces) > 0 and bool((self.valence == 2).all())

    @property
    def bad_edge_face(self) -> NDArray[np.bool_]:
        """Faces that have at least one edge with a valence other than 2."""
        return np.asarray((self.valence[self.edge_inv] != 2).any(axis=1))

    @property
    def inverted(self) -> NDArray[np.bool_]:
        """Per component: a closed shell whose normals point inward (negative signed volume)."""
        bad = np.bincount(
            self.labels, weights=self.bad_edge_face, minlength=len(self.comp_faces)
        )
        return np.asarray((bad == 0) & (self.comp_volume < 0))

    def debris_faces(self) -> NDArray[np.bool_]:
        """Per kept face: it belongs to a debris shell."""
        return (
            np.asarray(self.debris[self.labels])
            if len(self.labels)
            else np.zeros(0, dtype=bool)
        )

    @cached_property
    def nonmanifold_vertex(self) -> NDArray[np.bool_]:
        """(V,) more than one face fan meets at the vertex. Lazy: it is the costliest part of the analysis."""
        if len(self.faces) == 0:
            return np.zeros(self.n_vertices, dtype=bool)
        return _pinch_vertices(self.faces, self.edge_inv, self.valence, self.n_vertices)

    def clean(self) -> bool:
        # cheap checks first so the pinch pass only runs once everything else is fine
        return (
            self.watertight
            and self.inconsistent_edges == 0
            and self.degenerate == 0
            and not self.inverted.any()
            and not self.debris.any()
            and not self.nonmanifold_vertex.any()
        )


def _analyse(vertices: Array, faces: IntArray, options: MeshFixOptions) -> _Analysis:
    nv = len(vertices)
    keep = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 0] != faces[:, 2])
    )
    f = faces[keep]
    nf = len(f)
    index_degenerate = len(faces) - nf
    if nf == 0:
        empty_i = np.zeros(0, dtype=np.int64)
        return _Analysis(
            keep, f, np.zeros((0, 3), dtype=np.int64), empty_i, 0, np.zeros((0, 2), dtype=np.int64),
            np.zeros(0, dtype=bool), nv, empty_i, empty_i, np.zeros(0),
            np.zeros(0, dtype=bool), index_degenerate, 0.0,
        )  # fmt: skip

    # --- edges: valence and direction ------------------------------------------------------------
    a = f
    b = np.roll(f, -1, axis=1)
    lo = np.minimum(a, b)
    hi = np.maximum(a, b)
    keys = (lo * nv + hi).ravel()
    # one sort yields unique edges, their valence, each face edge's edge id and the faces of every edge
    order = np.argsort(keys)
    sorted_keys = keys[order]
    starts = np.flatnonzero(
        np.concatenate(([True], sorted_keys[1:] != sorted_keys[:-1]))
    )
    valence = np.diff(np.append(starts, len(keys))).astype(np.int64)
    edge_id = np.empty(len(keys), dtype=np.int64)
    edge_id[order] = np.repeat(np.arange(len(starts)), valence)
    direction = np.where(a < b, 1.0, -1.0).ravel()
    sign_sum = np.bincount(edge_id, weights=direction, minlength=len(valence))
    inv = edge_id.reshape(nf, 3)

    # --- the two faces of every valence-2 edge ---------------------------------------------------
    two = np.flatnonzero(valence == 2)
    first = order[starts[two]] // 3
    second = order[starts[two] + 1] // 3
    pair_faces = np.stack([first, second], axis=1)
    pair_bad = sign_sum[two] != 0
    inconsistent = int(pair_bad.sum())

    # --- components across valence-2 edges -------------------------------------------------------
    graph = sparse.coo_matrix((np.ones(len(first)), (first, second)), shape=(nf, nf))
    n_comp, labels = csgraph.connected_components(graph, directed=False)
    labels = labels.astype(np.int64)
    comp_faces = np.bincount(labels, minlength=n_comp).astype(np.int64)

    # --- volume and degeneracy -------------------------------------------------------------------
    p = vertices[f] - vertices[f].reshape(-1, 3).mean(axis=0)
    cross = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    area = 0.5 * np.linalg.norm(cross, axis=1)
    zero_area = area < _MIN_AREA_MM2
    face_volume = np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])) / 6.0
    comp_volume = np.bincount(labels, weights=face_volume, minlength=n_comp)

    # --- debris: small shells next to a bigger body ----------------------------------------------
    debris = np.zeros(n_comp, dtype=bool)
    if n_comp > 1:
        body = int(np.argmax(comp_faces))
        debris = (
            (comp_faces < options.debris_max_faces)
            & (np.abs(comp_volume) < options.debris_max_volume_mm3)
            & (comp_faces < comp_faces[body])
        )

    return _Analysis(
        keep=keep,
        faces=f,
        edge_inv=inv,
        valence=valence,
        inconsistent_edges=inconsistent,
        pair_faces=pair_faces,
        pair_bad=pair_bad,
        n_vertices=nv,
        labels=labels,
        comp_faces=comp_faces,
        comp_volume=comp_volume.astype(np.float64),
        debris=debris,
        degenerate=index_degenerate + int(zero_area.sum()),
        volume=float(face_volume.sum()),
    )


def _pinch_vertices(
    f: IntArray, edge_inv: IntArray, valence: IntArray, nv: int
) -> NDArray[np.bool_]:
    """Vertices where more than one face fan meets (two bodies touching at a point, hole boundaries touching).

    A fan is a set of faces around the vertex connected through edges at that vertex. Every (face, corner)
    is a node, joined to one node per incident edge-end; faces of one fan end up in one component.
    """
    nf = len(f)
    corner = np.arange(3 * nf)
    vert = f.ravel()
    face = corner // 3
    k = corner % 3
    edge_out = edge_inv[face, k]  # edge corner -> next corner
    edge_in = edge_inv[face, (k + 2) % 3]  # edge previous corner -> corner
    nxt = f[face, (k + 1) % 3]
    prv = f[face, (k + 2) % 3]
    # one node per (edge, end): end 1 = the higher-numbered vertex of the edge
    node_out = 3 * nf + 2 * edge_out + (vert > nxt)
    node_in = 3 * nf + 2 * edge_in + (vert > prv)
    n_nodes = 3 * nf + 2 * len(valence)
    graph = sparse.coo_matrix(
        (
            np.ones(2 * len(corner)),
            (np.concatenate([corner, corner]), np.concatenate([node_out, node_in])),
        ),
        shape=(n_nodes, n_nodes),
    )
    n_comp, labels = csgraph.connected_components(graph, directed=False)
    fan = labels[: 3 * nf].astype(np.int64)
    # one representative corner per fan: assigning in reverse order leaves the first corner of each fan
    representative = np.full(n_comp, -1, dtype=np.int64)
    representative[fan[::-1]] = corner[::-1]
    fans_per_vertex = np.bincount(
        vert[representative[representative >= 0]], minlength=nv
    )
    return np.asarray(fans_per_vertex > 1)


# --------------------------------------------------------------------------------------------- diagnosis


@dataclass
class Diagnosis:
    """What is wrong with a mesh, by count. `verdict`: PASS clean, WARN debris shells only, FAIL otherwise."""

    faces: int
    vertices: int
    components: int
    watertight: bool
    winding_consistent: bool
    is_volume: bool
    boundary_edges: int
    nonmanifold_edges: int
    nonmanifold_vertices: int
    degenerate_faces: int
    debris_shells: int
    volume_mm3: float
    extents_mm: list[float]
    defects: list[str]
    verdict: Verdict = field(init=False)

    def __post_init__(self) -> None:
        structural = (
            not self.watertight
            or not self.winding_consistent
            or not self.is_volume
            or self.nonmanifold_edges > 0
            or self.nonmanifold_vertices > 0
            or self.degenerate_faces > 0
        )
        self.verdict = (
            "FAIL" if structural else ("WARN" if self.debris_shells > 0 else "PASS")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "faces": int(self.faces),
            "vertices": int(self.vertices),
            "components": int(self.components),
            "watertight": bool(self.watertight),
            "winding_consistent": bool(self.winding_consistent),
            "is_volume": bool(self.is_volume),
            "boundary_edges": int(self.boundary_edges),
            "nonmanifold_edges": int(self.nonmanifold_edges),
            "nonmanifold_vertices": int(self.nonmanifold_vertices),
            "degenerate_faces": int(self.degenerate_faces),
            "debris_shells": int(self.debris_shells),
            "volume_mm3": float(self.volume_mm3),
            "extents_mm": [float(x) for x in self.extents_mm],
            "defects": list(self.defects),
            "verdict": self.verdict,
        }

    def summary(self) -> str:
        head = f"{self.faces:,} faces, {self.components} component(s), {self.volume_mm3:,.1f} mm3"
        if not self.defects:
            return f"{head}: watertight, 0 defects"
        return f"{head}: {self.verdict} — " + ", ".join(self.defects)


def _diagnosis(vertices: Array, faces: IntArray, a: _Analysis) -> Diagnosis:
    n_pinch = int(a.nonmanifold_vertex.sum())
    n_debris = int(a.debris.sum())
    watertight = a.watertight
    consistent = a.inconsistent_edges == 0
    is_volume = watertight and consistent and a.volume > 0
    defects: list[str] = []
    if a.boundary_edges:
        defects.append(f"{a.boundary_edges} open edge(s)")
    if a.nonmanifold_edges:
        defects.append(f"{a.nonmanifold_edges} non-manifold edge(s)")
    if n_pinch:
        defects.append(f"{n_pinch} non-manifold vertex/vertices")
    if not consistent:
        defects.append(f"inconsistent winding on {a.inconsistent_edges} edge(s)")
    if watertight and consistent and a.volume <= 0:
        defects.append("inside-out (non-positive volume)")
    if len(a.faces) == 0:
        defects.append("no non-degenerate faces")
    if a.degenerate:
        defects.append(f"{a.degenerate} degenerate face(s)")
    if n_debris:
        defects.append(f"{n_debris} debris shell(s)")
    used = a.faces.ravel()
    extents = (
        vertices[used].max(axis=0) - vertices[used].min(axis=0)
        if len(used)
        else np.zeros(3)
    )
    return Diagnosis(
        faces=len(faces),
        vertices=len(vertices),
        components=len(a.comp_faces),
        watertight=watertight,
        winding_consistent=consistent,
        is_volume=is_volume,
        boundary_edges=a.boundary_edges,
        nonmanifold_edges=a.nonmanifold_edges,
        nonmanifold_vertices=n_pinch,
        degenerate_faces=a.degenerate,
        debris_shells=n_debris,
        volume_mm3=a.volume,
        extents_mm=[float(x) for x in extents],
        defects=defects,
    )


def _arrays(mesh: trimesh.Trimesh) -> tuple[Array, IntArray]:
    return (
        np.ascontiguousarray(mesh.vertices, dtype=np.float64),
        np.ascontiguousarray(mesh.faces, dtype=np.int64),
    )


def diagnose(
    mesh: trimesh.Trimesh, *, options: MeshFixOptions = DEFAULT_OPTIONS
) -> Diagnosis:
    """Count every defect of `mesh` (vectorised; 411k faces in a few seconds). `options` sets the debris limits."""
    vertices, faces = _arrays(mesh)
    return _diagnosis(vertices, faces, _analyse(vertices, faces, options))


# --------------------------------------------------------------------------------------------- backends


@runtime_checkable
class MeshFixBackend(Protocol):
    """A repair engine. `repair` gets float64 vertices and int64 faces and returns the repaired pair plus a report."""

    name: str

    def available(self) -> bool: ...

    def repair(
        self, vertices: Array, faces: IntArray, options: MeshFixOptions
    ) -> tuple[Array, IntArray, Report]: ...


# --- builtin helpers ---------------------------------------------------------------------------------


def _packed_faces(sorted_faces: IntArray, nv: int) -> IntArray:
    """One int64 key per face (rows already sorted): equal keys <=> equal faces. Much faster than `unique(axis=0)`."""
    if nv**3 < 2**63:
        return (sorted_faces[:, 0] * nv + sorted_faces[:, 1]) * nv + sorted_faces[:, 2]
    return (
        np.unique(sorted_faces, axis=0, return_inverse=True)[1].ravel().astype(np.int64)
    )


def _merge_and_dedupe(vertices: Array, faces: IntArray) -> tuple[Array, IntArray, int]:
    """Merge coincident vertices, drop index-degenerate, area-degenerate and duplicate faces."""
    m = trimesh.Trimesh(vertices, faces, process=False)
    before = len(m.vertices)
    m.merge_vertices()
    v = np.asarray(m.vertices, dtype=np.float64)
    f = np.asarray(m.faces, dtype=np.int64)
    merged = before - len(v)
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    p = v[f]
    area = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    f = f[area >= _MIN_AREA_MM2]
    _, first = np.unique(_packed_faces(np.sort(f, axis=1), len(v)), return_index=True)
    first.sort()
    return v, f[first], merged


def _flip(faces: IntArray, which: NDArray[np.bool_]) -> IntArray:
    out = faces.copy()
    out[which] = out[which][:, ::-1]
    return out


def _fix_winding(a: _Analysis) -> tuple[IntArray, int]:
    """Make every connected component consistently oriented; returns the faces and how many were flipped."""
    nf = len(a.faces)
    first_of = np.full(len(a.comp_faces), -1, dtype=np.int64)
    first_of[a.labels[::-1]] = np.arange(
        nf - 1, -1, -1
    )  # lowest face index of each component
    root = nf
    rows = np.concatenate([a.pair_faces[:, 0], np.full(len(first_of), root)])
    cols = np.concatenate([a.pair_faces[:, 1], first_of])
    # data 1 = consistent pair, 2 = pair that must flip relative to its parent; root links carry 1
    data = np.concatenate(
        [1 + a.pair_bad.astype(np.int64), np.ones(len(first_of), dtype=np.int64)]
    )
    adj = sparse.csr_matrix((data, (rows, cols)), shape=(nf + 1, nf + 1))
    adj = adj + adj.T
    visit, parent = csgraph.breadth_first_order(
        adj, root, directed=False, return_predecessors=True
    )
    child = visit[1:]
    rel = (
        np.asarray(adj[parent[child], child]).ravel() - 1
    )  # 0 keep, 1 flip relative to the parent
    # the flip of a face is the XOR of the relative flips on its BFS-tree path to the root; pointer jumping
    # accumulates it in O(log depth) vector passes instead of a per-face Python loop
    flip = np.zeros(nf + 1, dtype=bool)
    flip[child] = rel.astype(bool)
    up = np.arange(nf + 1)
    up[child] = parent[child]
    while (up != root).any():
        flip ^= flip[up]
        up = up[up]
    flip = flip[:nf]
    # the BFS root's orientation is arbitrary; keep the majority so one flipped face costs one flip, not n - 1
    flipped = np.bincount(a.labels, weights=flip, minlength=len(a.comp_faces))
    flip ^= (flipped > a.comp_faces / 2)[a.labels]
    return _flip(a.faces, flip), int(flip.sum())


def _grow(
    faces: IntArray, drop: NDArray[np.bool_], nv: int, rings: int
) -> NDArray[np.bool_]:
    """`drop` plus `rings` vertex-neighbour dilations over the face set."""
    drop = drop.copy()
    for _ in range(rings):
        touched = np.zeros(nv, dtype=bool)
        touched[faces[drop]] = True
        drop |= touched[faces].any(axis=1)
    return drop


def _boundary_loops(a: _Analysis) -> tuple[list[list[int]], int]:
    """Ordered boundary loops (vertex ids in the direction the filling faces must run) and the count of
    boundary edges that cannot form a simple loop (pinch vertices)."""
    face_idx, corner = np.nonzero(a.valence[a.edge_inv] == 1)
    if len(face_idx) == 0:
        return [], 0
    f = a.faces
    src = f[
        face_idx, (corner + 1) % 3
    ]  # the filling face walks the existing boundary edge backwards
    dst = f[face_idx, corner]
    sources, counts = np.unique(src, return_counts=True)
    pinch = set(sources[counts > 1].tolist())
    nxt = dict(zip(src.tolist(), dst.tolist(), strict=True))
    seen: set[int] = set()
    loops: list[list[int]] = []
    broken = 0
    for start in src.tolist():
        if start in seen:
            continue
        loop = [start]
        seen.add(start)
        cur = nxt[start]
        ok = start not in pinch
        while cur != start and ok:
            if cur in seen or cur in pinch or cur not in nxt:
                ok = False
                break
            loop.append(cur)
            seen.add(cur)
            cur = nxt[cur]
        if ok:
            loops.append(loop)
        else:
            broken += len(loop)
    return loops, broken


def _min_area_triangulation(points: Array) -> list[tuple[int, int, int]]:
    """Triangles (local indices, oriented like the loop) of the minimum-total-area triangulation of a 3-D polygon.

    Interval dynamic programming over the polygon's chords (Barequet-Sharir / Liepa): the patch is the
    minimal-area surface spanning the loop, so it does not bulge or fold like a fan from one vertex. No
    vertex is added, so no detail is invented. O(n^3), vectorised per span.
    """
    n = len(points)
    scale2 = float(np.ptp(points, axis=0).max()) ** 2
    # a zero-area triangle costs nothing in area, so flat polygons with collinear boundary points would be
    # "filled" with slivers: those cost more than any real triangulation, and equal-area choices prefer
    # short chords (the small `longest_edge` term)
    sliver_cost = (n + 1) * scale2
    cost = np.zeros((n, n))
    split = np.zeros((n, n), dtype=np.int64)
    for span in range(2, n):
        i = np.arange(n - span)
        j = i + span
        k = i[:, None] + np.arange(1, span)[None, :]
        pi = points[i][:, None, :]
        pj = points[j][:, None, :]
        pk = points[k]
        area = 0.5 * np.linalg.norm(np.cross(pk - pi, pj - pi), axis=2)
        longest_edge = np.maximum(
            np.maximum(np.square(pk - pi).sum(axis=2), np.square(pj - pk).sum(axis=2)),
            np.square(pj - pi).sum(axis=2),
        )
        weight = (
            area
            + 1e-4 * longest_edge
            + np.where(area < 1e-6 * longest_edge, sliver_cost, 0.0)
        )
        total = cost[i[:, None], k] + cost[k, j[:, None]] + weight
        best = np.argmin(total, axis=1)
        rows = np.arange(len(i))
        cost[i, j] = total[rows, best]
        split[i, j] = k[rows, best]
    triangles: list[tuple[int, int, int]] = []
    pending = [(0, n - 1)]
    while pending:
        i0, j0 = pending.pop()
        k0 = int(split[i0, j0])
        triangles.append((i0, k0, j0))
        if k0 - i0 > 1:
            pending.append((i0, k0))
        if j0 - k0 > 1:
            pending.append((k0, j0))
    return triangles


def _fill_loops(
    vertices: Array, faces: IntArray, loops: list[list[int]], max_edges: int
) -> tuple[IntArray, int]:
    """Triangulate every loop of at most `max_edges` edges; returns the faces and the number of loops filled."""
    new_faces: list[IntArray] = []
    for loop in loops:
        if len(loop) > max_edges:
            continue
        ids = np.asarray(loop, dtype=np.int64)
        new_faces.append(ids[np.asarray(_min_area_triangulation(vertices[ids]))])
    if not new_faces:
        return faces, 0
    return np.vstack([faces, *new_faces]), len(new_faces)


def _compact(vertices: Array, faces: IntArray) -> tuple[Array, IntArray]:
    used = np.zeros(len(vertices), dtype=bool)
    used[faces] = True
    remap = np.cumsum(used) - 1
    return vertices[used], remap[faces].astype(np.int64)


def _manifold_status(vertices: Array, faces: IntArray) -> str:
    try:
        import manifold3d
    except ImportError:
        return "unavailable"
    m = manifold3d.Manifold(
        manifold3d.Mesh(
            vert_properties=vertices.astype(np.float32),
            tri_verts=faces.astype(np.uint32),
        )
    )
    return str(m.status().name)


class BuiltinBackend:
    """Local repair: debris, non-manifold cuts, hole filling, winding. No resampling of intact surface."""

    name = "builtin"

    def available(self) -> bool:
        return True

    def repair(
        self, vertices: Array, faces: IntArray, options: MeshFixOptions
    ) -> tuple[Array, IntArray, Report]:
        v, f, merged = _merge_and_dedupe(vertices, faces)
        report: Report = {
            "vertices_merged": merged,
            "degenerate_or_duplicate_faces_removed": len(faces) - len(f),
            "debris_shells_dropped": 0,
            "debris_faces_dropped": 0,
            "faces_cut": 0,
            "loops_filled": 0,
            "loops_left": 0,
            "faces_flipped": 0,
            "rounds": 0,
        }
        a = _analyse(v, f, options)
        for rnd in range(1, options.max_rounds + 1):
            if a.clean():
                break
            report["rounds"] = rnd
            changed = False
            if a.inconsistent_edges:
                f, flipped = _fix_winding(a)
                report["faces_flipped"] = int(report["faces_flipped"]) + flipped
                a = _analyse(v, f, options)
                changed = True
            f, a, dropped = self._drop_debris(v, f, a, options, report)
            changed |= dropped
            seeds = a.bad_edge_face & (a.valence[a.edge_inv] >= 3).any(axis=1)
            seeds |= a.nonmanifold_vertex[f].any(axis=1)
            if seeds.any():
                cut = _grow(f, seeds, len(v), min(rnd - 1, options.ring))
                report["faces_cut"] = int(report["faces_cut"]) + int(cut.sum())
                f = f[~cut]
                a = _analyse(v, f, options)
                f, a, _ = self._drop_debris(v, f, a, options, report)
                changed = True
            loops, _ = _boundary_loops(a)
            if loops:
                f, filled = _fill_loops(v, f, loops, options.max_hole_edges)
                report["loops_filled"] = int(report["loops_filled"]) + filled
                changed |= filled > 0
                a = _analyse(v, f, options)
            if not changed:
                break
        inverted = a.inverted
        if inverted.any():
            f = _flip(f, inverted[a.labels])
            a = _analyse(v, f, options)
        loops, broken = _boundary_loops(a)
        report["loops_left"] = len(loops) + int(broken > 0)
        v, f = _compact(v, f)
        report["manifold_status"] = _manifold_status(v, f)
        return v, f, report

    @staticmethod
    def _drop_debris(
        v: Array, f: IntArray, a: _Analysis, options: MeshFixOptions, report: Report
    ) -> tuple[IntArray, _Analysis, bool]:
        if not a.debris.any():
            return f, a, False
        mask = a.debris_faces()
        report["debris_shells_dropped"] = int(report["debris_shells_dropped"]) + int(
            a.debris.sum()
        )
        report["debris_faces_dropped"] = int(report["debris_faces_dropped"]) + int(
            mask.sum()
        )
        f = f[~mask]
        return f, _analyse(v, f, options), True


class ExternalBackend:
    """The separate public `meshfix` library (Python module `mesh_repair`); unavailable until it is installed."""

    name = "meshfix-lib"
    _module = "mesh_repair"

    def _load(self) -> Any:
        try:
            module = importlib.import_module(self._module)
        except ImportError:
            return None
        return module if callable(getattr(module, "repair_mesh", None)) else None

    def available(self) -> bool:
        # `auto` must not pick the adapter just because it imports: it clones and builds on first use, so it only
        # counts when meshfix can run now (installed, or a cached build exists). An adapter without `available()`
        # (a stand-in module) is taken at its word.
        module = self._load()
        if module is None:
            return False
        ready = getattr(module, "available", None)
        return bool(ready()) if callable(ready) else True

    def repair(
        self, vertices: Array, faces: IntArray, options: MeshFixOptions
    ) -> tuple[Array, IntArray, Report]:
        module = self._load()
        if module is None:
            raise MissingDependency(
                "mesh_repair (meshfix library)",
                install="pip install meshfix  # the public mesh-repair library",
                degrades="`3d prep` falls back to the builtin repair backend",
                command="prep",
            )
        out_v, out_f, report = module.repair_mesh(
            vertices, faces
        )  # the library has its own knobs; the builtin options do not apply
        return (
            np.ascontiguousarray(out_v, dtype=np.float64),
            np.ascontiguousarray(out_f, dtype=np.int64),
            dict(report),
        )


_REGISTRY: dict[str, MeshFixBackend] = {}


def register_backend(backend: MeshFixBackend) -> None:
    """Add (or replace, by name) a repair backend. Registration order decides what 'auto' prefers."""
    _REGISTRY[backend.name] = backend


def unregister_backend(name: str) -> None:
    """Remove a backend; unknown names are ignored."""
    _REGISTRY.pop(name, None)


def backends() -> list[MeshFixBackend]:
    """Registered backends in registration order."""
    return list(_REGISTRY.values())


register_backend(BuiltinBackend())
register_backend(ExternalBackend())


def _resolve(name: str) -> MeshFixBackend:
    if name == "auto":
        for b in _REGISTRY.values():
            if b.name != "builtin" and b.available():
                return b
        return _REGISTRY["builtin"]
    if name not in _REGISTRY:
        raise InvalidArgument("backend", name, ["auto", *_REGISTRY], command="prep")
    return _REGISTRY[name]


# --------------------------------------------------------------------------------------------- deviation


def _face_hashes(ids: IntArray) -> NDArray[np.uint64]:
    """Order-independent 64-bit hash of every face given its canonical vertex ids."""
    s = np.sort(ids, axis=1).astype(np.uint64)
    return np.asarray((s * _HASH_MULTIPLIERS).sum(axis=1), dtype=np.uint64)


def _surface_points(v: Array, f: IntArray, rng: np.random.Generator) -> Array:
    """Vertices of `f` plus area-weighted random surface samples (about one per `_SAMPLE_AREA_MM2`)."""
    tri = v[f]
    corners = tri.reshape(-1, 3)
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = 0.5 * np.linalg.norm(cross, axis=1)
    total = float(area.sum())
    if total <= 0.0:
        return corners
    n = int(np.clip(total / _SAMPLE_AREA_MM2, _SAMPLES_MIN, _SAMPLES_MAX))
    pick = rng.choice(len(f), size=n, p=area / total)
    r1 = np.sqrt(rng.random(n))[:, None]
    r2 = rng.random(n)[:, None]
    t = tri[pick]
    samples = (1 - r1) * t[:, 0] + r1 * (1 - r2) * t[:, 1] + r1 * r2 * t[:, 2]
    return np.vstack([corners, samples])


def _max_deviation(
    va: Array, fa: IntArray, vb: Array, fb: IntArray, ignore_a: NDArray[np.bool_]
) -> float:
    """Directed Hausdorff distance (mm) from the changed faces of mesh A to the repaired surface B near them."""
    if len(fa) == 0:
        return 0.0
    _, inverse = trimesh.grouping.unique_rows(np.vstack([va, vb]), digits=_MERGE_DIGITS)
    ha = _face_hashes(inverse[: len(va)][fa])
    hb = _face_hashes(inverse[len(va) :][fb])
    only_a = ~np.isin(ha, hb) & ~ignore_a
    if not only_a.any():
        return 0.0
    only_b = ~np.isin(hb, ha)
    rng = np.random.default_rng(0)
    source = _surface_points(va, fa[only_a], rng)
    if only_b.any():
        # the repaired surface near a patch includes the untouched faces around it, not just the patch
        near = _grow(fb, only_b, len(vb), 2)
        target = _surface_points(vb, fb[near], rng)
    else:  # surface was only removed (nothing added): compare with everything that is left
        target = vb[np.unique(fb)]
    return float(cKDTree(target).query(source)[0].max())


# --------------------------------------------------------------------------------------------- result


_ACTION_TEXT: dict[str, str] = {
    "vertices_merged": "merged {} duplicate vertices",
    "degenerate_or_duplicate_faces_removed": "removed {} degenerate/duplicate faces",
    "debris_shells_dropped": "dropped {} debris shell(s)",
    "debris_faces_dropped": "({} debris faces)",
    "faces_cut": "cut {} faces around non-manifold edges/vertices",
    "loops_filled": "filled {} hole(s)",
    "loops_left": "left {} hole(s) unfilled",
    "faces_flipped": "re-oriented {} faces",
    "rounds": "{} repair round(s)",
}


def _actions(report: Report) -> list[str]:
    actions = []
    for key, value in report.items():
        if value is False or value == 0:
            continue
        if key in _ACTION_TEXT:
            actions.append(_ACTION_TEXT[key].format(value))
        elif key == "manifold_status":
            continue
        else:
            actions.append(f"{key}={value}")
    return actions


def _plain(value: Any) -> Any:
    """JSON-safe copy of a backend report value (numpy scalars -> python)."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _plain(x) for k, x in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(x) for x in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


@dataclass
class RepairResult:
    """Outcome of `repair`. `mesh` is the repaired mesh in the input frame (not part of `to_dict`)."""

    mesh: trimesh.Trimesh
    before: Diagnosis
    after: Diagnosis
    backend: str
    actions: list[str]
    max_deviation_mm: float
    seconds: float
    report: Report = field(default_factory=dict)
    verdict: Verdict = field(init=False)

    def __post_init__(self) -> None:
        if self.after.verdict != "PASS":
            self.verdict = self.after.verdict
            return
        manifold_ok = self.report.get("manifold_status", "NoError") in (
            "NoError",
            "unavailable",
        )
        moved = self.max_deviation_mm > _DEVIATION_WARN_MM
        self.verdict = "PASS" if manifold_ok and not moved else "WARN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "backend": self.backend,
            "actions": list(self.actions),
            "max_deviation_mm": float(self.max_deviation_mm),
            "seconds": float(self.seconds),
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "report": _plain(self.report),
        }

    def summary(self) -> str:
        done = "; ".join(self.actions) if self.actions else "nothing to do"
        if self.verdict == "FAIL":
            tail = "still not a volume: " + ", ".join(self.after.defects)
        else:
            tail = f"{self.after.faces:,} faces, dev {self.max_deviation_mm:.3f} mm"
        return f"repair[{self.backend}] {done} -> {tail} ({self.seconds:.1f}s)"


def repair(
    mesh: trimesh.Trimesh,
    *,
    backend: str = "auto",
    options: MeshFixOptions = DEFAULT_OPTIONS,
) -> RepairResult:
    """Repair `mesh` into one watertight volume with the named backend ('auto' = first available external one,
    else builtin). A mesh that already diagnoses clean is returned as an identical copy."""
    t0 = time.perf_counter()
    engine = _resolve(backend)
    vertices, faces = _arrays(mesh)
    a = _analyse(vertices, faces, options)
    before = _diagnosis(vertices, faces, a)
    if before.verdict == "PASS":
        return RepairResult(
            mesh=mesh.copy(),
            before=before,
            after=before,
            backend="none",
            actions=["already clean"],
            max_deviation_mm=0.0,
            seconds=time.perf_counter() - t0,
        )
    out_v, out_f, report = engine.repair(vertices, faces, options)
    repaired = trimesh.Trimesh(out_v, out_f, process=False)
    after = diagnose(repaired, options=options)
    ignore = np.zeros(len(faces), dtype=bool)
    ignore[np.nonzero(a.keep)[0][a.debris_faces()]] = True
    deviation = _max_deviation(vertices, faces, out_v, out_f, ignore)
    return RepairResult(
        mesh=repaired,
        before=before,
        after=after,
        backend=engine.name,
        actions=_actions(report),
        max_deviation_mm=deviation,
        seconds=time.perf_counter() - t0,
        report=report,
    )
