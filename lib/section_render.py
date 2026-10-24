"""section_render.py — engineering cross-sections (`3d section`, `3d render --section`).

WHAT: cuts a model with an axis plane and draws the result the way an engineering drawing does:
  the CUT FACES are violet with 45-degree hatch lines, every uncut surface keeps its PART colour
  (the same parts, palette, roles and legend as `3d dimension`), and a legend strip names them all,
  "cut face" included. Output: one PNG of exactly `--size` pixels.

HOW: the model is read with colours (dimension_model.py) and split into parts (dimension_parts.py).
  Every triangle is clipped against the plane (Sutherland-Hodgman, part id kept). The two points a
  clipped triangle adds on the plane are one segment of the section outline; linking the segments of
  each part gives closed loops, and manifold3d's CrossSection (even-odd fill: holes are holes)
  unions them and triangulates the cut face. The cap is painted last (it is the nearest surface, the
  eye being on the removed side), then the 45-degree hatch is laid over it in image space.

WHY: the old section (an OpenSCAD half-space difference of an STL import) drew everything in one tan
  colour: the cut face could not be told from the surface behind it and the parts of an assembly
  could not be told apart (alex-mextner/3d-cli#586).

INVARIANTS: the plane never grazes a vertex (it is nudged by a hair when it would), so every triangle
  is wholly kept, wholly removed or cut; a part whose outline does not close (a non-watertight mesh)
  gets a warning and an incomplete cap, never a silent full one; the image is exactly `--size`.
"""

from __future__ import annotations

import argparse
import functools
import math
import operator
import os
import sys
from collections import defaultdict
from typing import Any

import numpy as np
from dimension_args import parse_define, parse_part
from dimension_legend import draw_legend, layout_legend
from dimension_model import RenderError, from_hex, load_raw
from dimension_parts import Model, build_model
from dimension_render import AMBIENT, DIFFUSE, SUPERSAMPLE, _font, _unit
from PIL import Image, ImageChops, ImageDraw, ImageFilter

AXIS = {"YZ": 0, "XZ": 1, "XY": 2}  # the axis the plane is perpendicular to
# Eye direction on the removed side, in the order (x, y, z); the plane's own axis dominates and the
# tilt shows depth. keep=pos mirrors it.
EYE = {"YZ": (1.0, 0.35, 0.5), "XZ": (0.35, 1.0, 0.5), "XY": (0.5, 0.35, 1.0)}
UP = {"YZ": (0.0, 0.0, 1.0), "XZ": (0.0, 0.0, 1.0), "XY": (0.0, 1.0, 0.0)}
CUT_FILL = "#B48CDC"  # the cut face: a violet tint ...
CUT_HATCH = (92, 48, 152)  # ... and the darker violet of its hatch lines and outline
CUT_LABEL = "cut face"
HATCH_STEP_PX = (
    13  # horizontal distance between two 45-degree hatch lines at 1200 px width
)
REFERENCE_WIDTH = 1200
MARGIN_PX = 28
HEADER_PX = 46
MIN_SIZE_PX = 160
MAX_SIZE_PX = 8192
GRAZE = 1e-7  # a vertex this close to the plane (relative to the model size) counts as on it


class Cut:
    """The clipped model: kept triangles (with part ids), the cap triangles and the outline."""

    def __init__(
        self,
        tris: np.ndarray,
        part: np.ndarray,
        cullable: np.ndarray,
        cap: np.ndarray,
        outline: list[np.ndarray],
        open_loops: int,
        at: float,
    ) -> None:
        self.tris = tris  # (K, 3, 3) kept triangles
        self.part = part  # (K,) part index of each
        self.cullable = cullable  # (K,) its part is watertight
        self.cap = cap  # (C, 3, 3) cut-face triangles, on the plane
        self.outline = outline  # closed 3D polylines of the section outline
        self.open_loops = open_loops  # outline chains that did not close
        self.at = at  # the plane position actually used (mm)


def _plane_coords(axis: int) -> tuple[int, int]:
    first, second = (a for a in range(3) if a != axis)
    return first, second


def _loops(
    segments: list[tuple[np.ndarray, np.ndarray]],
) -> tuple[list[np.ndarray], int]:
    """Closed loops (2D) and the number of open chains in a bag of 2D segments."""

    def key(p: np.ndarray) -> tuple[int, int]:
        return (round(float(p[0]) * 1e4), round(float(p[1]) * 1e4))

    adjacent: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    point: dict[tuple[int, int], np.ndarray] = {}
    for a, b in segments:
        ka, kb = key(a), key(b)
        if ka == kb:
            continue
        adjacent[ka].append(kb)
        adjacent[kb].append(ka)
        point[ka], point[kb] = a, b
    loops: list[np.ndarray] = []
    open_chains = 0
    while True:
        start = next((k for k, nbrs in adjacent.items() if nbrs), None)
        if start is None:
            return loops, open_chains
        chain = [start]
        here = start
        closed = False
        while adjacent[here]:
            nxt = adjacent[here].pop()
            adjacent[nxt].remove(here)
            if nxt == start:
                closed = True
                break
            chain.append(nxt)
            here = nxt
        if closed and len(chain) >= 3:
            loops.append(np.array([point[k] for k in chain]))
        else:
            open_chains += 1


def cut_model(model: Model, plane: str, keep: str, at: float | None) -> Cut:
    """Clip `model` at `plane` (position `at` mm on its axis; the middle by default), keeping `keep`."""
    import manifold3d

    axis = AXIS[plane]
    mesh = model.mesh
    tris = np.asarray(mesh.triangles, dtype=float)
    size = float(np.max(mesh.extents))
    lo, hi = float(mesh.bounds[0][axis]), float(mesh.bounds[1][axis])
    position = (lo + hi) / 2.0 if at is None else at
    if not lo < position < hi:
        raise RenderError(
            f"3d section: the plane at {'XYZ'[axis]} = {position:g} mm misses the model "
            f"({lo:.2f} .. {hi:.2f} mm); pick --at inside that range"
        )
    coords = tris[:, :, axis]
    graze = GRAZE * max(size, 1.0)
    if np.any(np.abs(coords - position) < graze):
        position += 100 * graze  # never let the plane touch a vertex exactly
    sign = -1.0 if keep == "neg" else 1.0  # s > 0 on the kept side
    s = sign * (coords - position)
    count = (s > 0).sum(axis=1)
    kept = [tris[count == 3]]
    kept_part = [model.face_part[count == 3]]
    kept_cull = [model.cullable[count == 3]]
    segments: dict[int, list[tuple[np.ndarray, np.ndarray]]] = defaultdict(list)
    a2, b2 = _plane_coords(axis)
    extra: list[np.ndarray] = []
    extra_part: list[int] = []
    extra_cull: list[bool] = []
    for idx in np.flatnonzero((count == 1) | (count == 2)):
        tri, sv = tris[idx], s[idx]
        poly: list[np.ndarray] = []
        added: list[np.ndarray] = []
        for i in range(3):
            p, q, sp, sq = tri[i], tri[(i + 1) % 3], sv[i], sv[(i + 1) % 3]
            if sp > 0:
                poly.append(p)
            if (sp > 0) != (sq > 0):
                # The crossing is computed from the edge's endpoints in a fixed order, so the two
                # triangles sharing the edge get bit-identical points and the outline links up.
                if tuple(p) > tuple(q):
                    p, q, sp, sq = q, p, sq, sp
                crossing = p + (sp / (sp - sq)) * (q - p)
                poly.append(crossing)
                added.append(crossing)
        for k in range(1, len(poly) - 1):
            extra.append(np.array([poly[0], poly[k], poly[k + 1]]))
            extra_part.append(int(model.face_part[idx]))
            extra_cull.append(bool(model.cullable[idx]))
        if len(added) == 2:
            segments[int(model.face_solid[idx])].append(
                (added[0][[a2, b2]], added[1][[a2, b2]])
            )
    if extra:
        kept.append(np.array(extra))
        kept_part.append(np.array(extra_part, dtype=np.int64))
        kept_cull.append(np.array(extra_cull, dtype=bool))
    sections = []
    outline: list[np.ndarray] = []
    open_total = 0
    for _solid, segs in sorted(segments.items()):
        loops, open_chains = _loops(segs)
        open_total += open_chains
        if loops:
            sections.append(manifold3d.CrossSection(loops, manifold3d.FillRule.EvenOdd))
        for loop in loops:
            flat = np.insert(np.vstack([loop, loop[:1]]), axis, position, axis=1)
            outline.append(flat)
    cap = np.zeros((0, 3, 3))
    if sections:
        region = functools.reduce(operator.add, sections)
        polygons = region.to_polygons()
        if polygons:
            flat_pts = np.vstack(polygons)
            index = manifold3d.triangulate(polygons)
            cap = np.insert(flat_pts, axis, position, axis=1)[index]
    return Cut(
        np.vstack(kept),
        np.concatenate(kept_part),
        np.concatenate(kept_cull),
        cap,
        outline,
        open_total,
        position,
    )


def _basis(plane: str, keep: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(eye direction, screen right, screen up); the eye is on the removed side."""
    eye = _unit(np.array(EYE[plane]) * (-1.0 if keep == "pos" else 1.0))
    right = _unit(np.cross(np.array(UP[plane]), eye))
    return eye, right, _unit(np.cross(eye, right))


def _hatch_mask(size: tuple[int, int], scale: float) -> Image.Image:
    """An 'L' mask of 45-degree lines (rising to the right) over the whole image."""
    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    step = max(HATCH_STEP_PX * scale, 4.0)
    line = max(int(round(1.6 * scale)), 1)
    x = -float(height)
    while x < width:
        draw.line([(x, height), (x + height, 0)], fill=255, width=line)
        x += step
    return mask


def render(
    model_path: str,
    out: str,
    *,
    plane: str = "YZ",
    keep: str = "neg",
    at: float | None = None,
    size: tuple[int, int] = (1200, 900),
    defines: list[str] | None = None,
    parts: list[str] | None = None,
) -> dict[str, Any]:
    """Render the section of `model_path` to `out`; returns a summary dict."""
    title = os.path.splitext(os.path.basename(model_path))[0]
    model = build_model(
        load_raw(model_path, defines), title, parts or [], reserved=[CUT_FILL]
    )
    cut = cut_model(model, plane, keep, at)
    width, height = size
    scale = width / REFERENCE_WIDTH
    small_font = _font(max(18 * scale, 9.0))
    legend_rows, legend_h = layout_legend(
        model, small_font, width, scale, [(CUT_LABEL, CUT_FILL, True)]
    )
    header = int(round(HEADER_PX * scale))
    view_h = height - header - legend_h
    if view_h < MIN_SIZE_PX // 2:
        raise RenderError(
            f"3d section: --size {width}x{height} leaves no room for the view next to the legend"
        )
    canvas = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    axis_letter = "XYZ"[AXIS[plane]]
    draw.text(
        (16, 8 * scale),
        f"{title} - section {plane}, {axis_letter} = {cut.at:.2f} mm, "
        f"keeps the {'negative' if keep == 'neg' else 'positive'} side",
        font=_font(max(22 * scale, 10.0), bold=True),
        fill=(20, 20, 20),
    )
    eye, right, up = _basis(plane, keep)
    everything = np.concatenate([cut.tris.reshape(-1, 3), cut.cap.reshape(-1, 3)])
    uv = np.column_stack([everything @ right, everything @ up])
    lo, hi = uv.min(axis=0), uv.max(axis=0)
    margin = MARGIN_PX * scale
    fit = float(
        min(
            (width - 2 * margin) / max(hi[0] - lo[0], 1e-6),
            (view_h - 2 * margin) / max(hi[1] - lo[1], 1e-6),
        )
    )
    centre_px = np.array([width / 2.0, header + view_h / 2.0])
    centre_uv = (lo + hi) / 2.0

    def project(points: np.ndarray) -> np.ndarray:
        flat = points.reshape(-1, 3)
        return np.column_stack(
            [
                centre_px[0] + (flat @ right - centre_uv[0]) * fit,
                centre_px[1] - (flat @ up - centre_uv[1]) * fit,
            ]
        ).reshape(points.shape[:-1] + (2,))

    ss = SUPERSAMPLE
    layer = Image.new("RGB", (width * ss, height * ss), (255, 255, 255))
    body = Image.new("L", (width * ss, height * ss), 0)
    capmask = Image.new("L", (width * ss, height * ss), 0)
    ldraw, bdraw, cdraw = (ImageDraw.Draw(im) for im in (layer, body, capmask))
    tris = cut.tris
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    norm = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, norm, out=np.zeros_like(normals), where=norm > 0)
    keep_face = ~cut.cullable | (normals @ eye > 1e-9)
    if not keep_face.any():
        keep_face = np.ones(len(tris), dtype=bool)
    tris, normals, part_of = tris[keep_face], normals[keep_face], cut.part[keep_face]
    normals[
        normals @ eye < 0
    ] *= -1  # shade a back face as seen, as the dimension views do
    light = _unit(eye + 0.45 * right + 0.30 * up)
    shade = AMBIENT + DIFFUSE * np.clip(normals @ light, 0.0, 1.0)
    order = np.argsort(-(tris @ eye).mean(axis=1))  # far first
    pix = project(tris[order]) * ss
    colors = np.clip(
        model.colors[part_of[order]] * shade[order, None] * 255.0, 0, 255
    ).astype(int)
    for tri, col in zip(pix, colors, strict=True):
        poly = [tuple(p) for p in tri]
        rgb = (int(col[0]), int(col[1]), int(col[2]))
        ldraw.polygon(poly, fill=rgb, outline=rgb)
        bdraw.polygon(poly, fill=255, outline=255)
    for tri in project(cut.cap) * ss:
        poly = [tuple(p) for p in tri]
        cdraw.polygon(poly, fill=255, outline=255)
        bdraw.polygon(poly, fill=255, outline=255)
    layer = layer.resize(size, Image.Resampling.LANCZOS)
    body = body.resize(size, Image.Resampling.LANCZOS)
    capmask = capmask.resize(size, Image.Resampling.LANCZOS)
    edge = ImageChops.subtract(body.filter(ImageFilter.MaxFilter(3)), body)
    fill = tuple(round(c * 255) for c in from_hex(CUT_FILL))
    canvas.paste(layer, (0, 0), body)
    canvas.paste(Image.new("RGB", size, (50, 60, 80)), (0, 0), edge)
    canvas.paste(Image.new("RGB", size, fill), (0, 0), capmask)
    canvas.paste(
        Image.new("RGB", size, CUT_HATCH),
        (0, 0),
        ImageChops.multiply(_hatch_mask(size, scale), capmask),
    )
    draw = ImageDraw.Draw(canvas)
    line = max(int(round(2 * scale)), 1)
    for loop in cut.outline:
        draw.line([tuple(p) for p in project(loop)], fill=CUT_HATCH, width=line)
    legend = (
        draw_legend(canvas, legend_rows, header + view_h, small_font, scale)
        if legend_rows
        else None
    )
    canvas.save(out, format="PNG")
    return {
        "image": os.path.abspath(out),
        "plane": plane,
        "at_mm": cut.at,
        "keep": keep,
        "parts": [p.as_dict() for p in model.parts],
        "legend": legend,
        "cap_triangles": len(cut.cap),
        "open_outlines": cut.open_loops,
        "warnings": model.warnings,
    }


def _parse_size(raw: str) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in raw.lower().split("x"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--size {raw!r}: expected WxH, e.g. 1200x900"
        ) from exc
    if not (MIN_SIZE_PX <= w <= MAX_SIZE_PX and MIN_SIZE_PX <= h <= MAX_SIZE_PX):
        raise argparse.ArgumentTypeError(
            f"--size {raw!r}: each side must be {MIN_SIZE_PX}..{MAX_SIZE_PX} px"
        )
    return w, h


def _parse_at(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--at {raw!r}: expected a number in mm"
        ) from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"--at {raw!r}: not a finite number")
    return value


def _checked(parse: Any) -> Any:
    """argparse `type=` that validates with a dimension_args parser but keeps the raw string."""

    def convert(raw: str) -> str:
        try:
            parse(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        return raw

    return convert


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="section_render.py")
    ap.add_argument("model")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--plane", default="YZ", choices=sorted(AXIS))
    ap.add_argument("--keep", default="neg", choices=["neg", "pos"])
    ap.add_argument("--at", type=_parse_at, default=None)
    ap.add_argument("--size", type=_parse_size, default=(1200, 900))
    ap.add_argument(
        "--define", action="append", default=[], type=_checked(parse_define)
    )
    ap.add_argument("--part", action="append", default=[], type=_checked(parse_part))
    args = ap.parse_args(argv)
    try:
        info = render(
            args.model,
            args.out,
            plane=args.plane,
            keep=args.keep,
            at=args.at,
            size=args.size,
            defines=args.define,
            parts=args.part,
        )
    except RenderError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    for warning in info["warnings"]:
        print(f"3d section: warning: {warning}", file=sys.stderr)
    if info["open_outlines"]:
        print(
            f"3d section: warning: {info['open_outlines']} section outline(s) did not close "
            "(the mesh is not watertight there): the cut face is incomplete",
            file=sys.stderr,
        )
    axis_letter = "XYZ"[AXIS[info["plane"]]]
    print(
        f"section: {args.model} plane={info['plane']} {axis_letter}={info['at_mm']:.3f} "
        f"keep={info['keep']}, {len(info['parts'])} part(s) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
