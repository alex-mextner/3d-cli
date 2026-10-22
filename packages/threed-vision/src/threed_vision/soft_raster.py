"""Differentiable soft-silhouette fit of the loft template (torch; GPU or CPU). Imported lazily by `fit_silhouette`.

Rasteriser: SoftRas-style `sigmoid(signed distance to convex polygon / sigma)` (half-plane form, one matmul) with MAX-union over
polygons, so the silhouette boundary sits exactly at p = 0.5 (a `1 - prod(1 - p)` union inflates wherever polygons overlap). The polygons
are, per station pair, the convex hull of the two projected rings (K-1 polygons of ~14 edges instead of ~100 triangles of the closed mesh:
fewer edge tests and, measured, a closer silhouette because there are only K-2 seams where triangles would leave a dip at every shared
edge). `soft_silhouette` is the same union over arbitrary triangles (reference implementation used by the tests).

The hull set is exact for this template: consecutive rings are homothetic (same superellipse, scaled and shifted along the
lateral axis), so the ruled strip between them lies on the 3-D convex hull of the two rings and the projection of that hull is
the hull of the projected points. Hull topology is picked on detached coordinates; gradients flow through the selected vertices.

Optimiser safeguards (each pinned by a regression test): numerically stable softplus inverse, a DETACHED scale in the
smoothness regulariser, max-union, and a non-finite loss/gradient guard that restores the last finite parameters.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np
import torch

from .fit_init import crop_view
from .loft import (
    ASPECT_MAX,
    ASPECT_MIN,
    RING_M,
    apply_fixed_dims,
    superellipse_ring,
)

STAGES = (
    (64, 40, 1.2, 0.03),
    (96, 30, 0.8, 0.015),
    (96, 20, 0.5, 0.008),
)  # (crop px, steps, sigma px, lr)
SMOOTH_WEIGHT = 2e-3


def softplus_inv(x: torch.Tensor) -> torch.Tensor:
    """Inverse of softplus, stable for large x: t + log(-expm1(-t)). The naive log(expm1(x)) overflows to inf for x > ~88."""
    t = torch.as_tensor(x, dtype=torch.float64)
    return (t + torch.log(-torch.expm1(-t))).to(torch.float32)


def rodrigues(r: torch.Tensor) -> torch.Tensor:
    th = r.norm().clamp_min(1e-9)
    k = r / th
    z = torch.zeros((), device=r.device, dtype=r.dtype)
    Kx = torch.stack(
        [
            torch.stack([z, -k[2], k[1]]),
            torch.stack([k[2], z, -k[0]]),
            torch.stack([-k[1], k[0], z]),
        ]
    )
    return (
        torch.eye(3, device=r.device, dtype=r.dtype)
        + torch.sin(th) * Kx
        + (1 - torch.cos(th)) * (Kx @ Kx)
    )


def project(
    V: torch.Tensor, K: torch.Tensor, R: torch.Tensor, t: torch.Tensor
) -> torch.Tensor:
    Xc = V @ R.T + t
    z = Xc[:, 2:3].clamp_min(1.0)
    return (Xc[:, :2] / z) * torch.stack([K[0, 0], K[1, 1]]) + K[:2, 2]


def pixel_grid(S: int, device: str) -> torch.Tensor:
    ys, xs = torch.meshgrid(
        torch.arange(S, device=device, dtype=torch.float32),
        torch.arange(S, device=device, dtype=torch.float32),
        indexing="ij",
    )
    return torch.stack([xs.reshape(-1), ys.reshape(-1)], -1)


BIG = 1e4  # "far inside" / "far outside" signed distance (px) for padded edges and collapsed polygons


def _soft_union(
    poly: torch.Tensor,
    pix: torch.Tensor,
    sigma: float,
    orient: torch.Tensor,
    alive: torch.Tensor | None = None,
) -> torch.Tensor:
    """poly (P, E, 2) closed CONVEX polygons (edge e: vertex e -> e+1), pix (N, 2) -> (N,) max-union soft occupancy.

    Signed distance of a pixel to a convex polygon = min over its edges of the signed distance to the edge LINE (positive inside):
    exact inside and next to edges, a slight underestimate of the distance beyond a corner. Each edge line is `pix . (A, B) + C`
    with per-edge constants, so the whole (N, P, E) tensor is ONE matmul instead of ~10 elementwise (N, P, E, 2) passes; the
    boundary of the union stays exactly at sd = 0, i.e. p = 0.5. Zero-length (padding) edges are ignored.
    """
    e = poly.roll(-1, dims=1) - poly  # P,E,2
    len2 = (e * e).sum(-1)
    valid = len2 > 1e-10
    inv = (
        torch.where(valid, len2.clamp_min(1e-10).rsqrt(), torch.zeros_like(len2))
        * orient[:, None]
    )
    A = -e[..., 1] * inv
    B = e[..., 0] * inv
    C = torch.where(
        valid,
        (e[..., 1] * poly[..., 0] - e[..., 0] * poly[..., 1]) * inv,
        torch.full_like(len2, BIG),
    )
    P, E = len2.shape
    s = pix @ torch.stack([A.reshape(-1), B.reshape(-1)]) + C.reshape(1, -1)  # N, P*E
    sd = s.reshape(-1, P, E).amin(-1)  # N,P
    if alive is not None:
        sd = torch.where(alive[None] > 0, sd, torch.full_like(sd, -BIG))
    return torch.sigmoid(sd.amax(1) / sigma)


def soft_silhouette(
    uv: torch.Tensor, faces: torch.Tensor, pix: torch.Tensor, sigma: float
) -> torch.Tensor:
    """Max-union soft silhouette of triangles (F,3 indices into uv (V,2)) at pixel centres `pix` (N,2): boundary exactly at 0.5."""
    tri = uv[faces]
    area2 = (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - (
        tri[:, 1, 1] - tri[:, 0, 1]
    ) * (tri[:, 2, 0] - tri[:, 0, 0])
    return _soft_union(
        tri, pix, sigma, torch.sign(area2.detach()) + (area2.detach() == 0).to(uv.dtype)
    )


def hull_indices(uv: torch.Tensor, K: int, M: int) -> torch.Tensor:
    """(K-1, E) vertex indices: convex hull of rings k and k+1 (CPU, on detached coordinates), padded by repeating the last index."""
    import cv2

    pts = uv.detach().cpu().numpy().astype(np.float32)
    np.clip(pts, -1e6, 1e6, out=pts)
    hulls = []
    for k in range(K - 1):
        h = (
            cv2.convexHull(pts[k * M : (k + 2) * M], returnPoints=False).reshape(-1)
            + k * M
        )
        hulls.append(h)
    E = max(len(h) for h in hulls)
    idx = np.stack([np.concatenate([h, np.full(E - len(h), h[-1])]) for h in hulls])
    return torch.from_numpy(idx.astype(np.int64)).to(uv.device)


def soft_silhouette_hulls(
    uv: torch.Tensor, idx: torch.Tensor, pix: torch.Tensor, sigma: float
) -> torch.Tensor:
    """Max-union soft silhouette of convex polygons uv[idx] (P, E)."""
    poly = uv[idx]
    x, y = poly[..., 0], poly[..., 1]
    area2 = (x * y.roll(-1, dims=1) - x.roll(-1, dims=1) * y).sum(1).detach()
    orient = torch.where(area2 >= 0, 1.0, -1.0).to(uv.dtype)
    alive = (area2.abs() > 1e-6).to(
        uv.dtype
    )  # a collapsed strip (end-on view) must not turn into a half-plane
    return _soft_union(poly, pix, sigma, orient, alive)


def smoothness_penalty(w: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
    """Second-difference regulariser of the width / centre profiles in units of the mean half-width.

    The scale is DETACHED: with a learnable scale the optimiser can shrink the penalty by inflating widths (or L) instead of smoothing.
    """
    d2w = w[2:] - 2 * w[1:-1] + w[:-2]
    d2c = c[2:] - 2 * c[1:-1] + c[:-2]
    sc = w.detach().mean() + 1.0
    return SMOOTH_WEIGHT * (((d2w / sc) ** 2).mean() + ((d2c / sc) ** 2).mean())


class LoftFit:
    """Adam fit of one loft template to the views. Parametrisation makes user overrides HARD constraints:

    length_mm -> L is a constant; max_width_mm -> w = (W/2) * s / max(s); thickness_mm -> aspect = T / (2 max(w)).
    A lathe (p = 2) keeps aspect = 1 (axisymmetric by definition).
    """

    def __init__(
        self,
        views: list[dict[str, Any]],
        *,
        template: str,
        p: float,
        K: int = 8,
        device: str = "cpu",
        M: int = RING_M,
        planar: bool = False,
        fixed_dims: dict[str, float] | None = None,
    ) -> None:
        self.dev, self.K, self.M, self.p, self.template = device, K, M, p, template
        self.planar = planar
        self.fixed = dict(fixed_dims or {})
        self.raw = views
        cy, sz = superellipse_ring(p, M)
        self.cy = torch.tensor(cy, dtype=torch.float32, device=device)
        self.sz = torch.tensor(sz, dtype=torch.float32, device=device)
        self.freeze_trans = len(views) == 1 and not planar
        # aspect is fixed (not learned) for a lathe (axisymmetric), under a thickness override, and for a single top-down view
        # (thickness is invisible there: keep the template prior instead of letting perspective noise drift it)
        self.fix_aspect = (
            template == "lathe"
            or "thickness_mm" in self.fixed
            or (len(views) == 1 and planar)
        )
        n = views[0]["R"].T @ np.array([0.0, 0.0, 1.0])
        self.plane_n = torch.tensor(n, dtype=torch.float32, device=device)
        self._cache: dict[int, tuple[list[dict[str, torch.Tensor]], torch.Tensor]] = {}

    # ---- state ----
    def set_init(self, init: dict[str, Any]) -> None:
        d = self.dev
        self.R0 = torch.tensor(init["R0"], device=d, dtype=torch.float32)
        self.t0 = torch.tensor(init["t0"], device=d, dtype=torch.float32)
        self.params = {
            "rot": torch.zeros(3, device=d),
            "trans": torch.zeros(3, device=d),
            "logL": torch.log(torch.tensor(max(float(init["L"]), 1e-3), device=d)),
            "c": torch.tensor(np.asarray(init["c"], np.float32).copy(), device=d),
            "wraw": softplus_inv(
                torch.tensor(np.maximum(np.asarray(init["w"], np.float64), 0.05))
            ).to(d),
            "loga": torch.log(
                torch.tensor(
                    float(np.clip(init.get("aspect", 1.0), ASPECT_MIN, ASPECT_MAX)),
                    device=d,
                )
            ),
        }
        for v in self.params.values():
            v.requires_grad_(True)

    def _views(self, S: int) -> tuple[list[dict[str, torch.Tensor]], torch.Tensor]:
        if S not in self._cache:
            out = []
            for rv in self.raw:
                cv = crop_view(rv["mask"], rv["K"], rv["R"], rv["t"], S)
                out.append(
                    {
                        k: torch.tensor(cv[k], device=self.dev, dtype=torch.float32)
                        for k in ("mask", "K", "R", "t")
                    }
                )
            self._cache[S] = (out, pixel_grid(S, self.dev))
        return self._cache[S]

    # ---- model ----
    def shape(self) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """(L, w half-widths (K,), aspect) with the fixed dimensions applied exactly."""
        P = self.params
        s = torch.nn.functional.softplus(P["wraw"])
        w = (
            s * (float(self.fixed["max_width_mm"]) / 2.0 / s.max())
            if "max_width_mm" in self.fixed
            else s
        )
        L = (
            torch.tensor(float(self.fixed["length_mm"]), device=self.dev)
            if "length_mm" in self.fixed
            else torch.exp(P["logL"])
        )
        if "thickness_mm" in self.fixed:
            a = float(self.fixed["thickness_mm"]) / (2.0 * w.max())
        elif self.template == "lathe":
            a = torch.ones((), device=self.dev)
        else:
            a = torch.exp(P["loga"]).clamp(ASPECT_MIN, ASPECT_MAX)
        return L, w, a

    def pose(self) -> tuple[torch.Tensor, torch.Tensor]:
        rot, tr = self.params["rot"], self.params["trans"]
        if self.freeze_trans:
            tr = tr.detach() * 0.0
        if self.planar:  # object lies on a plane parallel to view 0's image plane: in-plane rotation + in-plane shift only
            n = self.plane_n
            rot = (rot @ n) * n
            tr = tr - (tr @ n) * n
        return rodrigues(rot) @ self.R0, self.t0 + tr

    def mesh_world(self) -> tuple[torch.Tensor, torch.Tensor]:
        L, w, a = self.shape()
        c = self.params["c"]
        K, M = self.K, self.M
        xs = (torch.linspace(0, 1, K, device=self.dev) - 0.5) * L
        X = xs[:, None].expand(K, M)
        Y = c[:, None] + w[:, None] * self.cy[None]
        Z = (a * w)[:, None] * self.sz[None]
        V = torch.stack([X, Y, Z], -1).reshape(K * M, 3)
        z0 = torch.zeros((), device=self.dev)
        ends = torch.stack(
            [torch.stack([xs[0], c[0], z0]), torch.stack([xs[-1], c[-1], z0])]
        )
        R, t = self.pose()
        return torch.cat([V, ends], 0) @ R.T + t, w

    def loss(self, S: int, sigma: float) -> torch.Tensor:
        Vw, w = self.mesh_world()
        views, pix = self._views(S)
        tot = torch.zeros((), device=self.dev)
        for vw in views:
            uv = project(Vw, vw["K"], vw["R"], vw["t"])
            sil = soft_silhouette_hulls(
                uv, hull_indices(uv, self.K, self.M), pix, sigma
            ).reshape(S, S)
            inter = (sil * vw["mask"]).sum()
            uni = sil.sum() + vw["mask"].sum() - inter
            tot = tot + (1 - inter / uni.clamp_min(1.0))
        return tot / len(views) + smoothness_penalty(w, self.params["c"])

    def _snapshot(self) -> dict[str, torch.Tensor]:
        return {k: v.detach().clone() for k, v in self.params.items()}

    def _restore(self, snap: dict[str, torch.Tensor]) -> None:
        with torch.no_grad():
            for k, v in self.params.items():
                v.copy_(snap[k])

    def run(
        self, stages: tuple[tuple[int, int, float, float], ...] | None = None
    ) -> dict[str, Any]:
        """Adam over the stages (crop px, steps, sigma px, lr). A non-finite loss/gradient/parameter stops the run and restores the
        last finite parameters (`finite=False` in the result) instead of poisoning the fit with NaN."""
        t0 = time.perf_counter()
        hist: list[float] = []
        good = self._snapshot()
        finite = True
        for S, steps, sigma, lr in stages or STAGES:
            self._views(S)
            groups: list[dict[str, Any]] = [
                {"params": [self.params["rot"]], "lr": lr * 0.5}
            ]
            if not self.freeze_trans:
                groups.append({"params": [self.params["trans"]], "lr": lr * 20})
            shape_keys = ["c", "wraw"]
            if "length_mm" not in self.fixed:
                shape_keys.append("logL")
            if not self.fix_aspect:
                shape_keys.append("loga")
            groups.append(
                {"params": [self.params[k] for k in shape_keys], "lr": lr * 2.5}
            )
            opt = torch.optim.Adam(groups)
            for _ in range(steps):
                opt.zero_grad()
                loss = self.loss(S, sigma)
                lv = float(loss.detach())
                if not math.isfinite(lv):
                    finite = False
                    break
                loss.backward()
                for g in groups:
                    for prm in g["params"]:
                        if prm.grad is not None and not torch.isfinite(prm.grad).all():
                            prm.grad.nan_to_num_(nan=0.0, posinf=0.0, neginf=0.0)
                opt.step()
                hist.append(lv)
            if not finite or not all(
                bool(torch.isfinite(v).all()) for v in self.params.values()
            ):
                finite = False
                self._restore(good)
                break
            good = self._snapshot()
        if self.dev == "cuda":
            torch.cuda.synchronize()
        return {
            "time_s": time.perf_counter() - t0,
            "final_loss": hist[-1] if hist else float("nan"),
            "iters": len(hist),
            "finite": finite,
        }

    def result(self) -> dict[str, Any]:
        """Fitted shape + pose as float64 numpy, with fixed dimensions re-applied in float64 (exact to rounding)."""
        with torch.no_grad():
            L, w, a = self.shape()
            R, t = self.pose()
            c = self.params["c"].cpu().numpy().astype(np.float64)
        L64, w64, a64 = apply_fixed_dims(
            float(L), w.cpu().numpy().astype(np.float64), float(a), self.fixed
        )
        return {
            "L": L64,
            "w": w64,
            "c": c,
            "aspect": a64,
            "R0": R.cpu().numpy().astype(np.float64),
            "t0": t.cpu().numpy().astype(np.float64),
        }
