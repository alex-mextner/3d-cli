"""meshmath.py — vectorised triangle-mesh primitives shared by every meshfit op.

WHAT: face / vertex normals, the vertex adjacency matrix, the cotangent Laplacian with lumped mass,
  signed volume, watertightness, and a conforming longest-edge refinement that keeps every original
  vertex bit-identical (so keep-out zones survive a refine).

WHY: ops must be fast on 400k-face meshes and deterministic: everything here is numpy / scipy.sparse
  with a fixed accumulation order (np.bincount, CSR products), no thread-dependent reductions.
"""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp
from numpy.typing import NDArray

from errors import UsageError

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]


def face_vectors(V: Array, F: IntArray) -> Array:
    """Unnormalised face normals (length = 2 x area)."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    return np.cross(b - a, c - a)


def face_normals(V: Array, F: IntArray) -> Array:
    n = face_vectors(V, F)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return n / np.maximum(ln, 1e-30)


def vertex_normals(V: Array, F: IntArray) -> Array:
    """Area-weighted vertex normals (unit length; zero vector stays zero)."""
    fv = face_vectors(V, F)
    out = np.empty_like(V)
    for k in range(3):
        acc = np.zeros(len(V))
        for corner in range(3):
            acc += np.bincount(F[:, corner], weights=fv[:, k], minlength=len(V))
        out[:, k] = acc
    ln = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(ln, 1e-30)


def adjacency(F: IntArray, n: int) -> sp.csr_matrix:
    """Symmetric 0/1 vertex adjacency (each undirected edge once per direction)."""
    i = np.concatenate([F[:, 0], F[:, 1], F[:, 2], F[:, 1], F[:, 2], F[:, 0]])
    j = np.concatenate([F[:, 1], F[:, 2], F[:, 0], F[:, 0], F[:, 1], F[:, 2]])
    A = sp.coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n)).tocsr()
    A.sum_duplicates()
    A.data[:] = 1.0
    return A


def smooth_normals(V: Array, F: IntArray, iters: int) -> Array:
    """Vertex normals relaxed `iters` times over the one-ring (the displacement direction of relief ops)."""
    n = vertex_normals(V, F)
    if iters <= 0:
        return n
    A = adjacency(F, len(V))
    deg = np.maximum(np.asarray(A.sum(axis=1)).ravel(), 1.0)
    for _ in range(iters):
        n = (n + (A @ n) / deg[:, None]) * 0.5
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)
    return n


def cotan_weights(
    V: Array, F: IntArray, floor: float = 1e-6, ceil: float = 1e4
) -> sp.csr_matrix:
    """Symmetric cotangent edge weights W (clamped to [floor, ceil] so the Laplacian stays SPD)."""
    n = len(V)
    rows: list[IntArray] = []
    cols: list[IntArray] = []
    vals: list[Array] = []
    for k in range(3):
        i, j, m = F[:, k], F[:, (k + 1) % 3], F[:, (k + 2) % 3]
        u, w = V[j] - V[i], V[m] - V[i]
        cross = np.linalg.norm(np.cross(u, w), axis=1)
        cot = (u * w).sum(1) / np.maximum(cross, 1e-30)
        rows += [j, m]
        cols += [m, j]
        vals += [0.5 * cot, 0.5 * cot]
    W = sp.coo_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(n, n),
    ).tocsr()
    W.sum_duplicates()
    W.data = np.clip(W.data, floor, ceil)
    return W


def cotan_laplacian(V: Array, F: IntArray) -> tuple[sp.csr_matrix, Array]:
    """(L, mass): L = D - W (positive semi-definite), mass = lumped barycentric vertex area."""
    W = cotan_weights(V, F)
    deg = np.asarray(W.sum(axis=1)).ravel()
    L = (sp.diags(deg) - W).tocsr()
    area = np.linalg.norm(face_vectors(V, F), axis=1) * 0.5
    mass = np.zeros(len(V))
    for corner in range(3):
        mass += np.bincount(F[:, corner], weights=area / 3.0, minlength=len(V))
    return L, np.maximum(mass, 1e-12)


def signed_volume(V: Array, F: IntArray) -> float:
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def is_watertight(F: IntArray) -> bool:
    """Every undirected edge is shared by exactly two faces, traversed once in each direction."""
    if len(F) == 0:
        return False
    d = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    n = int(F.max()) + 1
    directed = d[:, 0] * n + d[:, 1]
    reverse = d[:, 1] * n + d[:, 0]
    directed.sort()
    if len(np.unique(directed)) != len(directed):
        return False
    return bool(np.isin(reverse, directed, assume_unique=False).all())


def mean_edge_length(V: Array, F: IntArray) -> float:
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    return float(np.linalg.norm(V[e[:, 0]] - V[e[:, 1]], axis=1).mean())


def refine_long_edges(
    V: Array, F: IntArray, max_edge: float, max_rounds: int = 10
) -> tuple[Array, IntArray, int]:
    """Split every edge longer than `max_edge` at its midpoint (conforming 1-/2-/3-edge triangle patterns).

    Original vertices keep their index and coordinates; new vertices are appended in a fixed order.
    Returns (V, F, rounds). Midpoint splitting never opens the mesh, so watertight stays watertight.
    """
    V2, F2, rounds, _, _ = refine_edges(V, F, max_edge, max_rounds)
    return V2, F2, rounds


def refine_edges(
    V: Array,
    F: IntArray,
    max_edge: float,
    max_rounds: int = 10,
    zone: NDArray[np.bool_] | None = None,
    max_faces: int | None = None,
) -> tuple[Array, IntArray, int, IntArray, NDArray[np.bool_] | None]:
    """`refine_long_edges` with an optional vertex `zone` and the parent of every output face.

    With a zone only faces that have a vertex in it are tested; an edge that one of them splits is split
    in every face that shares it (a neighbour outside the zone is split too, so the mesh stays closed),
    and the new midpoint belongs to the zone when either end does. Without a zone every long edge splits.
    Returns (V, F, rounds, parent, zone): parent[i] is the index into the INPUT faces of the face output
    face i was cut from (the identity for faces never split); zone is the zone extended to the new vertices
    (None without one).
    """
    rounds = 0
    parent = np.arange(len(F), dtype=np.int64)
    zone_now = None if zone is None else np.asarray(zone, dtype=bool).copy()
    for _ in range(max_rounds):
        n = len(V)
        e = np.stack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]], axis=1)  # (m, 3, 2)
        length = np.linalg.norm(V[e[..., 0]] - V[e[..., 1]], axis=2)
        split = length > max_edge
        if zone_now is not None:
            split &= zone_now[F].any(axis=1)[:, None]
        if not split.any():
            break
        lo, hi = np.minimum(e[..., 0], e[..., 1]), np.maximum(e[..., 0], e[..., 1])
        key = lo * n + hi
        ukeys = np.unique(key[split])
        split = np.isin(
            key, ukeys
        )  # every face edge that is split, also those of faces outside the zone
        count = split.sum(axis=1)
        projected = len(F) + int(
            count.sum()
        )  # a face with k split edges becomes k + 1 faces
        if max_faces is not None and projected > max_faces:
            raise UsageError(
                f"remesh refine: round {rounds + 1} would grow the mesh to {projected:,} faces, above the {max_faces:,}-face budget",
                command="mesh deform",
                remediation=[
                    "Use a larger target_edge / max_edge, limit the op with a `region`, or raise `max_faces` on the op."
                ],
            )
        mid_index = n + np.searchsorted(ukeys, key)  # only read where split is True
        mid = V[ukeys // n] * 0.5 + V[ukeys % n] * 0.5
        Vn = np.concatenate([V, mid])
        if zone_now is not None:
            zone_now = np.concatenate(
                [zone_now, zone_now[ukeys // n] | zone_now[ukeys % n]]
            )
        keep = count == 0
        parts = [F[keep]]
        sources = [np.nonzero(keep)[0]]
        for faces, rows in _split_faces(Vn, F, mid_index, split, count):
            parts.append(faces)
            sources.append(rows)
        F = np.concatenate(parts)
        parent = parent[np.concatenate(sources)]
        V = Vn
        rounds += 1
    return V, F, rounds, parent, zone_now


def _split_faces(
    Vn: Array,
    F: IntArray,
    mid_index: IntArray,
    split: NDArray[np.bool_],
    count: IntArray,
) -> list[tuple[IntArray, IntArray]]:
    """-> [(new faces, row of F each came from)] for the 1-, 2- and 3-edge patterns, in a fixed order."""
    out: list[tuple[IntArray, IntArray]] = []
    idx1 = np.nonzero(count == 1)[0]
    if len(idx1):
        s = np.argmax(split[idx1], axis=1)
        a, b, c, m = _roll(F, idx1, s, mid_index, 0)
        out += [(np.stack([a, m, c], 1), idx1), (np.stack([m, b, c], 1), idx1)]
    idx2 = np.nonzero(count == 2)[0]
    if len(idx2):
        u = np.argmin(split[idx2], axis=1)  # the unsplit edge goes last
        shift = (u + 1) % 3
        a, b, c, m0 = _roll(F, idx2, shift, mid_index, 0)
        m1 = _roll(F, idx2, shift, mid_index, 1)[3]
        # quad (a, m0, m1, c): cut along the shorter diagonal, ties to a-m1
        d1 = np.linalg.norm(Vn[a] - Vn[m1], axis=1)
        d2 = np.linalg.norm(Vn[m0] - Vn[c], axis=1)
        first = d1 <= d2
        t_a = np.where(
            first[:, None], np.stack([a, m0, m1], 1), np.stack([a, m0, c], 1)
        )
        t_b = np.where(
            first[:, None], np.stack([a, m1, c], 1), np.stack([m0, m1, c], 1)
        )
        out += [(np.stack([m0, b, m1], 1), idx2), (t_a, idx2), (t_b, idx2)]
    idx3 = np.nonzero(count == 3)[0]
    if len(idx3):
        a, b, c = F[idx3, 0], F[idx3, 1], F[idx3, 2]
        m0, m1, m2 = mid_index[idx3, 0], mid_index[idx3, 1], mid_index[idx3, 2]
        out += [
            (np.stack([a, m0, m2], 1), idx3),
            (np.stack([m0, b, m1], 1), idx3),
            (np.stack([m2, m1, c], 1), idx3),
            (np.stack([m0, m1, m2], 1), idx3),
        ]
    return out


def _roll(
    F: IntArray, idx: IntArray, shift: IntArray, mid_index: IntArray, edge: int
) -> tuple[IntArray, IntArray, IntArray, IntArray]:
    """Cyclically rotate each selected face by `shift`; also return the midpoint of rotated edge `edge`."""
    rows = np.arange(len(idx))
    cols = (np.arange(3)[None, :] + shift[:, None]) % 3
    v = F[idx][rows[:, None], cols]
    mids = mid_index[idx][rows[:, None], cols]
    return v[:, 0], v[:, 1], v[:, 2], mids[:, edge]
