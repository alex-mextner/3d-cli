"""raster.py — dependency-free mesh and section rendering for the proof pack (pure numpy, optional PIL for text).

WHAT: `render_mesh` draws a triangle mesh from a named or free (azimuth, elevation) view as an RGB array;
  `render_section` draws one planar cut; `write_png` saves any RGB array; `Plot2D` is the small
  millimetre-to-pixel canvas (polygons, lines, discs, text, scale bar) that `panels.py` builds its charts on.
WHY: the proof pack must render on a headless box with neither OpenGL, Blender, matplotlib nor embree,
  and a 411k-face mesh must take seconds, not minutes (the hand-made proofs of the Chekhov print cost
  hours). Rendering is a point splat: every face is sampled with a number of points proportional to its
  projected area in pixels, points are z-buffered with one `minimum.at`, then 1-pixel holes are closed.
INVARIANTS:
  * Orthographic camera fitted to the model bbox (`camera_for`), screen right/up derived from (azimuth,
    elevation): azimuth 0 looks from -Y toward +Y (the 'front' view of the print frame, X to the right),
    90 from +X, elevation 90 from above. No singularity at the poles.
  * Sample density is `SAMPLES_PER_PIXEL` points per projected pixel of area. A pixel is therefore missed
    with probability ~exp(-4) = 2 %, and the 3x3 morphological closing of the coverage mask fills those
    isolated misses from the nearest covered pixel, so a closed solid has no interior background pixel.
    A miss in a NEAR surface would let the surface behind show through, so a pixel much farther (>2.5 px
    of depth) than its 3x3 neighbourhood's nearest sample is refilled the same way.
    A face smaller than a pixel is sampled with probability proportional to its area (unbiased).
  * Faces are NOT back-face culled: the nearest sample wins the z-buffer, so inconsistent winding still
    renders solid, and the open hollow shell of a scan shows its inside from below. Shading uses the face
    normal flipped toward the camera.
  * Deterministic: a seeded generator (`seed`), stable sort, no wall-clock input; same call -> same bytes.
  * Text needs PIL; without it text calls are no-ops (`has_text()` says which) and everything else is
    unchanged.
ASSUMPTIONS: meshes are in the print frame (Z up) but nothing here requires it.
"""

from __future__ import annotations

import functools
import math
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy import ndimage

from errors import InvalidArgument

RGB = tuple[int, int, int]
View = str | tuple[float, float]
Image = NDArray[np.uint8]
Point = NDArray[np.float64] | Sequence[float]
#: named views as (azimuth deg, elevation deg); see the module docstring for the convention
VIEWS: dict[str, tuple[float, float]] = {
    "iso": (40.0, 28.0),
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "right": (90.0, 0.0),
    "left": (270.0, 0.0),
    "top": (0.0, 90.0),
    "bottom": (0.0, -90.0),
    "low": (40.0, -35.0),
}

SAMPLES_PER_PIXEL = 4.0
#: above this many samples the density is scaled down (closing hides the extra misses); bounds memory
MAX_SAMPLES = 6_000_000
DEFAULT_COLOR: RGB = (190, 192, 200)
DEFAULT_LIGHT: tuple[float, float, float] = (-0.4, 0.6, 0.7)
_DEPTH_BITS = 24


def view_names() -> list[str]:
    """The accepted view names, in a stable order."""
    return list(VIEWS)


def _view_angles(view: View) -> tuple[float, float]:
    if isinstance(view, str):
        if view not in VIEWS:
            raise InvalidArgument("view", view, view_names(), command="prep")
        return VIEWS[view]
    if isinstance(view, tuple) and len(view) == 2:
        az, el = float(view[0]), float(view[1])
        if math.isfinite(az) and math.isfinite(el):
            return az, el
    raise InvalidArgument(
        "view",
        repr(view),
        [*view_names(), "(azimuth_deg, elevation_deg)"],
        command="prep",
    )


def _check_size(size: tuple[int, int]) -> tuple[int, int]:
    w, h = size
    if int(w) != w or int(h) != h or w < 16 or h < 16:
        raise InvalidArgument(
            "size", repr(size), ["(width, height) integers >= 16"], command="prep"
        )
    return int(w), int(h)


@dataclass(frozen=True)
class Camera:
    """Orthographic camera: world point -> (pixel x, pixel y, depth), depth larger = nearer the viewer."""

    right: NDArray[np.float64]
    up: NDArray[np.float64]
    toward: NDArray[np.float64]  # unit vector from the model to the camera
    scale: float  # pixels per mm
    centre_uv: tuple[float, float]  # (right, up) coordinates of the model bbox centre
    size: tuple[int, int]  # (width, height)

    def project(
        self, points: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
        """Pixel coordinates (continuous, y down) and depth of (N, 3) world points."""
        u = points @ self.right
        v = points @ self.up
        px = (u - self.centre_uv[0]) * self.scale + self.size[0] / 2
        py = self.size[1] / 2 - (v - self.centre_uv[1]) * self.scale
        return px, py, points @ self.toward


def camera_for(
    mesh: trimesh.Trimesh,
    view: View = "iso",
    size: tuple[int, int] = (900, 900),
    *,
    margin_frac: float = 0.04,
) -> Camera:
    """Camera of `view` whose scale makes the bbox fill the image minus `margin_frac` on each side."""
    az_deg, el_deg = _view_angles(view)
    w, h = _check_size(size)
    az, el = math.radians(az_deg), math.radians(el_deg)
    toward = np.array(
        [math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)]
    )
    right = np.array([math.cos(az), math.sin(az), 0.0])
    up = np.cross(right, -toward)
    lo, hi = mesh.bounds
    corners = np.array(
        [
            [x, y, z]
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]
    )
    u, v = corners @ right, corners @ up
    du, dv = max(float(u.max() - u.min()), 1e-9), max(float(v.max() - v.min()), 1e-9)
    scale = min(w * (1 - 2 * margin_frac) / du, h * (1 - 2 * margin_frac) / dv)
    centre = (float(u.min() + u.max()) / 2, float(v.min() + v.max()) / 2)
    return Camera(right, up, toward, scale, centre, (w, h))


def _face_colors(
    mesh: trimesh.Trimesh, face_colors: NDArray[np.generic] | None
) -> NDArray[np.float64]:
    n = len(mesh.faces)
    if face_colors is None:
        return np.broadcast_to(np.asarray(DEFAULT_COLOR, dtype=np.float64), (n, 3))
    arr = np.asarray(face_colors)
    if arr.ndim != 2 or arr.shape[0] != n or arr.shape[1] not in (3, 4):
        raise InvalidArgument(
            "face_colors",
            f"shape {arr.shape}",
            [f"({n}, 3) or ({n}, 4) per-face colours"],
            command="prep",
        )
    return arr[:, :3].astype(np.float64)


def render_mesh(
    mesh: trimesh.Trimesh,
    *,
    view: View = "iso",
    size: tuple[int, int] = (900, 900),
    face_colors: NDArray[np.generic] | None = None,
    background: RGB = (255, 255, 255),
    light: tuple[float, float, float] = DEFAULT_LIGHT,
    ambient: float = 0.4,
    seed: int = 0,
) -> Image:
    """Shaded orthographic render of `mesh` as an (H, W, 3) uint8 array.

    `light` is a direction in camera space (right, up, toward-viewer); shading is `ambient` plus Lambert,
    so `ambient` near 1 keeps `face_colors` close to their nominal value (used by the heatmaps).
    """
    if len(mesh.faces) == 0:
        raise InvalidArgument(
            "mesh", "no faces", ["a mesh with at least one triangle"], command="prep"
        )
    cam = camera_for(mesh, view, size)
    w, h = cam.size
    base = _face_colors(mesh, face_colors)

    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    px, py, depth = cam.project(verts)
    p0, p1, p2 = px[faces[:, 0]], px[faces[:, 1]], px[faces[:, 2]]
    q0, q1, q2 = py[faces[:, 0]], py[faces[:, 1]], py[faces[:, 2]]
    area_px = 0.5 * np.abs((p1 - p0) * (q2 - q0) - (p2 - p0) * (q1 - q0))

    rng = np.random.default_rng(seed)
    lam = area_px * SAMPLES_PER_PIXEL
    total = float(lam.sum())
    if total > MAX_SAMPLES:
        lam *= MAX_SAMPLES / total
    counts = np.floor(lam).astype(np.int64) + (
        rng.random(len(lam)) < (lam - np.floor(lam))
    )
    face_idx = np.repeat(np.arange(len(faces)), counts)

    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = np.asarray(background, dtype=np.uint8)
    if face_idx.size == 0:
        return img

    # Uniform point in a triangle: v0 + u*e1 + v*e2 with (u, v) folded back into the triangle.
    u = rng.random(face_idx.size, dtype=np.float32)
    v = rng.random(face_idx.size, dtype=np.float32)
    fold = (u + v) > 1.0
    u[fold], v[fold] = 1.0 - u[fold], 1.0 - v[fold]
    pts = np.stack([px, py, depth], axis=1).astype(np.float32)
    v0, v1, v2 = pts[faces[:, 0]], pts[faces[:, 1]], pts[faces[:, 2]]
    s = (
        v0[face_idx]
        + u[:, None] * (v1 - v0)[face_idx]
        + v[:, None] * (v2 - v0)[face_idx]
    )
    ix = np.floor(s[:, 0]).astype(np.int64)
    iy = np.floor(s[:, 1]).astype(np.int64)
    sd = s[:, 2]
    inside = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
    ix, iy, sd, face_idx = ix[inside], iy[inside], sd[inside], face_idx[inside]

    # z-buffer: nearest quantised depth per pixel via minimum.at, then the sample that holds it names the face.
    dlo, dhi = float(depth.min()), float(depth.max())
    levels = (1 << _DEPTH_BITS) - 1
    near = np.clip((dhi - sd) / max(dhi - dlo, 1e-12), 0.0, 1.0)
    near_q = (near * levels).astype(np.int64)
    pix_id = iy * w + ix
    zbuf = np.full(h * w, levels + 1, dtype=np.int64)
    np.minimum.at(zbuf, pix_id, near_q)
    hit = near_q == zbuf[pix_id]
    face_at = np.full(h * w, -1, dtype=np.int64)
    face_at[pix_id[hit]] = face_idx[
        hit
    ]  # equal-depth ties: the later sample wins, deterministically
    win_pix = np.flatnonzero(face_at >= 0)
    win_face = face_at[win_pix]

    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    to_cam = normals @ cam.toward
    normals = np.where((to_cam < 0)[:, None], -normals, normals)
    light_world = light[0] * cam.right + light[1] * cam.up + light[2] * cam.toward
    light_world = light_world / np.linalg.norm(light_world)
    shade = ambient + (1.0 - ambient) * np.clip(normals @ light_world, 0.0, 1.0)
    colours = np.clip(base * shade[:, None], 0, 255).astype(np.uint8)

    flat = img.reshape(-1, 3)
    flat[win_pix] = colours[win_face]
    covered_flat = np.zeros(h * w, dtype=bool)
    covered_flat[win_pix] = True
    covered = covered_flat.reshape(h, w)

    # A pixel the nearest surface missed shows the surface BEHIND it. Such a leak is a pixel much farther
    # than its 3x3 neighbourhood's nearest sample; treat it as a hole too and refill it from a good pixel.
    nearest = zbuf.reshape(h, w)
    slack = int(levels * min(1.0, 2.5 / cam.scale / max(dhi - dlo, 1e-12)))
    good = covered & ((nearest - ndimage.minimum_filter(nearest, size=3)) <= slack)

    closed = ndimage.binary_closing(covered, structure=np.ones((3, 3), dtype=bool))
    fill = closed & ~good
    if fill.any():
        _, (ni, nj) = ndimage.distance_transform_edt(~good, return_indices=True)
        img[fill] = img[ni[fill], nj[fill]]
    return img


def write_png(path: str | Path, rgb: Image) -> None:
    """Write an (H, W, 3) uint8 array as a PNG (numpy + zlib only; parent dirs are created)."""
    arr = np.ascontiguousarray(rgb, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise InvalidArgument(
            "rgb", f"shape {arr.shape}", ["an (H, W, 3) uint8 array"], command="prep"
        )
    h, w, _ = arr.shape
    rows = np.zeros((h, 1 + w * 3), dtype=np.uint8)  # filter byte 0 (None) + the row
    rows[:, 1:] = arr.reshape(h, w * 3)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------------------------
# text (PIL when importable)


@functools.cache
def _font(size: int) -> object | None:
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: fixed-size bitmap font only
        return ImageFont.load_default()


def has_text() -> bool:
    """True when PIL is importable, so captions and tick labels are drawn."""
    return _font(12) is not None


def text_size(text: str, size: int = 14) -> tuple[int, int]:
    """Pixel (width, height) `draw_text` will use; a character-count estimate without PIL."""
    font = _font(size)
    if font is None:
        return int(len(text) * size * 0.6), size
    from PIL import Image as PILImage
    from PIL import ImageDraw

    box = ImageDraw.Draw(PILImage.new("RGB", (1, 1))).textbbox((0, 0), text, font=font)  # type: ignore[arg-type]
    return int(box[2]), int(box[3])


def draw_text(
    img: Image,
    xy: tuple[float, float],
    text: str,
    color: RGB = (20, 20, 20),
    size: int = 14,
) -> None:
    """Draw `text` with its top-left at `xy` into `img` in place; no-op without PIL."""
    font = _font(size)
    if font is None or not text:
        return
    from PIL import Image as PILImage
    from PIL import ImageDraw

    pil = PILImage.fromarray(img)
    ImageDraw.Draw(pil).text((int(xy[0]), int(xy[1])), text, fill=color, font=font)  # type: ignore[arg-type]
    img[:] = np.asarray(pil)


# ---------------------------------------------------------------------------------------------
# 2-D millimetre canvas


def _polygon_mask(px: NDArray[np.float64], shape: tuple[int, int]) -> NDArray[np.bool_]:
    """Even-odd fill of a closed polygon (pixel coordinates, y down): pixels whose centre lies inside.

    Scanline crossings are toggled in a difference array, so it is vectorised and needs no raster library
    (skimage is avoided on purpose: mypy at python_version 3.10 cannot follow it into tifffile).
    """
    h, w = shape
    x0, y0 = px[:, 0], px[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    rows_c = np.arange(h) + 0.5
    lo = np.minimum(y0, y1)
    hi = np.maximum(y0, y1)
    rows, edges = np.nonzero(
        (rows_c[:, None] >= lo[None, :]) & (rows_c[:, None] < hi[None, :])
    )
    t = (rows_c[rows] - y0[edges]) / (y1[edges] - y0[edges])
    cols = np.clip(
        np.ceil(x0[edges] + t * (x1[edges] - x0[edges]) - 0.5).astype(np.int64), 0, w
    )
    toggles = np.bincount(rows * (w + 1) + cols, minlength=h * (w + 1)).reshape(
        h, w + 1
    )
    return np.asarray(np.cumsum(toggles, axis=1)[:, :w] % 2 == 1)


class Plot2D:
    """A canvas whose world coordinates are mm (x right, y up) fitted into `size` pixels.

    The fit rule: the bbox of the content plus `margin_px` all round, uniform scale, centred.
    """

    def __init__(
        self,
        lo: Point,
        hi: Point,
        size: tuple[int, int] = (900, 600),
        *,
        margin_px: int = 40,
        background: RGB = (255, 255, 255),
    ) -> None:
        self.width, self.height = _check_size(size)
        self.lo = (float(lo[0]), float(lo[1]))
        self.hi = (float(hi[0]), float(hi[1]))
        dx, dy = max(self.hi[0] - self.lo[0], 1e-9), max(self.hi[1] - self.lo[1], 1e-9)
        self.scale = min(
            (self.width - 2 * margin_px) / dx, (self.height - 2 * margin_px) / dy
        )
        self._cx, self._cy = (
            (self.lo[0] + self.hi[0]) / 2,
            (self.lo[1] + self.hi[1]) / 2,
        )
        self.canvas: Image = np.empty((self.height, self.width, 3), dtype=np.uint8)
        self.canvas[:] = np.asarray(background, dtype=np.uint8)

    def to_px(self, xy: Point) -> NDArray[np.float64]:
        """World (..., 2) mm -> pixel (..., 2) as continuous (x, y-down)."""
        a = np.asarray(xy, dtype=np.float64)
        out = np.empty_like(a)
        out[..., 0] = (a[..., 0] - self._cx) * self.scale + self.width / 2
        out[..., 1] = self.height / 2 - (a[..., 1] - self._cy) * self.scale
        return out

    def _poly_mask(self, pts: NDArray[np.float64]) -> NDArray[np.bool_]:
        return _polygon_mask(self.to_px(pts), (self.height, self.width))

    def fill_rings(
        self,
        rings: Sequence[NDArray[np.float64]],
        fill: RGB,
        outline: RGB | None = None,
        outline_px: int = 2,
    ) -> NDArray[np.bool_]:
        """Fill closed rings with the even-odd rule (inner rings are holes); returns the filled mask."""
        mask = np.zeros((self.height, self.width), dtype=bool)
        for ring in rings:
            if len(ring) >= 3:
                mask ^= self._poly_mask(np.asarray(ring, dtype=np.float64))
        self.canvas[mask] = fill
        if outline is not None:
            edge = mask & ~ndimage.binary_erosion(mask, iterations=outline_px)
            self.canvas[edge] = outline
        return mask

    def fill_polygon(self, pts: NDArray[np.float64], color: RGB) -> None:
        """Fill one polygon with a flat colour."""
        self.canvas[self._poly_mask(np.asarray(pts, dtype=np.float64))] = color

    def line(self, p0: Point, p1: Point, color: RGB, width: int = 2) -> None:
        """Straight line between two world points, `width` px thick."""
        a, b = self.to_px(p0), self.to_px(p1)
        n = int(max(abs(b - a).max(), 1.0) * 2) + 1
        t = np.linspace(0.0, 1.0, n)[:, None]
        pts = a + (b - a) * t
        lo, hi = -(width // 2), width - width // 2
        for dy in range(lo, hi):
            for dx in range(lo, hi):
                x = np.floor(pts[:, 0]).astype(np.int64) + dx
                y = np.floor(pts[:, 1]).astype(np.int64) + dy
                ok = (x >= 0) & (x < self.width) & (y >= 0) & (y < self.height)
                self.canvas[y[ok], x[ok]] = color

    def polyline(
        self, pts: NDArray[np.float64], color: RGB, width: int = 2, closed: bool = False
    ) -> None:
        """Connected lines through world points."""
        n = len(pts)
        for i in range(n if closed else n - 1):
            self.line(pts[i], pts[(i + 1) % n], color, width)

    def disc(self, centre: Point, radius_px: float, color: RGB) -> None:
        """Filled circle of `radius_px` pixels at a world point."""
        cx, cy = self.to_px(centre)
        yy, xx = np.ogrid[: self.height, : self.width]
        self.canvas[(xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2 <= radius_px**2] = color

    def arrow(
        self,
        p0: Point,
        p1: Point,
        color: RGB,
        width: int = 2,
        head_px: int = 12,
    ) -> None:
        """Line with a filled head at `p1`."""
        self.line(p0, p1, color, width)
        a, b = self.to_px(p0), self.to_px(p1)
        d = b - a
        norm = float(np.hypot(d[0], d[1]))
        if norm < 1e-6:
            return
        d /= norm
        perp = np.array([-d[1], d[0]])
        head = np.array(
            [
                b,
                b - d * head_px + perp * head_px * 0.45,
                b - d * head_px - perp * head_px * 0.45,
            ]
        )
        self.canvas[_polygon_mask(head, (self.height, self.width))] = color

    def text(
        self,
        xy_px: tuple[float, float],
        text: str,
        color: RGB = (20, 20, 20),
        size: int = 14,
    ) -> None:
        """Text with its top-left at a PIXEL position (no-op without PIL)."""
        draw_text(self.canvas, xy_px, text, color, size)

    def scale_bar(
        self, length_mm: float | None = None, color: RGB = (20, 20, 20)
    ) -> float:
        """Bottom-left bar of a round length (1/2/5 x 10^k mm, about a fifth of the width); returns that length."""
        if length_mm is None:
            raw = self.width * 0.2 / self.scale
            mag = 10 ** math.floor(math.log10(raw))
            length_mm = max(m * mag for m in (1, 2, 5) if m * mag <= raw)
        bar = int(round(length_mm * self.scale))
        x0, y0 = 16, self.height - 22
        self.canvas[y0 : y0 + 5, x0 : x0 + bar] = color
        self.canvas[y0 - 4 : y0 + 9, x0 : x0 + 2] = color
        self.canvas[y0 - 4 : y0 + 9, x0 + bar - 2 : x0 + bar] = color
        label = f"{length_mm:g} mm"
        _, th = text_size(label, 13)
        self.text((x0 + bar + 8, y0 + 2 - th // 2), label, color, 13)
        return float(length_mm)


# ---------------------------------------------------------------------------------------------
# sections

SECTION_FILL: RGB = (150, 185, 225)
SECTION_OUTLINE: RGB = (20, 40, 90)


def section_rings(
    mesh: trimesh.Trimesh,
    plane_origin: Sequence[float],
    plane_normal: Sequence[float],
    *,
    right: Sequence[float] | None = None,
    up: Sequence[float] | None = None,
) -> list[NDArray[np.float64]]:
    """Closed 2-D rings of the cut of `mesh` by a plane, in (right, up) mm relative to `plane_origin`.

    Default axes: `up` = +Z (+Y when the plane is horizontal), `right` = normal x up, so a cut through
    +Y-normal plane shows +X to the right. Pass both `right` and `up` to pick the axes explicitly.
    """
    normal = np.asarray(plane_normal, dtype=np.float64)
    if normal.shape != (3,) or not np.linalg.norm(normal) > 0:
        raise InvalidArgument(
            "plane_normal", repr(plane_normal), ["a non-zero 3-vector"], command="prep"
        )
    normal = normal / np.linalg.norm(normal)
    origin = np.asarray(plane_origin, dtype=np.float64)
    if (right is None) != (up is None):
        raise InvalidArgument(
            "right/up", "only one given", ["both or neither"], command="prep"
        )
    if right is None or up is None:
        ref = (
            np.array([0.0, 0.0, 1.0])
            if abs(normal[2]) < 0.9
            else np.array([0.0, 1.0, 0.0])
        )
        r_axis = np.cross(normal, ref)
        r_axis /= np.linalg.norm(r_axis)
        u_axis = np.cross(r_axis, normal)
    else:
        r_axis, u_axis = (
            np.asarray(right, dtype=np.float64),
            np.asarray(up, dtype=np.float64),
        )
    path = mesh.section(plane_origin=origin, plane_normal=normal)
    if path is None:
        return []
    rings: list[NDArray[np.float64]] = []
    for loop in path.discrete:
        rel = np.asarray(loop, dtype=np.float64) - origin
        rings.append(np.column_stack([rel @ r_axis, rel @ u_axis]))
    return rings


def render_section(
    mesh: trimesh.Trimesh,
    plane_origin: Sequence[float],
    plane_normal: Sequence[float],
    size: tuple[int, int] = (700, 700),
    *,
    right: Sequence[float] | None = None,
    up: Sequence[float] | None = None,
) -> Image:
    """Flat-colour filled cut of `mesh` with outline and a scale bar (a blank canvas when the plane misses it)."""
    rings = section_rings(mesh, plane_origin, plane_normal, right=right, up=up)
    if rings:
        pts = np.vstack(rings)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
    else:
        lo, hi = np.zeros(2), np.ones(2)
    plot = Plot2D(lo, hi, size)
    plot.fill_rings(rings, SECTION_FILL, SECTION_OUTLINE)
    plot.scale_bar()
    return plot.canvas
