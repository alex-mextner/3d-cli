"""raster.py — cameras and a pure-numpy triangle rasteriser (silhouette mask, depth, face id).

WHAT: orthographic and pinhole cameras plus `rasterize`, which fills projected triangles into an image
  grid with batched barycentric tests (triangles are bucketed by bounding-box size, so 400k tiny faces
  and a handful of huge ones both run in vectorised numpy). Every triangle also splats its corners and
  centroid, so sub-pixel faces never leave holes in a silhouette. Output is a bool mask, a depth
  buffer and the face id of the nearest surface per pixel.

WHY: fitting a mesh to a reference silhouette needs thousands of renders; going through OpenSCAD or an
  OpenGL context would make that slow and GUI/driver dependent. This needs only numpy and is
  deterministic (no atomics: samples are ordered with a stable lexsort).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import UsageError

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]

VIEW_DIRECTIONS: dict[
    str, tuple[tuple[float, float, float], tuple[float, float, float]]
] = {
    # name: (viewing direction, image-up). Front looks along +Y from the -Y side (the cat faces -Y).
    "front": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "back": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "left": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "bottom": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
}


@dataclass(frozen=True)
class Camera:
    """Projects world points to pixel coordinates (u right, v down, pixel centres at +0.5) and a depth."""

    origin: Array  # ortho: world point at the image centre; perspective: eye
    forward: Array
    right: Array
    up: Array
    width: int
    height: int
    scale: float  # ortho: mm per pixel; perspective: focal length in pixels
    perspective: bool

    def project(self, P: Array) -> tuple[Array, Array]:
        rel = P - self.origin
        x, y, z = rel @ self.right, rel @ self.up, rel @ self.forward
        if self.perspective:
            zc = np.where(z > 1e-9, z, np.nan)
            uv = np.column_stack(
                [
                    self.width / 2.0 + self.scale * x / zc,
                    self.height / 2.0 - self.scale * y / zc,
                ]
            )
        else:
            uv = np.column_stack(
                [self.width / 2.0 + x / self.scale, self.height / 2.0 - y / self.scale]
            )
        return uv, z


def _basis(forward: Array, up: Array) -> tuple[Array, Array, Array]:
    f = np.asarray(forward, dtype=np.float64)
    f = f / np.linalg.norm(f)
    r = np.cross(f, np.asarray(up, dtype=np.float64))
    if np.linalg.norm(r) < 1e-9:
        raise UsageError(
            "camera: up vector is parallel to the viewing direction",
            command="mesh deform",
        )
    r /= np.linalg.norm(r)
    return f, r, np.cross(r, f)


def ortho_camera(
    direction: Array,
    up: Array,
    center: Array,
    mm_per_px: float,
    width: int,
    height: int,
) -> Camera:
    f, r, u = _basis(direction, up)
    return Camera(
        np.asarray(center, dtype=np.float64),
        f,
        r,
        u,
        width,
        height,
        float(mm_per_px),
        False,
    )


def perspective_camera(
    eye: Array, center: Array, up: Array, fov_deg: float, width: int, height: int
) -> Camera:
    eye = np.asarray(eye, dtype=np.float64)
    f, r, u = _basis(np.asarray(center, dtype=np.float64) - eye, up)
    focal = (height / 2.0) / np.tan(np.radians(fov_deg) / 2.0)
    return Camera(eye, f, r, u, width, height, float(focal), True)


def auto_ortho(
    view: str, V: Array, width: int, height: int, fill: float = 0.8
) -> Camera:
    """Named axis view framed on the mesh bounding box (the box fills `fill` of the image)."""
    if view not in VIEW_DIRECTIONS:
        raise UsageError(
            f"camera: view must be one of {', '.join(VIEW_DIRECTIONS)}, got {view!r}",
            command="mesh deform",
        )
    d, u = VIEW_DIRECTIONS[view]
    center = (V.min(axis=0) + V.max(axis=0)) / 2.0
    f, r, up = _basis(np.array(d), np.array(u))
    extent_u = np.ptp(V @ r)
    extent_v = np.ptp(V @ up)
    scale = max(extent_u / (fill * width), extent_v / (fill * height), 1e-9)
    return Camera(center, f, r, up, width, height, float(scale), False)


def rasterize(
    uv: Array,
    depth: Array,
    F: IntArray,
    width: int,
    height: int,
    *,
    want_depth: bool = False,
) -> tuple[NDArray[np.bool_], Array | None, IntArray | None]:
    """Fill the projected triangles. Returns (mask, depth buffer or None, nearest face id or None; -1 = empty)."""
    ok = np.asarray(np.isfinite(uv).all(axis=1))
    tri = uv[F]  # (m, 3, 2)
    tri_ok = ok[F].all(axis=1)
    px_ids: list[IntArray] = []
    z_vals: list[Array] = []
    f_ids: list[IntArray] = []

    def emit(px: IntArray, py: IntArray, z: Array, face: IntArray) -> None:
        inside = (px >= 0) & (px < width) & (py >= 0) & (py < height)
        px_ids.append(py[inside] * width + px[inside])
        z_vals.append(z[inside])
        f_ids.append(face[inside])

    face_idx = np.nonzero(tri_ok)[0]
    T = tri[face_idx]
    Z = depth[F[face_idx]]
    # corners + centroid splats: never lose a sub-pixel face
    for k in range(3):
        emit(
            np.floor(T[:, k, 0]).astype(np.int64),
            np.floor(T[:, k, 1]).astype(np.int64),
            Z[:, k],
            face_idx,
        )
    cen = T.mean(axis=1)
    emit(
        np.floor(cen[:, 0]).astype(np.int64),
        np.floor(cen[:, 1]).astype(np.int64),
        Z.mean(axis=1),
        face_idx,
    )

    lo = np.floor(T.min(axis=1)).astype(np.int64)
    hi = np.floor(T.max(axis=1)).astype(np.int64)
    lo = np.maximum(lo, 0)
    hi = np.minimum(hi, [width - 1, height - 1])
    span = np.maximum(hi - lo + 1, 0)
    size = span.max(axis=1)
    bucket_edges = [1, 2, 4, 8, 16, 32, 64]
    prev = 0
    for edge in [*bucket_edges, None]:
        sel = np.nonzero((size > prev) & (size <= edge if edge is not None else True))[
            0
        ]
        prev = edge if edge is not None else prev
        if len(sel) == 0:
            continue
        b = int(size[sel].max())
        if edge is None and b > 64:
            for i in sel:  # huge triangles: one at a time
                _fill(T[i], Z[i], lo[i], span[i], int(face_idx[i]), emit)
            continue
        chunk = max(1, 4_000_000 // (b * b))
        for s in range(0, len(sel), chunk):
            _fill_batch(
                T[sel[s : s + chunk]],
                Z[sel[s : s + chunk]],
                lo[sel[s : s + chunk]],
                b,
                face_idx[sel[s : s + chunk]],
                emit,
            )
    pix = np.concatenate(px_ids) if px_ids else np.zeros(0, dtype=np.int64)
    mask = np.zeros(width * height, dtype=bool)
    mask[pix] = True
    if not want_depth:
        return mask.reshape(height, width), None, None
    z = np.concatenate(z_vals)
    face = np.concatenate(f_ids)
    order = np.lexsort(
        (face, z, pix)
    )  # pixel, then nearest depth, then face id: fully ordered
    pix_s = pix[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = pix_s[1:] != pix_s[:-1]
    zbuf = np.full(width * height, np.inf)
    fbuf = np.full(width * height, -1, dtype=np.int64)
    zbuf[pix_s[first]] = z[order][first]
    fbuf[pix_s[first]] = face[order][first]
    return (
        mask.reshape(height, width),
        zbuf.reshape(height, width),
        fbuf.reshape(height, width),
    )


def _barycentric(
    a: Array, b: Array, c: Array, p: Array
) -> tuple[Array, Array, Array, Array]:
    """Barycentric coordinates of p (...,2) in triangle(s) a,b,c (...,2) and the signed doubled area."""
    d = (b[..., 0] - a[..., 0]) * (c[..., 1] - a[..., 1]) - (b[..., 1] - a[..., 1]) * (
        c[..., 0] - a[..., 0]
    )
    safe = np.where(np.abs(d) < 1e-18, 1.0, d)
    w1 = (
        (p[..., 0] - a[..., 0]) * (c[..., 1] - a[..., 1])
        - (p[..., 1] - a[..., 1]) * (c[..., 0] - a[..., 0])
    ) / safe
    w2 = (
        (b[..., 0] - a[..., 0]) * (p[..., 1] - a[..., 1])
        - (b[..., 1] - a[..., 1]) * (p[..., 0] - a[..., 0])
    ) / safe
    return 1.0 - w1 - w2, w1, w2, d


def _fill_batch(
    T: Array, Z: Array, lo: IntArray, b: int, face: IntArray, emit: Any
) -> None:
    g = np.arange(b)
    gx, gy = np.meshgrid(g, g, indexing="xy")
    px = lo[:, 0, None, None] + gx[None]  # (t, b, b)
    py = lo[:, 1, None, None] + gy[None]
    p = np.stack([px + 0.5, py + 0.5], axis=-1)
    a, bb, c = (T[:, k, None, None, :] for k in range(3))
    w0, w1, w2, d = _barycentric(a, bb, c, p)
    inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9) & (np.abs(d) > 1e-18)
    t_idx, iy, ix = np.nonzero(inside)
    if len(t_idx) == 0:
        return
    z = w0[inside] * Z[t_idx, 0] + w1[inside] * Z[t_idx, 1] + w2[inside] * Z[t_idx, 2]
    emit(px[t_idx, iy, ix], py[t_idx, iy, ix], z, face[t_idx])


def _fill(
    T: Array, Z: Array, lo: IntArray, span: IntArray, face: int, emit: Any
) -> None:
    gx, gy = np.meshgrid(np.arange(span[0]), np.arange(span[1]), indexing="xy")
    px, py = lo[0] + gx.ravel(), lo[1] + gy.ravel()
    p = np.stack([px + 0.5, py + 0.5], axis=-1)
    w0, w1, w2, d = _barycentric(T[0], T[1], T[2], p)
    inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9) & (abs(d) > 1e-18)
    z = w0[inside] * Z[0] + w1[inside] * Z[1] + w2[inside] * Z[2]
    emit(px[inside], py[inside], z, np.full(int(inside.sum()), face, dtype=np.int64))


def silhouette(camera: Camera, V: Array, F: IntArray) -> NDArray[np.bool_]:
    uv, z = camera.project(V)
    return rasterize(uv, z, F, camera.width, camera.height)[0]


def mask_iou(a: NDArray[np.bool_], b: NDArray[np.bool_]) -> float:
    union = int((a | b).sum())
    return float((a & b).sum() / union) if union else 1.0


def shade(camera: Camera, V: Array, F: IntArray) -> NDArray[np.uint8]:
    """Grey Lambert shading from the camera (headlight) for a quick visual; background is white."""
    from . import meshmath

    uv, z = camera.project(V)
    _, zbuf, fbuf = rasterize(uv, z, F, camera.width, camera.height, want_depth=True)
    assert fbuf is not None and zbuf is not None  # want_depth=True
    img = np.full((camera.height, camera.width), 255, dtype=np.uint8)
    n = meshmath.face_normals(V, F)
    hit = fbuf >= 0
    lam = np.abs(n[fbuf[hit]] @ camera.forward)
    img[hit] = (60 + 180 * lam).astype(np.uint8)
    return img
