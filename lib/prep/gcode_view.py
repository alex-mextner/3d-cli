"""gcode_view.py — proof renders of a SLICED G-code (not of the model) and the layer statistics the proof pack plots.

WHAT: `render_layers(parsed, outdir, colours=...)` rasterises the extrusion of a `gcode_gates.Parsed` with numpy and
  writes PNGs: `front` (elevation of the outer walls coloured by the tool that printed each layer, so Full Spectrum
  stripes and variable layer thickness are visible), `closeup` (the same elevation magnified on the Z window with the
  most tool changes, or on `zoom`) and `top` (plate view with bed outline, every extrusion coloured by tool, prime
  tower darker). `layer_histogram(parsed)` returns plain data (heights per layer, histogram on a 0.01 mm grid,
  tool changes per layer) that the report renders as a chart.
WHY: a layer-height or mix-stripe fault is only obvious in a picture of what the slicer really produced; the Chekhov
  U1 session built these views by hand from the G-code each time (`render_gcode.py`).
INVARIANTS:
  * Pure numpy + the repo's PNG writer (`gcode_overhangs.write_png`, zlib): works without PIL.
  * Top view geometry is fixed so callers/tests can map mm to pixels: pixel column = int(x * TOP_SCALE),
    pixel row = H - 1 - int(y * TOP_SCALE), H = int(bed_y * TOP_SCALE).
  * Front view looks along +Y: only outer-wall segments on the near half (y <= mid-plane) are drawn, farther ones
    first so near ones overwrite them. Row 0 is the top of the model.
  * Unknown tools (not in `colours`) draw grey; extrusion with no `Tn` command (single-tool slice) counts as tool 0.
ASSUMPTIONS: the front of the part faces -Y (as in the print frame of `prep.frame`).
Past bugs: none yet.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import InvalidArgument
from gcode_overhangs import write_png
from prep.gcode_gates import Parsed

VIEWS = ("front", "top", "closeup")
TOP_SCALE = 2.2  # px per mm of the top view
FRONT_SCALE = (
    7.0  # px per mm of the front view (shrunk when the model would exceed _MAX_PX)
)
_MAX_PX = 1800
_CLOSEUP_PX = 900
_CLOSEUP_LAYERS = 40  # layers in the automatic closeup window
_BACKGROUND = (90, 98, 110)
_PLATE = (235, 235, 235)
_GREY = (120, 120, 120)

Rgb = tuple[int, int, int]
Img = NDArray[np.uint8]


def _parse_colours(colours: Mapping[int, str]) -> dict[int, Rgb]:
    out: dict[int, Rgb] = {}
    for tool, text in colours.items():
        h = text.lstrip("#")
        if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
            raise InvalidArgument(f"colours[{tool}]", text, ["#RRGGBB"], command="prep")
        out[tool] = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return out


def _tool_rgb(cols: Mapping[int, Rgb], tool: int | None) -> Rgb:
    return cols.get(0 if tool is None else tool, _GREY)


class _Elevation:
    """Near-half outer-wall segments of a G-code as one table: x range, layer top z, thickness, depth y, tool."""

    def __init__(self, parsed: Parsed) -> None:
        pieces: list[NDArray[np.float64]] = []
        tools: list[NDArray[np.int64]] = []
        for layer in parsed.layers:
            for r in layer.runs:
                if r.kind != "Outer wall" or len(r.pts) < 2:
                    continue
                p, q = r.pts[:-1], r.pts[1:]
                n = len(p)
                pieces.append(
                    np.column_stack(
                        [
                            p[:, 0],
                            q[:, 0],
                            (p[:, 1] + q[:, 1]) / 2,
                            np.full(n, layer.z),
                            np.full(n, layer.h),
                        ]
                    )
                )
                tools.append(
                    np.full(n, -1 if r.tool is None else r.tool, dtype=np.int64)
                )
        if not pieces:
            raise InvalidArgument(
                "G-code",
                parsed.path,
                ["a slice with `;TYPE:Outer wall` extrusion"],
                command="prep",
                extra="nothing to draw in the front view",
            )
        table = np.concatenate(pieces)
        tool = np.concatenate(tools)
        mid = (float(table[:, 2].min()) + float(table[:, 2].max())) / 2
        near = table[:, 2] <= mid
        order = np.argsort(-table[near, 2], kind="stable")  # far (large y) first
        self.t = table[near][order]
        self.tool = tool[near][order]
        self.x_min = float(np.minimum(table[:, 0], table[:, 1]).min())
        self.x_max = float(np.maximum(table[:, 0], table[:, 1]).max())
        self.z_max = float(table[:, 3].max())


def _paint_front(
    el: _Elevation,
    cols: Mapping[int, Rgb],
    x0: float,
    z0: float,
    scale: float,
    w: int,
    h: int,
) -> Img:
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = _BACKGROUND
    t = el.t
    left = np.floor((np.minimum(t[:, 0], t[:, 1]) - x0) * scale).astype(np.int64)
    right = np.ceil((np.maximum(t[:, 0], t[:, 1]) - x0) * scale).astype(np.int64)
    top = np.floor(h - (t[:, 3] - z0) * scale).astype(np.int64)
    bot = np.ceil(h - (t[:, 3] - t[:, 4] - z0) * scale).astype(np.int64)
    bot = np.maximum(bot, top + 1)  # a thin layer stays visible
    keep = (right >= 0) & (left < w) & (bot > 0) & (top < h)
    palette = {
        tool: _tool_rgb(cols, None if tool < 0 else tool)
        for tool in set(el.tool[keep].tolist())
    }
    for i in np.flatnonzero(keep):
        img[max(top[i], 0) : min(bot[i], h), max(left[i], 0) : min(right[i] + 1, w)] = (
            palette[int(el.tool[i])]
        )
    return img


def _front(parsed: Parsed, cols: Mapping[int, Rgb]) -> Img:
    el = _Elevation(parsed)
    scale = min(FRONT_SCALE, _MAX_PX / max(el.x_max - el.x_min + 8, el.z_max + 4))
    w, h = int((el.x_max - el.x_min + 8) * scale), int((el.z_max + 4) * scale)
    return _paint_front(el, cols, el.x_min - 4, 0.0, scale, w, h)


def _auto_zoom(parsed: Parsed, el: _Elevation) -> tuple[float, float, float]:
    """Centre (x, z) and half width of the window with the most tool changes, else the middle of the model."""
    layers = parsed.layers
    changes = np.array([len(layer.tool_changes) for layer in layers], dtype=np.int64)
    span = min(_CLOSEUP_LAYERS, len(layers))
    sums = np.convolve(changes, np.ones(span, dtype=np.int64), mode="valid")
    start = (
        int(np.argmax(sums)) if sums.max() > 0 else max(0, (len(layers) - span) // 2)
    )
    window = layers[start : start + span]
    cz = (window[0].z - window[0].h + window[-1].z) / 2
    cx = (el.x_min + el.x_max) / 2
    return cx, cz, max(2.0, min(20.0, (el.x_max - el.x_min) / 2))


def _closeup(
    parsed: Parsed, cols: Mapping[int, Rgb], zoom: tuple[float, float, float] | None
) -> Img:
    el = _Elevation(parsed)
    cx, cz, hw = zoom if zoom is not None else _auto_zoom(parsed, el)
    scale = _CLOSEUP_PX / (2 * hw)
    h = int(2 * hw * 0.55 * scale)
    return _paint_front(el, cols, cx - hw, cz - hw * 0.55, scale, _CLOSEUP_PX, h)


def _top(parsed: Parsed, cols: Mapping[int, Rgb], bed_mm: tuple[float, float]) -> Img:
    w, h = int(bed_mm[0] * TOP_SCALE), int(bed_mm[1] * TOP_SCALE)
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = _PLATE
    img[:2, :] = img[-2:, :] = img[:, :2] = img[:, -2:] = 0  # bed outline
    for layer in (
        parsed.layers
    ):  # later layers overwrite earlier ones: the top of the print wins
        for r in layer.runs:
            if len(r.pts) < 2 or r.kind == "Custom":
                continue
            rgb = np.array(_tool_rgb(cols, r.tool), dtype=np.float64)
            if r.kind == "Prime tower":
                rgb = rgb * 0.7
            p, q = r.pts[:-1] * TOP_SCALE, r.pts[1:] * TOP_SCALE
            n = np.maximum(np.ceil(np.hypot(*(q - p).T)).astype(np.int64), 1) + 1
            seg = np.repeat(np.arange(len(p)), n)
            frac = (
                np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
            ) / np.repeat(n - 1, n)
            xy = p[seg] + (q[seg] - p[seg]) * frac[:, None]
            col = xy[:, 0].astype(np.int64)
            row = h - 1 - xy[:, 1].astype(np.int64)
            ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
            img[row[ok], col[ok]] = rgb.astype(np.uint8)
    return img


def render_layers(
    parsed: Parsed,
    outdir: str | Path,
    *,
    views: Sequence[str] = VIEWS,
    colours: Mapping[int, str],
    bed_mm: tuple[float, float] = (270.0, 270.0),
    zoom: tuple[float, float, float] | None = None,
) -> list[Path]:
    """Write `<stem>_<view>.png` for each view into `outdir` and return the paths in `views` order.

    `colours` maps tool number -> `#RRGGBB`. `zoom` = (centre x mm, centre z mm, half width mm) for `closeup`.
    """
    unknown = [v for v in views if v not in VIEWS]
    if unknown:
        raise InvalidArgument("views", ",".join(unknown), list(VIEWS), command="prep")
    cols = _parse_colours(colours)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(parsed.path).stem
    paths: list[Path] = []
    for view in views:
        img = (
            _front(parsed, cols)
            if view == "front"
            else _closeup(parsed, cols, zoom)
            if view == "closeup"
            else _top(parsed, cols, bed_mm)
        )
        path = out / f"{stem}_{view}.png"
        write_png(str(path), img)
        paths.append(path)
    return paths


def layer_histogram(parsed: Parsed) -> dict[str, Any]:
    """Plain data for the layer-height chart: per-layer z/height/tool changes and a 0.01 mm height histogram.

    `by_height` maps a height rounded to 0.01 mm (string key) to its layer count; `height_mm_by_bin` to the total
    thickness printed at that height. The first layer is reported separately (`first_layer_h`) and excluded from
    the histogram, like the layer-height gate.
    """
    layers = parsed.layers
    body = layers[1:] if len(layers) > 1 else layers
    counts: Counter[str] = Counter()
    thickness: defaultdict[str, float] = defaultdict(float)
    for layer in body:
        key = f"{round(layer.h, 2):.2f}"
        counts[key] += 1
        thickness[key] += layer.h
    hs = [layer.h for layer in body]
    per_tool: Counter[str] = Counter()
    for layer in layers:
        for t in layer.model_tools:
            per_tool[str(t)] += 1
    return {
        "layers": len(layers),
        "first_layer_h": layers[0].h if layers else None,
        "z": [layer.z for layer in layers],
        "h": [layer.h for layer in layers],
        "tool_changes_per_layer": [len(layer.tool_changes) for layer in layers],
        "by_height": dict(sorted(counts.items())),
        "height_mm_by_bin": {k: round(v, 4) for k, v in sorted(thickness.items())},
        "min": min(hs) if hs else None,
        "max": max(hs) if hs else None,
        "median": float(np.median(hs)) if hs else None,
        "layers_per_tool": dict(sorted(per_tool.items())),
        "tool_changes": parsed.n_tool_changes,
        "tool_changes_per_layer_mean": round(parsed.n_tool_changes / len(layers), 3)
        if layers
        else 0.0,
    }


__all__ = ["VIEWS", "TOP_SCALE", "layer_histogram", "render_layers"]
