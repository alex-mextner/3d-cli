"""Synthetic meshes and spec builders for the meshfit tests (no purchased or private model is ever used)."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]


def cube_sphere(
    n: int, radius: float = 20.0, center: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[Array, IntArray]:
    """Closed, evenly tessellated sphere: a cube grid (n x n per side, 12 n^2 faces) projected onto the sphere."""
    g = np.linspace(-1.0, 1.0, n + 1)
    a, b = np.meshgrid(g, g, indexing="ij")
    one = np.ones_like(a)
    sides = [
        (np.stack([a, b, one], -1), False),
        (np.stack([a, b, -one], -1), True),
        (np.stack([one, a, b], -1), False),
        (np.stack([-one, a, b], -1), True),
        (np.stack([a, one, b], -1), True),
        (np.stack([a, -one, b], -1), False),
    ]
    verts: list[Array] = []
    faces: list[IntArray] = []
    offset = 0
    idx = np.arange((n + 1) * (n + 1)).reshape(n + 1, n + 1)
    for pts, flip in sides:
        verts.append(pts.reshape(-1, 3))
        v00, v10, v01, v11 = (
            idx[:-1, :-1].ravel(),
            idx[1:, :-1].ravel(),
            idx[:-1, 1:].ravel(),
            idx[1:, 1:].ravel(),
        )
        quad = [(v00, v10, v11), (v00, v11, v01)]
        for tri in quad:
            t = np.stack(tri, axis=1) + offset
            faces.append(t[:, ::-1] if flip else t)
        offset += (n + 1) * (n + 1)
    V = np.concatenate(verts)
    F = np.concatenate(faces).astype(np.int64)
    # weld the cube seams: identical cube-grid positions collapse to one vertex (exact float compare is safe: shared edges use the same linspace values)
    _, first, inverse = np.unique(
        np.round(V, 12), axis=0, return_index=True, return_inverse=True
    )
    V = V[first]
    F = inverse.reshape(-1)[F]
    V = V / np.linalg.norm(V, axis=1, keepdims=True) * radius + np.asarray(center)
    return V.astype(np.float32).astype(np.float64), F


def torus(
    major: float = 20.0, minor: float = 6.0, nu: int = 48, nv: int = 24
) -> tuple[Array, IntArray]:
    u = np.linspace(0, 2 * np.pi, nu, endpoint=False)
    v = np.linspace(0, 2 * np.pi, nv, endpoint=False)
    uu, vv = np.meshgrid(u, v, indexing="ij")
    V = np.stack(
        [
            (major + minor * np.cos(vv)) * np.cos(uu),
            (major + minor * np.cos(vv)) * np.sin(uu),
            minor * np.sin(vv),
        ],
        -1,
    ).reshape(-1, 3)
    i, j = np.meshgrid(np.arange(nu), np.arange(nv), indexing="ij")
    a, b = i * nv + j, ((i + 1) % nu) * nv + j
    c, d = ((i + 1) % nu) * nv + (j + 1) % nv, i * nv + (j + 1) % nv
    F = np.concatenate(
        [np.stack([a, b, c], -1).reshape(-1, 3), np.stack([a, c, d], -1).reshape(-1, 3)]
    )
    return V.astype(np.float32).astype(np.float64), F.astype(np.int64)


def box(
    size: tuple[float, float, float] = (30.0, 20.0, 10.0), n: int = 8
) -> tuple[Array, IntArray]:
    """A closed box tessellated n per edge (via a cube-sphere grid pushed back to the box surface)."""
    V, F = cube_sphere(n, 1.0)
    m = np.abs(V).max(axis=1, keepdims=True)
    V = V / m * (np.asarray(size) / 2.0)
    return V.astype(np.float32).astype(np.float64), F


def spec(
    regions: dict[str, Any] | None = None,
    ops: list[dict[str, Any]] | None = None,
    keepout: list[Any] | None = None,
    seed: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """A normalised spec dict (same path the CLI takes)."""
    from geometry.meshfit import spec as spec_mod

    raw: dict[str, Any] = {
        "seed": seed,
        "regions": regions or {},
        "keepout": keepout or [],
        "ops": ops or [{"op": "smooth", "iterations": 1}],
        **extra,
    }
    return spec_mod.normalise_spec(raw, ".")


def write_stl(path: Any, V: Array, F: IntArray) -> None:
    from geometry.meshfit import meshio

    meshio.save_mesh(str(path), V, F)
