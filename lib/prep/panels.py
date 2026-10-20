"""panels.py — the proof-pack images: heatmaps, diffs, region maps, charts and tables as RGB arrays.

WHAT: composition on top of `raster.py` — `contact_sheet` tiles images with captions; `overhang_heatmap`,
  `before_after`, `region_map` colour a mesh per face and render it; `layer_band_plot`, `stability_diagram`,
  `profile_with_slab`, `verdict_matrix` draw the 2-D charts the step reports need.
WHY: each proof the Chekhov print needed (overhangs, what the repair added, which region is which colour,
  layer heights, tip-over margin, phone fit) was a one-off script; these are the reusable primitives so the
  orchestrator wires step results into images in minutes.
INVARIANTS:
  * Every function returns plain (H, W, 3) uint8 arrays (or a list of them); nothing writes files.
  * Overhang angle is measured from VERTICAL, the slicer convention: a vertical wall is 0 deg, a flat
    ceiling facing down is 90 deg. Only down-facing faces can be overhangs; faces whose three vertices lie on
    the bed (lowest Z within `bed_tol_mm`) are bed contact, never an overhang.
  * Difference masks compare SURFACES, not vertices: the other mesh is sampled densely enough that the
    sample spacing is below the tolerance, and the effective tolerance is `max(tol, spacing)` so a face that
    merely lies on the other surface is never reported. Sample count is capped at 1M, so on a large model
    the spacing can exceed the default tolerance; the tolerance actually used is `DiffMasks.tolerance_mm`.
  * Caption and title rows are reserved even when text cannot be drawn (no PIL); legend entries wrap by their
    measured text width, so only the glyphs and the legend row count differ between machines.
  * Verdict colours: PASS green, WARN/RISK amber, FAIL red, SKIP grey.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points

from errors import InvalidArgument
from prep.raster import (
    RGB,
    Image,
    Plot2D,
    View,
    draw_text,
    render_mesh,
    section_rings,
    text_size,
)

GREEN: RGB = (70, 170, 90)
YELLOW: RGB = (240, 200, 40)
RED: RGB = (215, 40, 40)
BED_GREY: RGB = (150, 150, 150)
BLUE: RGB = (40, 90, 220)
NEUTRAL: RGB = (200, 200, 205)
INK: RGB = (20, 20, 20)
WHITE: RGB = (255, 255, 255)

VERDICT_COLORS: dict[str, RGB] = {
    "PASS": (90, 180, 100),
    "WARN": (240, 180, 40),
    "RISK": (240, 180, 40),
    "FAIL": (210, 60, 60),
    "SKIP": (190, 190, 190),
}

#: strong colours need near-flat shading to stay recognisable
HEAT_AMBIENT = 0.6
CAPTION_H = 24
TITLE_H = 36
LEGEND_H = 30
GAP = 8


# ---------------------------------------------------------------------------------------------
# generic composition


def add_legend(img: Image, entries: Sequence[tuple[str, RGB]]) -> Image:
    """`img` with a legend appended below it: a colour swatch and a label per entry.

    Entries wrap onto further `LEGEND_H` rows when they do not fit the width, so none is ever clipped.
    """
    w = img.shape[1]
    slots: list[tuple[int, int, str, RGB]] = []
    row, x = 0, 10
    for label, colour in entries:
        tw, _ = text_size(label, 13)
        if x > 10 and x + 22 + tw > w - 10:
            row, x = row + 1, 10
        slots.append((row, x, label, colour))
        x += 22 + tw + 22
    strip = np.empty(((row + 1) * LEGEND_H, w, 3), dtype=np.uint8)
    strip[:] = WHITE
    for r, sx, label, colour in slots:
        y = r * LEGEND_H
        strip[y + 7 : y + 23, sx : sx + 16] = colour
        draw_text(strip, (sx + 22, y + 8), label, INK, 13)
    return np.concatenate([img, strip], axis=0)


def contact_sheet(
    images: Sequence[Image], captions: Sequence[str], cols: int, title: str
) -> Image:
    """Tile `images` row by row in `cols` columns, each with its caption underneath and `title` on top.

    Cells are as large as the biggest image; smaller images are centred in their cell.
    """
    if not images:
        raise InvalidArgument("images", "empty", ["at least one image"], command="prep")
    if len(captions) != len(images):
        raise InvalidArgument(
            "captions",
            f"{len(captions)} for {len(images)} images",
            ["one caption per image"],
            command="prep",
        )
    if cols < 1:
        raise InvalidArgument("cols", str(cols), ["an integer >= 1"], command="prep")
    cols = min(cols, len(images))
    rows = math.ceil(len(images) / cols)
    cell_w = max(im.shape[1] for im in images)
    cell_h = max(im.shape[0] for im in images)
    top = TITLE_H if title else 0
    sheet = np.empty(
        (top + rows * (cell_h + CAPTION_H + GAP) + GAP, cols * (cell_w + GAP) + GAP, 3),
        dtype=np.uint8,
    )
    sheet[:] = WHITE
    if title:
        draw_text(sheet, (GAP + 2, 7), title, INK, 20)
    for i, (im, cap) in enumerate(zip(images, captions, strict=True)):
        r, c = divmod(i, cols)
        x0 = GAP + c * (cell_w + GAP) + (cell_w - im.shape[1]) // 2
        y0 = top + GAP + r * (cell_h + CAPTION_H + GAP) + (cell_h - im.shape[0]) // 2
        sheet[y0 : y0 + im.shape[0], x0 : x0 + im.shape[1]] = im
        draw_text(
            sheet,
            (
                GAP + c * (cell_w + GAP) + 2,
                top + GAP + r * (cell_h + CAPTION_H + GAP) + cell_h + 3,
            ),
            cap,
            INK,
            14,
        )
    return sheet


# ---------------------------------------------------------------------------------------------
# overhang heatmap

#: class ids returned by `overhang_face_classes`
OK, WARN, OVERHANG, BED = 0, 1, 2, 3
_CLASS_COLORS: tuple[RGB, ...] = (GREEN, YELLOW, RED, BED_GREY)


def overhang_face_classes(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = 45.0,
    warn_margin_deg: float = 10.0,
    bed_tol_mm: float = 0.05,
) -> NDArray[np.int8]:
    """Per-face class: OK, WARN (within `warn_margin_deg` below the limit), OVERHANG (> `angle_deg`), BED."""
    if not 0.0 < angle_deg < 90.0:
        raise InvalidArgument(
            "angle_deg", str(angle_deg), ["a number in (0, 90)"], command="prep"
        )
    nz = np.asarray(mesh.face_normals)[:, 2]
    theta = np.degrees(
        np.arcsin(np.clip(-nz, 0.0, 1.0))
    )  # 0 for up-facing and vertical faces
    cls = np.full(len(nz), OK, dtype=np.int8)
    cls[theta > angle_deg - warn_margin_deg] = WARN
    cls[theta > angle_deg] = OVERHANG
    zmin = float(mesh.bounds[0][2])
    on_bed = (mesh.vertices[mesh.faces][:, :, 2] <= zmin + bed_tol_mm).all(axis=1) & (
        nz < 0
    )
    cls[on_bed] = BED
    return cls


def overhang_heatmap(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = 45.0,
    warn_margin_deg: float = 10.0,
    bed_tol_mm: float = 0.05,
    views: Sequence[View] = ("low", "iso", "bottom"),
    size: tuple[int, int] = (700, 700),
) -> list[Image]:
    """One render per view with faces coloured by overhang class, each with a legend (area shares included)."""
    cls = overhang_face_classes(
        mesh,
        angle_deg=angle_deg,
        warn_margin_deg=warn_margin_deg,
        bed_tol_mm=bed_tol_mm,
    )
    colours = np.asarray(_CLASS_COLORS, dtype=np.uint8)[cls]
    area = np.asarray(mesh.area_faces)
    share = [
        100.0 * float(area[cls == k].sum()) / max(float(area.sum()), 1e-12)
        for k in range(4)
    ]
    legend = [
        (f"ok {share[OK]:.0f}%", GREEN),
        (
            f"{angle_deg - warn_margin_deg:g}-{angle_deg:g} deg {share[WARN]:.0f}%",
            YELLOW,
        ),
        (f">{angle_deg:g} deg overhang {share[OVERHANG]:.0f}%", RED),
        (f"bed contact {share[BED]:.0f}%", BED_GREY),
    ]
    return [
        add_legend(
            render_mesh(
                mesh, view=v, size=size, face_colors=colours, ambient=HEAT_AMBIENT
            ),
            legend,
        )
        for v in views
    ]


# ---------------------------------------------------------------------------------------------
# before / after


@dataclass(frozen=True)
class DiffMasks:
    """Faces of `after` with no `before` surface within tolerance (added) and vice versa (removed)."""

    added: NDArray[np.bool_]
    removed: NDArray[np.bool_]
    tolerance_mm: float  # effective tolerance actually used (>= the requested one)


def _far_from(
    mesh: trimesh.Trimesh, other: trimesh.Trimesh, tol_mm: float, seed: int
) -> tuple[NDArray[np.bool_], float]:
    """Mask of `mesh` faces whose centroid or any vertex is farther than the tolerance from `other`'s surface."""
    area = float(other.area)
    n = int(np.clip(area / (tol_mm / 3.0) ** 2, 20_000, 1_000_000))
    spacing = math.sqrt(area / n)
    eff = max(tol_mm, spacing)
    rng = np.random.default_rng(seed)
    cum = np.cumsum(other.area_faces)
    fi = np.minimum(np.searchsorted(cum, rng.random(n) * cum[-1]), len(cum) - 1)
    r1 = np.sqrt(rng.random(n))
    r2 = rng.random(n)
    tri = other.vertices[other.faces[fi]]
    samples = (
        (1 - r1)[:, None] * tri[:, 0]
        + (r1 * (1 - r2))[:, None] * tri[:, 1]
        + (r1 * r2)[:, None] * tri[:, 2]
    )
    tree = cKDTree(samples)
    d_vert = tree.query(np.asarray(mesh.vertices), workers=-1)[0]
    d_cent = tree.query(np.asarray(mesh.triangles_center), workers=-1)[0]
    worst = np.maximum(d_vert[mesh.faces].max(axis=1), d_cent)
    return worst > eff, eff


def diff_face_masks(
    before: trimesh.Trimesh,
    after: trimesh.Trimesh,
    *,
    tol_mm: float = 0.3,
    seed: int = 0,
) -> DiffMasks:
    """Which faces of `after` are new material and which faces of `before` are gone."""
    if tol_mm <= 0:
        raise InvalidArgument(
            "tol_mm", str(tol_mm), ["a positive number of mm"], command="prep"
        )
    added, tol_a = _far_from(after, before, tol_mm, seed)
    removed, tol_r = _far_from(before, after, tol_mm, seed)
    return DiffMasks(added, removed, max(tol_a, tol_r))


def _flat(
    mesh: trimesh.Trimesh, mask: NDArray[np.bool_], hot: RGB
) -> NDArray[np.uint8]:
    colours = np.empty((len(mesh.faces), 3), dtype=np.uint8)
    colours[:] = NEUTRAL
    colours[mask] = hot
    return colours


def before_after(
    before: trimesh.Trimesh,
    after: trimesh.Trimesh,
    *,
    views: Sequence[View] = ("iso", "low"),
    tol_mm: float = 0.3,
    highlight_diff: bool = True,
    size: tuple[int, int] = (600, 600),
) -> Image:
    """Sheet with `before` (removed faces blue) left and `after` (added faces red) right, one row per view."""
    if highlight_diff:
        masks = diff_face_masks(before, after, tol_mm=tol_mm)
        col_before = _flat(before, masks.removed, BLUE)
        col_after = _flat(after, masks.added, RED)
        title = f"before / after - red added, blue removed (tolerance {masks.tolerance_mm:.2f} mm)"
    else:
        col_before = _flat(before, np.zeros(len(before.faces), dtype=bool), BLUE)
        col_after = _flat(after, np.zeros(len(after.faces), dtype=bool), RED)
        title = "before / after"
    images: list[Image] = []
    captions: list[str] = []
    for v in views:
        label = v if isinstance(v, str) else f"az {v[0]:g} el {v[1]:g}"
        images.append(
            render_mesh(
                before, view=v, size=size, face_colors=col_before, ambient=HEAT_AMBIENT
            )
        )
        captions.append(f"before - {label}")
        images.append(
            render_mesh(
                after, view=v, size=size, face_colors=col_after, ambient=HEAT_AMBIENT
            )
        )
        captions.append(f"after - {label}")
    return contact_sheet(images, captions, 2, title)


# ---------------------------------------------------------------------------------------------
# region map


def region_map(
    mesh: trimesh.Trimesh,
    labels: NDArray[np.integer],
    palette: Mapping[int, RGB],
    names: Mapping[int, str],
    *,
    views: Sequence[View] = ("iso", "back", "top"),
    size: tuple[int, int] = (700, 700),
) -> list[Image]:
    """Renders with every face coloured by its region label; labels missing from `palette` are neutral grey.

    The legend lists the labels that occur, named from `names` (the number when unnamed).
    """
    lab = np.asarray(labels)
    if lab.shape != (len(mesh.faces),):
        raise InvalidArgument(
            "labels",
            f"shape {lab.shape}",
            [f"one integer label per face ({len(mesh.faces)})"],
            command="prep",
        )
    colours = np.empty((len(lab), 3), dtype=np.uint8)
    colours[:] = NEUTRAL
    present = sorted(int(k) for k in np.unique(lab))
    for k in present:
        if k in palette:
            colours[lab == k] = palette[k]
    legend = [(names.get(k, str(k)), palette.get(k, NEUTRAL)) for k in present]
    return [
        add_legend(
            render_mesh(
                mesh, view=v, size=size, face_colors=colours, ambient=HEAT_AMBIENT
            ),
            legend,
        )
        for v in views
    ]


# ---------------------------------------------------------------------------------------------
# layer bands


def _nice_ticks(lo: float, hi: float, target: int = 6) -> list[float]:
    span = max(hi - lo, 1e-12)
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9)
    last = math.floor(hi / step + 1e-9)
    return [round(i * step, 10) for i in range(first, last + 1)]


def layer_band_plot(
    z_tops: Sequence[float],
    heights: Sequence[float],
    *,
    mix_ranges: Sequence[tuple[float, float]] = (),
    size: tuple[int, int] = (900, 360),
) -> Image:
    """Layer height (y) against Z (x) as a step line; `mix_ranges` (z0, z1) are shaded as colour-mix zones."""
    if len(z_tops) == 0 or len(z_tops) != len(heights):
        raise InvalidArgument(
            "z_tops/heights",
            f"{len(z_tops)} vs {len(heights)}",
            ["equal-length non-empty sequences"],
            command="prep",
        )
    zt = np.asarray(z_tops, dtype=np.float64)
    hh = np.asarray(heights, dtype=np.float64)
    zb = zt - hh
    w, h = size
    left, right, top, bottom = 70, 24, 34, 48
    # Pixel-space canvas: Plot2D with scale 1 and y flipped by hand, so each axis scales independently.
    plot = Plot2D((0, 0), (w, h), size, margin_px=0)
    z0, z1 = float(zb.min()), float(zt.max())
    h1 = float(hh.max()) * 1.2
    pw, ph = w - left - right, h - top - bottom

    def X(z: float) -> float:
        return left + (z - z0) / max(z1 - z0, 1e-12) * pw

    def Y(v: float) -> float:
        return h - bottom - v / max(h1, 1e-12) * ph

    for a, b in mix_ranges:
        xa, xb = int(round(X(max(a, z0)))), int(round(X(min(b, z1))))
        if xb > xa:
            plot.canvas[top : h - bottom, xa:xb] = (252, 226, 190)
    for tz in _nice_ticks(z0, z1):
        plot.line((X(tz), h - Y(0)), (X(tz), h - Y(h1)), (225, 225, 225), 1)
        plot.line((X(tz), h - Y(0)), (X(tz), h - Y(0) - 5), INK, 1)
        plot.text((X(tz) - 10, h - bottom + 8), f"{tz:g}", INK, 12)
    for tv in _nice_ticks(0.0, h1, 5):
        plot.line((left, h - Y(tv)), (w - right, h - Y(tv)), (225, 225, 225), 1)
        plot.line((left - 5, h - Y(tv)), (left, h - Y(tv)), INK, 1)
        plot.text((8, Y(tv) - 8), f"{tv:g}", INK, 12)
    plot.line((left, h - Y(0)), (w - right, h - Y(0)), INK, 2)
    plot.line((left, h - Y(0)), (left, h - Y(h1)), INK, 2)
    pts = [(X(float(zb[0])), h - Y(float(hh[0])))]
    for i in range(len(zt)):
        pts.append((X(float(zb[i])), h - Y(float(hh[i]))))
        pts.append((X(float(zt[i])), h - Y(float(hh[i]))))
    plot.polyline(np.asarray(pts), (30, 90, 200), 2)
    plot.text((left, 8), "layer height (mm) vs Z (mm)", INK, 14)
    plot.text((w - right - 150, h - 22), "Z (mm)", INK, 13)
    if mix_ranges:
        plot.canvas[10:22, w - right - 200 : w - right - 186] = (252, 226, 190)
        plot.text((w - right - 180, 8), "colour-mix zone", INK, 13)
    return plot.canvas


# ---------------------------------------------------------------------------------------------
# stability


COM_COLOR: RGB = (220, 40, 40)
LOAD_COM_COLOR: RGB = (30, 150, 70)
SUPPORT_FILL: RGB = (205, 222, 245)
SUPPORT_OUTLINE: RGB = (30, 60, 120)
ARROW_COLOR: RGB = (
    110,
    40,
    150,
)  # non-grey so it cannot be confused with anti-aliased black text


def stability_diagram(
    support_polygon_xy: NDArray[np.float64] | Sequence[Sequence[float]],
    com_xy: Sequence[float],
    load_com_xy: Sequence[float] | None = None,
    tip_margin_mm: float | None = None,
    size: tuple[int, int] = (700, 700),
) -> Image:
    """Top-down plan: support polygon shaded, CoM (red disc) and loaded CoM (green disc), margin arrow.

    The arrow runs from the governing CoM (the loaded one when given) to the nearest polygon edge and is
    labelled with `tip_margin_mm`; no arrow is drawn when `tip_margin_mm` is None. The canvas is fitted to the
    polygon and the CoMs, so a CoM outside the polygon is visible (drawn in the same colour; the arrow shows
    the distance to the edge it would tip over).
    """
    poly = np.asarray(support_polygon_xy, dtype=np.float64)
    if poly.ndim != 2 or poly.shape[1] != 2 or len(poly) < 3:
        raise InvalidArgument(
            "support_polygon_xy",
            f"shape {poly.shape}",
            ["an (N>=3, 2) array of x, y mm"],
            command="prep",
        )
    marks = [np.asarray(com_xy, dtype=np.float64)]
    if load_com_xy is not None:
        marks.append(np.asarray(load_com_xy, dtype=np.float64))
    pts = np.vstack([poly, *marks])
    plot = Plot2D(pts.min(axis=0), pts.max(axis=0), size, margin_px=70)
    plot.fill_rings([poly], SUPPORT_FILL, SUPPORT_OUTLINE, 3)
    governing = marks[-1]
    if tip_margin_mm is not None:
        edge = nearest_points(Polygon(poly).exterior, Point(governing))[0]
        plot.arrow(governing, (edge.x, edge.y), ARROW_COLOR, 2)
        mid_px = plot.to_px((governing + np.array([edge.x, edge.y])) / 2)
        plot.text(
            (mid_px[0] + 8, mid_px[1] - 8), f"margin {tip_margin_mm:.1f} mm", INK, 14
        )
    plot.disc(marks[0], 9, COM_COLOR)
    if load_com_xy is not None:
        plot.disc(marks[1], 9, LOAD_COM_COLOR)
    plot.scale_bar()
    plot.text((14, 10), "support polygon (top view)", INK, 15)
    plot.canvas[34:46, 14:26] = COM_COLOR
    plot.text((32, 31), "centre of mass", INK, 13)
    if load_com_xy is not None:
        plot.canvas[54:66, 14:26] = LOAD_COM_COLOR
        plot.text((32, 51), "centre of mass with load", INK, 13)
    return plot.canvas


# ---------------------------------------------------------------------------------------------
# profile with a leaning slab (phone / item fit)

SLAB_COLOR: RGB = (235, 120, 40)
_SLAB_KEYS = ("y0", "z0", "phi_deg", "thickness", "height")


def slab_corners(pose: Mapping[str, float]) -> NDArray[np.float64]:
    """(4, 2) (y, z) corners of the slab: back-bottom corner B = (y0, z0), long axis tilted `phi_deg` toward +Y.

    Same convention as the phone-fit solver: height runs along u = (sin phi, cos phi), thickness along
    n = (-cos phi, sin phi) (the screen side faces -Y).
    """
    missing = [k for k in _SLAB_KEYS if k not in pose]
    if missing:
        raise InvalidArgument(
            "slab_pose",
            f"missing {missing}",
            [f"keys {list(_SLAB_KEYS)} (+ optional width, x)"],
            command="prep",
        )
    phi = math.radians(float(pose["phi_deg"]))
    u = np.array([math.sin(phi), math.cos(phi)])
    n = np.array([-math.cos(phi), math.sin(phi)])
    b = np.array([float(pose["y0"]), float(pose["z0"])])
    hgt, thk = float(pose["height"]), float(pose["thickness"])
    return np.array([b, b + hgt * u, b + hgt * u + thk * n, b + thk * n])


def profile_with_slab(
    mesh: trimesh.Trimesh,
    slab_pose: Mapping[str, float],
    size: tuple[int, int] = (800, 800),
) -> Image:
    """YZ section of `mesh` (Y to the right, Z up) with the leaning slab drawn translucent on top.

    The cut is at X = `slab_pose['x']` when given, else the bbox centre. Where the slab overlaps the
    section the colours mix darker, which is how a collision shows. `width` (X extent) is only labelled.
    """
    corners = slab_corners(slab_pose)
    x_cut = (
        float(slab_pose["x"]) if "x" in slab_pose else float(mesh.bounds[:, 0].mean())
    )
    rings = section_rings(
        mesh,
        (x_cut, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        right=(0.0, 1.0, 0.0),
        up=(0.0, 0.0, 1.0),
    )
    # section_rings is relative to the plane origin, whose Y and Z are 0, so these are absolute Y/Z.
    pts = np.vstack([corners, *rings]) if rings else corners
    plot = Plot2D(pts.min(axis=0), pts.max(axis=0), size, margin_px=50)
    plot.fill_rings(rings, (150, 185, 225), (20, 40, 90))
    under = plot.canvas.copy()
    slab = plot.fill_rings([corners], SLAB_COLOR)
    plot.canvas[slab] = (
        0.45 * under[slab].astype(np.float64)
        + 0.55 * np.asarray(SLAB_COLOR, dtype=np.float64)
    ).astype(np.uint8)
    plot.polyline(corners, (140, 60, 10), 2, closed=True)
    plot.disc(corners[0], 6, (140, 60, 10))
    plot.scale_bar()
    width = f", width {slab_pose['width']:g} mm" if "width" in slab_pose else ""
    plot.text(
        (14, 10),
        f"YZ section at X={x_cut:.1f}, slab tilt {slab_pose['phi_deg']:.1f} deg{width}",
        INK,
        15,
    )
    return plot.canvas


# ---------------------------------------------------------------------------------------------
# verdict matrix

_MATRIX_CELL = (96, 34)
_MATRIX_HEADER_H = 40
_MATRIX_LABEL_MIN_W = 120
_MATRIX_PAD = 12


def _matrix_label_w(rows: Sequence[str]) -> int:
    return max(
        [_MATRIX_LABEL_MIN_W, *(text_size(r, 14)[0] + 2 * _MATRIX_PAD for r in rows)]
    )


def verdict_matrix_cell(
    rows: Sequence[str], cols: Sequence[str], i: int, j: int
) -> tuple[int, int, int, int]:
    """Pixel box (x0, y0, x1, y1), exclusive end, of the coloured cell of row `i`, column `j`."""
    cw, ch = _MATRIX_CELL
    x0 = _matrix_label_w(rows) + j * cw
    y0 = _MATRIX_HEADER_H + i * ch
    return x0 + 2, y0 + 2, x0 + cw - 2, y0 + ch - 2


def verdict_matrix(
    rows: Sequence[str], cols: Sequence[str], verdicts: Sequence[Sequence[str]]
) -> Image:
    """Table of coloured verdict cells: green PASS, amber WARN/RISK, red FAIL, grey SKIP."""
    if len(verdicts) != len(rows) or any(len(r) != len(cols) for r in verdicts):
        raise InvalidArgument(
            "verdicts",
            "shape mismatch",
            [f"{len(rows)} rows x {len(cols)} columns of verdict strings"],
            command="prep",
        )
    for r in verdicts:
        for v in r:
            if v not in VERDICT_COLORS:
                raise InvalidArgument(
                    "verdict", v, list(VERDICT_COLORS), command="prep"
                )
    cw, ch = _MATRIX_CELL
    label_w = _matrix_label_w(rows)
    img = np.empty(
        (_MATRIX_HEADER_H + len(rows) * ch + 6, label_w + len(cols) * cw + 6, 3),
        dtype=np.uint8,
    )
    img[:] = WHITE
    for j, c in enumerate(cols):
        draw_text(img, (label_w + j * cw + 6, 12), c, INK, 14)
    for i, r in enumerate(rows):
        draw_text(img, (_MATRIX_PAD, _MATRIX_HEADER_H + i * ch + 9), r, INK, 14)
        for j, v in enumerate(verdicts[i]):
            x0, y0, x1, y1 = verdict_matrix_cell(rows, cols, i, j)
            img[y0:y1, x0:x1] = VERDICT_COLORS[v]
            tw, th = text_size(v, 13)
            draw_text(
                img,
                (x0 + (x1 - x0 - tw) // 2, y0 + (y1 - y0 - th) // 2 - 2),
                v,
                INK,
                13,
            )
    return img
