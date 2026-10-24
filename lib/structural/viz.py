"""Stress picture: boundary triangles of every body coloured by von Mises, hotspots numbered.

Pillow only (a core dependency): an orthographic painter's-algorithm render of the (exaggerated)
deformed surface, a colour bar in MPa and a header with the verdict. It is a review aid, not a
CAD viewer: one fixed isometric view, flat shading, no anti-aliased silhouettes beyond 2x supersampling.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    import pathlib

    from structural.fea import BodyField

_STOPS = (
    (0.00, (49, 54, 149)),
    (0.25, (69, 117, 180)),
    (0.45, (116, 173, 209)),
    (0.60, (254, 224, 144)),
    (0.80, (244, 109, 67)),
    (1.00, (165, 0, 38)),
)


def _colour(value: float) -> tuple[int, int, int]:
    v = min(max(value, 0.0), 1.0)
    for (a, ca), (b, cb) in zip(_STOPS, _STOPS[1:], strict=False):
        if v <= b:
            t = (v - a) / (b - a) if b > a else 0.0
            return (
                int(ca[0] + t * (cb[0] - ca[0])),
                int(ca[1] + t * (cb[1] - ca[1])),
                int(ca[2] + t * (cb[2] - ca[2])),
            )
    return _STOPS[-1][1]


def _view_matrix(azimuth_deg: float = 35.0, elevation_deg: float = 28.0) -> np.ndarray:
    """Rows: screen right, screen up (model +Z points up), depth toward the viewer (larger = nearer)."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    rz = np.array([[math.cos(az), -math.sin(az), 0.0], [math.sin(az), math.cos(az), 0.0], [0.0, 0.0, 1.0]])
    tilt = np.array(
        [[1.0, 0.0, 0.0], [0.0, math.sin(el), math.cos(el)], [0.0, -math.cos(el), math.sin(el)]]
    )
    return tilt @ rz


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: no scalable default font
        return ImageFont.load_default()


def render_stress_png(
    fields: Sequence[BodyField],
    out: pathlib.Path,
    *,
    title: str,
    subtitle: str,
    hotspots: Sequence[dict[str, Any]],
    value_max: float | None = None,
    size: tuple[int, int] = (1200, 900),
) -> pathlib.Path:
    """Write the stress PNG for one load case and return its path."""
    width, height = size
    scale_ss = 2
    view = _view_matrix()
    all_nodes = np.vstack([f.body.mesh.p.T for f in fields])
    diag = float(np.linalg.norm(all_nodes.max(axis=0) - all_nodes.min(axis=0))) or 1.0
    max_disp = max(float(np.linalg.norm(f.u, axis=1).max()) for f in fields)
    exaggeration = 1.0 if max_disp <= 0.0 else min(0.04 * diag / max_disp, 200.0)
    exaggeration = max(exaggeration, 1.0)

    tris: list[np.ndarray] = []
    colours: list[np.ndarray] = []
    vm_peak = max(float(f.failure["von_mises"].max()) for f in fields)
    vmax = value_max if value_max and value_max > 0 else (vm_peak or 1.0)
    for f in fields:
        mesh = f.body.mesh
        node_sum = np.zeros(mesh.p.shape[1])
        node_count = np.zeros(mesh.p.shape[1])
        np.add.at(node_sum, mesh.t.ravel(), f.failure["von_mises"].ravel())
        np.add.at(node_count, mesh.t.ravel(), 1.0)
        node_vm = node_sum / np.maximum(node_count, 1.0)
        facets = mesh.facets[:, f.body.facet_ids]  # (3, nf)
        corner = mesh.p.T + exaggeration * f.u
        tris.append(corner[facets.T])  # (nf, 3, 3)
        colours.append(
            np.stack(
                [node_vm[facets[0]], node_vm[facets[1]], node_vm[facets[2]]], axis=1
            ).mean(axis=1)
        )
    tri = np.vstack(tris)
    value = np.concatenate(colours)
    if not (value_max and value_max > 0):  # a singular spike must not flatten the whole picture
        vmax = max(float(np.percentile(value, 99.0)), 0.15 * vm_peak, 1e-9)
    centre = (tri.reshape(-1, 3).max(axis=0) + tri.reshape(-1, 3).min(axis=0)) / 2.0
    rotated = (tri - centre) @ view.T
    depth = rotated[:, :, 2].mean(axis=1)
    e1, e2 = rotated[:, 1] - rotated[:, 0], rotated[:, 2] - rotated[:, 0]
    normal = np.cross(e1, e2)
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
    shade = 0.55 + 0.45 * np.abs(normal @ np.array([0.3, -0.5, 0.81]))
    extent = float(np.abs(rotated[:, :, :2]).max()) or 1.0
    px = 0.78 * min(width, height) / 2.0 / extent * scale_ss
    cx, cy = width * scale_ss / 2.0 * 0.92, height * scale_ss / 2.0 * 1.02

    image = Image.new("RGB", (width * scale_ss, height * scale_ss), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    for k in np.argsort(
        depth
    ):  # far to near: +z of the view points away from the viewer
        poly = [(cx + px * p[0], cy - px * p[1]) for p in rotated[k]]
        r, g, b = _colour(float(value[k]) / vmax)
        s = float(shade[k])
        draw.polygon(
            poly, fill=(int(r * s), int(g * s), int(b * s)), outline=(60, 60, 60)
        )

    def project(point: np.ndarray) -> tuple[float, float]:
        q = (point - centre) @ view.T
        return cx + px * q[0], cy - px * q[1]

    font = _font(15 * scale_ss)
    for i, spot in enumerate(hotspots, 1):
        x, y = project(np.asarray(spot["xyz"], dtype=float))
        rad = 9 * scale_ss
        draw.ellipse(
            (x - rad, y - rad, x + rad, y + rad),
            outline=(0, 0, 0),
            width=3 * scale_ss // 2,
            fill=(255, 255, 255),
        )
        draw.text((x, y), str(i), fill=(0, 0, 0), font=font, anchor="mm")

    # colour bar
    bar_x, bar_y, bar_w, bar_h = (
        (width - 90) * scale_ss,
        120 * scale_ss,
        28 * scale_ss,
        (height - 240) * scale_ss,
    )
    for row in range(bar_h):
        r, g, b = _colour(1.0 - row / bar_h)
        draw.rectangle(
            (bar_x, bar_y + row, bar_x + bar_w, bar_y + row + 1), fill=(r, g, b)
        )
    draw.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), outline=(40, 40, 40))
    small = _font(13 * scale_ss)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        ty = bar_y + bar_h * (1.0 - frac)
        draw.text(
            (bar_x + bar_w + 6 * scale_ss, ty),
            f"{vmax * frac:.3g}",
            fill=(0, 0, 0),
            font=small,
            anchor="lm",
        )
    draw.text(
        (bar_x, bar_y - 16 * scale_ss),
        "von Mises MPa",
        fill=(0, 0, 0),
        font=small,
        anchor="lm",
    )
    draw.text(
        (24 * scale_ss, 22 * scale_ss),
        title,
        fill=(0, 0, 0),
        font=_font(20 * scale_ss),
        anchor="lm",
    )
    draw.text(
        (24 * scale_ss, 52 * scale_ss),
        subtitle,
        fill=(60, 60, 60),
        font=small,
        anchor="lm",
    )
    note = (
        f"deformation x{exaggeration:.0f}"
        if exaggeration > 1.0
        else "deformation true scale"
    )
    draw.text(
        (24 * scale_ss, (height - 24) * scale_ss),
        note,
        fill=(60, 60, 60),
        font=small,
        anchor="lm",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    image.resize((width, height), Image.Resampling.LANCZOS).save(out, format="PNG")
    return out
