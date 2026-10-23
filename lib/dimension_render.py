"""dimension_render.py — shaded multi-view renders of a part with mm DIMENSION ARROWS (`3d dimension`).

WHAT: top / front / side / iso views of the REAL mesh (flat-shaded orthographic), each carrying
  double-headed dimension arrows with the value in mm: the overall bounding box (X/Y/Z)
  automatically, plus any `--dim name:x,y,z:x,y,z` feature (a boss length, a hole diameter),
  an optional bed outline (`--bed 220x220`, model coordinates = bed coordinates, as in a
  print-pose STL) and the part name. Output: one PNG + `<png>.dims.json` (label, value, view,
  pixel box of every label, per-view panel + silhouette grid, image sha256) that
  dimension_check.py — and tg's send gate — verify for legibility.

WHY: the owner confirms a model before a print by SEEING its sizes; a plain render proves
  nothing (spoon150: bed cleared, but a render without sizes cannot confirm 150 mm / +25 mm
  bosses). Ported from garage-band/tools/render_dimensioned.py (the orthographic projection and
  Lambert shading), re-built on Pillow so the labels are pixel-exact and measurable.

HOW: each label is placed by trying offsets/sides from the part until it clears the silhouette,
  the other labels and the panel edge; the check re-verifies the result from the sidecar and the
  pixels, so a placement that could not be satisfied is reported rather than hidden.

INVARIANTS: the tool measures the mesh itself (bbox) — `--dim` points are the caller's feature
  points; labels are drawn at >= MIN_FONT_PX at the 1280 px reference width unless a caller
  asks for a smaller `--label-px` (tests do, to prove the gate fails).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

import numpy as np
from dimension_args import parse_bed, parse_dim, parse_width, positive
from dimension_check import (
    REFERENCE_WIDTH,
    rects_overlap,
    sha256_file,
    sidecar_path,
)
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

VIEWS = ("top", "front", "side", "iso")
CAMERAS = {
    "top": (0.0, 0.0, 1.0),
    "front": (0.0, -1.0, 0.0),
    "side": (1.0, 0.0, 0.0),
    "iso": (0.5, -0.5, 0.7071),
}
AXIS_COLORS = {"X": (170, 30, 30), "Y": (20, 105, 40), "Z": (20, 70, 190)}
CUSTOM_COLOR = (105, 40, 150)
# Preferred side of the part (screen direction, y up) for each bbox axis's dimension line.
AXIS_SIDE = {0: (-0.3, -1.0), 1: (1.0, -0.3), 2: (-1.0, 0.0)}
SUPERSAMPLE = 2
HEADER_PX = 64
PANEL_MARGIN_X = 118
PANEL_MARGIN_TOP = 52
PANEL_MARGIN_BOTTOM = 84
OFFSETS = (26, 42, 58, 76, 96, 118, 140, 166)
CLEAN_PLACEMENT = 300.0  # a score above this means a label collision remains
GRID_CELL = 4
# A custom/span arrow is drawn only where it keeps >= 90% of its true length.
MIN_TRUE_LENGTH_RATIO = 0.9
SPAN_MIN_GAIN = 1.03  # show the part's longest span when its oriented box beats the axis-aligned one by 3%
BODY_COLOR = (0.46, 0.62, 0.84)
AMBIENT, DIFFUSE = 0.34, 0.66
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)
BOLD_CANDIDATES = tuple(
    p.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf").replace(
        "Arial.ttf", "Arial Bold.ttf"
    )
    for p in FONT_CANDIDATES
)


def _font(
    size: float, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in BOLD_CANDIDATES if bold else FONT_CANDIDATES:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size=max(int(round(size)), 1))
    return ImageFont.load_default(size=max(int(round(size)), 1))


@dataclass
class Placed:
    label: str
    value_mm: float
    view: str
    kind: str
    text: str
    font_px: float
    box: list[float]
    text_box: list[float]
    arrow: list[list[float]]

    def as_dict(self) -> dict[str, Any]:
        """The sidecar entry of this label."""
        return {
            "label": self.label,
            "value_mm": round(self.value_mm, 3),
            "view": self.view,
            "kind": self.kind,
            "text": self.text,
            "font_px": self.font_px,
            "box": [round(v, 1) for v in self.box],
            "text_box": [round(v, 1) for v in self.text_box],
            "arrow": [[round(v, 1) for v in pt] for pt in self.arrow],
        }


def _unit(vec: Any) -> np.ndarray:
    arr = np.asarray(vec, dtype=float)
    return arr / np.linalg.norm(arr)


def view_basis(view: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Orthonormal (camera, right, up) for one named viewpoint."""
    cam = _unit(CAMERAS[view])
    up_hint = np.array([0.0, 1.0, 0.0]) if view == "top" else np.array([0.0, 0.0, 1.0])
    right = _unit(np.cross(up_hint, cam))
    return cam, right, _unit(np.cross(cam, right))


class RenderError(Exception):
    """A failure the user can act on; `main` prints it and exits with `exit_code`."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _scad_to_stl(path: str, out: str) -> None:
    try:
        done = subprocess.run(
            ["openscad", "-o", out, path], capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise RenderError(
            "3d dimension: openscad is not installed, so a .scad input cannot be rendered "
            "(pass an STL/3MF, or install openscad: https://openscad.org/downloads.html)",
            exit_code=127,
        ) from exc
    if done.returncode != 0:
        raise RenderError(
            f"3d dimension: openscad failed on {path}: {done.stderr.strip()[-300:]}"
        )


def load_mesh(path: str) -> Any:
    """STL/3MF/OBJ via trimesh; .scad is exported through openscad first."""
    import trimesh

    if path.lower().endswith(".scad"):
        fd, tmp = tempfile.mkstemp(suffix=".stl")
        os.close(fd)
        try:
            _scad_to_stl(path, tmp)
            mesh: Any = trimesh.load(tmp, force="mesh")
        finally:
            os.unlink(tmp)
    else:
        mesh = trimesh.load(path, force="mesh")
    if mesh.is_empty or len(mesh.faces) == 0:
        raise RenderError(f"3d dimension: no triangles in {path}")
    return mesh


class Panel:
    """One view: projection into pixel space, shaded body, silhouette mask, placed labels."""

    def __init__(
        self,
        view: str,
        rect: tuple[int, int, int, int],
        mesh: Any,
        bed: tuple[float, float] | None,
        ui: float,
    ) -> None:
        self.view = view
        self.ui = ui  # image width / 1280: margins and offsets scale with the image
        self.rect = rect
        self.mesh = mesh
        self.cam, self.right, self.up = view_basis(view)
        pts = [np.asarray(mesh.bounds[0]), np.asarray(mesh.bounds[1])]
        corner_pts = np.array(
            [
                [x, y, z]
                for x in (pts[0][0], pts[1][0])
                for y in (pts[0][1], pts[1][1])
                for z in (pts[0][2], pts[1][2])
            ]
        )
        fit_pts = [self._uv(corner_pts)]
        if bed is not None and view == "top":
            fit_pts.append(
                self._uv(np.array([[0, 0, 0], [bed[0], bed[1], 0]], dtype=float))
            )
        allpts = np.vstack(fit_pts)
        self.lo, self.hi = allpts.min(axis=0), allpts.max(axis=0)
        x0, y0, x1, y1 = rect
        avail_w = (x1 - x0) - 2 * PANEL_MARGIN_X * ui
        avail_h = (y1 - y0) - (PANEL_MARGIN_TOP + PANEL_MARGIN_BOTTOM) * ui
        span = np.maximum(self.hi - self.lo, 1e-6)
        self.scale = float(min(avail_w / span[0], avail_h / span[1]))
        self.centre_uv = (self.lo + self.hi) / 2.0
        self.centre_px = np.array(
            [(x0 + x1) / 2.0, y0 + PANEL_MARGIN_TOP * ui + avail_h / 2.0]
        )
        self.mask: np.ndarray | None = None
        self.cover: np.ndarray | None = None
        self.cells: np.ndarray | None = None
        # Boxes nothing may cover: placed labels plus the reserved panel title / bed caption.
        self.labels: list[tuple[float, float, float, float]] = []
        # Arrow and extension segments already drawn, so a later label avoids striking them out.
        self.segments: list[tuple[np.ndarray, np.ndarray]] = []

    def _uv(self, pts: np.ndarray) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        return np.column_stack([pts @ self.right, pts @ self.up])

    def px(self, pts: np.ndarray) -> np.ndarray:
        """3D points -> pixel coordinates (y down) inside the final image."""
        uv = self._uv(pts)
        return np.column_stack(
            [
                self.centre_px[0] + (uv[:, 0] - self.centre_uv[0]) * self.scale,
                self.centre_px[1] - (uv[:, 1] - self.centre_uv[1]) * self.scale,
            ]
        )

    def paint_body(self, canvas: Image.Image) -> None:
        """Shade the mesh (supersampled), outline its silhouette, record the silhouette mask."""
        x0, y0, x1, y1 = self.rect
        w, h = x1 - x0, y1 - y0
        ss = SUPERSAMPLE
        layer = Image.new("RGB", (w * ss, h * ss), (255, 255, 255))
        alpha = Image.new("L", (w * ss, h * ss), 0)
        draw, adraw = ImageDraw.Draw(layer), ImageDraw.Draw(alpha)
        normals = np.asarray(self.mesh.face_normals, dtype=float)
        tris = np.asarray(self.mesh.triangles, dtype=float)
        facing = normals @ self.cam
        keep = (
            facing > 1e-9
            if self.mesh.is_watertight
            else np.ones(len(normals), dtype=bool)
        )
        if not keep.any():
            keep = np.ones(len(normals), dtype=bool)
        tris, normals = tris[keep], normals[keep].copy()
        normals[normals @ self.cam < 0] *= -1
        light = _unit(self.cam + 0.45 * self.right + 0.30 * self.up)
        shade = AMBIENT + DIFFUSE * np.clip(normals @ light, 0.0, 1.0)
        order = np.argsort((tris @ self.cam).mean(axis=1))
        flat = tris[order].reshape(-1, 3)
        pix = self.px(flat).reshape(-1, 3, 2)
        pix[:, :, 0] = (pix[:, :, 0] - x0) * ss
        pix[:, :, 1] = (pix[:, :, 1] - y0) * ss
        cols = np.clip(
            np.asarray(BODY_COLOR)[None, :] * shade[order, None] * 255.0, 0, 255
        ).astype(int)
        for tri, col in zip(pix, cols, strict=True):
            poly = [tuple(p) for p in tri]
            c = (int(col[0]), int(col[1]), int(col[2]))
            draw.polygon(poly, fill=c, outline=c)
            adraw.polygon(poly, fill=255, outline=255)
        layer = layer.resize((w, h), Image.Resampling.LANCZOS)
        alpha = alpha.resize((w, h), Image.Resampling.LANCZOS)
        edge = ImageChops.subtract(alpha.filter(ImageFilter.MaxFilter(3)), alpha)
        canvas.paste(layer, (x0, y0), alpha)
        canvas.paste(Image.new("RGB", (w, h), (50, 60, 80)), (x0, y0), edge)
        full = np.zeros((canvas.height, canvas.width), dtype=bool)
        full[y0:y1, x0:x1] = np.asarray(alpha) > 40
        self.mask = full
        # The sidecar stores the silhouette as GRID_CELL cells counted from the panel origin (any
        # covered pixel marks the cell); placement scores against that same conservative footprint.
        sub = full[y0:y1, x0:x1]
        cell = GRID_CELL
        cell_rows, cell_cols = -(-sub.shape[0] // cell), -(-sub.shape[1] // cell)
        padded = np.zeros((cell_rows * cell, cell_cols * cell), dtype=bool)
        padded[: sub.shape[0], : sub.shape[1]] = sub
        self.cells = np.asarray(
            padded.reshape(cell_rows, cell, cell_cols, cell).any(axis=(1, 3))
        )
        cover = np.zeros_like(full)
        cover[y0:y1, x0:x1] = np.kron(self.cells, np.ones((cell, cell), dtype=bool))[
            : sub.shape[0], : sub.shape[1]
        ]
        self.cover = cover

    def silhouette_grid(self) -> dict[str, Any]:
        assert self.cells is not None
        rows = ["".join("#" if on else "." for on in row) for row in self.cells]
        return {"x0": self.rect[0], "y0": self.rect[1], "cell": GRID_CELL, "rows": rows}


def _text_bbox(font: Any, text: str) -> tuple[float, float, float, float]:
    left, top, right, bottom = font.getbbox(text)
    return float(left), float(top), float(right), float(bottom)


def _score_box(panel: Panel, box: tuple[float, float, float, float]) -> float:
    """0 when the box sits on clean paper inside the panel; grows with every collision."""
    assert panel.cover is not None
    x0, y0, x1, y1 = panel.rect
    bx0, by0, bx1, by1 = box
    score = 0.0
    if bx0 < x0 + 2 or by0 < y0 + 2 or bx1 > x1 - 2 or by1 > y1 - 2:
        score += 2000.0
    ix0, iy0, ix1, iy1 = (
        max(int(bx0), x0),
        max(int(by0), y0),
        min(int(np.ceil(bx1)), x1),
        min(int(np.ceil(by1)), y1),
    )
    if ix1 > ix0 and iy1 > iy0:
        share = float(panel.cover[iy0:iy1, ix0:ix1].mean())
        score += 1500.0 * share + (1000.0 if share > 0.05 else 0.0)
    # `labels` holds the placed labels AND the boxes reserved for the panel title and bed caption.
    score += 1000.0 * sum(
        1 for other in panel.labels if rects_overlap(list(box), list(other))
    )
    # Nor may a label sit on an arrow or extension line already drawn.
    if any(_segment_hits_box(start, end, box) for start, end in panel.segments):
        score += 1000.0
    return score


def _segment_hits_box(
    start: np.ndarray, end: np.ndarray, box: tuple[float, float, float, float]
) -> bool:
    """Exact segment/rectangle intersection (Liang-Barsky), however long the segment."""
    lo, hi = 0.0, 1.0
    delta = end - start
    for axis, (low, high) in enumerate(((box[0], box[2]), (box[1], box[3]))):
        if delta[axis] == 0.0:
            if not low <= start[axis] <= high:
                return False
            continue
        t0, t1 = (low - start[axis]) / delta[axis], (high - start[axis]) / delta[axis]
        lo, hi = max(lo, min(t0, t1)), min(hi, max(t0, t1))
        if lo > hi:
            return False
    return True


def _line_silhouette(panel: Panel, a: np.ndarray, b: np.ndarray) -> float:
    assert panel.mask is not None
    samples = np.linspace(a, b, 40)
    xs = np.clip(samples[:, 0].astype(int), 0, panel.mask.shape[1] - 1)
    ys = np.clip(samples[:, 1].astype(int), 0, panel.mask.shape[0] - 1)
    return float(panel.mask[ys, xs].mean())


def _line_hits_labels(panel: Panel, a: np.ndarray, b: np.ndarray) -> bool:
    """Does the segment a-b pass through a box already placed (it would strike out its value)?"""
    samples = np.linspace(a, b, 60)
    return any(
        box[0] <= x <= box[2] and box[1] <= y <= box[3]
        for x, y in samples
        for box in panel.labels
    )


def _arrowhead(
    draw: ImageDraw.ImageDraw,
    tip: np.ndarray,
    direction: np.ndarray,
    color: tuple[int, int, int],
    size: float,
) -> None:
    normal = np.array([-direction[1], direction[0]])
    base = tip - direction * size
    draw.polygon(
        [
            tuple(tip),
            tuple(base + normal * size * 0.36),
            tuple(base - normal * size * 0.36),
        ],
        fill=color,
    )


def place_dimension(
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    p1: np.ndarray,
    p2: np.ndarray,
    name: str,
    value: float,
    kind: str,
    color: tuple[int, int, int],
    font: Any,
    label_px: float,
    prefer: tuple[float, float] | None = None,
) -> Placed | None:
    """Draw one dimension (extension lines, double arrow, boxed label) at the cleanest offset."""
    a, b = panel.px(np.array([p1, p2]))
    length = float(np.linalg.norm(b - a))
    true_px = float(np.linalg.norm(np.asarray(p2) - np.asarray(p1))) * panel.scale
    # Foreshortened: the arrow would be shorter than the value it carries.
    if kind != "bbox" and length < MIN_TRUE_LENGTH_RATIO * true_px:
        return None
    arrow_px = max(label_px * 0.6, 10.0)
    if length < 2 * arrow_px + 4:
        return None  # edge-on or too short to carry an arrow in this view
    along = (b - a) / length
    normal = np.array([-along[1], along[0]])
    text = f"{name} {round(value, 1):.1f} mm"
    tl, tt, tr, tb = _text_bbox(font, text)
    pad = max(label_px * 0.25, 3.0)
    half_w, half_h = (tr - tl) / 2.0 + pad, (tb - tt) / 2.0 + pad
    centre = panel.px(np.array([(panel.mesh.bounds[0] + panel.mesh.bounds[1]) / 2.0]))[
        0
    ]
    pref = np.asarray(
        prefer if prefer is not None else (a + b) / 2.0 - centre, dtype=float
    )
    sign0 = 1.0 if float(normal @ pref) >= 0 else -1.0
    best: (
        tuple[float, np.ndarray, np.ndarray, tuple[float, float, float, float]] | None
    ) = None
    # Extent of the label box measured ALONG the dimension line: a wide label on a vertical
    # line costs only its height, a wide label on a horizontal one its width.
    along_extent = 2 * (abs(along[0]) * half_w + abs(along[1]) * half_h)
    fits_on_line = length >= along_extent + 2 * arrow_px
    lo_t = (along_extent / 2 + arrow_px) / length if fits_on_line else 0.5
    slides = [t for t in (0.5, 0.3, 0.7, 0.15, 0.85) if lo_t <= t <= 1.0 - lo_t] or [
        0.5
    ]
    beside = abs(normal[0]) * half_w + abs(normal[1]) * half_h + 6
    # A custom/span arrow must not cut across the part; a bbox edge sits on the silhouette
    # anyway, so only its offset line is judged.
    line_weight = 300.0 if kind == "bbox" else 3000.0
    for base_off in OFFSETS:
        off = base_off * panel.ui
        for sign in (sign0, -sign0):
            a2, b2 = a + normal * sign * off, b + normal * sign * off
            off_panel = (
                min(a2[0], b2[0]) < panel.rect[0] + 2
                or max(a2[0], b2[0]) > panel.rect[2] - 2
                or min(a2[1], b2[1]) < panel.rect[1] + 2
                or max(a2[1], b2[1]) > panel.rect[3] - 2
            )
            line_cost = line_weight * _line_silhouette(panel, a2, b2) + (
                2000.0 if off_panel else 0.0
            )
            # The arrow, its extension lines and a later label must not strike out a placed label.
            if any(
                _line_hits_labels(panel, s, e) for s, e in ((a2, b2), (a, a2), (b, b2))
            ):
                line_cost += 1000.0
            spots: list[tuple[np.ndarray, float]] = []
            if fits_on_line:
                spots += [(a2 + (b2 - a2) * t, abs(t - 0.5) * 0.5) for t in slides]
            reach = along_extent / 2 + 6
            spots += [
                (a2 - along * reach, 0.6),
                (b2 + along * reach, 0.6),
            ]  # beyond an arrow tip
            for side in (sign, -sign):
                spots += [
                    (
                        a2 + (b2 - a2) * t + normal * side * beside,
                        0.3 + abs(t - 0.5) * 0.5,
                    )
                    for t in (0.5, 0.3, 0.7)
                ]
            for mid, tie_break in spots:
                box = (
                    mid[0] - half_w,
                    mid[1] - half_h,
                    mid[0] + half_w,
                    mid[1] + half_h,
                )
                score = _score_box(panel, box) + line_cost + off * 0.01 + tie_break
                if best is None or score < best[0]:
                    best = (score, a2, b2, box)
        if best is not None and best[0] < 1.0:
            break
    assert best is not None
    if kind != "bbox" and best[0] >= CLEAN_PLACEMENT:
        return None  # no clean spot for an extra dimension in this view: omit it rather than hide geometry
    _, a2, b2, box = best
    for src, dst in ((a, a2), (b, b2)):
        draw.line(
            [
                tuple(src),
                tuple(dst + (dst - src) / max(np.linalg.norm(dst - src), 1e-9) * 6),
            ],
            fill=color,
            width=1,
        )
    draw.line(
        [tuple(a2 + along * arrow_px), tuple(b2 - along * arrow_px)],
        fill=color,
        width=2,
    )
    _arrowhead(draw, a2, -along, color, arrow_px)
    _arrowhead(draw, b2, along, color, arrow_px)
    draw.rectangle(box, fill=(255, 255, 255), outline=color, width=1)
    draw.text(
        (
            (box[0] + box[2]) / 2.0 - (tl + tr) / 2.0,
            (box[1] + box[3]) / 2.0 - (tt + tb) / 2.0,
        ),
        text,
        font=font,
        fill=color,
    )
    panel.labels.append(box)
    panel.segments += [(a, a2), (b, b2), (a2, b2)]
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    text_box = [
        cx - (tr - tl) / 2.0,
        cy - (tb - tt) / 2.0,
        cx + (tr - tl) / 2.0,
        cy + (tb - tt) / 2.0,
    ]
    return Placed(
        name,
        float(value),
        panel.view,
        kind,
        text,
        float(label_px),
        [float(v) for v in box],
        [float(v) for v in text_box],
        [[float(a2[0]), float(a2[1])], [float(b2[0]), float(b2[1])]],
    )


def bbox_edges(
    bounds: np.ndarray, panel: Panel
) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """For each axis, the outermost bbox edge on that axis's preferred side in this view."""
    lo, hi = bounds
    centre2d = panel.px(np.array([(lo + hi) / 2.0]))[0]
    out = []
    for axis in range(3):
        others = [i for i in range(3) if i != axis]
        best: tuple[float, np.ndarray, np.ndarray] | None = None
        for u in (lo[others[0]], hi[others[0]]):
            for v in (lo[others[1]], hi[others[1]]):
                p, q = np.zeros(3), np.zeros(3)
                p[others[0]] = q[others[0]] = u
                p[others[1]] = q[others[1]] = v
                p[axis], q[axis] = lo[axis], hi[axis]
                mid = panel.px(np.array([(p + q) / 2.0]))[0] - centre2d
                pref = np.array(
                    [AXIS_SIDE[axis][0], -AXIS_SIDE[axis][1]]
                )  # screen y is down
                score = float(mid @ pref)
                if best is None or score > best[0]:
                    best = (score, p, q)
        assert best is not None
        out.append((axis, best[1], best[2]))
    return out


def longest_span(mesh: Any) -> tuple[np.ndarray, np.ndarray]:
    """Farthest pair of vertices (double sweep over the convex hull): the part's true length."""
    hull = np.asarray(mesh.convex_hull.vertices, dtype=float)
    best_len, best = -1.0, (hull[0], hull[0])
    for seed in (0, len(hull) // 3, 2 * len(hull) // 3):
        a = hull[seed]
        for _ in range(3):
            b = hull[int(np.argmax(np.linalg.norm(hull - a, axis=1)))]
            a, b = b, a
            a = hull[int(np.argmax(np.linalg.norm(hull - b, axis=1)))]
        length = float(np.linalg.norm(a - b))
        if length > best_len:
            best_len, best = length, (a, b)
    return best


def is_slanted(mesh: Any) -> bool:
    """Is the part's oriented bounding box longer than the longest side of its axis-aligned one (by SPAN_MIN_GAIN)?"""
    oriented = float(np.max(mesh.bounding_box_oriented.primitive.extents))
    return oriented > SPAN_MIN_GAIN * float(np.max(mesh.extents))


def _draw_bed(
    draw: ImageDraw.ImageDraw,
    panel: Panel,
    bed: tuple[float, float],
    view_font: Any,
) -> dict[str, Any]:
    """Dashed bed outline + caption in the top view; returns the sidecar `bed` entry."""
    mesh = panel.mesh
    corners = panel.px(
        np.array(
            [[0, 0, 0], [bed[0], 0, 0], [bed[0], bed[1], 0], [0, bed[1], 0], [0, 0, 0]],
            dtype=float,
        )
    )
    for p, q in zip(corners[:-1], corners[1:], strict=True):
        length = float(np.linalg.norm(q - p))
        for s in np.arange(0.0, length, 14.0):
            start = p + (q - p) * (s / length)
            end = p + (q - p) * (min(s + 8.0, length) / length)
            draw.line([tuple(start), tuple(end)], fill=(120, 120, 130), width=2)
    inside = bool(
        mesh.bounds[0][0] >= -1e-6
        and mesh.bounds[0][1] >= -1e-6
        and mesh.bounds[1][0] <= bed[0] + 1e-6
        and mesh.bounds[1][1] <= bed[1] + 1e-6
    )
    caption = f"bed {bed[0]:g} x {bed[1]:g} mm" + (
        "" if inside else "  PART OUTSIDE BED"
    )
    bl, bt, br, bb = _text_bbox(view_font, caption)
    x, y = panel.rect[2] - (br - bl) - 14, panel.rect[3] - (bb - bt) - 12
    draw.text(
        (x - bl, y - bt),
        caption,
        font=view_font,
        fill=(90, 95, 105) if inside else (190, 30, 30),
    )
    box = (x - 4.0, y - 4.0, x + (br - bl) + 4.0, y + (bb - bt) + 4.0)
    panel.labels.append(box)  # no dimension label may cover the caption
    return {
        "width_mm": bed[0],
        "depth_mm": bed[1],
        "part_inside": inside,
        "box": list(box),
    }


def render(
    model: str,
    out: str,
    *,
    name: str | None = None,
    dims: list[str] | None = None,
    bed: tuple[float, float] | None = None,
    width: int = REFERENCE_WIDTH,
    label_px: float | None = None,
) -> dict[str, Any]:
    """Render `model` to `out` (+ sidecar); returns the sidecar dict."""
    mesh = load_mesh(model)
    title = name or os.path.splitext(os.path.basename(model))[0]
    scale = width / REFERENCE_WIDTH
    px_font = (label_px if label_px is not None else 20.0) * scale
    panel_w, panel_h = width // 2, int(round(560 * scale))
    header = int(round(HEADER_PX * scale))
    height = header + 2 * panel_h
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _font(px_font)
    head_font, view_font = _font(26 * scale, bold=True), _font(18 * scale, bold=True)
    size_font = _font(20 * scale)
    ext = mesh.extents
    draw.text((16, 10 * scale), title, font=head_font, fill=(20, 20, 20))
    draw.text(
        (16, 40 * scale),
        f"overall X {ext[0]:.1f} x Y {ext[1]:.1f} x Z {ext[2]:.1f} mm",
        font=size_font,
        fill=(60, 60, 60),
    )
    custom = [
        (cname, np.array(c1), np.array(c2))
        for cname, c1, c2 in (parse_dim(d) for d in (dims or []))
    ]
    # A slanted part's own length (its oriented box) beats the longest side of the axis-aligned box;
    # an axis-aligned block, strap or wedge has no such length, so it gets no space-diagonal "span".
    if is_slanted(mesh):
        custom.insert(0, ("span", *longest_span(mesh)))
    sidecar: dict[str, Any] = {
        "part": title,
        "source": os.path.abspath(model),
        "bbox_mm": [float(v) for v in ext],
        "views": {},
    }
    placed: list[Placed] = []
    for index, view in enumerate(VIEWS):
        col, row = index % 2, index // 2
        rect = (
            col * panel_w,
            header + row * panel_h,
            (col + 1) * panel_w,
            header + (row + 1) * panel_h,
        )
        draw.rectangle(
            [rect[0], rect[1], rect[2] - 1, rect[3] - 1], outline=(200, 205, 212)
        )
        panel = Panel(view, rect, mesh, bed, scale)
        panel.paint_body(canvas)
        draw = ImageDraw.Draw(canvas)
        tl, tt, tr, tb = _text_bbox(view_font, view.upper())
        title_box = (
            rect[0] + 8.0,
            rect[1] + 6.0,
            rect[0] + 8.0 + (tr - tl) + 10,
            rect[1] + 6.0 + (tb - tt) + 8,
        )
        draw.text(
            (title_box[0] + 5 - tl, title_box[1] + 4 - tt),
            view.upper(),
            font=view_font,
            fill=(90, 95, 105),
        )
        panel.labels.append(title_box)
        if bed is not None and view == "top":
            sidecar["bed"] = _draw_bed(draw, panel, bed, view_font)
        for axis, p, q in bbox_edges(np.asarray(mesh.bounds), panel):
            letter = "XYZ"[axis]
            item = place_dimension(
                draw,
                panel,
                p,
                q,
                letter,
                float(ext[axis]),
                "bbox",
                AXIS_COLORS[letter],
                font,
                px_font,
                (AXIS_SIDE[axis][0], -AXIS_SIDE[axis][1]),
            )
            if item:
                placed.append(item)
        for cname, c1, c2 in custom:
            item = place_dimension(
                draw,
                panel,
                c1,
                c2,
                cname,
                float(np.linalg.norm(c2 - c1)),
                "custom",
                CUSTOM_COLOR,
                font,
                px_font,
            )
            if item:
                placed.append(item)
        sidecar["views"][view] = {
            "rect": list(rect),
            "silhouette_grid": panel.silhouette_grid(),
        }
    canvas.save(out, format="PNG")
    sidecar["image"] = {
        "path": os.path.abspath(out),
        "width": width,
        "height": height,
        "sha256": sha256_file(out),
    }
    sidecar["dimensions"] = [p.as_dict() for p in placed]
    with open(sidecar_path(out), "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=1)
    return sidecar


def _checked_dim_spec(raw: str) -> str:
    """Validate a --dim spec but keep the raw string: `render` parses it again."""
    parse_dim(raw)
    return raw


def _arg_type(to_value: Any) -> Any:
    """argparse `type=` that turns a ValueError from dimension_args into a usage error."""

    def convert(raw: str) -> Any:
        try:
            return to_value(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return convert


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dimension_render.py")
    ap.add_argument("model")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--name")
    ap.add_argument(
        "--dim",
        action="append",
        default=[],
        type=_arg_type(_checked_dim_spec),
    )
    ap.add_argument("--bed", type=_arg_type(parse_bed))
    ap.add_argument("--width", type=_arg_type(parse_width), default=REFERENCE_WIDTH)
    ap.add_argument("--label-px", type=_arg_type(lambda r: positive(r, "--label-px")))
    args = ap.parse_args(argv)
    try:
        sidecar = render(
            args.model,
            args.out,
            name=args.name,
            dims=args.dim,
            bed=args.bed,
            width=args.width,
            label_px=args.label_px,
        )
    except RenderError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    print(
        f"wrote {args.out} + {sidecar_path(args.out)}: {len(sidecar['dimensions'])} dimension label(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
