"""brushes.py — sculpt-brush kernels as vectorised numpy: smooth, inflate, draw, pinch, crease, scale-local, symmetrize.

WHAT: each brush takes the current vertices, a per-vertex weight (the "brush footprint" from a region)
  and its parameters, and returns the new vertex array. No GUI, no stroke order, no random state:
  the footprint IS the stroke, so the result is a pure function of (mesh, region, params).

WHY: these are the Blender sculpt brushes that matter for fitting a model to a reference — Smooth,
  Inflate, Draw, Pinch, Crease, scale-in-region and mirror — rebuilt as one-pass kernels so they run
  in well under a second on a 400k-face mesh and are byte-reproducible.

Definitions (w = region weight in [0,1], n = vertex normal, lambda/mu Taubin factors):
  smooth     V += lam * w * (mean_ring(V) - V), Taubin (lam, then mu < 0) by default so volume does not shrink
  inflate    V += amount * w * n                       (per-vertex normal, `normal_smoothing` ring passes)
  draw       V += amount * w * n_avg                   (one direction: the weight-averaged normal)
  pinch      V += strength * w * tangent(c - V)        (pull toward a point / line, normal part removed)
  crease     pinch + V -= depth * w^sharpness * n      (a groove: pinch the sides in, push the bottom down)
  scale      V += w * (factor - 1) * (V - pivot)       (per-axis factor)
  symmetrize V += w * (target - V), target from the mirrored nearest vertex (average | mirror from one side)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import UsageError

from .regions import RegionContext

Array = NDArray[np.float64]


def _num(params: Mapping[str, Any], key: str, default: float) -> float:
    return float(params.get(key, default))


def _centroid(V: Array, w: Array) -> Array:
    total = float(w.sum())
    if total <= 0:
        return np.asarray(V.mean(axis=0))
    return np.asarray((V * w[:, None]).sum(axis=0) / total)


def smooth(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    iterations = int(params.get("iterations", 10))
    lam = _num(params, "lambda", 0.5)
    method = str(params.get("method", "taubin"))
    if method not in ("taubin", "laplacian"):
        raise UsageError(
            f"smooth: method must be taubin or laplacian, got {method!r}",
            command="mesh deform",
        )
    mu = 1.0 / (0.1 - 1.0 / lam) if method == "taubin" else lam
    A = ctx.adjacency()
    deg = np.maximum(np.asarray(A.sum(axis=1)).ravel(), 1.0)
    V = ctx.V.copy()
    for _ in range(iterations):
        for factor in (lam, mu) if method == "taubin" else (lam,):
            V += (factor * w)[:, None] * ((A @ V) / deg[:, None] - V)
    return V


def inflate(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    n = ctx.normals(int(params.get("normal_smoothing", 2)))
    return ctx.V + (_num(params, "amount", 0.0) * w)[:, None] * n


def draw(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    n = ctx.normals(int(params.get("normal_smoothing", 2)))
    avg = (n * w[:, None]).sum(axis=0)
    norm = float(np.linalg.norm(avg))
    direction = avg / norm if norm > 1e-12 else np.zeros(3)
    return ctx.V + (_num(params, "amount", 0.0) * w)[:, None] * direction


def _pull(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    """Unit-strength pull vector toward the pinch target, with the normal component removed."""
    V = ctx.V
    toward = params.get("toward")
    if toward is None:
        target = np.broadcast_to(_centroid(V, w), V.shape)
    elif "point" in toward:
        target = np.broadcast_to(np.asarray(toward["point"], dtype=np.float64), V.shape)
    elif "line" in toward:
        a, b = (
            np.asarray(toward["line"][0], dtype=np.float64),
            np.asarray(toward["line"][1], dtype=np.float64),
        )
        ab = b - a
        t = np.clip(((V - a) @ ab) / max(float(ab @ ab), 1e-30), 0.0, 1.0)
        target = a + t[:, None] * ab
    else:
        raise UsageError(
            "pinch/crease: toward needs {point: [x,y,z]} or {line: [[x,y,z],[x,y,z]]}",
            command="mesh deform",
        )
    pull = target - V
    if params.get("keep_normal", True):
        n = ctx.normals(int(params.get("normal_smoothing", 2)))
        pull = pull - np.einsum("ij,ij->i", pull, n)[:, None] * n
    return np.asarray(pull)


def pinch(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    return ctx.V + (_num(params, "strength", 0.5) * w)[:, None] * _pull(ctx, params, w)


def crease(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    n = ctx.normals(int(params.get("normal_smoothing", 2)))
    V = ctx.V + (_num(params, "strength", 0.5) * w)[:, None] * _pull(ctx, params, w)
    return (
        V
        - (_num(params, "depth", 0.3) * w ** _num(params, "sharpness", 2.0))[:, None]
        * n
    )


def scale_local(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    factor = np.broadcast_to(
        np.asarray(params.get("factor", 1.0), dtype=np.float64), (3,)
    )
    pivot = (
        np.asarray(params["pivot"], dtype=np.float64)
        if "pivot" in params
        else _centroid(ctx.V, w)
    )
    return ctx.V + w[:, None] * (factor - 1.0) * (ctx.V - pivot)


def symmetrize(ctx: RegionContext, params: Mapping[str, Any], w: Array) -> Array:
    from scipy.spatial import cKDTree

    axis = "xyz".index(str(params.get("axis", "x")))
    at = _num(params, "at", 0.0)
    mode = str(params.get("mode", "average"))
    if mode not in ("average", "mirror_positive", "mirror_negative"):
        raise UsageError(
            f"symmetrize: mode must be average, mirror_positive or mirror_negative, got {mode!r}",
            command="mesh deform",
        )
    V = ctx.V
    flip = np.ones(3)
    flip[axis] = -1.0
    offset = np.zeros(3)
    offset[axis] = 2.0 * at

    def mirror(P: Array) -> Array:
        return np.asarray(P * flip + offset)

    side = V[:, axis] - at
    if mode == "average":
        nearest = cKDTree(V).query(mirror(V), workers=1)[1]
        target = 0.5 * (V + mirror(V[nearest]))
        target[:, axis] = np.where(np.abs(side) < 1e-9, V[:, axis], target[:, axis])
        return V + w[:, None] * (target - V)
    keep = side >= 0 if mode == "mirror_positive" else side <= 0
    src = np.nonzero(keep)[0]
    nearest = src[cKDTree(V[src]).query(mirror(V), workers=1)[1]]
    out = V.copy()
    take = ~keep
    out[take] = V[take] + w[take, None] * (mirror(V[nearest[take]]) - V[take])
    return out
