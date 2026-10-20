"""regions.py — named vertex masks with smooth falloff: the "mask / brush footprint" of every op.

WHAT: turns a region definition from the spec into a per-vertex weight in [0, 1]. Shapes (box, sphere,
  ellipsoid, capsule, half-space, z-range, point cloud), field ramps (normal components, coordinates,
  mean curvature), a normal cone, a narrow-gap detector (ray cast) and combinators (union, intersect,
  product, difference) — composable by name. Weights fall off with a Hermite ramp, never a hard step.

WHY: sculpting is "apply this brush where the mask says"; a deterministic mask language is what lets
  a landmark file (boxes around eyes, ears, head …) drive the same ops a human paints by hand.
  Keep-out zones are just regions whose weight multiplies every op's displacement by (1 - w).

Region definition (dict; exactly one shape/field/combinator key, plus optional modifiers):
  box: [x0, x1, y0, y1, z0, z1]            sphere: {center: [x,y,z], radius: r}
  ellipsoid: {center, radii: [a,b,c]}       capsule: {a: [x,y,z], b: [x,y,z], radius: r}
  halfspace: {point: [x,y,z], normal: [x,y,z]}   (weight 1 behind the plane)
  zrange: [z0, z1]                          points: {file: pts.json|pts.npy | list, radius: r}
  ramp: {quantity: nx|ny|nz|x|y|z|curvature, from: a, to: b, [smooth_iters: 6]}
  normal_cone: {axis: [x,y,z], angle_deg: a, soft_deg: s}
  gap: {min: a, max: b, rays: 7, cone_deg: 50, [within: region], [backend: auto|rtree]}
       (weight 0 where another surface is near; auto uses embree when installed: ~100x faster, float32)
  union | intersect | product: [region, ...]   difference: [region, minuend-first ...]   all: true
Modifiers: falloff (mm, shapes only), margin (mm, grows a shape), invert, scale, blur (mask smoothing iterations).
A reference is a region name, an inline definition, or a list of references (their union).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from errors import UsageError

from . import meshmath
from .noise import smoothstep

Array = NDArray[np.float64]

SHAPES = ("box", "sphere", "ellipsoid", "capsule", "halfspace", "zrange", "points")
FIELDS = ("ramp", "normal_cone", "gap", "all")
COMBINATORS = ("union", "intersect", "product", "difference")
MODIFIERS = ("falloff", "margin", "invert", "scale", "blur")
QUANTITIES = ("nx", "ny", "nz", "x", "y", "z", "curvature")
LOCK_WEIGHT = 1.0  # weight at/above which a vertex is "inside" a region (keep-out lock, handle set)


def _fail(where: str, message: str) -> UsageError:
    return UsageError(
        f"spec: {where}: {message}",
        command="mesh deform",
        remediation=["See `3d mesh deform --help` for the region grammar."],
    )


def _vec(value: Any, where: str, n: int = 3) -> Array:
    try:
        arr = np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError) as err:
        raise _fail(where, f"expected {n} numbers, got {value!r}") from err
    if arr.shape != (n,) or not np.isfinite(arr).all():
        raise _fail(where, f"expected {n} finite numbers, got {value!r}")
    return arr


def validate_region(name: str, definition: Any, known: set[str]) -> None:
    """Structural check of one region definition (names in combinators must exist)."""
    where = f"regions.{name}"
    if not isinstance(definition, Mapping):
        raise _fail(where, f"must be a mapping, got {type(definition).__name__}")
    keys = [k for k in definition if k in SHAPES + FIELDS + COMBINATORS]
    extra = [
        k for k in definition if k not in SHAPES + FIELDS + COMBINATORS + MODIFIERS
    ]
    if len(keys) != 1:
        raise _fail(
            where,
            f"needs exactly one of {', '.join(SHAPES + FIELDS + COMBINATORS)}; found {keys or 'none'}",
        )
    if extra:
        raise _fail(
            where, f"unknown keys {extra}; accepted modifiers: {', '.join(MODIFIERS)}"
        )
    kind = keys[0]
    body = definition[kind]
    if kind in COMBINATORS:
        if not isinstance(body, list) or not body:
            raise _fail(
                f"{where}.{kind}", "must be a non-empty list of region references"
            )
        for ref in body:
            validate_ref(ref, known, f"{where}.{kind}")
    elif kind == "box":
        _vec(body, f"{where}.box", 6)
    elif kind == "zrange":
        _vec(body, f"{where}.zrange", 2)
    elif kind == "sphere":
        _vec(
            body.get("center") if isinstance(body, Mapping) else None,
            f"{where}.sphere.center",
        )
        _positive(body, "radius", f"{where}.sphere")
    elif kind == "ellipsoid":
        _vec(
            body.get("center") if isinstance(body, Mapping) else None,
            f"{where}.ellipsoid.center",
        )
        _vec(
            body.get("radii") if isinstance(body, Mapping) else None,
            f"{where}.ellipsoid.radii",
        )
    elif kind == "capsule":
        for k in ("a", "b"):
            _vec(
                body.get(k) if isinstance(body, Mapping) else None,
                f"{where}.capsule.{k}",
            )
        _positive(body, "radius", f"{where}.capsule")
    elif kind == "halfspace":
        _vec(
            body.get("point") if isinstance(body, Mapping) else None,
            f"{where}.halfspace.point",
        )
        _vec(
            body.get("normal") if isinstance(body, Mapping) else None,
            f"{where}.halfspace.normal",
        )
    elif kind == "points":
        if not isinstance(body, Mapping) or not ("file" in body or "list" in body):
            raise _fail(
                f"{where}.points",
                "needs {file: ..., radius: r} or {list: [[x,y,z], ...], radius: r}",
            )
        _positive(body, "radius", f"{where}.points")
    elif kind == "ramp":
        if (
            not isinstance(body, Mapping)
            or body.get("quantity") not in QUANTITIES
            or "from" not in body
            or "to" not in body
        ):
            raise _fail(
                f"{where}.ramp", f"needs quantity ({', '.join(QUANTITIES)}), from, to"
            )
    elif kind == "normal_cone":
        _vec(
            body.get("axis") if isinstance(body, Mapping) else None,
            f"{where}.normal_cone.axis",
        )
        if "angle_deg" not in body:
            raise _fail(f"{where}.normal_cone", "needs angle_deg")
    elif kind == "gap":
        if not isinstance(body, Mapping) or "min" not in body or "max" not in body:
            raise _fail(f"{where}.gap", "needs min and max (mm)")
        if "within" in body:
            validate_ref(body["within"], known, f"{where}.gap.within")


def _positive(body: Any, key: str, where: str) -> None:
    if (
        not isinstance(body, Mapping)
        or not isinstance(body.get(key), int | float)
        or body[key] <= 0
    ):
        raise _fail(where, f"{key} must be a positive number")


def validate_ref(ref: Any, known: set[str], where: str) -> None:
    if ref is None:
        return
    if isinstance(ref, str):
        if ref not in known:
            raise _fail(
                where,
                f"unknown region {ref!r}; defined: {', '.join(sorted(known)) or 'none'}",
            )
    elif isinstance(ref, list):
        for item in ref:
            validate_ref(item, known, where)
    elif isinstance(ref, Mapping):
        validate_region("<inline>", ref, known)
    else:
        raise _fail(
            where,
            f"region reference must be a name, a definition or a list, got {type(ref).__name__}",
        )


class RegionContext:
    """Evaluates region weights on one fixed vertex state; memoises normals, curvature and named regions."""

    def __init__(
        self,
        V: Array,
        F: Any,
        definitions: Mapping[str, Mapping[str, Any]],
        base_dir: str = ".",
    ) -> None:
        self.V = V
        self.F = F
        self.defs = definitions
        self.base_dir = base_dir
        self._memo: dict[str, Array] = {}
        self._normals: dict[int, Array] = {}
        self._curv: dict[int, Array] = {}
        self._stack: list[str] = []
        self._adj: Any = None

    # ---- shared fields
    def normals(self, smooth_iters: int = 0) -> Array:
        if smooth_iters not in self._normals:
            self._normals[smooth_iters] = meshmath.smooth_normals(
                self.V, self.F, smooth_iters
            )
        return self._normals[smooth_iters]

    def adjacency(self) -> Any:
        if self._adj is None:
            self._adj = meshmath.adjacency(self.F, len(self.V))
        return self._adj

    def curvature(self, smooth_iters: int) -> Array:
        """Mean curvature (1/mm, positive on convex surface) from the cotangent Laplacian."""
        if smooth_iters not in self._curv:
            L, mass = meshmath.cotan_laplacian(self.V, self.F)
            n = meshmath.vertex_normals(self.V, self.F)
            h = np.einsum("ij,ij->i", np.asarray(L @ self.V), n) / (2.0 * mass)
            if smooth_iters > 0:
                deg = np.maximum(np.asarray(self.adjacency().sum(axis=1)).ravel(), 1.0)
                for _ in range(smooth_iters):
                    h = 0.5 * h + 0.5 * (self.adjacency() @ h) / deg
            self._curv[smooth_iters] = h
        return self._curv[smooth_iters]

    # ---- evaluation
    def weight(self, ref: Any) -> Array:
        """Weight for a reference: None -> all ones; name; inline definition; list -> union."""
        if ref is None:
            return np.ones(len(self.V))
        if isinstance(ref, str):
            if ref in self._memo:
                return self._memo[ref]
            if ref in self._stack:
                raise _fail(
                    f"regions.{ref}",
                    "circular reference: " + " -> ".join([*self._stack, ref]),
                )
            if ref not in self.defs:
                raise _fail(
                    "region",
                    f"unknown region {ref!r}; defined: {', '.join(sorted(self.defs)) or 'none'}",
                )
            self._stack.append(ref)
            try:
                w = self._eval(self.defs[ref])
            finally:
                self._stack.pop()
            self._memo[ref] = w
            return w
        if isinstance(ref, list):
            out = np.zeros(len(self.V))
            for item in ref:
                out = np.maximum(out, self.weight(item))
            return out
        if isinstance(ref, Mapping):
            return self._eval(ref)
        raise _fail("region", f"cannot use {ref!r} as a region")

    def _eval(self, d: Mapping[str, Any]) -> Array:
        kind = next(k for k in d if k in SHAPES + FIELDS + COMBINATORS)
        body = d[kind]
        if kind in SHAPES:
            dist = self._distance(kind, body) - float(d.get("margin", 0.0))
            falloff = float(d.get("falloff", 0.0))
            w = (
                1.0 - smoothstep(np.maximum(dist, 0.0), 0.0, falloff)
                if falloff > 0
                else (dist <= 0.0).astype(np.float64)
            )
        elif kind == "ramp":
            w = self._ramp(body)
        elif kind == "normal_cone":
            w = self._cone(body)
        elif kind == "gap":
            w = self._gap(body)
        elif kind == "all":
            w = np.ones(len(self.V))
        else:
            w = self._combine(kind, body)
        if d.get("invert"):
            w = 1.0 - w
        if "scale" in d:
            w = w * float(d["scale"])
        blur = int(d.get("blur", 0))
        if blur > 0:
            deg = np.maximum(np.asarray(self.adjacency().sum(axis=1)).ravel(), 1.0)
            for _ in range(blur):
                w = 0.5 * w + 0.5 * (self.adjacency() @ w) / deg
        return np.clip(w, 0.0, 1.0)

    def _combine(self, kind: str, refs: list[Any]) -> Array:
        ws = [self.weight(r) for r in refs]
        if kind == "union":
            return np.max(ws, axis=0)
        if kind == "intersect":
            return np.min(ws, axis=0)
        if kind == "product":
            out = ws[0].copy()
            for w in ws[1:]:
                out = out * w
            return out
        out = ws[0].copy()  # difference: first minus the union of the rest
        for w in ws[1:]:
            out = out * (1.0 - w)
        return out

    def _distance(self, kind: str, b: Any) -> Array:
        P = self.V
        if kind == "box":
            lo = np.array([b[0], b[2], b[4]], dtype=np.float64)
            hi = np.array([b[1], b[3], b[5]], dtype=np.float64)
            q = np.maximum(np.maximum(lo - P, P - hi), 0.0)
            return np.linalg.norm(q, axis=1)
        if kind == "sphere":
            return np.linalg.norm(
                P - _vec(b["center"], "sphere.center"), axis=1
            ) - float(b["radius"])
        if kind == "ellipsoid":
            r = _vec(b["radii"], "ellipsoid.radii")
            q = np.linalg.norm((P - _vec(b["center"], "ellipsoid.center")) / r, axis=1)
            return (q - 1.0) * float(
                r.min()
            )  # first-order distance (exact for spheres)
        if kind == "capsule":
            a, c = _vec(b["a"], "capsule.a"), _vec(b["b"], "capsule.b")
            ab = c - a
            t = np.clip(((P - a) @ ab) / max(float(ab @ ab), 1e-30), 0.0, 1.0)
            return np.linalg.norm(P - (a + t[:, None] * ab), axis=1) - float(
                b["radius"]
            )
        if kind == "halfspace":
            n = _vec(b["normal"], "halfspace.normal")
            return (P - _vec(b["point"], "halfspace.point")) @ (n / np.linalg.norm(n))
        if kind == "zrange":
            return np.maximum(
                np.maximum(float(b[0]) - P[:, 2], P[:, 2] - float(b[1])), 0.0
            )
        pts = self._points(b)
        if len(pts) == 0:
            return np.full(len(P), 1e9)
        from scipy.spatial import cKDTree

        return np.asarray(
            cKDTree(pts).query(P, workers=1)[0], dtype=np.float64
        ) - float(b["radius"])

    def _points(self, body: Mapping[str, Any]) -> Array:
        if "list" in body:
            return np.asarray(body["list"], dtype=np.float64).reshape(-1, 3)
        path = (
            body["file"]
            if os.path.isabs(body["file"])
            else os.path.join(self.base_dir, body["file"])
        )
        if path.endswith(".npy"):
            return np.asarray(np.load(path), dtype=np.float64).reshape(-1, 3)
        with open(path, encoding="utf-8") as fh:
            return np.asarray(json.load(fh), dtype=np.float64).reshape(-1, 3)

    def _ramp(self, b: Mapping[str, Any]) -> Array:
        q = b["quantity"]
        if q in ("nx", "ny", "nz"):
            x = self.normals(int(b.get("smooth_iters", 6)))[:, "xyz".index(q[1])]
        elif q in ("x", "y", "z"):
            x = self.V[:, "xyz".index(q)]
        else:
            x = self.curvature(int(b.get("smooth_iters", 2)))
        return smoothstep(x, float(b["from"]), float(b["to"]))

    def _cone(self, b: Mapping[str, Any]) -> Array:
        axis = _vec(b["axis"], "normal_cone.axis")
        axis = axis / np.linalg.norm(axis)
        n = self.normals(int(b.get("smooth_iters", 0)))
        ang = np.degrees(np.arccos(np.clip(n @ axis, -1.0, 1.0)))
        cone, soft = float(b["angle_deg"]), float(b.get("soft_deg", 0.0))
        return (
            1.0 - smoothstep(ang, cone, cone + soft)
            if soft > 0
            else (ang <= cone).astype(np.float64)
        )

    def _gap(self, b: Mapping[str, Any]) -> Array:
        """Weight 0 where another surface lies within `min` mm in front of the vertex, 1 beyond `max` (ray cone)."""
        import trimesh

        cand = (
            np.nonzero(self.weight(b["within"]) > 0.0)[0]
            if "within" in b
            else np.arange(len(self.V))
        )
        dist = np.full(len(self.V), float(b["max"]) * 2.0)
        if len(cand):
            mesh = trimesh.Trimesh(self.V, self.F, process=False)
            backend = str(b.get("backend", "auto"))
            if (
                backend == "rtree"
            ):  # float64 triangle tests: identical on every machine (embree is float32 and ~100x faster)
                from trimesh.ray.ray_triangle import RayMeshIntersector

                cast(Any, mesh).ray = RayMeshIntersector(mesh)
            elif backend != "auto":
                raise _fail("gap", f"backend must be auto or rtree, got {backend!r}")
            n = self.normals(int(b.get("smooth_iters", 6)))[cand]
            t1 = np.cross(n, np.array([0.0, 0.0, 1.0]))
            bad = np.linalg.norm(t1, axis=1) < 1e-3
            t1[bad] = np.cross(n[bad], np.array([1.0, 0.0, 0.0]))
            t1 /= np.linalg.norm(t1, axis=1, keepdims=True)
            t2 = np.cross(n, t1)
            origin = self.V[cand] + n * 0.02
            rays = int(b.get("rays", 7))
            cone = np.radians(float(b.get("cone_deg", 50.0)))
            best = np.full(len(cand), float(b["max"]) * 2.0)
            for k in range(rays):
                if k == 0:
                    direction = n
                else:
                    phi = 2.0 * np.pi * (k - 1) / max(rays - 1, 1)
                    direction = n * np.cos(cone) + (
                        t1 * np.cos(phi) + t2 * np.sin(phi)
                    ) * np.sin(cone)
                loc, ridx, _ = mesh.ray.intersects_location(
                    origin, direction, multiple_hits=False
                )
                hit = np.full(len(cand), float(b["max"]) * 2.0)
                if len(ridx):
                    hit[ridx] = np.linalg.norm(loc - origin[ridx], axis=1)
                best = np.minimum(best, hit)
            dist[cand] = best
        return smoothstep(dist, float(b["min"]), float(b["max"]))
