#!/usr/bin/env python3
"""Benchmark harness for deterministic, GUI-free mesh deformation / fitting tooling.

Companion of docs/research/mesh-fit-2026-10-20.md.  Nothing here is imported by 3d-cli; it is a
research tool.  Every op runs in a FRESH subprocess ("worker") so peak RSS, thread settings and
library global state are per op, and each op is run three times to check determinism:

    A = N threads, B = N threads again (same-config repeat), C = 1 thread

``sha_raw`` is the SHA-256 of the float64 vertex + int64 face bytes (strict), ``sha_stl`` the
SHA-256 of a trimesh binary STL export (float32, what users actually get).

Usage (on the remote 16-core box; heavy jobs go through the box's `heavy` wrapper):

    export LD_LIBRARY_PATH=$HOME/.local/mamba/envs/gl/lib   # pymeshlab/open3d import libGL/libEGL
    heavy -n bench --wait -- v312/bin/python mesh-fit-bench.py suite --ops all --threads 8
    v312/bin/python mesh-fit-bench.py list
    v312/bin/python mesh-fit-bench.py worker --op brush_grab --threads 8        # one run, JSON

Thread pinning = OMP/OPENBLAS/MKL/NUMEXPR/TBB env vars AND sched_setaffinity to the first N cores
(TBB/OpenMP defaults follow the affinity mask), done before numpy is imported.

Mesh: geodesic icosphere (frequency n, 20*n^2 faces) built with numpy only, radius 30 mm, plus a
seeded smooth radial bump field.  "400k" = n=141 -> 397,620 faces / 198,812 vertices;
"50k" = n=50 -> 50,000 faces / 25,002 vertices.  Units are millimetres.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import time

THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "TBB_NUM_THREADS",
)
FREQ = {"400k": 141, "50k": 50, "5k": 16}
RADIUS = 30.0
SEED = 20261020


def pin_threads(n: int) -> None:
    for k in THREAD_ENV:
        os.environ[k] = str(n)
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, set(range(min(n, os.cpu_count() or n))))


# ----------------------------------------------------------------------------- mesh helpers
def icosphere(freq: int, radius: float):
    """Geodesic icosphere: (V float64 (10n^2+2, 3), F int64 (20n^2, 3)), outward, deterministic."""
    import numpy as np

    t = (1 + 5**0.5) / 2
    v0 = np.array(
        [
            [-1, t, 0],
            [1, t, 0],
            [-1, -t, 0],
            [1, -t, 0],
            [0, -1, t],
            [0, 1, t],
            [0, -1, -t],
            [0, 1, -t],
            [t, 0, -1],
            [t, 0, 1],
            [-t, 0, -1],
            [-t, 0, 1],
        ],
        float,
    )
    v0 /= np.linalg.norm(v0, axis=1, keepdims=True)
    f0 = np.array(
        [
            [0, 11, 5],
            [0, 5, 1],
            [0, 1, 7],
            [0, 7, 10],
            [0, 10, 11],
            [1, 5, 9],
            [5, 11, 4],
            [11, 10, 2],
            [10, 7, 6],
            [7, 1, 8],
            [3, 9, 4],
            [3, 4, 2],
            [3, 2, 6],
            [3, 6, 8],
            [3, 8, 9],
            [4, 9, 5],
            [2, 4, 11],
            [6, 2, 10],
            [8, 6, 7],
            [9, 8, 1],
        ]
    )
    n = freq
    ij = np.array([(i, j) for i in range(n + 1) for j in range(n + 1 - i)])
    idx = -np.ones((n + 2, n + 2), np.int64)
    idx[ij[:, 0], ij[:, 1]] = np.arange(len(ij))
    ii, jj = ij[:, 0], ij[:, 1]
    bary = np.stack([(n - ii - jj) / n, ii / n, jj / n], 1)  # (m,3)
    up = ij[ij.sum(1) <= n - 1]
    dn = ij[ij.sum(1) <= n - 2]
    tri = np.concatenate(
        [
            np.stack(
                [
                    idx[up[:, 0], up[:, 1]],
                    idx[up[:, 0] + 1, up[:, 1]],
                    idx[up[:, 0], up[:, 1] + 1],
                ],
                1,
            ),
            np.stack(
                [
                    idx[dn[:, 0] + 1, dn[:, 1]],
                    idx[dn[:, 0] + 1, dn[:, 1] + 1],
                    idx[dn[:, 0], dn[:, 1] + 1],
                ],
                1,
            ),
        ]
    )  # (n^2,3)
    pts = np.einsum("pk,fkd->fpd", bary, v0[f0])  # (20,m,3)
    pts /= np.linalg.norm(pts, axis=2, keepdims=True)
    m = len(ij)
    allp = pts.reshape(-1, 3)
    key = np.round(allp * 1e7).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    V = allp[first]
    F = (inv.reshape(20, m)[:, tri.reshape(-1)]).reshape(20 * len(tri), 3)
    nrm = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    flip = (nrm * V[F].mean(1)).sum(1) < 0
    F[flip] = F[flip][:, ::-1]
    assert len(V) == 10 * n * n + 2 and len(F) == 20 * n * n
    return V * radius, F.astype(np.int64)


def bumpy(V, amp=1.5):
    """Seeded smooth radial displacement (sum of 6 plane waves), keeps the mesh star-shaped."""
    import numpy as np

    rng = np.random.default_rng(SEED)
    u = V / np.linalg.norm(V, axis=1, keepdims=True)
    d = np.zeros(len(V))
    for k in range(1, 7):
        w = rng.normal(size=3)
        w *= rng.uniform(0.05, 0.25) / np.linalg.norm(w)  # rad/mm
        d += amp / k * np.sin(V @ w + rng.uniform(0, 2 * np.pi))
    return u * (np.linalg.norm(V, axis=1) + d)[:, None]


def make_mesh(tag: str):
    V, F = icosphere(FREQ[tag], RADIUS)
    return bumpy(V), F


def face_normals(V, F, unit=True):
    import numpy as np

    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    if unit:
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-300)
    return n


def vertex_normals(V, F):
    """Area-weighted vertex normals; bincount accumulation is sequential => deterministic."""
    import numpy as np

    fn = face_normals(V, F, unit=False)
    out = np.empty_like(V)
    for c in range(3):
        out[:, c] = sum(
            np.bincount(F[:, k], weights=fn[:, c], minlength=len(V)) for k in range(3)
        )
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-300)


def edges_of(F):
    import numpy as np

    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    e.sort(axis=1)
    return np.unique(e, axis=0)


def uniform_adjacency(V, F):
    import numpy as np
    import scipy.sparse as sp

    e = edges_of(F)
    n = len(V)
    A = sp.coo_matrix(
        (np.ones(2 * len(e)), (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])),
        shape=(n, n),
    ).tocsr()
    return A


def cot_laplacian(V, F):
    """Positive semi-definite cotangent Laplacian L = D - W (so L = -igl.cotmatrix)."""
    import numpy as np
    import scipy.sparse as sp

    n = len(V)
    P = V[F]  # (f,3,3)
    I, J, W = [], [], []
    for a in range(3):
        b, c = (a + 1) % 3, (a + 2) % 3
        u, w = P[:, b] - P[:, a], P[:, c] - P[:, a]
        cot = (u * w).sum(1) / np.maximum(
            np.linalg.norm(np.cross(u, w), axis=1), 1e-300
        )
        I += [F[:, b], F[:, c]]
        J += [F[:, c], F[:, b]]
        W += [0.5 * cot, 0.5 * cot]
    W = sp.coo_matrix(
        (np.concatenate(W), (np.concatenate(I), np.concatenate(J))), shape=(n, n)
    ).tocsr()
    return sp.diags(np.asarray(W.sum(1)).ravel()) - W


def splu_sym(A):
    """SuperLU on an SPD matrix: symmetric fill-reducing ordering, no pivoting (single-threaded)."""
    import scipy.sparse.linalg as sla

    return sla.splu(
        A,
        permc_spec="MMD_AT_PLUS_A",
        diag_pivot_thresh=0.0,
        options={"SymmetricMode": True},
    )


def mass_diag(V, F):
    import numpy as np

    a = 0.5 * np.linalg.norm(face_normals(V, F, unit=False), axis=1) / 3.0
    return sum(np.bincount(F[:, k], weights=a, minlength=len(V)) for k in range(3))


def signed_volume(V, F):
    import numpy as np

    return float(
        np.einsum("ij,ij->i", V[F[:, 0]], np.cross(V[F[:, 1]], V[F[:, 2]])).sum() / 6.0
    )


class Timer(dict):
    def __call__(self, name):
        outer = self

        class _C:
            def __enter__(self):
                self.t = time.perf_counter()

            def __exit__(self, *a):
                outer[name] = round(time.perf_counter() - self.t, 4)

        return _C()


# ----------------------------------------------------------------------------- 1. deformation
def handle_setup(V):
    """Handle = vertices within 3 mm of the top pole, moved +8 mm in z; anchors = bottom cap z<-0.9R (~5% of vertices)."""
    import numpy as np

    top = V[np.argmax(V[:, 2])]
    handle = np.where(np.linalg.norm(V - top, axis=1) < 3.0)[0]
    anchors = np.where(V[:, 2] < -0.9 * RADIUS)[0]
    b = np.concatenate([handle, anchors]).astype(np.int64)
    bc = V[b].copy()
    bc[: len(handle), 2] += 8.0
    return top, handle, anchors, b, bc


def reach_metrics(V, U, top, handle):
    import numpy as np

    disp = np.linalg.norm(U - V, axis=1)
    hd = disp[handle].mean()
    dist = np.linalg.norm(V - top, axis=1)
    out = {"handle_disp_mm": round(float(hd), 3)}
    for frac in (0.5, 0.1):
        m = disp >= frac * hd
        out[f"reach_{int(frac * 100)}pct_mm"] = round(float(dist[m].max()), 2)
    return out


def edge_stretch(V, U, F):
    import numpy as np

    e = edges_of(F)
    l0 = np.linalg.norm(V[e[:, 0]] - V[e[:, 1]], axis=1)
    l1 = np.linalg.norm(U[e[:, 0]] - U[e[:, 1]], axis=1)
    r = l1 / l0
    return {
        "edge_ratio_min": round(float(r.min()), 3),
        "edge_ratio_max": round(float(r.max()), 3),
        "edge_ratio_p99": round(float(np.percentile(np.abs(r - 1), 99)), 3),
    }


def op_arap_igl(ctx, iters=10):
    import igl
    import numpy as np

    V, F = ctx.V, ctx.F
    top, handle, _, b, bc = handle_setup(V)
    T = Timer()
    data = igl.ARAPData()
    data.max_iter = iters
    with T("precompute"):
        igl.arap_precomputation(V, F, 3, b.astype(np.int32), data)
    with T("solve"):
        U = igl.arap_solve(bc, data, V.copy())
    return dict(
        out=(U, F),
        timers=T,
        extra={
            "iters": iters,
            "n_constraints": len(b),
            **reach_metrics(V, U, top, handle),
            **edge_stretch(V, U, F),
        },
    )


def arap_prefactor(V, F, b, kind="splu"):
    """Classic ARAP (Sorkine & Alexa 2007) with cotan weights and the 'spokes' energy.
    Returns closure solve(rhs_free) with the free-block Laplacian factorised ONCE."""
    import numpy as np

    n = len(V)
    L = cot_laplacian(V, F).tocsr()
    free = np.setdiff1d(np.arange(n), b)
    Lff = L[free][:, free].tocsc()
    Lfc = L[free][:, b].tocsr()
    if kind == "splu":
        lu = splu_sym(Lff)
        solve = lu.solve
    elif kind == "cholespy":
        import cholespy

        c = Lff.tocoo()
        cs = cholespy.CholeskySolverD(
            Lff.shape[0],
            c.row.astype(np.int32),
            c.col.astype(np.int32),
            c.data.astype(np.float64),
            cholespy.MatrixType.COO,
        )

        def solve(rhs):
            x = np.zeros_like(rhs)
            cs.solve(np.ascontiguousarray(rhs), x)
            return x
    else:
        raise ValueError(kind)
    return L, free, Lfc, solve


def arap_iterate(V, F, b, bc, iters, kind):
    """Local-global ARAP with prefactorised global step.  Returns U, timers."""
    import numpy as np

    T = Timer()
    n = len(V)
    with T("precompute"):
        L, free, Lfc, solve = arap_prefactor(V, F, b, kind)
        Lc = L.tocoo()
        i, j, w = Lc.row, Lc.col, -Lc.data  # w_ij = cotan weight (off-diag)
        off = i != j
        i, j, w = i[off], j[off], w[off]
        dP = V[i] - V[j]
        order = np.argsort(i, kind="stable")  # deterministic reduction order
        i, j, w, dP = i[order], j[order], w[order], dP[order]
        U = V.copy()
        U[b] = bc
    with T("solve"):
        # warm start: linear Laplacian edit  L_ff U_f = (L V)_f - L_fc U_c
        U[free] = solve(np.ascontiguousarray(L[free] @ V - Lfc @ bc))
        for _ in range(iters):
            dU = U[i] - U[j]
            S = np.empty((n, 3, 3))
            for p in range(3):
                for q in range(3):
                    S[:, p, q] = np.bincount(
                        i, weights=w * dP[:, p] * dU[:, q], minlength=n
                    )
            Uu, _, Vt = np.linalg.svd(S)
            R = np.einsum("nij,njk->nik", Vt.transpose(0, 2, 1), Uu.transpose(0, 2, 1))
            neg = np.linalg.det(R) < 0
            if neg.any():
                Uu[neg, :, 2] *= -1
                R[neg] = np.einsum(
                    "nij,njk->nik",
                    Vt[neg].transpose(0, 2, 1),
                    Uu[neg].transpose(0, 2, 1),
                )
            RR = 0.5 * (R[i] + R[j])
            e = np.einsum("eij,ej->ei", RR, dP) * w[:, None]
            rhs = np.stack(
                [np.bincount(i, weights=e[:, c], minlength=n) for c in range(3)], 1
            )
            U[free] = solve(np.ascontiguousarray(rhs[free] - Lfc @ bc))
    return U, T


def op_arap_own(ctx, iters=10, kind="splu"):
    V, F = ctx.V, ctx.F
    top, handle, _, b, bc = handle_setup(V)
    U, T = arap_iterate(V, F, b, bc, iters, kind)
    return dict(
        out=(U, F),
        timers=T,
        extra={
            "iters": iters,
            "factor": kind,
            "n_constraints": len(b),
            **reach_metrics(V, U, top, handle),
            **edge_stretch(V, U, F),
        },
    )


def op_biharm_igl(ctx):
    import igl

    V, F = ctx.V, ctx.F
    top, handle, _, b, bc = handle_setup(V)
    T = Timer()
    with T("solve"):  # precompute + solve are one call in igl.harmonic
        Z = igl.harmonic(
            V, F, b, bc - V[b], 2
        )  # biharmonic interpolation of the displacement
    U = V + Z
    return dict(
        out=(U, F),
        timers=T,
        extra={**reach_metrics(V, U, top, handle), **edge_stretch(V, U, F)},
    )


def op_biharm_own(ctx):
    """Biharmonic deformation: min ||L M^-1 L d||  s.t. d[b]=bc-V[b]  (Botsch & Kobbelt 2004 style)."""
    import numpy as np
    import scipy.sparse as sp

    V, F = ctx.V, ctx.F
    top, handle, _, b, bc = handle_setup(V)
    T = Timer()
    n = len(V)
    with T("precompute"):
        L = cot_laplacian(V, F).tocsr()
        Minv = sp.diags(1.0 / mass_diag(V, F))
        Q = (L @ Minv @ L).tocsr()
        free = np.setdiff1d(np.arange(n), b)
        Qff = Q[free][:, free].tocsc()
        Qfc = Q[free][:, b]
        lu = splu_sym(Qff)
    with T("solve"):
        D = np.zeros_like(V)
        D[b] = bc - V[b]
        D[free] = lu.solve(np.ascontiguousarray(-(Qfc @ D[b])))
    U = V + D
    return dict(
        out=(U, F),
        timers=T,
        extra={
            "factor": "splu",
            **reach_metrics(V, U, top, handle),
            **edge_stretch(V, U, F),
        },
    )


# ----------------------------------------------------------------------------- 2. brush kernels
def smoothstep_falloff(d, r):
    import numpy as np

    x = np.clip(1.0 - d / r, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)  # Blender "Smooth" falloff curve (3x^2 - 2x^3)


def brush_center(V):
    import numpy as np

    return V[np.argmax(V @ np.array([1.0, 1.0, 1.0]))]


def sel(ctx, r):
    """Sphere selection + falloff (KD-tree, sorted indices so the result is order-independent)."""
    import numpy as np
    from scipy.spatial import cKDTree

    c = brush_center(ctx.V)
    tree = cKDTree(ctx.V)
    idx = np.sort(np.asarray(tree.query_ball_point(c, r), dtype=np.int64))
    d = np.linalg.norm(ctx.V[idx] - c, axis=1)
    return c, idx, smoothstep_falloff(d, r)


def mesh_extra(V, U, F):
    import numpy as np

    return {
        "verts": len(V),
        "faces": len(F),
        "max_move_mm": round(float(np.linalg.norm(U - V, axis=1).max()), 3),
        "vol_ratio": round(signed_volume(U, F) / signed_volume(V, F), 6),
    }


def op_brush_grab(ctx, r=8.0, delta=3.0):
    import numpy as np

    T = Timer()
    with T("select_falloff_kdtree"):
        c, idx, w = sel(ctx, r)
    with T("kernel"):
        n = vertex_normals(ctx.V, ctx.F)[
            idx[np.argmin(np.linalg.norm(ctx.V[idx] - c, axis=1))]
        ]
        U = ctx.V.copy()
        U[idx] += (w * delta)[:, None] * n
    return dict(
        out=(U, ctx.F),
        timers=T,
        extra={"n_sel": len(idx), **mesh_extra(ctx.V, U, ctx.F)},
    )


def op_brush_inflate_crease_pinch_clay(ctx, r=8.0):
    import numpy as np

    T = Timer()
    V, F = ctx.V, ctx.F
    with T("select_falloff_kdtree"):
        c, idx, w = sel(ctx, r)
    with T("normals"):
        N = vertex_normals(V, F)
    U = V.copy()
    nc = N[idx].mean(0)
    nc /= np.linalg.norm(nc)
    with T("inflate"):  # move along per-vertex normal
        U[idx] += (w * 1.0)[:, None] * N[idx]
    with T("pinch"):  # tangential pull toward the brush centre
        t = c - V[idx]
        t -= (t * N[idx]).sum(1, keepdims=True) * N[idx]
        U[idx] += (0.5 * w)[:, None] * t
    with T("crease"):  # pinch + dig along the average normal (Blender-style crease)
        t = c - V[idx]
        t -= (t * nc).sum(1, keepdims=True) * nc
        U[idx] += (0.4 * w)[:, None] * t - (0.8 * w)[:, None] * nc
    with T("clay_flatten"):  # project toward the mean plane
        pl = V[idx].mean(0)
        dist = (U[idx] - pl) @ nc
        U[idx] -= (0.7 * w * dist)[:, None] * nc
    return dict(out=(U, F), timers=T, extra={"n_sel": len(idx), **mesh_extra(V, U, F)})


def op_smooth_kernels(ctx, iters=10):
    """Uniform Laplacian / Taubin / HC explicit smoothing, `iters` passes, vol shrink reported."""
    import numpy as np

    V, F = ctx.V, ctx.F
    T = Timer()
    with T("adjacency"):
        A = uniform_adjacency(V, F)
        Dinv = 1.0 / np.asarray(A.sum(1)).ravel()

    def lap(X):
        return Dinv[:, None] * (A @ X) - X

    res = {}
    with T("laplacian_l0.5"):
        X = V.copy()
        for _ in range(iters):
            X = X + 0.5 * lap(X)
        res["laplacian"] = X
    with T("taubin_l0.5_mu-0.53"):
        X = V.copy()
        for _ in range(iters):
            X = X + 0.5 * lap(X)
            X = X - 0.53 * lap(X)
        res["taubin"] = X
    with T("hc_a0_b0.5"):
        X, O = V.copy(), V.copy()
        for _ in range(iters):
            Pn = X + lap(X)
            B = Pn - O
            Bm = Dinv[:, None] * (A @ B)
            X = Pn - (0.5 * B + 0.5 * Bm)
        res["hc"] = X
    ex = {
        k: {
            "vol_ratio": round(signed_volume(v, F) / signed_volume(V, F), 5),
            "rms_move_mm": round(float(np.sqrt(((v - V) ** 2).sum(1).mean())), 5),
        }
        for k, v in res.items()
    }
    return dict(out=(res["taubin"], F), timers=T, extra=ex)


def op_trimesh_smoothing(ctx, iters=10):
    """trimesh.smoothing filters (reference implementations: sparse-matrix, uniform weights)."""
    import trimesh
    from trimesh import smoothing

    V, F = ctx.V, ctx.F
    T = Timer()
    res = {}
    for name, fn, kw in (
        ("laplacian", smoothing.filter_laplacian, {"lamb": 0.5}),
        ("taubin", smoothing.filter_taubin, {"lamb": 0.5, "nu": 0.53}),
        ("humphrey", smoothing.filter_humphrey, {"alpha": 0.1, "beta": 0.5}),
    ):
        m = trimesh.Trimesh(V.copy(), F.copy(), process=False)
        with T(name):
            fn(m, iterations=iters, **kw)
        res[name] = m.vertices.copy()
    ex = {k: {"vol_ratio": round(signed_volume(v, F) / signed_volume(V, F), 5)} for k, v in res.items()}
    return dict(out=(res["taubin"], F), timers=T, extra=ex)


def op_smooth_implicit(ctx, kind="splu", t=1.0):
    """Implicit cotan smoothing (Desbrun 1999): (M + t L) X = M X0, one backward-Euler step."""
    import numpy as np
    import scipy.sparse as sp

    V, F = ctx.V, ctx.F
    T = Timer()
    with T("assemble"):
        L = cot_laplacian(V, F)
        m = mass_diag(V, F)
        A = (sp.diags(m) + t * L).tocsc()
    with T("factor"):
        if kind == "splu":
            solve = splu_sym(A).solve
        else:
            import cholespy

            c = A.tocoo()
            cs = cholespy.CholeskySolverD(
                A.shape[0],
                c.row.astype(np.int32),
                c.col.astype(np.int32),
                c.data,
                cholespy.MatrixType.COO,
            )

            def solve(rhs):
                x = np.zeros_like(rhs)
                cs.solve(np.ascontiguousarray(rhs), x)
                return x

    with T("solve"):
        U = solve(np.ascontiguousarray(m[:, None] * V))
    return dict(
        out=(U, F), timers=T, extra={"t": t, "factor": kind, **mesh_extra(V, U, F)}
    )


def op_symmetrize(ctx):
    """Mirror x->-x, nearest-neighbour pair (cKDTree, workers=-1), average => exactly symmetric pairs."""
    import numpy as np
    from scipy.spatial import cKDTree

    V, F = ctx.V, ctx.F
    T = Timer()
    with T("kdtree_mirror_average"):
        M = V * np.array([-1.0, 1.0, 1.0])
        d, j = cKDTree(V).query(M, workers=-1)
        U = V.copy()
        U[:, 0] = 0.5 * (V[:, 0] - V[j, 0])
        U[:, 1:] = 0.5 * (V[:, 1:] + V[j, 1:])
    return dict(
        out=(U, F),
        timers=T,
        extra={
            "mirror_gap_mean_mm": round(float(d.mean()), 4),
            "mirror_gap_max_mm": round(float(d.max()), 4),
        },
    )


# ----------------------------------------------------------------------------- 3. relief / noise
class Noise:
    """Vectorised gradient (Perlin) noise in 3D with a seeded permutation table, fBm, ridged, warp."""

    GRAD = None

    def __init__(self, seed=SEED):
        import numpy as np

        rng = np.random.default_rng(seed)
        p = rng.permutation(256)
        self.p = np.concatenate([p, p]).astype(np.int64)
        g = rng.normal(size=(256, 3))
        self.g = g / np.linalg.norm(g, axis=1, keepdims=True)

    def noise(self, X):
        import numpy as np

        i = np.floor(X).astype(np.int64)
        f = X - i
        u = f * f * f * (f * (f * 6 - 15) + 10)
        i &= 255
        p, g = self.p, self.g
        out = 0.0
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    h = p[p[p[i[:, 0] + dx] + i[:, 1] + dy] + i[:, 2] + dz]
                    d = f - np.array([dx, dy, dz])
                    wx = u[:, 0] if dx else 1 - u[:, 0]
                    wy = u[:, 1] if dy else 1 - u[:, 1]
                    wz = u[:, 2] if dz else 1 - u[:, 2]
                    out = out + wx * wy * wz * (g[h] * d).sum(1)
        return out * 1.1547  # ~[-1,1]

    def fbm(self, X, octaves=4, lac=2.0, gain=0.5):
        a, s, out = 1.0, 1.0, 0.0
        for _ in range(octaves):
            out = out + a * self.noise(X * s)
            a *= gain
            s *= lac
        return out

    def ridged(self, X, octaves=4):
        import numpy as np

        a, s, out = 1.0, 1.0, 0.0
        for _ in range(octaves):
            out = out + a * (1.0 - np.abs(self.noise(X * s)))
            a *= 0.5
            s *= 2.0
        return out

    def warp(self, X, amp=0.6):
        import numpy as np

        off = np.stack([self.noise(X + o) for o in (0.0, 17.1, 41.7)], 1)
        return X + amp * off


def hash_u32(x):
    import numpy as np

    x = x.astype(np.uint32)
    x ^= x >> np.uint32(16)
    x *= np.uint32(0x7FEB352D)
    x ^= x >> np.uint32(15)
    x *= np.uint32(0x846CA68B)
    x ^= x >> np.uint32(16)
    return x


def voronoi_f1(X):
    """Cellular noise F1 (distance to the nearest of one jittered feature point per unit cell)."""
    import numpy as np

    c = np.floor(X).astype(np.int64)
    best = np.full(len(X), 1e9)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                cc = c + np.array([dx, dy, dz])
                h = hash_u32(
                    hash_u32(
                        hash_u32(cc[:, 0].astype(np.uint32))
                        + cc[:, 1].astype(np.uint32)
                    )
                    + cc[:, 2].astype(np.uint32)
                )
                jit = np.stack(
                    [
                        (h & 1023) / 1023.0,
                        ((h >> 10) & 1023) / 1023.0,
                        ((h >> 20) & 1023) / 1023.0,
                    ],
                    1,
                )
                best = np.minimum(best, np.linalg.norm(cc + jit - X, axis=1))
    return best


def lipschitz_clamp(d, indptr, indices, elen, smax, max_iter=2000):
    """Largest smax-Lipschitz (w.r.t. edge length) field <= d: Bellman-Ford min-plus relaxation.
    Guarantees |d_i - d_j| <= smax*|e_ij| on every edge => bounded height-field tilt."""
    import numpy as np

    d = d.copy()
    starts = indptr[:-1]
    it = 0
    for it in range(1, max_iter + 1):
        cand = np.minimum.reduceat(d[indices] + smax * elen, starts)
        new = np.minimum(d, cand)
        if np.array_equal(new, d):
            break
        d = new
    return d, it


def tilt_stats(V, U, F):
    import numpy as np

    a, b = face_normals(V, F), face_normals(U, F)
    ang = np.degrees(np.arccos(np.clip((a * b).sum(1), -1, 1)))
    return {
        "tilt_max_deg": round(float(ang.max()), 2),
        "tilt_p99_deg": round(float(np.percentile(ang, 99)), 2),
    }


def op_relief(ctx, amp=0.6, smax=0.7):
    """Normal-direction relief: fBm, ridged+domain warp, Voronoi, triplanar, flow-strand; then slope clamp."""
    import numpy as np

    V, F = ctx.V, ctx.F
    nz = Noise()
    N = vertex_normals(V, F)
    T = Timer()
    f = 0.35  # base frequency 1/mm
    with T("fbm"):
        d_fbm = nz.fbm(V * f)
    with T("ridged_warp"):
        d_rw = nz.ridged(nz.warp(V * f))
    with T("voronoi_f1"):
        d_vor = voronoi_f1(V * f * 0.8)
    with T("triplanar"):
        w = np.abs(N) ** 4
        w /= w.sum(1, keepdims=True)
        d_tri = np.zeros(len(V))
        for ax, (a, b) in enumerate(((1, 2), (0, 2), (0, 1))):
            q = np.stack(
                [V[:, a] * f, V[:, b] * f, np.full(len(V), 0.37 * (ax + 1))], 1
            )
            d_tri += w[:, ax] * nz.ridged(q, octaves=3)
    with T("flow_strands"):
        t = np.cross(N, np.array([0.0, 0.0, 1.0]))
        t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
        bvec = np.cross(N, t)
        q = np.stack(
            [(V * bvec).sum(1) * 2.5, (V * t).sum(1) * 0.15, (V * N).sum(1) * 0.0], 1
        )
        d_flow = nz.ridged(q, octaves=3)
    fields = {
        "fbm": d_fbm,
        "ridged_warp": d_rw,
        "voronoi": d_vor,
        "triplanar": d_tri,
        "flow": d_flow,
    }
    ex = {}
    with T("graph_setup"):
        A = uniform_adjacency(V, F)
        A.sort_indices()
        e = np.linalg.norm(
            V[np.repeat(np.arange(len(V)), np.diff(A.indptr))] - V[A.indices], axis=1
        )
    U_all = V.copy()
    with T("slope_clamp_all_fields"):
        for name, d in fields.items():
            d = (d - d.min()) / (np.ptp(d) + 1e-12) * amp  # [0, amp] mm height
            src = np.repeat(np.arange(len(V)), np.diff(A.indptr))
            s0 = float((np.abs(d[src] - d[A.indices]) / e).max())
            dc, iters = lipschitz_clamp(d, A.indptr, A.indices, e, smax)
            s1 = float((np.abs(dc[src] - dc[A.indices]) / e).max())
            U = V + dc[:, None] * N
            ex[name] = {
                "edge_slope_max_before": round(s0, 2),
                "edge_slope_max_after": round(s1, 3),
                "clamp_iters": iters,
                **tilt_stats(V, U, F),
            }
            if name == "ridged_warp":
                U_all = U
    return dict(out=(U_all, F), timers=T, extra={"smax": smax, "amp_mm": amp, **ex})


# ----------------------------------------------------------------------------- 4. remesh / decimate
def op_pymeshlab_isotropic(ctx, target=0.6):
    import pymeshlab as pm

    V, F = ctx.V, ctx.F
    T = Timer()
    ms = pm.MeshSet()
    ms.add_mesh(pm.Mesh(V, F))
    with T("isotropic_explicit_remeshing"):
        ms.meshing_isotropic_explicit_remeshing(
            iterations=3, targetlen=pm.PureValue(target)
        )
    m = ms.current_mesh()
    return dict(
        out=(m.vertex_matrix(), m.face_matrix()),
        timers=T,
        extra={"target_mm": target, "faces_out": m.face_number(), **surface_dev(V, F, m.vertex_matrix(), m.face_matrix())},
    )


def op_pymeshlab_qec(ctx, faces=70000):
    import pymeshlab as pm

    V, F = ctx.V, ctx.F
    T = Timer()
    ms = pm.MeshSet()
    ms.add_mesh(pm.Mesh(V, F))
    with T("quadric_edge_collapse"):
        ms.meshing_decimation_quadric_edge_collapse(
            targetfacenum=faces, preservenormal=True
        )
    m = ms.current_mesh()
    return dict(
        out=(m.vertex_matrix(), m.face_matrix()),
        timers=T,
        extra={"faces_out": m.face_number(), **surface_dev(V, F, m.vertex_matrix(), m.face_matrix())},
    )


def surface_dev(V, F, V2, F2):
    """Symmetric surface deviation estimate (mm) plus edge-length / angle quality of the result: vertices of each mesh vs. dense samples
    (vertices + face centroids) of the other.  Subset-vertex decimators (manifold3d) score ~0 on
    a vertex-only metric, hence the samples."""
    import numpy as np
    from scipy.spatial import cKDTree

    P1 = np.concatenate([V, V[F].mean(1)])
    P2 = np.concatenate([V2, V2[F2].mean(1)])
    d12, _ = cKDTree(P1).query(V2, workers=-1)
    d21, _ = cKDTree(P2).query(V, workers=-1)
    e = np.concatenate([V2[F2[:, [0, 1]]], V2[F2[:, [1, 2]]], V2[F2[:, [2, 0]]]])
    el = np.linalg.norm(e[:, 0] - e[:, 1], axis=1)
    P = V2[F2]
    ang = []
    for a in range(3):
        u, w = P[:, (a + 1) % 3] - P[:, a], P[:, (a + 2) % 3] - P[:, a]
        c = (u * w).sum(1) / np.maximum(np.linalg.norm(u, axis=1) * np.linalg.norm(w, axis=1), 1e-300)
        ang.append(np.degrees(np.arccos(np.clip(c, -1, 1))))
    ang = np.concatenate(ang)
    return {
        "dev_max_mm": round(float(max(d12.max(), d21.max())), 4),
        "dev_mean_mm": round(float(0.5 * (d12.mean() + d21.mean())), 5),
        "edge_mm_p5_p50_p95": [round(float(x), 3) for x in np.percentile(el, [5, 50, 95])],
        "min_angle_deg_min_p1": [round(float(ang.min()), 2), round(float(np.percentile(ang, 1)), 2)],
    }


def op_fast_simplification(ctx, reduction=0.82):
    import fast_simplification as fs

    T = Timer()
    with T("simplify"):
        V2, F2 = fs.simplify(ctx.V, ctx.F, target_reduction=reduction)
    return dict(
        out=(V2, F2),
        timers=T,
        extra={"faces_out": len(F2), **surface_dev(ctx.V, ctx.F, V2, F2)},
    )


def op_pyfqmr(ctx, faces=70000):
    import pyfqmr

    T = Timer()
    s = pyfqmr.Simplify()
    s.setMesh(ctx.V, ctx.F)
    with T("simplify_mesh"):
        s.simplify_mesh(
            target_count=faces, aggressiveness=7, preserve_border=True, verbose=False
        )
    V2, F2, _ = s.getMesh()
    return dict(
        out=(V2.astype("float64"), F2.astype("int64")),
        timers=T,
        extra={"faces_out": len(F2), **surface_dev(ctx.V, ctx.F, V2, F2)},
    )


def op_manifold_simplify(ctx, tol=0.02):
    import manifold3d as m
    import numpy as np

    T = Timer()
    with T("build"):
        mf = m.Manifold(m.Mesh(ctx.V.astype(np.float32), ctx.F.astype(np.uint32)))
    with T("simplify"):
        mf2 = mf.simplify(tol)
    mesh = mf2.to_mesh()
    V2 = mesh.vert_properties[:, :3].astype(np.float64)
    F2 = mesh.tri_verts.astype(np.int64)
    return dict(
        out=(V2, F2),
        timers=T,
        extra={
            "tol_mm": tol,
            "status": str(mf.status()),
            "faces_out": len(F2),
            **surface_dev(ctx.V, ctx.F, V2, F2),
        },
    )


def op_open3d_quadric(ctx, faces=70000):
    import numpy as np
    import open3d as o3d

    T = Timer()
    mesh = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(ctx.V), o3d.utility.Vector3iVector(ctx.F)
    )
    with T("simplify_quadric_decimation"):
        m2 = mesh.simplify_quadric_decimation(target_number_of_triangles=faces)
    V2, F2 = np.asarray(m2.vertices), np.asarray(m2.triangles).astype(np.int64)
    return dict(
        out=(V2, F2),
        timers=T,
        extra={"faces_out": len(F2), **surface_dev(ctx.V, ctx.F, V2, F2)},
    )


def op_igl_qslim(ctx, faces=70000):
    import igl
    import numpy as np

    T = Timer()
    with T("qslim"):
        res = igl.qslim(
            np.asfortranarray(ctx.V), np.asfortranarray(ctx.F.astype(np.int32)), faces
        )
    V2, F2 = res[-4], res[-3]
    return dict(
        out=(np.asarray(V2), np.asarray(F2, dtype=np.int64)),
        timers=T,
        extra={"faces_out": len(F2), **surface_dev(ctx.V, ctx.F, np.asarray(V2), np.asarray(F2, dtype=np.int64))},
    )


def op_gpytoolbox_botsch(ctx, target=0.6):
    import gpytoolbox as g

    T = Timer()
    with T("remesh_botsch"):
        V2, F2 = g.remesh_botsch(ctx.V, ctx.F, i=3, h=float(target))
    return dict(
        out=(V2, F2.astype("int64")),
        timers=T,
        extra={"target_mm": target, "faces_out": len(F2), **surface_dev(ctx.V, ctx.F, V2, F2)},
    )


# ----------------------------------------------------------------------------- 5. SDF offset
def op_sdf_scipy(ctx, voxel=0.2, offset=0.3, sigma_vox=1.0):
    """Mesh -> narrow-band SDF (KD-tree on dense surface samples, flood-fill sign) -> +/-offset ->
    gaussian smooth (scipy.ndimage) -> skimage marching cubes.  Pure-wheel OpenVDB substitute."""
    import numpy as np
    import scipy.ndimage as ndi
    from scipy.spatial import cKDTree
    from skimage.measure import marching_cubes

    V, F = ctx.V, ctx.F
    T = Timer()
    ex = {"voxel_mm": voxel}
    with T("samples_kdtree"):
        P = np.concatenate([V, V[F].mean(1)])
        tree = cKDTree(P)
        lo = V.min(0) - 6 * voxel - offset - 4 * sigma_vox * voxel
        hi = V.max(0) + 6 * voxel + offset + 4 * sigma_vox * voxel
        shape = np.ceil((hi - lo) / voxel).astype(int) + 1
    with T("band_mask"):
        occ = np.zeros(shape, bool)
        ijk = np.floor((P - lo) / voxel).astype(int)
        occ[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True
        band = ndi.binary_dilation(occ, iterations=3)
    with T("flood_fill_sign"):
        lab, nl = ndi.label(~band)
        outside_lab = lab[0, 0, 0]
        outside = lab == outside_lab
    with T("band_distance"):
        Np = np.concatenate([vertex_normals(V, F), face_normals(V, F)])
        bi = np.argwhere(band)
        q = lo + bi * voxel
        d, nn = tree.query(q, workers=-1)
        sgn = np.where(((q - P[nn]) * Np[nn]).sum(1) >= 0.0, 1.0, -1.0)  # side of the nearest sample's normal
        sdf = np.full(shape, 6 * voxel, np.float32)
        sdf[~outside & ~band] = -6 * voxel
        sdf[tuple(bi.T)] = (sgn * d).astype(np.float32)
        ex["grid"] = [int(s) for s in shape]
    with T("gaussian_smooth"):
        sdf = ndi.gaussian_filter(sdf, sigma_vox)
    out = {}
    for name, off in (("plus", offset), ("minus", -offset)):
        with T(f"marching_cubes_{name}"):
            v, f, _, _ = marching_cubes(
                sdf, level=off, spacing=(voxel,) * 3, method="lewiner"
            )
            v = (v + lo).astype(np.float64)
            f = f.astype(np.int64)
        d2, _ = cKDTree(P).query(v, workers=-1)
        ex[f"offset_{name}"] = {
            "faces": len(f),
            "dist_to_src_mean_mm": round(float(d2.mean()), 3),
            "dist_to_src_p5_p95_mm": [
                round(float(np.percentile(d2, 5)), 3),
                round(float(np.percentile(d2, 95)), 3),
            ],
        }
        out[name] = (v, f)
    return dict(out=out["plus"], timers=T, extra=ex)


def op_openvdb_probe(ctx, voxel=0.2, offset=0.3):
    """Mesh -> VDB level set -> offset +0.3 mm (via grid.mapOn) -> mesh.  Needs the openvdb Python
    module, which is on conda-forge only (NOT PyPI; PyPI has just stale cp37/cp38 `pyopenvdb`)."""
    import numpy as np
    from scipy.spatial import cKDTree

    try:
        import openvdb as vdb
    except ImportError:
        import pyopenvdb as vdb
    T = Timer()
    with T("createLevelSetFromPolygons"):
        g = vdb.FloatGrid.createLevelSetFromPolygons(
            ctx.V.astype(np.float32),
            triangles=ctx.F.astype(np.uint32),
            transform=vdb.createLinearTransform(voxelSize=voxel),
            halfWidth=3.0,
        )
    with T("offset_mapOn_python_callback"):
        g2 = g.deepCopy()
        g2.mapOn(
            lambda x: x - offset
        )  # per-voxel Python call: slow, no LevelSetFilter in the Python API
    with T("convertToPolygons"):
        pts, tris, quads = g2.convertToPolygons(isovalue=0.0)
    tri2 = (
        np.concatenate([tris, quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]])
        if len(quads)
        else tris
    )
    pts = np.asarray(pts, float)
    d, _ = cKDTree(np.concatenate([ctx.V, ctx.V[ctx.F].mean(1)])).query(pts)
    return dict(
        out=(pts, tri2.astype(np.int64)),
        timers=T,
        extra={
            "active_voxels": int(g.activeVoxelCount()),
            "grid_filter_like_attrs": sorted(
                a for a in dir(g) if "filter" in a.lower() or "offset" in a.lower()
            ),
            "dist_to_src_mean_mm": round(float(d.mean()), 3),
            "dist_to_src_p5_p95_mm": [
                round(float(np.percentile(d, 5)), 3),
                round(float(np.percentile(d, 95)), 3),
            ],
        },
    )


# ----------------------------------------------------------------------------- 6. self-intersection
def two_spheres(tag="50k", shift=25.0):
    """Two bumpy spheres overlapping (intersecting) vs. 0-intersection reference (shifted far apart)."""
    import numpy as np

    V, F = make_mesh(tag)
    V2 = V + np.array([shift, 0, 0])
    return np.concatenate([V, V2]), np.concatenate([F, F + len(V)])


def selfint_numpy(V, F, chunk=2_000_000):
    """Edge-vs-triangle Moller-Trumbore on KD-tree candidates; skips pairs that share a vertex.
    Count of piercing (edge, face) pairs; 0 => no (transversal) self-intersection."""
    import numpy as np
    from scipy.spatial import cKDTree

    E = edges_of(F)
    mid = 0.5 * (V[E[:, 0]] + V[E[:, 1]])
    cen = V[F].mean(1)
    elen = np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1)
    rf = np.linalg.norm(V[F] - cen[:, None], axis=2).max(1)
    r = float(elen.max() / 2 + rf.max()) * 1.001
    te, tf = cKDTree(mid), cKDTree(cen)
    sdm = te.sparse_distance_matrix(tf, r, output_type="ndarray")
    ei, fi = sdm["i"].astype(np.int64), sdm["j"].astype(np.int64)
    share = (E[ei][:, :, None] == F[fi][:, None, :]).any(axis=(1, 2))
    ei, fi = ei[~share], fi[~share]
    hits = 0
    for s in range(0, len(ei), chunk):
        a, b = ei[s : s + chunk], fi[s : s + chunk]
        p0, p1 = V[E[a, 0]], V[E[a, 1]]
        t0, t1, t2 = V[F[b, 0]], V[F[b, 1]], V[F[b, 2]]
        dirv = p1 - p0
        e1, e2 = t1 - t0, t2 - t0
        h = np.cross(dirv, e2)
        det = (e1 * h).sum(1)
        ok = np.abs(det) > 1e-14
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        sv = p0 - t0
        u = (sv * h).sum(1) * inv
        q = np.cross(sv, e1)
        v = (dirv * q).sum(1) * inv
        t = (e2 * q).sum(1) * inv
        hit = ok & (u > 0) & (v > 0) & (u + v < 1) & (t > 0) & (t < 1)
        hits += int(hit.sum())
    return hits, len(ei)


def op_selfint(ctx):
    """On the clean mesh (expect 0) and on a deliberately overlapping 2-sphere mesh (expect > 0)."""
    import pymeshlab as pm
    import open3d as o3d
    from scipy.spatial import cKDTree  # noqa: F401

    T = Timer()
    ex = {}
    Vb, Fb = two_spheres("50k")
    Vc, Fc = two_spheres("50k", shift=70.0)

    def meshlab(V, F):
        ms = pm.MeshSet()
        ms.add_mesh(pm.Mesh(V, F))
        ms.compute_selection_by_self_intersections_per_face()
        return int(ms.current_mesh().selected_face_number())

    def o3d_si(V, F):
        m = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(V), o3d.utility.Vector3iVector(F)
        )
        return bool(m.is_self_intersecting())

    with T("pymeshlab_400k_clean"):
        ex["pymeshlab_400k_clean_sel_faces"] = meshlab(ctx.V, ctx.F)
    with T("open3d_400k_clean"):
        ex["open3d_400k_clean_self_intersecting"] = o3d_si(ctx.V, ctx.F)
    with T("numpy_kdtree_400k_clean"):
        ex["numpy_400k_clean_hits_candidates"] = list(selfint_numpy(ctx.V, ctx.F))
    for name, (V, F) in (("overlap_100k", (Vb, Fb)), ("apart_100k", (Vc, Fc))):
        with T(f"pymeshlab_{name}"):
            ex[f"pymeshlab_{name}_sel_faces"] = meshlab(V, F)
        with T(f"open3d_{name}"):
            ex[f"open3d_{name}_self_intersecting"] = o3d_si(V, F)
        with T(f"numpy_{name}"):
            ex[f"numpy_{name}_hits_candidates"] = list(selfint_numpy(V, F))
    return dict(out=None, timers=T, extra=ex)


# ----------------------------------------------------------------------------- 7. fit-to-target
def synthetic_target(V):
    """Deterministic target shape: anisotropic bulge + ripple of the source (independent topology of the fit)."""
    import numpy as np

    return V * (1.0 + 0.04 * np.sin(V[:, [0]] * 0.15 + 1.0)) + 0.5 * np.sin(V[:, [1]] * 0.2)


def biharmonic_factor(V, F, b):
    """Prefactor  Q = L M^-1 L  restricted to the free vertices (hard constraints at b)."""
    import numpy as np
    import scipy.sparse as sp

    n = len(V)
    L = cot_laplacian(V, F).tocsr()
    Q = (L @ sp.diags(1.0 / mass_diag(V, F)) @ L).tocsr()
    free = np.setdiff1d(np.arange(n), b)
    return splu_sym(Q[free][:, free].tocsc()), free, Q[free][:, b]


def op_fit_biharm_icp(ctx, rounds=4, stride=400, step=0.5):
    """Non-rigid fit of the mesh to a different bumpy target with a cheap deterministic loop:
    every `stride`-th vertex is a hard landmark whose target = nearest target vertex (cKDTree);
    biharmonic interpolation (own, prefactored ONCE) of the displacement, repeated `rounds` times with a
    damped step (rounds>1 re-estimates correspondences on the already-deformed mesh = sliding)."""
    import numpy as np
    from scipy.spatial import cKDTree

    V, F = ctx.V, ctx.F
    T = Timer()
    tgt = synthetic_target(V)
    b = np.arange(0, len(V), stride, dtype=np.int64)
    with T("precompute"):
        lu, free, Qfc = biharmonic_factor(V, F, b)
        ttree = cKDTree(tgt)
    cur = V.copy()
    hist = []
    with T("fit_rounds"):
        for r in range(rounds):
            _, j = ttree.query(cur[b], workers=-1)
            D = np.zeros_like(V)
            st = 1.0 if r == 0 else step
            D[b] = cur[b] + st * (tgt[j] - cur[b]) - V[b]
            D[free] = lu.solve(np.ascontiguousarray(-(Qfc @ D[b])))
            cur = V + D
            d, _ = ttree.query(cur, workers=-1)
            hist.append(round(float(d.mean()), 4))
    d0, _ = ttree.query(V, workers=-1)
    return dict(
        out=(cur, F),
        timers=T,
        extra={
            "landmarks": len(b),
            "mean_dist_before_mm": round(float(d0.mean()), 4),
            "mean_dist_after_round_mm (round 1 = full step)": hist,
        },
    )


def op_rbf_tps(ctx, stride=400):
    """Landmark fit with a thin-plate-spline RBF field in R^3 (scipy RBFInterpolator, dense solve)."""
    import numpy as np
    from scipy.interpolate import RBFInterpolator
    from scipy.spatial import cKDTree

    V, F = ctx.V, ctx.F
    T = Timer()
    tgt = synthetic_target(V)
    b = np.arange(0, len(V), stride, dtype=np.int64)
    ttree = cKDTree(tgt)
    with T("landmark_targets"):
        _, j = ttree.query(V[b], workers=-1)
        Db = tgt[j] - V[b]
    with T("rbf_fit"):
        rbf = RBFInterpolator(V[b], Db, kernel="thin_plate_spline", smoothing=0.0)
    with T("rbf_eval_all_vertices"):
        U = V + rbf(V)
    d, _ = ttree.query(U, workers=-1)
    return dict(out=(U, F), timers=T, extra={"landmarks": len(b), "mean_dist_after_mm": round(float(d.mean()), 4),
                                              **edge_stretch(V, U, F)})


def op_ffd(ctx, stride=400, cells=4):
    """Lattice FFD (cubic Bezier tensor product, (cells+1)^3 control points) fitted to the same landmarks."""
    import numpy as np
    from math import comb
    from scipy.spatial import cKDTree

    V, F = ctx.V, ctx.F
    T = Timer()
    tgt = synthetic_target(V)
    b = np.arange(0, len(V), stride, dtype=np.int64)
    lo, hi = V.min(0), V.max(0)
    deg = cells

    def basis(X):
        t = (X - lo) / (hi - lo)
        B = [np.stack([comb(deg, i) * t[:, a] ** i * (1 - t[:, a]) ** (deg - i) for i in range(deg + 1)], 1) for a in range(3)]
        return np.einsum("ni,nj,nk->nijk", *B).reshape(len(X), -1)

    ttree = cKDTree(tgt)
    with T("fit_control_points_ridge"):
        _, j = ttree.query(V[b], workers=-1)
        Bl = basis(V[b])
        A = Bl.T @ Bl
        C = np.linalg.solve(A + 1e-6 * np.trace(A) / len(A) * np.eye(len(A)), Bl.T @ (tgt[j] - V[b]))  # ridge: shell data leaves inner control points free
    with T("eval_all_vertices"):
        U = V + basis(V) @ C
    d, _ = ttree.query(U, workers=-1)
    return dict(out=(U, F), timers=T, extra={"control_points": (deg + 1) ** 3, "mean_dist_after_mm": round(float(d.mean()), 4),
                                              **edge_stretch(V, U, F)})


def op_fit_silhouette(ctx, rounds=4, stride_contour=4, step=0.5):
    """Silhouette fit (orthographic, view along z): limb vertices (|n_z|<0.12) are moved in the image
    plane toward the nearest target-silhouette point (2D KD-tree); biharmonic interpolation, z free."""
    import numpy as np
    from scipy.spatial import cKDTree

    V, F = ctx.V, ctx.F
    T = Timer()
    tgt = synthetic_target(V)
    nS, nT = vertex_normals(V, F), vertex_normals(tgt, F)
    src_c = np.where(np.abs(nS[:, 2]) < 0.12)[0]
    tgt_c = np.where(np.abs(nT[:, 2]) < 0.05)[0]
    b = src_c[::stride_contour].astype(np.int64)
    ttree2 = cKDTree(tgt[tgt_c][:, :2])
    with T("precompute"):
        lu, free, Qfc = biharmonic_factor(V, F, b)
    cur = V.copy()
    hist = []

    def contour_gap(U):
        d1, _ = ttree2.query(U[src_c][:, :2], workers=-1)
        d2, _ = cKDTree(U[src_c][:, :2]).query(tgt[tgt_c][:, :2], workers=-1)
        return round(float(0.5 * (d1.mean() + d2.mean())), 4), round(float(max(d1.max(), d2.max())), 4)

    g0 = contour_gap(V)
    with T("fit_rounds"):
        for r in range(rounds):
            _, j = ttree2.query(cur[b][:, :2], workers=-1)
            goal = cur[b].copy()
            goal[:, :2] += (1.0 if r == 0 else step) * (tgt[tgt_c][j][:, :2] - cur[b][:, :2])
            D = np.zeros_like(V)
            D[b] = goal - V[b]
            D[free] = lu.solve(np.ascontiguousarray(-(Qfc @ D[b])))
            cur = V + D
            hist.append(contour_gap(cur))
    return dict(out=(cur, F), timers=T, extra={"contour_vertices": len(src_c), "landmarks": len(b),
                                                "contour_gap_mean_max_before_mm": list(g0),
                                                "contour_gap_mean_max_after_round_mm": hist,
                                                **edge_stretch(V, cur, F)})


def op_geodesic_laplacian(ctx):
    """potpourri3d heat-method geodesic distance from the brush centre (surface-distance falloff) and the
    robust_laplacian intrinsic-Delaunay Laplacian build, vs. the numpy cotan Laplacian above."""
    import numpy as np
    import potpourri3d as pp3d
    import robust_laplacian

    V, F = ctx.V, ctx.F
    T = Timer()
    src = int(np.argmax(V @ np.array([1.0, 1.0, 1.0])))
    with T("heat_solver_precompute"):
        solver = pp3d.MeshHeatMethodDistanceSolver(V, F)
    with T("heat_distance_one_source"):
        dist = solver.compute_distance(src)
    with T("dijkstra_edge_graph_limit_16mm"):
        import scipy.sparse as sp
        from scipy.sparse.csgraph import dijkstra

        e = edges_of(F)
        w = np.linalg.norm(V[e[:, 0]] - V[e[:, 1]], axis=1)
        G = sp.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(V),) * 2).tocsr()
        dd = dijkstra(G, directed=False, indices=src, limit=16.0)
    with T("robust_laplacian_L_M"):
        L, M = robust_laplacian.mesh_laplacian(V, F)
    with T("numpy_cot_laplacian"):
        cot_laplacian(V, F)
    eu = np.linalg.norm(V - V[src], axis=1)
    near = eu < 8.0
    return dict(out=None, timers=T, extra={
        "dijkstra_vs_heat_median_ratio_within_16mm": round(float(np.median(dd[dd < 16.0][1:] / np.maximum(dist[dd < 16.0][1:], 1e-9))), 4),
        "geodesic_max_mm": round(float(dist.max()), 2), "expected_pi_R_mm": round(float(np.pi * RADIUS), 2),
        "geo_over_euclid_within_8mm": round(float(np.median(dist[near][1:] / np.maximum(eu[near][1:], 1e-9))), 4),
        "L_nnz": int(L.nnz), "M_total_area_mm2": round(float(M.sum()), 1)})


# ----------------------------------------------------------------------------- 8. bpy probes
def op_bpy_modifiers(ctx):
    """Headless Blender-as-module: modifiers applied via bpy.ops.object.modifier_apply (needs py3.13 wheel)."""
    import bpy
    import numpy as np

    T = Timer()
    ex = {"bpy": bpy.app.version_string}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    me = bpy.data.meshes.new("m")
    with T("from_pydata_foreach_set"):
        me.vertices.add(len(ctx.V))
        me.vertices.foreach_set("co", ctx.V.astype(np.float32).ravel())
        me.loops.add(3 * len(ctx.F))
        me.loops.foreach_set("vertex_index", ctx.F.astype(np.int32).ravel())
        me.polygons.add(len(ctx.F))
        me.polygons.foreach_set(
            "loop_start", np.arange(0, 3 * len(ctx.F), 3, dtype=np.int32)
        )
        me.polygons.foreach_set("loop_total", np.full(len(ctx.F), 3, np.int32))
        me.update()
        me.validate()
    ob = bpy.data.objects.new("o", me)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)

    def apply(kind, **kw):
        md = ob.modifiers.new(kind, kind)
        for k, v in kw.items():
            setattr(md, k, v)
        with T(f"modifier_{kind}"):
            bpy.ops.object.modifier_apply(modifier=md.name)

    apply("SMOOTH", factor=0.5, iterations=10)
    apply("REMESH", mode="VOXEL", voxel_size=0.5)
    apply("DECIMATE", ratio=0.5)
    ex["faces_out"] = len(ob.data.polygons)
    try:
        bpy.ops.object.mode_set(mode="SCULPT")
        r = bpy.ops.sculpt.brush_stroke(
            stroke=[
                {
                    "name": "s",
                    "location": (0, 0, 30),
                    "mouse": (0, 0),
                    "pressure": 1.0,
                    "size": 50,
                    "pen_flip": False,
                    "x_tilt": 0,
                    "y_tilt": 0,
                    "mouse_event": (0, 0),
                    "is_start": True,
                    "time": 0.0,
                }
            ]
        )
        ex["sculpt_brush_stroke"] = str(r)
    except Exception as e:  # the point of the probe
        ex["sculpt_brush_stroke_error"] = str(e)[:300]
    return dict(out=None, timers=T, extra=ex)


# ----------------------------------------------------------------------------- registry + runner
# name -> (fn, mesh size, determinism runs?)
OPS = {
    "arap_igl_50k": (op_arap_igl, "50k", True),
    "arap_own_splu_50k": (op_arap_own, "50k", True),
    "arap_own_cholespy_50k": (lambda c: op_arap_own(c, kind="cholespy"), "50k", True),
    "biharm_igl_50k": (op_biharm_igl, "50k", True),
    "biharm_own_50k": (op_biharm_own, "50k", True),
    "arap_igl_400k": (op_arap_igl, "400k", True),
    "arap_own_splu_400k": (op_arap_own, "400k", True),
    "arap_own_cholespy_400k": (lambda c: op_arap_own(c, kind="cholespy"), "400k", True),
    "biharm_igl_400k": (op_biharm_igl, "400k", True),
    "biharm_own_400k": (op_biharm_own, "400k", True),
    "brush_grab_400k": (op_brush_grab, "400k", True),
    "brush_inflate_crease_pinch_clay_400k": (
        op_brush_inflate_crease_pinch_clay,
        "400k",
        True,
    ),
    "smooth_explicit_400k": (op_smooth_kernels, "400k", True),
    "smooth_trimesh_400k": (op_trimesh_smoothing, "400k", True),
    "smooth_implicit_splu_400k": (op_smooth_implicit, "400k", True),
    "smooth_implicit_cholespy_400k": (
        lambda c: op_smooth_implicit(c, kind="cholespy"),
        "400k",
        True,
    ),
    "symmetrize_400k": (op_symmetrize, "400k", True),
    "relief_400k": (op_relief, "400k", True),
    "fit_biharm_50k": (op_fit_biharm_icp, "50k", True),
    "fit_biharm_400k": (op_fit_biharm_icp, "400k", True),
    "fit_silhouette_50k": (op_fit_silhouette, "50k", True),
    "fit_silhouette_400k": (op_fit_silhouette, "400k", True),
    "rbf_tps_400k": (op_rbf_tps, "400k", True),
    "ffd_400k": (op_ffd, "400k", True),
    "geodesic_laplacian_400k": (op_geodesic_laplacian, "400k", True),
    "pymeshlab_isotropic_400k": (op_pymeshlab_isotropic, "400k", True),
    "pymeshlab_qec_400k": (op_pymeshlab_qec, "400k", True),
    "fast_simplification_400k": (op_fast_simplification, "400k", True),
    "pyfqmr_400k": (op_pyfqmr, "400k", True),
    "manifold_simplify_400k": (op_manifold_simplify, "400k", True),
    "open3d_quadric_400k": (op_open3d_quadric, "400k", True),
    "igl_qslim_400k": (op_igl_qslim, "400k", True),
    "gpytoolbox_botsch_400k": (op_gpytoolbox_botsch, "400k", True),
    "sdf_scipy_400k": (op_sdf_scipy, "400k", True),
    "openvdb_probe_400k": (op_openvdb_probe, "400k", True),
    "selfint_400k": (op_selfint, "400k", False),
    "bpy_modifiers_400k": (op_bpy_modifiers, "400k", False),
}


def sha_raw(V, F):
    import numpy as np

    h = hashlib.sha256()
    h.update(np.ascontiguousarray(V, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(F, dtype=np.int64).tobytes())
    return h.hexdigest()


def sha_stl(V, F):
    import trimesh

    m = trimesh.Trimesh(V, F, process=False)
    return hashlib.sha256(m.export(file_type="stl")).hexdigest()


class Ctx:
    def __init__(self, tag, cache):
        import numpy as np

        tag = os.environ.get(
            "MESHFIT_SIZE", tag
        )  # e.g. MESHFIT_SIZE=5k for quick smoke runs
        path = os.path.join(cache, f"mesh_{tag}.npz")
        if os.path.exists(path):
            z = np.load(path)
            self.V, self.F = z["V"], z["F"]
        else:
            self.V, self.F = make_mesh(tag)
            os.makedirs(cache, exist_ok=True)
            np.savez(path, V=self.V, F=self.F)
        self.tag = tag


def run_worker(args):
    pin_threads(args.threads)
    import numpy as np  # noqa: F401  (after pinning)

    fn, tag, _ = OPS[args.op]
    ctx = Ctx(tag, args.cache)
    t0 = time.perf_counter()
    res = fn(ctx)
    wall = time.perf_counter() - t0
    out = res.get("out")
    rec = {
        "op": args.op,
        "threads": args.threads,
        "mesh": f"{tag} ({len(ctx.F)} faces)",
        "secs": round(wall, 3),
        "timers": res["timers"],
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "extra": res["extra"],
    }
    if out is not None:
        V, F = out
        rec["faces_out"] = int(len(F))
        rec["sha_raw"] = sha_raw(V, F)[:16]
        rec["sha_stl"] = sha_stl(V, F)[:16]
        if args.dump:
            import trimesh

            trimesh.Trimesh(V, F, process=False).export(args.dump)
    print("RESULT " + json.dumps(rec))


def run_suite(args):
    ops = list(OPS) if args.ops == "all" else args.ops.split(",")
    rows = []
    for op in ops:
        det = OPS[op][2] and not args.no_det
        cfgs = (
            [("A", args.threads), ("B", args.threads), ("C", 1)]
            if det
            else [("A", args.threads)]
        )
        recs = {}
        for tag, th in cfgs:
            cmd = [
                sys.executable,
                os.path.abspath(__file__),
                "worker",
                "--op",
                op,
                "--threads",
                str(th),
                "--cache",
                args.cache,
            ]
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=args.timeout
            )
            line = next(
                (ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None
            )
            if line is None:
                recs[tag] = {
                    "op": op,
                    "error": (p.stderr.strip().splitlines() or ["no output"])[-1][:300],
                }
                print(f"[{op}/{tag}] FAILED: {recs[tag]['error']}", file=sys.stderr)
                break
            recs[tag] = json.loads(line[7:])
            print(
                f"[{op}/{tag} t={th}] {recs[tag]['secs']}s rss={recs[tag]['peak_rss_mb']}MB",
                file=sys.stderr,
            )
        a = recs.get("A", {})
        row = dict(a)
        if "sha_raw" in a:
            row["same_cfg_repeat_equal"] = (
                (recs["B"]["sha_raw"] == a["sha_raw"]) if "B" in recs else None
            )
            row["threads1_equal_raw"] = (
                (recs["C"]["sha_raw"] == a["sha_raw"]) if "C" in recs else None
            )
            row["threads1_equal_stl"] = (
                (recs["C"]["sha_stl"] == a["sha_stl"]) if "C" in recs else None
            )
            if "C" in recs:
                row["secs_1thread"] = recs["C"]["secs"]
        rows.append(row)
        with open(args.json, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    print(json.dumps(rows, indent=1))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("worker", "suite"):
        p = sub.add_parser(name)
        p.add_argument("--threads", type=int, default=8)
        p.add_argument("--cache", default=os.path.expanduser("~/.cache/mesh-fit-bench"))
        if name == "worker":
            p.add_argument("--op", required=True, choices=sorted(OPS))
            p.add_argument("--dump", help="write the result mesh here (STL/PLY)")
        else:
            p.add_argument("--ops", default="all")
            p.add_argument("--no-det", action="store_true", help="single run per op")
            p.add_argument("--timeout", type=int, default=3000)
            p.add_argument("--json", default="mesh-fit-bench.jsonl")
    args = ap.parse_args()
    if args.cmd == "list":
        for k, (_, tag, det) in OPS.items():
            print(f"{k:42s} mesh={tag:5s} determinism-runs={det}")
    elif args.cmd == "worker":
        run_worker(args)
    else:
        run_suite(args)


if __name__ == "__main__":
    main()
