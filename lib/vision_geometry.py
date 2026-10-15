"""vision_geometry.py — per-printer camera calibration for `3d printer vision` (issue #150).

A fixed chamber camera sees the bed and the print from one pose. Once that pose and the lens
are known (a `CameraModel`), the detector can project bed millimetres into the image: the bed
plate, the expected part silhouette from the print's object footprints at the current Z, and
the newest band of each growing part. Those masks feed the ROI (ignore detections outside the
bed + part volume), foreign-object, detached/shifted-part and no-extrusion checks.

Frames: bed coordinates are the printer's X/Y in mm plus `h`, the height above the bed
surface. On a bed-moves-Z printer (Snapmaker U1) the nozzle stays put and the bed drops, so a
bed point (X, Y, h) photographed at gcode Z = z sits at world (X, Y, h - z); on a
gantry-moves-Z printer it stays at (X, Y, h). The camera maps world -> pixels with a pinhole
model plus OpenCV-compatible radial/tangential distortion, or an equidistant fisheye model
(cv2.fisheye layout) — whichever fits a lens better.

Everything here is numpy + scipy (core deps); OpenCV is only imported lazily by `overlay()`
and the calibration CLI, so the masks also work where `opencv` is not installed.

Calibration (`python lib/vision_geometry.py fit points.yaml out.yaml --overlay frame.jpg Z
out.jpg`) fits a model to hand-picked pixel <-> bed-point correspondences taken from frames at
several Z values; the U1 default lives in lib/data/vision/u1-camera.yaml and its points next
to it. Docs: docs/research/printer-vision-geometry.md.
"""

from __future__ import annotations

import math
import pathlib
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

Polygon = list[tuple[float, float]]

_BUILTIN_DIR = pathlib.Path(__file__).resolve().parent / "data" / "vision"
# normalised printer model -> bundled calibration, used when a device has no own file
_BUNDLED = {"u1": "u1-camera.yaml"}
_MODELS = ("pinhole", "fisheye")


@dataclass(frozen=True)
class Correspondence:
    """Pixel (u, v) <-> bed point (x, y) at height h above the bed, in a frame taken at gcode Z = z."""

    u: float
    v: float
    x: float
    y: float
    h: float
    z: float
    label: str = field(default="", compare=False)


@dataclass(frozen=True)
class CameraModel:
    """A fixed camera looking at the bed.

    `dist` is OpenCV's (k1, k2, p1, p2, k3) for `model="pinhole"` or cv2.fisheye's
    (k1, k2, k3, k4) for `model="fisheye"`. `rvec`/`tvec` map world mm (bed frame at Z = 0)
    into camera coordinates. Pixel outputs are in this model's `width` x `height`; masks
    rescale to the requested shape (same aspect and crop assumed).
    """

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    dist: tuple[float, ...]
    rvec: tuple[float, float, float]
    tvec: tuple[float, float, float]
    bed: tuple[float, float, float, float] = (
        0.0,
        0.0,
        270.0,
        270.0,
    )  # x0, y0, x1, y1 mm
    bed_moves_z: bool = True
    model: str = "pinhole"

    def world(self, pts_bed_mm: np.ndarray, z: float) -> np.ndarray:
        """Nx3 bed points (X, Y, height above bed) at gcode Z -> Nx3 world points."""
        import numpy as np

        pts = np.asarray(pts_bed_mm, dtype=float).reshape(-1, 3).copy()
        if self.bed_moves_z:
            pts[:, 2] -= z
        return pts

    def project(self, pts_bed_mm: np.ndarray, z: float) -> np.ndarray:
        """Nx3 bed points (X, Y, height above bed) at gcode Z -> Nx2 pixels (NaN behind the camera)."""
        return _project(self._params(), self.model, self.world(pts_bed_mm, z))

    def camera_center(self) -> np.ndarray:
        """Camera position in world mm (bed frame at Z = 0)."""
        import numpy as np

        r = _rodrigues(np.asarray(self.rvec, dtype=float))
        return -r.T @ np.asarray(self.tvec, dtype=float)

    def _params(self) -> np.ndarray:
        import numpy as np

        return np.array(
            [self.fx, self.fy, self.cx, self.cy, *self.dist, *self.rvec, *self.tvec],
            dtype=float,
        )

    def to_yaml(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("dist", "rvec", "tvec", "bed"):
            d[key] = [float(v) for v in d[key]]
        for key in ("fx", "fy", "cx", "cy"):
            d[key] = float(d[key])
        return d

    @classmethod
    def from_yaml(cls, d: dict[str, Any]) -> CameraModel:
        model = str(d.get("model", "pinhole"))
        if model not in _MODELS:
            raise ValueError(f"camera model {model!r} is not one of {_MODELS}")
        dist = tuple(float(v) for v in d["dist"])
        if len(dist) != _n_dist(model):
            raise ValueError(
                f"{model} camera needs {_n_dist(model)} distortion coefficients, got {len(dist)}"
            )
        rvec = tuple(float(v) for v in d["rvec"])
        tvec = tuple(float(v) for v in d["tvec"])
        bed = tuple(float(v) for v in d.get("bed", (0.0, 0.0, 270.0, 270.0)))
        if len(rvec) != 3 or len(tvec) != 3 or len(bed) != 4:
            raise ValueError(
                "camera rvec/tvec need 3 values and bed needs 4 (x0, y0, x1, y1)"
            )
        return cls(
            width=int(d["width"]),
            height=int(d["height"]),
            fx=float(d["fx"]),
            fy=float(d["fy"]),
            cx=float(d["cx"]),
            cy=float(d["cy"]),
            dist=dist,
            rvec=(rvec[0], rvec[1], rvec[2]),
            tvec=(tvec[0], tvec[1], tvec[2]),
            bed=(bed[0], bed[1], bed[2], bed[3]),
            bed_moves_z=bool(d.get("bed_moves_z", True)),
            model=model,
        )


def _n_dist(model: str) -> int:
    return 4 if model == "fisheye" else 5


def _rodrigues(r: np.ndarray) -> np.ndarray:
    import numpy as np

    theta = float(np.linalg.norm(r))
    if theta < 1e-12:
        return np.eye(3)
    k = r / theta
    kx = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return (
        np.eye(3) * math.cos(theta)
        + (1 - math.cos(theta)) * np.outer(k, k)
        + math.sin(theta) * kx
    )


def _rodrigues_inv(rot: np.ndarray) -> np.ndarray:
    import numpy as np

    cos = max(-1.0, min(1.0, (float(np.trace(rot)) - 1) / 2))
    theta = math.acos(cos)
    if theta < 1e-9:
        return np.zeros(3)
    if math.pi - theta < 1e-6:  # 180 degrees: axis from the symmetric part
        k = np.sqrt(np.maximum((np.diag(rot) + 1) / 2, 0))
        k *= np.sign(np.array([1.0, rot[0, 1] + 1e-12, rot[0, 2] + 1e-12]))
        return k * theta
    w = np.array([rot[2, 1] - rot[1, 2], rot[0, 2] - rot[2, 0], rot[1, 0] - rot[0, 1]])
    return w * theta / (2 * math.sin(theta))


def _project(p: np.ndarray, model: str, pw: np.ndarray) -> np.ndarray:
    """Params [fx, fy, cx, cy, dist..., rvec(3), tvec(3)] + Nx3 world -> Nx2 pixels."""
    import numpy as np

    nd = _n_dist(model)
    fx, fy, cx, cy = p[:4]
    d = p[4 : 4 + nd]
    rot = _rodrigues(p[4 + nd : 7 + nd])
    pc = pw @ rot.T + p[7 + nd : 10 + nd]
    zc = pc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        x = pc[:, 0] / zc
        y = pc[:, 1] / zc
        if model == "fisheye":
            r = np.hypot(x, y)
            th = np.arctan(r)
            th2 = th * th
            thd = th * (1 + th2 * (d[0] + th2 * (d[1] + th2 * (d[2] + th2 * d[3]))))
            s = np.where(r > 1e-12, thd / np.where(r > 1e-12, r, 1.0), 1.0)
            xd, yd = x * s, y * s
        else:
            k1, k2, p1, p2, k3 = d
            r2 = x * x + y * y
            rad = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
            xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
            yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    uv = np.stack([fx * xd + cx, fy * yd + cy], axis=1)
    uv[zc <= 1e-6] = np.nan
    return uv


def _dlt_init(
    corr: list[Correspondence], width: int, height: int, bed_moves_z: bool
) -> np.ndarray:
    """Pinhole pose + focal from a linear DLT (distortion ignored) -> params with zero distortion."""
    import numpy as np
    from scipy.linalg import rq

    pw = np.array(
        [[c.x, c.y, c.h - (c.z if bed_moves_z else 0.0)] for c in corr], dtype=float
    )
    uv = np.array([[c.u, c.v] for c in corr], dtype=float)
    # normalise both sides for a well-conditioned SVD
    mw, sw = pw.mean(0), pw.std() or 1.0
    mu, su = uv.mean(0), uv.std() or 1.0
    tw = np.diag([1 / sw] * 3 + [1.0])
    tw[:3, 3] = -mw / sw
    tu = np.array([[1 / su, 0, -mu[0] / su], [0, 1 / su, -mu[1] / su], [0, 0, 1]])
    a = []
    for (xw, yw, zw), (u, v) in zip((pw - mw) / sw, (uv - mu) / su, strict=True):
        a.append([xw, yw, zw, 1, 0, 0, 0, 0, -u * xw, -u * yw, -u * zw, -u])
        a.append([0, 0, 0, 0, xw, yw, zw, 1, -v * xw, -v * yw, -v * zw, -v])
    pn = np.linalg.svd(np.array(a))[2][-1].reshape(3, 4)
    pm = np.linalg.inv(tu) @ pn @ tw
    k, r = rq(pm[:, :3])
    sign = np.diag(np.sign(np.diag(k)))
    k, r = k @ sign, sign @ r
    t = np.linalg.solve(k, pm[:, 3])
    if np.linalg.det(r) < 0:
        r, t = -r, -t
    if (
        np.median((pw @ r.T + t)[:, 2]) < 0
    ):  # the DLT sign is free: put the points in front
        r = np.diag([-1.0, -1.0, 1.0]) @ r
        t = np.diag([-1.0, -1.0, 1.0]) @ t
        k = k @ np.diag([-1.0, -1.0, 1.0])
    k = k / k[2, 2]
    f = float(abs(k[0, 0]) + abs(k[1, 1])) / 2
    cx, cy = float(k[0, 2]), float(k[1, 2])
    if not (0 < cx < width and 0 < cy < height):
        cx, cy = width / 2, height / 2
    return np.array([f, f, cx, cy, 0, 0, 0, 0, 0, *_rodrigues_inv(r), *t], dtype=float)


def _residuals(p: np.ndarray, model: str, pw: np.ndarray, uv: np.ndarray) -> np.ndarray:
    import numpy as np

    out = _project(p, model, pw) - uv
    return np.nan_to_num(out, nan=1e4).ravel()


def fit_camera(
    corr: list[Correspondence],
    width: int,
    height: int,
    init: CameraModel | None = None,
    *,
    model: str = "pinhole",
    bed: tuple[float, float, float, float] = (0.0, 0.0, 270.0, 270.0),
    bed_moves_z: bool = True,
) -> tuple[CameraModel, float]:
    """Fit a camera to correspondences; returns (model, rms reprojection error in px).

    Without `init`, starts from a linear DLT and fits pose + focal first, then the lens
    distortion, so a strongly distorted lens still converges. Needs >= 8 points that are not
    all at one world height (use frames at several Z or points above the bed)."""
    import numpy as np
    from scipy.optimize import least_squares

    if model not in _MODELS:
        raise ValueError(f"camera model {model!r} is not one of {_MODELS}")
    if len(corr) < 8:
        raise ValueError(f"need at least 8 correspondences, got {len(corr)}")
    if init is not None:
        bed, bed_moves_z = init.bed, init.bed_moves_z
    pw = np.array(
        [[c.x, c.y, c.h - (c.z if bed_moves_z else 0.0)] for c in corr], dtype=float
    )
    uv = np.array([[c.u, c.v] for c in corr], dtype=float)
    nd = _n_dist(model)

    if init is not None and init.model == model:
        p = init._params()
    else:
        base = (
            init._params()
            if init is not None
            else _dlt_init(corr, width, height, bed_moves_z)
        )

        # stage 1: pose + focal + principal point, no distortion
        def r1(q: np.ndarray) -> np.ndarray:
            return _residuals(
                np.concatenate([[q[0], q[0]], q[1:3], np.zeros(nd), q[3:]]),
                model,
                pw,
                uv,
            )

        q0 = np.concatenate([[base[0]], base[2:4], base[-6:]])
        q = least_squares(r1, q0, method="lm", x_scale="jac", max_nfev=4000).x
        p = np.concatenate([[q[0], q[0]], q[1:3], np.zeros(nd), q[3:]])

    # stage 2: everything, square pixels assumed (fx = fy) then released
    def r2(q: np.ndarray) -> np.ndarray:
        return _residuals(np.concatenate([[q[0]], q]), model, pw, uv)

    q = least_squares(r2, p[1:], method="lm", x_scale="jac", max_nfev=8000).x
    p = np.concatenate([[q[0]], q])
    if len(corr) >= 15:
        p = least_squares(
            _residuals,
            p,
            args=(model, pw, uv),
            method="lm",
            x_scale="jac",
            max_nfev=8000,
        ).x
    res = _residuals(p, model, pw, uv).reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum(res * res, axis=1))))
    cam = CameraModel(
        width=int(width),
        height=int(height),
        fx=float(p[0]),
        fy=float(p[1]),
        cx=float(p[2]),
        cy=float(p[3]),
        dist=tuple(float(v) for v in p[4 : 4 + nd]),
        rvec=(float(p[4 + nd]), float(p[5 + nd]), float(p[6 + nd])),
        tvec=(float(p[7 + nd]), float(p[8 + nd]), float(p[9 + nd])),
        bed=bed,
        bed_moves_z=bed_moves_z,
        model=model,
    )
    return cam, rms


def residuals(cam: CameraModel, corr: list[Correspondence]) -> np.ndarray:
    """Per-point reprojection error in px (Euclidean)."""
    import numpy as np

    out = []
    for c in corr:
        uv = cam.project(np.array([[c.x, c.y, c.h]]), c.z)[0]
        out.append(float(np.hypot(uv[0] - c.u, uv[1] - c.v)))
    return np.array(out)


# ---------------------------------------------------------------- polygons and rasterising


def _convex_hull(pts: np.ndarray) -> np.ndarray:
    """Andrew's monotone chain; counter-clockwise, no repeated last point."""
    import numpy as np

    p = np.unique(np.asarray(pts, dtype=float).reshape(-1, 2), axis=0)
    if len(p) < 3:
        return p

    def half(seq: np.ndarray) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for q in seq:
            while len(out) >= 2:
                a, b = out[-2], out[-1]
                if (b[0] - a[0]) * (q[1] - a[1]) - (b[1] - a[1]) * (
                    q[0] - a[0]
                ) > 1e-12:
                    break
                out.pop()
            out.append(q)
        return out

    lower, upper = half(p), half(p[::-1])
    return np.array(lower[:-1] + upper[:-1])


def _pad(poly: Polygon | np.ndarray, pad_mm: float) -> np.ndarray:
    """Footprint grown by `pad_mm` (convex hull of the footprint + pad circle); unchanged at 0."""
    import numpy as np

    p = np.asarray(poly, dtype=float).reshape(-1, 2)
    if len(p) > 1 and np.allclose(p[0], p[-1]):
        p = p[:-1]
    if pad_mm <= 0:
        return p
    ang = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    ring = np.stack([np.cos(ang), np.sin(ang)], axis=1) * pad_mm
    return _convex_hull((p[:, None, :] + ring[None, :, :]).reshape(-1, 2))


def _densify(poly: np.ndarray, step: float) -> np.ndarray:
    """Closed polygon with extra vertices every `step` mm, so distorted edges stay curved."""
    import numpy as np

    out = []
    for a, b in zip(poly, np.roll(poly, -1, axis=0), strict=True):
        n = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
        out.append(a + (b - a) * (np.arange(n)[:, None] / n))
    return np.concatenate(out)


def _fill(mask: np.ndarray, poly: np.ndarray) -> None:
    """Even-odd scanline fill of one polygon (pixel centres at integer coords) into a bool mask."""
    import numpy as np

    poly = poly[np.all(np.isfinite(poly), axis=1)]
    if len(poly) < 3:
        return
    h, w = mask.shape
    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    lo, hi = (
        int(max(0, math.ceil(float(y0.min())))),
        int(min(h - 1, math.floor(float(y0.max())))),
    )
    if lo > hi:
        return
    rows = np.arange(lo, hi + 1, dtype=float)[:, None]
    ymin, ymax = np.minimum(y0, y1), np.maximum(y0, y1)
    hit = (rows >= ymin) & (rows < ymax)
    ri, ei = np.nonzero(hit)
    if not len(ri):
        return
    xs = x0[ei] + (rows[ri, 0] - y0[ei]) * (x1[ei] - x0[ei]) / (y1[ei] - y0[ei])
    cols = np.clip(np.ceil(xs), 0, w).astype(int)
    toggles = np.zeros((hi - lo + 1, w + 1), dtype=np.int32)
    np.add.at(toggles, (ri, cols), 1)
    mask[lo : hi + 1] |= (np.cumsum(toggles, axis=1)[:, :w] % 2).astype(bool)


def _scale(cam: CameraModel, shape: tuple[int, ...]) -> tuple[int, int, float, float]:
    h, w = int(shape[0]), int(shape[1])
    return h, w, w / cam.width, h / cam.height


def _prism_faces(
    cam: CameraModel,
    z: float,
    poly: np.ndarray,
    h0: float,
    h1: float,
    step: float = 4.0,
) -> list[np.ndarray]:
    """Image polygons (model px) of a vertical prism's bottom, top and side faces; every edge
    is sampled each `step` mm, so lens distortion bends it like the real one."""
    import numpy as np

    def at(xy: np.ndarray, hs: np.ndarray) -> np.ndarray:
        return cam.project(np.column_stack([xy, hs]), z)

    ring = _densify(poly, step)
    n = len(ring)
    faces = [at(ring, np.full(n, h0)), at(ring, np.full(n, h1))]
    nh = max(1, int(math.ceil((h1 - h0) / step)))
    up = np.linspace(h0, h1, nh + 1)
    for a, b in zip(poly, np.roll(poly, -1, axis=0), strict=True):
        ne = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
        edge = a + (b - a) * (np.arange(ne + 1)[:, None] / ne)
        side = [
            np.column_stack([edge, np.full(ne + 1, h0)]),
            np.column_stack([np.repeat(b[None], nh - 1, axis=0), up[1:-1]]),
            np.column_stack([edge[::-1], np.full(ne + 1, h1)]),
            np.column_stack([np.repeat(a[None], nh - 1, axis=0), up[-2:0:-1]]),
        ]
        faces.append(cam.project(np.concatenate(side), z))
    return faces


def _paint(
    cam: CameraModel, shape: tuple[int, ...], polys: list[np.ndarray]
) -> np.ndarray:
    import numpy as np

    h, w, sx, sy = _scale(cam, shape)
    mask = np.zeros((h, w), dtype=bool)
    for p in polys:
        _fill(mask, p * [sx, sy])
    return mask.astype(np.uint8) * 255


def _heights(objects: list[Polygon], heights: list[float] | None) -> list[float]:
    if heights is None:
        return [math.inf] * len(objects)
    if len(heights) != len(objects):
        raise ValueError(f"{len(heights)} heights for {len(objects)} objects")
    return [float(h) for h in heights]


def bed_mask(
    cam: CameraModel, z: float, shape: tuple[int, ...], margin_mm: float = 0
) -> np.ndarray:
    """uint8 0/255 mask of the bed plate surface (grown by `margin_mm`) as seen at gcode Z."""
    import numpy as np

    x0, y0, x1, y1 = cam.bed
    m = margin_mm
    plate = np.array(
        [[x0 - m, y0 - m], [x1 + m, y0 - m], [x1 + m, y1 + m], [x0 - m, y1 + m]],
        dtype=float,
    )
    ring = _densify(plate, 4.0)
    return _paint(
        cam, shape, [cam.project(np.column_stack([ring, np.zeros(len(ring))]), z)]
    )


def volume_mask(
    cam: CameraModel,
    z: float,
    shape: tuple[int, ...],
    objects: list[Polygon],
    heights: list[float] | None = None,
    pad_mm: float = 5,
    above_mm: float = 15,
) -> np.ndarray:
    """Where print-related pixels may legitimately be: the bed plate plus each object's padded
    footprint prism from the bed up to min(z, height) + `above_mm` (nozzle, fresh extrusion)."""
    polys: list[np.ndarray] = []
    for poly, top in zip(objects, _heights(objects, heights), strict=True):
        polys += _prism_faces(cam, z, _pad(poly, pad_mm), 0.0, min(z, top) + above_mm)
    return bed_mask(cam, z, shape) | _paint(cam, shape, polys)


def silhouette_mask(
    cam: CameraModel,
    z: float,
    shape: tuple[int, ...],
    objects: list[Polygon],
    heights: list[float] | None = None,
    pad_mm: float = 2,
) -> np.ndarray:
    """Expected silhouette of the printed parts: footprint prisms from the bed to min(z, height).
    Footprints are convex hulls (exclude_object), so thin or concave parts are over-covered."""
    polys: list[np.ndarray] = []
    for poly, top in zip(objects, _heights(objects, heights), strict=True):
        hi = min(z, top)
        if hi > 0:
            polys += _prism_faces(cam, z, _pad(poly, pad_mm), 0.0, hi)
    return _paint(cam, shape, polys)


def top_band_mask(
    cam: CameraModel,
    z: float,
    shape: tuple[int, ...],
    objects: list[Polygon],
    band_mm: float = 3,
    heights: list[float] | None = None,
) -> np.ndarray:
    """The newest `band_mm` (z - band_mm .. z) of every object still growing at gcode Z — where
    fresh extrusion must appear; finished objects (height < z) are left out."""
    polys: list[np.ndarray] = []
    for poly, top in zip(objects, _heights(objects, heights), strict=True):
        if top + 1e-6 >= z and z > 0:
            polys += _prism_faces(cam, z, _pad(poly, 0), max(0.0, z - band_mm), z)
    return _paint(cam, shape, polys)


def prism_mask(
    cam: CameraModel,
    z: float,
    shape: tuple[int, ...],
    objects: list[Polygon],
    h0: float,
    h1: float,
    pad_mm: float = 0,
) -> np.ndarray:
    """Vertical prisms over every footprint between heights `h0`..`h1` mm above the bed, seen
    at gcode Z — e.g. the part body under the newest band, or the empty space above it, or the
    toolhead's box around the nozzle."""
    polys: list[np.ndarray] = []
    if h1 > h0:
        for poly in objects:
            polys += _prism_faces(cam, z, _pad(poly, pad_mm), h0, h1)
    return _paint(cam, shape, polys)


def overlay(
    bgr: np.ndarray,
    cam: CameraModel,
    z: float,
    objects: list[Polygon],
    heights: list[float] | None = None,
) -> np.ndarray:
    """Review image: bed outline (cyan), footprints at the bed (yellow) and at the current top
    min(z, height) (magenta), with vertical edges joining them."""
    import cv2
    import numpy as np

    out = bgr.copy()
    h, w, sx, sy = _scale(cam, out.shape)
    thick = max(1, round(w / 640))

    def line(pts: np.ndarray, color: tuple[int, int, int], closed: bool = True) -> None:
        pts = pts * [sx, sy]
        ok = np.all(np.isfinite(pts), axis=1)
        if ok.sum() >= 2:
            cv2.polylines(
                out,
                [np.round(pts[ok]).astype(np.int32)],
                closed,
                color,
                thick,
                cv2.LINE_AA,
            )

    x0, y0, x1, y1 = cam.bed
    plate = _densify(
        np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float), 4.0
    )
    line(cam.project(np.column_stack([plate, np.zeros(len(plate))]), z), (255, 255, 0))
    for poly, top in zip(objects, _heights(objects, heights), strict=True):
        p = _pad(poly, 0)
        ring = _densify(p, 4.0)
        hi = max(0.0, min(z, top))
        line(
            cam.project(np.column_stack([ring, np.zeros(len(ring))]), z), (0, 255, 255)
        )
        line(
            cam.project(np.column_stack([ring, np.full(len(ring), hi)]), z),
            (255, 0, 255),
        )
        for vx, vy in p:
            line(
                cam.project(np.array([[vx, vy, 0.0], [vx, vy, hi]]), z),
                (255, 0, 255),
                closed=False,
            )
    return out


# ---------------------------------------------------------------- persistence


def _model_key(model: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", model.lower())
    return "u1" if key.endswith("u1") else key


def bundled_calibration(model: str) -> CameraModel | None:
    """The calibration shipped for a printer model (e.g. 'Snapmaker U1'), or None."""
    name = _BUNDLED.get(_model_key(model))
    return read_camera(_BUILTIN_DIR / name) if name else None


def read_camera(path: pathlib.Path) -> CameraModel:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return CameraModel.from_yaml(
        data["camera"] if isinstance(data, dict) and "camera" in data else data
    )


def load_calibration(printer: str) -> CameraModel | None:
    """`<config dir>/vision/<printer>-camera.yaml`, else the bundled calibration of the
    device's model (devices.yaml `model:`), else None."""
    from cli.paths import config_dir

    own = config_dir() / "vision" / f"{printer}-camera.yaml"
    if own.is_file():
        return read_camera(own)
    try:
        from printer_ops.devices import load_devices

        device = load_devices().get(printer)
    except Exception:  # noqa: BLE001 — a broken devices.yaml must not take the vision ROI down
        device = None
    if device is None or not device.model:
        return None
    return bundled_calibration(device.model)


def read_points(path: pathlib.Path) -> tuple[int, int, list[Correspondence]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    pts = [
        Correspondence(
            float(p["u"]),
            float(p["v"]),
            float(p["x"]),
            float(p["y"]),
            float(p.get("h", 0.0)),
            float(p["z"]),
            str(p.get("label", "")),
        )
        for p in data["points"]
    ]
    return int(data["width"]), int(data["height"]), pts


def _cli(argv: list[str]) -> int:
    """`fit points.yaml out.yaml [--model pinhole|fisheye] [--overlay frame.jpg Z out.jpg]`."""
    import numpy as np
    import yaml

    if len(argv) < 3 or argv[0] != "fit":
        print(_cli.__doc__)
        return 2
    src, dst = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    rest = argv[3:]
    model = "pinhole"
    if "--model" in rest:
        model = rest[rest.index("--model") + 1]
    width, height, corr = read_points(src)
    cam, rms = fit_camera(corr, width, height, model=model)
    res = residuals(cam, corr)
    print(f"{model}: {len(corr)} points, rms {rms:.2f} px, max {res.max():.2f} px")
    for i in np.argsort(-res)[:8]:
        c = corr[i]
        print(
            f"  {res[i]:6.2f} px  {c.label or '-'}  ({c.x:g}, {c.y:g}, h={c.h:g}) z={c.z:g}"
        )
    doc = {
        "camera": cam.to_yaml(),
        "fit": {
            "points": src.name,
            "n": len(corr),
            "rms_px": round(rms, 3),
            "max_px": round(float(res.max()), 3),
        },
    }
    dst.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    if "--overlay" in rest:
        import cv2

        i = rest.index("--overlay")
        frame, z, out = rest[i + 1], float(rest[i + 2]), rest[i + 3]
        img = overlay(cv2.imread(frame), cam, z, [])
        for c in corr:
            if abs(c.z - z) < 0.05:
                uv = cam.project(np.array([[c.x, c.y, c.h]]), c.z)[0]
                cv2.circle(img, (round(c.u), round(c.v)), 6, (0, 0, 255), 2)
                cv2.line(
                    img,
                    (round(c.u), round(c.v)),
                    (round(uv[0]), round(uv[1])),
                    (0, 255, 0),
                    2,
                )
        cv2.imwrite(out, img)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
