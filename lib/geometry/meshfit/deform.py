"""deform.py — handle-based deformation: harmonic, biharmonic and ARAP solves with anchors.

WHAT: move a set of handle vertices to target positions while anchor vertices stay put and the rest
  of the surface follows smoothly. Three solvers, increasing in quality and cost:
    harmonic    minimise sum |laplacian d|^2 per vertex via L d = 0   (C0 at handles, fastest)
    biharmonic  minimise the thin-plate energy via (L M^-1 L) d = 0   (C1, smooth bulge-free pull)
    arap        as-rigid-as-possible (Sorkine & Alexa 2007), local/global iterations seeded by biharmonic
  All solve only a local patch (`influence` mm around the handles) and prefactor one sparse LU.

WHY: this is the "grab / move with anchors" a sculptor does with Blender's Grab + mask, but exact,
  local and reproducible; ARAP keeps strand-scale detail rigid instead of smearing it. Pure
  numpy/scipy.sparse: SuperLU is single-threaded and deterministic, the local step uses stacked
  3x3 SVDs and ordered np.bincount reductions.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import scipy.sparse as sp
from numpy.typing import NDArray
from scipy.sparse.linalg import splu

from errors import UsageError

from . import meshmath

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]
METHODS = ("harmonic", "biharmonic", "arap")


def ring(A: sp.csr_matrix, idx: IntArray, n: int) -> IntArray:
    """`idx` plus all one-ring neighbours (sorted, unique)."""
    mask = np.zeros(n, dtype=bool)
    mask[idx] = True
    reach = (A @ mask.astype(np.float64)) > 0
    return np.nonzero(mask | reach)[0]


class PatchSystem:
    """Prefactored harmonic / biharmonic energy of a free patch: solve for any constrained displacements.

    Everything that is not `free` is constrained (handle, anchor, keep-out or outside the patch); its
    displacement enters the right-hand side. One sparse LU serves any number of right-hand sides.
    """

    def __init__(self, V: Array, F: IntArray, free: IntArray, method: str) -> None:
        n = len(V)
        self.free = free
        self.L, mass = meshmath.cotan_laplacian(V, F)
        self.A = meshmath.adjacency(F, n)
        K = ring(self.A, free, n)  # free + ring1: the rows an energy term can touch
        T = ring(self.A, K, n)  # columns of the L rows in K
        if method == "harmonic":
            Q = self.L[free][:, T].tocsc()
        else:
            Q = (self.L[free][:, K] @ sp.diags(1.0 / mass[K]) @ self.L[K][:, T]).tocsc()
        pos_free = np.searchsorted(T, free)
        keep = np.ones(len(T), dtype=bool)
        keep[pos_free] = False
        cols = np.nonzero(keep)[0]
        self.lu = splu(sp.csc_matrix(Q[:, pos_free]))
        self.constrained_cols = sp.csr_matrix(Q[:, cols])
        self.constrained_vertices = T[cols]

    def solve(self, delta: Array) -> Array:
        """Displacement of the free vertices for constrained displacements `delta` (n, 3)."""
        rhs = -(self.constrained_cols @ delta[self.constrained_vertices])
        return np.asarray(self.lu.solve(np.ascontiguousarray(rhs)))


def _fail(message: str) -> UsageError:
    return UsageError(
        f"grab: {message}",
        command="mesh deform",
        remediation=[
            "A handle region, at least one anchor/keep-out vertex and a non-empty free patch are required."
        ],
    )


def solve_handles(
    V: Array,
    F: IntArray,
    handles: IntArray,
    targets: Array,
    fixed: NDArray[np.bool_],
    zone: NDArray[np.bool_] | None,
    method: str,
    iterations: int = 10,
) -> tuple[Array, dict[str, Any]]:
    """Return new vertices where `handles` sit at `targets`, `fixed` (anchors, keep-out) stay, the rest follow."""
    if method not in METHODS:
        raise UsageError(
            f"grab: solver must be one of {', '.join(METHODS)} (or none), got {method!r}",
            command="mesh deform",
        )
    n = len(V)
    is_handle = np.zeros(n, dtype=bool)
    is_handle[handles] = True
    constrained = is_handle | fixed
    free_mask = ~constrained if zone is None else (zone & ~constrained)
    free = np.nonzero(free_mask)[0]
    if len(handles) == 0:
        raise _fail("the handle region selects no vertices (weight 1)")
    if len(free) == 0:
        return _apply_handles(V, handles, targets), {
            "free_vertices": 0,
            "iterations": 0,
        }
    if not (fixed.any() or (zone is not None and (~zone).any())):
        raise _fail(
            "no anchors: add `anchors:`, a keep-out zone or an `influence` radius so the patch is pinned"
        )

    system = PatchSystem(
        V, F, free, "harmonic" if method == "harmonic" else "biharmonic"
    )
    delta = np.zeros_like(V)
    delta[handles] = targets - V[handles]
    stats: dict[str, Any] = {"free_vertices": int(len(free)), "method": method}
    out = V.copy()
    out[free] += system.solve(delta)
    out = _apply_handles(out, handles, targets)
    if method == "arap":
        out, stats["iterations"], stats["arap_residual"] = _arap(
            V, F, system.L, out, free, iterations
        )
    return out, stats


def _apply_handles(V: Array, handles: IntArray, targets: Array) -> Array:
    out = V.copy()
    out[handles] = targets
    return out


def _arap(
    V0: Array,
    F: IntArray,
    L: sp.csr_matrix,
    start: Array,
    free: IntArray,
    iterations: int,
) -> tuple[Array, int, float]:
    n = len(V0)
    W = meshmath.cotan_weights(V0, F)
    A = meshmath.adjacency(F, n)
    rot_idx = ring(A, free, n)  # vertices whose rotation matters
    # directed edges of the rotation vertices, grouped by their row
    indptr, indices, weights = W.indptr, W.indices, W.data
    counts = indptr[rot_idx + 1] - indptr[rot_idx]
    row_of_edge = np.repeat(np.arange(len(rot_idx)), counts)
    starts = np.repeat(indptr[rot_idx], counts)
    offset = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    edge = starts + offset
    ei = rot_idx[row_of_edge]
    ej = indices[edge]
    ew = weights[edge]
    e0 = V0[ei] - V0[ej]
    pos_of = np.full(n, -1, dtype=np.int64)
    pos_of[rot_idx] = np.arange(len(rot_idx))

    Lf = L[free]
    nbr = ring(A, free, n)
    f_mask = np.zeros(n, dtype=bool)
    f_mask[free] = True
    const_cols = nbr[~f_mask[nbr]]
    Lff = sp.csc_matrix(Lf[:, free])
    Lfc = sp.csr_matrix(Lf[:, const_cols])
    lu = splu(Lff)

    # rows of the edge list that belong to free vertices (for the right-hand side)
    free_edge = np.isin(ei, free)
    ei_f, ej_f, ew_f, e0_f = ei[free_edge], ej[free_edge], ew[free_edge], e0[free_edge]
    row_f = np.searchsorted(free, ei_f)
    pos_i_f, pos_j_f = pos_of[ei_f], pos_of[ej_f]
    cur = start.copy()
    residual = 0.0
    done = 0
    for _ in range(iterations):
        done += 1
        ep = cur[ei] - cur[ej]
        s9 = (ew[:, None, None] * (e0[:, :, None] * ep[:, None, :])).reshape(-1, 9)
        S = np.stack(
            [
                np.bincount(row_of_edge, weights=s9[:, k], minlength=len(rot_idx))
                for k in range(9)
            ],
            axis=1,
        ).reshape(-1, 3, 3)
        U, sigma, Vt = np.linalg.svd(S)
        R = np.einsum("nij,nkj->nik", Vt.transpose(0, 2, 1), U)
        flip = np.linalg.det(R) < 0
        if flip.any():
            Vt = Vt.copy()
            Vt[flip, 2, :] *= -1.0
            R = np.einsum("nij,nkj->nik", Vt.transpose(0, 2, 1), U)
        Rsum = R[pos_i_f] + R[pos_j_f]
        contrib = 0.5 * ew_f[:, None] * np.einsum("eij,ej->ei", Rsum, e0_f)
        b = np.stack(
            [
                np.bincount(row_f, weights=contrib[:, k], minlength=len(free))
                for k in range(3)
            ],
            axis=1,
        )
        rhs = b - Lfc @ cur[const_cols]
        new_free = lu.solve(np.ascontiguousarray(rhs))
        residual = float(np.linalg.norm(new_free - cur[free], axis=1).max())
        cur = cur.copy()
        cur[free] = new_free
        if residual < 1e-7:
            break
    return cur, done, residual


def handle_targets(V: Array, handles: IntArray, spec: dict[str, Any]) -> Array:
    """Apply translate / rotate / scale (about `pivot`, default the handle centroid) to the handle vertices."""
    P = V[handles]
    pivot = (
        np.asarray(spec["pivot"], dtype=np.float64)
        if "pivot" in spec
        else P.mean(axis=0)
    )
    out = (P - pivot) * float(spec.get("scale", 1.0))
    rot = spec.get("rotate")
    if rot:
        axis = np.asarray(rot["axis"], dtype=np.float64)
        axis = axis / np.linalg.norm(axis)
        a = np.radians(float(rot["deg"]))
        out = (
            out * np.cos(a)
            + np.cross(axis, out) * np.sin(a)
            + axis * (out @ axis)[:, None] * (1 - np.cos(a))
        )
    return np.asarray(
        out
        + pivot
        + np.asarray(spec.get("translate", [0.0, 0.0, 0.0]), dtype=np.float64)
    )
