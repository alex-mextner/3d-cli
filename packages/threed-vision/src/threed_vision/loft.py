"""Parametric loft template shared by the fitters, the hard rasteriser and the mesh exporter (numpy only).

Template: straight axis (object-local +X), K stations. Station k sits at x_k = (k/(K-1) - 1/2) * L, its cross-section
is the superellipse |y/w_k|^p + |z/(a*w_k)|^p = 1 centred at lateral offset c_k. One template covers

  lathe  p = 2, a = 1     screwdrivers, bottles, drill bodies (radius profile = w_k)
  flat   p = 4, a ~ 0.22  pliers, snips, plates (thin loft of the silhouette)
  box    p = 8, a ~ 0.5   multimeters, power supplies, tape measures

`w_k` is a HALF-width (mm); full width = 2 w_k, full thickness = 2 a w_k. All lengths are mm.

A *fit* is the plain dict every fitter returns (see `make_fit`): params + pose (object -> world) + diagnostics.
It is JSON-serialisable as is (lists, floats) so the service, `fit.json` and `3d view-check` share it.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

TEMPLATE_P: dict[str, float] = {"lathe": 2.0, "flat": 4.0, "box": 8.0}
# Thickness / width prior used where thickness is not observable (single view, planar photo).
TEMPLATE_ASPECT: dict[str, float] = {"lathe": 1.0, "flat": 0.22, "box": 0.5}
# closed-form `kind` (fit_profile) -> template
KIND_TEMPLATE: dict[str, str] = {
    "box": "box",
    "cylinder_capsule": "lathe",
    "lathe_profile": "lathe",
    "ring_lathe": "lathe",
    "loft_flat": "flat",
}
# Ring resolution: a multiple of 4 so the ring has vertices exactly on the y and z extrema (dims are exact in the mesh).
RING_M = 12
EXPORT_RING = 24  # mesh export / canonical-view resolution (also a multiple of 4)
ASPECT_MIN, ASPECT_MAX = 0.05, 3.0
W_MIN_MM = 0.05


def superellipse_ring(p: float, M: int = RING_M) -> tuple[np.ndarray, np.ndarray]:
    """Unit superellipse sample (cy, sz) of M points; exact zeros on the axes (no clamp artefacts)."""
    phi = np.arange(M, dtype=np.float64) * (2 * math.pi / M)
    c, s = np.cos(phi), np.sin(phi)
    c[np.abs(c) < 1e-12] = 0.0
    s[np.abs(s) < 1e-12] = 0.0
    e = 2.0 / p
    return np.sign(c) * np.abs(c) ** e, np.sign(s) * np.abs(s) ** e


def build_faces(K: int, M: int = RING_M) -> np.ndarray:
    """Triangles of a closed K-station loft: K*M ring vertices then two cap-centre vertices (K*M, K*M+1)."""
    f: list[list[int]] = []
    for k in range(K - 1):
        for j in range(M):
            a, b = k * M + j, k * M + (j + 1) % M
            c, d = (k + 1) * M + j, (k + 1) * M + (j + 1) % M
            f += [[a, b, c], [b, d, c]]
    n = K * M
    for j in range(M):
        f.append([n, (j + 1) % M, j])
        f.append([n + 1, (K - 1) * M + j, (K - 1) * M + (j + 1) % M])
    return np.array(f, np.int64)


def loft_vertices(L: float, c: np.ndarray, w: np.ndarray, aspect: float, p: float, M: int = RING_M) -> np.ndarray:
    """(K*M+2, 3) object-local vertices, X along the axis centred at 0."""
    c = np.asarray(c, np.float64)
    w = np.asarray(w, np.float64)
    K = c.shape[0]
    cy, sz = superellipse_ring(p, M)
    xs = (np.linspace(0.0, 1.0, K) - 0.5) * L
    X = np.repeat(xs[:, None], M, axis=1)
    Y = c[:, None] + w[:, None] * cy[None]
    Z = (aspect * w)[:, None] * sz[None]
    V = np.stack([X, Y, Z], -1).reshape(K * M, 3)
    ends = np.array([[xs[0], c[0], 0.0], [xs[-1], c[-1], 0.0]])
    return np.concatenate([V, ends], 0)


def rodrigues(r: np.ndarray) -> np.ndarray:
    r = np.asarray(r, np.float64)
    th = float(np.linalg.norm(r))
    if th < 1e-12:
        return np.eye(3)
    k = r / th
    Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * Kx + (1 - math.cos(th)) * (Kx @ Kx)


def basis_from_axis(axis: np.ndarray, roll: float) -> np.ndarray:
    """Right-handed object->world rotation whose first column is `axis`; `roll` spins Y/Z about it."""
    x = np.asarray(axis, np.float64)
    x = x / np.linalg.norm(x)
    tmp = np.array([0.0, 0.0, 1.0]) if abs(x[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    y = np.cross(tmp, x)
    y /= np.linalg.norm(y)
    z = np.cross(x, y)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.stack([x, cr * y + sr * z, -sr * y + cr * z], 1)


def apply_fixed_dims(
    L: float, w: np.ndarray, aspect: float, fixed_dims: dict[str, float] | None
) -> tuple[float, np.ndarray, float]:
    """Make (L, w, aspect) satisfy the user's overrides EXACTLY (hard constraints, not penalties).

    length_mm -> L; max_width_mm -> w scaled so 2*max(w) equals it; thickness_mm -> aspect = T / (2*max(w)).
    """
    w = np.maximum(np.asarray(w, np.float64), W_MIN_MM)
    if not fixed_dims:
        return L, w, aspect
    if "length_mm" in fixed_dims:
        L = float(fixed_dims["length_mm"])
    if "max_width_mm" in fixed_dims:
        w = w * (float(fixed_dims["max_width_mm"]) / 2.0 / float(w.max()))
    if "thickness_mm" in fixed_dims:
        aspect = float(fixed_dims["thickness_mm"]) / (2.0 * float(w.max()))
    return L, w, aspect


def dims_mm(L: float, w: np.ndarray, aspect: float) -> dict[str, float]:
    wmax = float(np.max(w))
    return {"length_mm": float(L), "max_width_mm": 2.0 * wmax, "thickness_mm": 2.0 * float(aspect) * wmax}


def make_fit(
    *,
    template: str,
    K: int,
    L: float,
    c: np.ndarray,
    w: np.ndarray,
    aspect: float,
    R0: np.ndarray,
    t_mm: np.ndarray,
    method: str,
    kind: str | None = None,
    fixed_dims: dict[str, float] | None = None,
    ring: int = RING_M,
) -> dict[str, Any]:
    """The fit schema (JSON-ready). `pose.R0` maps object-local -> world, `pose.t_mm` is the object centre in world mm."""
    R0 = np.asarray(R0, np.float64)
    w = np.asarray(w, np.float64)
    return {
        "kind": kind,
        "template": {"name": template, "p": TEMPLATE_P[template], "stations": int(K), "ring": int(ring)},
        "params": {"L_mm": float(L), "c_mm": [float(x) for x in c], "w_mm": [float(x) for x in w], "aspect": float(aspect)},
        "dims_mm": dims_mm(L, w, aspect),
        "pose": {"R0": R0.tolist(), "t_mm": [float(x) for x in t_mm], "axis": R0[:, 0].tolist()},
        "fixed_dims": dict(fixed_dims) if fixed_dims else None,
        "method": method,
    }


def local_mesh(fit: dict[str, Any], ring: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(V, F) of the fit in the object-local frame (mm)."""
    t, p = fit["template"], fit["params"]
    M = int(ring or t.get("ring", RING_M))
    K = len(p["w_mm"])
    V = loft_vertices(p["L_mm"], np.asarray(p["c_mm"]), np.asarray(p["w_mm"]), p["aspect"], t["p"], M)
    return V, build_faces(K, M)


def object_view_angles(R0: np.ndarray, t_mm: np.ndarray, cam_center: np.ndarray) -> dict[str, Any]:
    """Where a camera sits relative to the object, in the OBJECT frame: azimuth in the X(length)-Y(width) plane from +X toward +Y,
    elevation from that plane toward +Z (thickness axis); elevation 90 = looking straight down the thickness axis."""
    d = np.asarray(cam_center, np.float64) - np.asarray(t_mm, np.float64)
    n = float(np.linalg.norm(d))
    if n < 1e-9:
        return {"azimuth_deg": 0.0, "elevation_deg": 0.0, "distance_mm": 0.0}
    do = np.asarray(R0, np.float64).T @ (d / n)
    return {
        "azimuth_deg": float(math.degrees(math.atan2(do[1], do[0]))),
        "elevation_deg": float(math.degrees(math.asin(max(-1.0, min(1.0, do[2]))))),
        "distance_mm": n,
    }
