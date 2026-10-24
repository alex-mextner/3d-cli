"""viz.py — PNGs of an assembly: shaded views, exploded views and section highlights, every part in its own colour.

WHAT: a small software renderer (numpy + Pillow only; no GL, no pyvista) so every assembly report can carry a
  picture without extra installs:
    - `render_view`     orthographic shaded view of all parts (painter's algorithm), overlap regions in red,
                        optional per-part offsets (the exploded view);
    - `render_section`  axis-aligned cross-section through an overlap region: each part's cut face filled in its colour,
                        the overlap region outlined and filled red, zoomed to the region and dimensioned.
  Colours come ONLY from `parts.assign_styles` (deterministic per part name + role, stand-ins muted ghost) and every
  image carries the legend strip (`parts.legend`): no two parts share a colour, runs are identical.

INVARIANTS: images are produced at 2x and downsampled (smooth edges); a part drawn ghost stays visible (alpha 0.35);
  nothing here modifies a mesh.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageChops, ImageDraw, ImageFont

from assembly.scene import Scene
from parts import PartStyle, assign_styles

RED = (230, 30, 30)
BG = (246, 246, 248)
INK = (30, 30, 34)
_VIEWS: dict[str, tuple[float, float]] = {  # azimuth, elevation (deg) of the camera
    "iso": (35.0, 28.0),
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "left": (-90.0, 0.0),
    "right": (90.0, 0.0),
    "top": (0.0, 89.9),
}
VIEW_NAMES = tuple(_VIEWS)


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no scalable default
        return ImageFont.load_default()


def hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _camera(view: str) -> Any:
    """Rows = screen right, screen up, towards the viewer (a rotation matrix for a Z-up world)."""
    az, el = _VIEWS[view]
    a, e = math.radians(az), math.radians(el)
    # camera sits at azimuth `az` from -Y (front) towards +X, elevated `el`
    towards = np.array(
        [math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)]
    )
    right = np.cross([0.0, 0.0, 1.0], towards)
    if np.linalg.norm(right) < 1e-6:
        right = np.array([1.0, 0.0, 0.0])
    right = right / np.linalg.norm(right)
    up = np.cross(towards, right)
    return np.vstack([right, up, towards])


def legend_strip(
    styles: Mapping[str, PartStyle], width: int, scale: int = 2
) -> Image.Image:
    """The colour legend: a swatch + `name (role)` per part, wrapped to the image width."""
    font = _font(13 * scale)
    pad, sw, row_h = 10 * scale, 14 * scale, 22 * scale
    items = sorted(styles.values(), key=lambda s: s.name)
    x, y = pad, pad
    placed: list[tuple[int, int, PartStyle, str]] = []
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for st in items:
        label = f"{st.name} ({st.role}{', ghost' if st.style == 'ghost' else ''})"
        w = sw + 6 * scale + int(probe.textlength(label, font=font)) + 18 * scale
        if x + w > width - pad and x > pad:
            x, y = pad, y + row_h
        placed.append((x, y, st, label))
        x += w
    img = Image.new("RGB", (width, y + row_h + pad), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for px, py, st, label in placed:
        draw.rectangle([px, py, px + sw, py + sw], fill=hex_rgb(st.color), outline=INK)
        draw.text((px + sw + 6 * scale, py - 1 * scale), label, fill=INK, font=font)
    return img


def _shade(normals: Any, light: Any, base: tuple[int, int, int]) -> Any:
    lam = np.clip(normals @ light, 0.0, 1.0)
    k = 0.45 + 0.55 * lam
    return np.clip(np.array(base)[None, :] * k[:, None], 0, 255).astype(np.uint8)


def render_view(
    scene: Scene,
    out: str | Path,
    *,
    view: str = "iso",
    styles: Mapping[str, PartStyle] | None = None,
    offsets: Mapping[str, Sequence[float]] | None = None,
    highlights: Sequence[trimesh.Trimesh] = (),
    size: tuple[int, int] = (1000, 760),
    title: str | None = None,
) -> Path:
    """Shaded orthographic view of the whole assembly with overlap regions (`highlights`) in red."""
    if view not in _VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEW_NAMES)}")
    styles = styles or assign_styles([p.part for p in scene.spec.parts.values()])
    scale = 2
    rot = _camera(view)
    light = rot @ (np.array([-0.4, -0.6, 0.7]) / np.linalg.norm([-0.4, -0.6, 0.7]))
    light = np.array([light[0], light[1], light[2]])
    offs = {k: np.asarray(v, dtype=float) for k, v in (offsets or {}).items()}

    tris: list[
        tuple[Any, Any, Any, float]
    ] = []  # (screen xy tris, depth, colour, alpha)
    for name, body in scene.bodies.items():
        st = styles[name]
        verts = np.asarray(body.mesh.vertices) + offs.get(name, np.zeros(3))
        cam = verts @ rot.T
        faces = np.asarray(body.mesh.faces)
        nrm = np.asarray(body.mesh.face_normals) @ rot.T
        tris.append(
            (
                cam[faces][:, :, :2],
                cam[faces][:, :, 2].mean(axis=1),
                _shade(nrm, light, hex_rgb(st.color)),
                st.alpha,
            )
        )
    hl: list[tuple[Any, Any]] = []
    for mesh in highlights:
        cam = np.asarray(mesh.vertices) @ rot.T
        hl.append(
            (
                cam[np.asarray(mesh.faces)][:, :, :2],
                cam[np.asarray(mesh.faces)][:, :, 2].mean(axis=1),
            )
        )

    allxy = np.concatenate([t[0].reshape(-1, 2) for t in tris])
    lo, hi = allxy.min(axis=0), allxy.max(axis=0)
    w, h = size[0] * scale, size[1] * scale
    legend = legend_strip(styles, w, scale)
    avail_h = h - legend.height - (30 * scale if title else 0)
    k = min(
        (w * 0.92) / max(hi[0] - lo[0], 1e-6),
        (avail_h * 0.92) / max(hi[1] - lo[1], 1e-6),
    )
    cx, cy = (lo + hi) / 2.0

    def to_px(xy: Any) -> Any:
        x = (xy[..., 0] - cx) * k + w / 2.0
        y = avail_h / 2.0 - (xy[..., 1] - cy) * k + (30 * scale if title else 0)
        return np.stack([x, y], axis=-1)

    canvas = Image.new("RGB", (w, h), BG)
    draw = ImageDraw.Draw(canvas)
    solids = [(xy, d, c) for xy, d, c, a in tris if a >= 0.999]
    ghosts = [(xy, d, c) for xy, d, c, a in tris if a < 0.999]
    for xy, _depth, col in _sorted_tris(solids):
        draw.polygon([tuple(p) for p in to_px(xy)], fill=tuple(int(v) for v in col))
    if ghosts:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(layer)
        alpha = int(255 * min(a for *_, a in tris if a < 0.999))
        for xy, _depth, col in _sorted_tris(ghosts):
            gd.polygon(
                [tuple(p) for p in to_px(xy)], fill=(*(int(v) for v in col), alpha)
            )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")
        draw = ImageDraw.Draw(canvas)
    for xy, _ in hl:
        for tri in to_px(xy):
            draw.polygon([tuple(p) for p in tri], fill=RED, outline=(120, 0, 0))
    if title:
        draw.text((12 * scale, 6 * scale), title, fill=INK, font=_font(16 * scale))
    canvas.paste(legend, (0, h - legend.height))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.resize(size, Image.Resampling.LANCZOS).save(out)
    return out


def _sorted_tris(items: list[tuple[Any, Any, Any]]) -> Any:
    """Far-to-near triangles of all parts (painter's algorithm)."""
    if not items:
        return
    xy = np.concatenate([i[0] for i in items])
    depth = np.concatenate([i[1] for i in items])
    col = np.concatenate([i[2] for i in items])
    for idx in np.argsort(depth):
        yield xy[idx], depth[idx], col[idx]


def _section_axes(
    depth_axis: Sequence[float], extent: Sequence[float]
) -> tuple[int, int, int]:
    """Pick the plane normal axis `n` (and the in-plane axes u, v): the plane that contains the depth axis best."""
    order = sorted(range(3), key=lambda i: (abs(depth_axis[i]), -extent[i]))
    n = order[0]
    u, v = [i for i in range(3) if i != n]
    # horizontal axis: the one the depth is measured along, so the thin direction is visible
    if abs(depth_axis[v]) > abs(depth_axis[u]):
        u, v = v, u
    return n, u, v


def _fill_even_odd(size: tuple[int, int], loops: Sequence[Any]) -> Image.Image:
    mask = Image.new("L", size, 0)
    for loop in loops:
        tmp = Image.new("L", size, 0)
        ImageDraw.Draw(tmp).polygon([tuple(p) for p in loop], fill=255)
        mask = ImageChops.logical_xor(mask.convert("1"), tmp.convert("1")).convert("L")
    return mask


def render_section(
    scene: Scene,
    region: Mapping[str, Any],
    out: str | Path,
    *,
    styles: Mapping[str, PartStyle] | None = None,
    region_mesh: trimesh.Trimesh | None = None,
    size: tuple[int, int] = (1000, 760),
    title: str | None = None,
) -> Path:
    """Cross-section through the centre of an overlap region: parts in their colours, the overlap in red."""
    styles = styles or assign_styles([p.part for p in scene.spec.parts.values()])
    centre = np.asarray(region["center"], dtype=float)
    lo = np.asarray(region["bbox"]["min"], dtype=float)
    hi = np.asarray(region["bbox"]["max"], dtype=float)
    extent = hi - lo
    n, u, v = _section_axes(region["depth_axis"], extent)
    normal = np.zeros(3)
    normal[n] = 1.0
    scale = 2
    w, h = size[0] * scale, size[1] * scale
    legend = legend_strip(styles, w, scale)
    avail_h = h - legend.height - 30 * scale
    span = max(float(extent[u]), float(extent[v]), 1.0)
    half = max(span * 2.5, 6.0)
    ctr = np.array([centre[u], centre[v]])
    k = min((w * 0.94) / (2 * half), (avail_h * 0.94) / (2 * half))

    def px(p2: Any) -> Any:
        return np.stack(
            [
                (p2[:, 0] - ctr[0]) * k + w / 2.0,
                avail_h / 2.0 - (p2[:, 1] - ctr[1]) * k + 30 * scale,
            ],
            axis=-1,
        )

    canvas = Image.new("RGB", (w, h), BG)
    order = sorted(scene.bodies, key=lambda nme: -float(scene.bodies[nme].mesh.volume))
    for name in order:
        body = scene.bodies[name]
        sec = body.mesh.section(plane_origin=centre, plane_normal=normal)
        if sec is None:
            continue
        loops = [px(np.asarray(d)[:, [u, v]]) for d in sec.discrete if len(d) >= 3]
        mask = _fill_even_odd((w, h), loops)
        colour = hex_rgb(styles[name].color)
        alpha = styles[name].alpha
        fill = Image.new("RGB", (w, h), colour)
        canvas = Image.composite(Image.blend(canvas, fill, alpha), canvas, mask)
        outline = ImageDraw.Draw(canvas)
        for loop in loops:
            outline.line(
                [tuple(p) for p in loop] + [tuple(loop[0])], fill=INK, width=scale
            )
    draw = ImageDraw.Draw(canvas)
    if region_mesh is not None:
        sec = region_mesh.section(plane_origin=centre, plane_normal=normal)
        if sec is not None:
            for d in sec.discrete:
                if len(d) >= 3:
                    loop = px(np.asarray(d)[:, [u, v]])
                    draw.polygon(
                        [tuple(p) for p in loop], fill=RED, outline=(120, 0, 0)
                    )
    # zoom marker + caption
    cx, cy = px(ctr[None, :])[0]
    r = 14 * scale
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=RED, width=2 * scale)
    axis_names = "XYZ"
    caption = title or (
        f"{region.get('a')} x {region.get('b')}: depth {region['depth_mm']:.3f} mm, "
        f"{region['volume_mm3']:.2f} mm3 at ({centre[0]:.2f}, {centre[1]:.2f}, {centre[2]:.2f}); "
        f"section {axis_names[n]}={centre[n]:.2f}, {axis_names[u]} right, {axis_names[v]} up, {2 * half:.1f} mm wide"
    )
    draw.text((12 * scale, 6 * scale), caption, fill=INK, font=_font(15 * scale))
    # scale bar
    bar = 1.0 if half < 10 else 5.0
    bx, by = 12 * scale, avail_h + 30 * scale - 12 * scale
    draw.line([(bx, by), (bx + bar * k, by)], fill=INK, width=3 * scale)
    draw.text((bx, by - 18 * scale), f"{bar:g} mm", fill=INK, font=_font(13 * scale))
    canvas.paste(legend, (0, h - legend.height))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.resize(size, Image.Resampling.LANCZOS).save(out)
    return out


def explode_offsets(
    scene: Scene, factor: float = 1.0
) -> dict[str, tuple[float, float, float]]:
    """Per-part offsets that pull every mover (and what it carries) back along its insertion axis."""
    offs: dict[str, np.ndarray] = {n: np.zeros(3) for n in scene.bodies}
    for mate in scene.spec.mates:
        ins = mate.insertion
        if ins is None:
            continue
        shift = -np.asarray(ins.axis) * ins.distance_mm * factor
        for name in (ins.part, *ins.carry):
            offs[name] = offs[name] + shift
    return {n: (float(v[0]), float(v[1]), float(v[2])) for n, v in offs.items()}
