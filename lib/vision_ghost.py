"""vision_ghost.py — 3D ghost of the rest of a print over the chamber camera (issue #261).

The running job's G-code knows every wall still to come. `ShellBuilder` collects the walls
(outer/overhang walls, prime tower) while a caller streams the G-code past it once, `Shell`
keeps them compactly, and `render()` turns them into a `Ghost` for one layer:

- the walls become vertical quads (one band per kept layer, tiling the height);
- the quads are painted far-to-near into one id buffer through the printer's `CameraModel`
  (painter's z-buffer: the quads are ~1 mm wide, so depth order per quad is enough);
- printed walls are painted only to occlude — the real part is in the frame — and only when
  the camera is below the current top (from above it they can hide nothing above the top);
  the walls above the current layer become the ghost: filament colour washed towards pale
  cyan, shaded by wall orientation, more transparent with depth, outlined at silhouettes and
  depth steps, lightly hatched;
- the band just printed glows, marking "now".

A `Ghost` is a sparse blend table (pixel index, premultiplied colour, inverse alpha), so
applying it to every camera frame is one gather/scatter; render once per layer change.

Needs numpy and Pillow (`render` imports PIL lazily). Bed-moves-Z printers (Snapmaker U1) and
gantry-Z printers both work: the camera model maps bed points at gcode Z to pixels.
"""

from __future__ import annotations

import math
import re
from array import array
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from vision_geometry import CameraModel

# Slicer feature names (lower case, Orca/Bambu/Prusa spellings) whose paths form a part's skin.
SHELL_FEATURES = frozenset(
    {
        "outer wall",
        "overhang wall",
        "external perimeter",
        "overhang perimeter",
        "wall-outer",
        "prime tower",
        "wipe tower",
    }
)
# A visible ghost of tool T uses filament_colour[T]; without one, this palette.
FALLBACK_COLOURS = ("#4FC3F7", "#AED581", "#FFB74D", "#F06292", "#BA68C8", "#E0E0E0")
_HEX = re.compile(r"#?([0-9a-fA-F]{6})")

# look
OPACITY_NEAR = 0.80  # alpha of the nearest ghost wall
OPACITY_FAR = 0.28  # alpha of the farthest one
EDGE_OPACITY = 0.95  # silhouette / depth-step lines (fades with depth by 1/3)
DEPTH_STEP_MM = 12.0  # depth jump between neighbouring pixels drawn as an edge
HATCH_PERIOD = 16  # px between diagonal hatch lines
GLOW = (255, 190, 50)  # the band printed now
GLOW_CORE = (255, 238, 190)
NOW_BAND_MM = (
    0.4  # height of the glowing band when only the top of the printed part is painted
)
MIN_LUMA = 140  # dark filaments are lifted towards this so they read on a dark chamber
# the ghost is the filament colour washed towards a pale cyan, so it never reads as the real part
GHOST_TINT = (230, 248, 255)
TINT = 0.3


@dataclass(frozen=True)
class Shell:
    """Wall polylines of one job: `xy[starts[i]:starts[i+1]]` is polyline i of `layer[i]`,
    printed by `tool[i]` (-1 unknown). `layer_z` maps a layer number to its top Z (mm)."""

    xy: np.ndarray
    starts: np.ndarray
    layer: np.ndarray
    tool: np.ndarray
    layer_z: dict[int, float]
    colours: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def points(self) -> int:
        return int(self.starts[-1]) if len(self.starts) else 0


class ShellBuilder:
    """Collects a job's walls while its G-code streams past, one call per line.

    The caller owns the parsing (layer numbering, feature names, tool, E bookkeeping) and
    reports each motion with `move()` and each comment with `comment()`; points closer than
    `min_step_mm` are merged, and past `max_points` collection stops (`Shell.truncated`).
    """

    def __init__(self, max_points: int = 3_000_000, min_step_mm: float = 0.5) -> None:
        self.max_points = max_points
        self.min_step = min_step_mm
        self._xy = array("f")
        self._starts = array("q", [0])
        self._layer = array("i")
        self._tool = array("h")
        self._layer_z: dict[int, float] = {}
        self._colours: tuple[str, ...] = ()
        self._truncated = False
        self._run: list[float] = []
        self._key: tuple[int, int] | None = None
        self._x: float | None = None
        self._y: float | None = None

    def comment(self, text: str) -> None:
        """A G-code comment without the leading ';' (only `filament_colour = ...` is used)."""
        if self._colours or not text.startswith(("filament_colour", "extruder_colour")):
            return
        _, _, value = text.partition("=")
        found = tuple(
            "#" + m.group(1).upper() for m in map(_HEX.search, value.split(";")) if m
        )
        if found:
            self._colours = found

    def move(
        self,
        layer: int,
        z: float | None,
        tool: int | None,
        feature: str | None,
        x: float | None,
        y: float | None,
        extruding: bool,
        arc: tuple[float, float, bool] | None = None,
    ) -> None:
        """One G0/G1/G2/G3: absolute X/Y if given, whether it extrudes, and for G2/G3 the
        centre offset and direction `arc=(I, J, clockwise)` (sampled into chords)."""
        nx = self._x if x is None else x
        ny = self._y if y is None else y
        if layer > 0 and z is not None:
            self._layer_z.setdefault(layer, z)
        x0, y0 = self._x, self._y
        self._x, self._y = nx, ny
        if nx is None or ny is None or x0 is None or y0 is None:
            return
        full_turn = arc is not None and (arc[0], arc[1]) != (0.0, 0.0)
        if (nx, ny) == (x0, y0) and not full_turn:
            return
        if not (
            extruding
            and layer > 0
            and z is not None
            and feature is not None
            and feature.lower() in SHELL_FEATURES
        ):
            self._flush()
            return
        if self._truncated:
            return
        key = (layer, -1 if tool is None else tool)
        if key != self._key:
            self._flush()
            self._key = key
        if not self._run:
            self._run = [x0, y0]
        points = self._arc(x0, y0, nx, ny, arc) if arc is not None else [(nx, ny)]
        for px, py in points:
            self._add(px, py)

    def _arc(
        self, x0: float, y0: float, x1: float, y1: float, arc: tuple[float, float, bool]
    ) -> list[tuple[float, float]]:
        """Chord points of a G2/G3 arc from (x0, y0) to (x1, y1); a closed arc is a full turn."""
        cx, cy = x0 + arc[0], y0 + arc[1]
        r = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        sweep = math.atan2(y1 - cy, x1 - cx) - a0
        if arc[2]:  # clockwise
            sweep = sweep - 2 * math.pi if sweep >= 0 else sweep
        else:
            sweep = sweep + 2 * math.pi if sweep <= 0 else sweep
        # chords no shorter than min_step, so the point merging keeps every one of them
        step = max(self.min_step, 0.25)
        n = min(720, max(1, math.floor(abs(sweep) * r / step * 0.98)))
        pts = [
            (
                cx + r * math.cos(a0 + sweep * k / n),
                cy + r * math.sin(a0 + sweep * k / n),
            )
            for k in range(1, n)
        ]
        return [*pts, (x1, y1)]

    def _add(self, nx: float, ny: float) -> None:
        run = self._run
        if len(self._xy) // 2 + len(run) // 2 >= self.max_points:
            self._truncated, self._run = True, []  # a ghost missing walls would mislead
            return
        # the last point is provisional: it stays only once it is min_step from the one before
        if (
            len(run) >= 4
            and math.hypot(run[-2] - run[-4], run[-1] - run[-3]) < self.min_step
        ):
            run[-2], run[-1] = nx, ny
        else:
            run += (nx, ny)

    def _flush(self) -> None:
        run, self._run = self._run, []
        if len(run) < 4 or self._key is None or self._truncated:
            return
        self._xy.extend(run)
        self._starts.append(len(self._xy) // 2)
        self._layer.append(self._key[0])
        self._tool.append(self._key[1])

    def finish(self) -> Shell:
        import numpy as np

        self._flush()
        return Shell(
            xy=np.frombuffer(self._xy, dtype=np.float32).reshape(-1, 2).copy(),
            starts=np.frombuffer(self._starts, dtype=np.int64).copy(),
            layer=np.frombuffer(self._layer, dtype=np.int32).copy(),
            tool=np.frombuffer(self._tool, dtype=np.int16).copy(),
            layer_z=dict(self._layer_z),
            colours=self._colours,
            truncated=self._truncated,
        )


def bands(
    layer_z: dict[int, float], lo: int, hi: int, stride: int, floor: float
) -> dict[int, tuple[float, float]]:
    """Every `stride`-th layer of lo..hi (always including hi) -> (h0, h1): consecutive kept
    layers tile the height from `floor` without gaps."""
    kept = sorted(n for n in layer_z if lo <= n <= hi)[::-1][::stride][::-1]
    out: dict[int, tuple[float, float]] = {}
    prev = floor
    for n in kept:
        out[n] = (prev, layer_z[n])
        prev = layer_z[n]
    return out


def wall_quads(
    shell: Shell, keep: dict[int, tuple[float, float]]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vertical quads (Q x 4 x 3, bed mm: a@h0, b@h0, b@h1, a@h1) of every segment of the kept
    layers' polylines, with each quad's tool and layer."""
    import numpy as np

    if not keep or not len(shell.layer):
        empty = np.zeros((0, 4, 3), np.float32)
        return empty, np.zeros(0, np.int16), np.zeros(0, np.int32)
    lay = np.fromiter(keep, dtype=np.int32)
    sel = np.nonzero(np.isin(shell.layer, lay))[0]
    h0_of = np.zeros(int(lay.max()) + 1, np.float32)
    h1_of = np.zeros(int(lay.max()) + 1, np.float32)
    for n, (lo_h, hi_h) in keep.items():
        h0_of[n], h1_of[n] = lo_h, hi_h
    lo, hi = shell.starts[sel], shell.starts[sel + 1]
    nseg = (hi - lo - 1).astype(np.int64)
    a = np.repeat(lo, nseg) + (
        np.arange(int(nseg.sum())) - np.repeat(np.cumsum(nseg) - nseg, nseg)
    )
    pa, pb = shell.xy[a], shell.xy[a + 1]
    which = np.repeat(sel, nseg)
    layer = shell.layer[which]
    h0, h1 = h0_of[layer][:, None], h1_of[layer][:, None]
    q = np.stack(
        [
            np.hstack([pa, h0]),
            np.hstack([pb, h0]),
            np.hstack([pb, h1]),
            np.hstack([pa, h1]),
        ],
        1,
    )
    return q, shell.tool[which], layer


def palette(colours: tuple[str, ...], tools: int) -> np.ndarray:
    """RGB float palette per tool: the filament colour, dark ones lifted to stay visible, washed
    `TINT` of the way towards `GHOST_TINT`."""
    import numpy as np

    out = []
    for t in range(max(tools, 1)):
        hexa = (
            colours[t]
            if t < len(colours)
            else FALLBACK_COLOURS[t % len(FALLBACK_COLOURS)]
        )
        out.append([int(hexa[i : i + 2], 16) for i in (1, 3, 5)])
    pal = np.array(out, np.float32)
    lum = pal.max(axis=1, keepdims=True)
    lifted = np.where(lum < MIN_LUMA, pal + (MIN_LUMA - lum) * 0.9 + 20, pal)
    washed = lifted * (1 - TINT) + np.array(GHOST_TINT, np.float32) * TINT
    return np.clip(washed, 0, 255)


@dataclass(frozen=True)
class Ghost:
    """A rendered ghost for one (job, layer, frame size): blend it onto every frame."""

    shape: tuple[int, int]
    index: np.ndarray  # flat pixel indices, int64
    inv_alpha: np.ndarray  # (n, 1) uint16, 256 - alpha * 256
    premult: np.ndarray  # (n, 3) uint16, colour * alpha * 256
    layer: int = field(default=0, compare=False)

    def blend(self, rgb: np.ndarray) -> np.ndarray:
        """Blend into an H x W x 3 uint8 RGB frame in place; returns it."""
        if rgb.shape[:2] != self.shape:
            raise ValueError(f"ghost is {self.shape}, frame is {rgb.shape[:2]}")
        flat = rgb.reshape(-1, 3)
        sub = flat[self.index].astype("uint16")
        flat[self.index] = ((sub * self.inv_alpha + self.premult) >> 8).astype("uint8")
        return rgb


def render(
    shell: Shell,
    cam: CameraModel,
    layer: int,
    shape: tuple[int, int],
    *,
    printed_stride: int = 3,
    ghost_stride: int = 2,
) -> Ghost | None:
    """The ghost of everything above `layer` (the layer printing now) for a frame of `shape`
    (H, W); None when there is nothing trustworthy to draw (truncated shell, unknown layer,
    nothing left)."""
    import numpy as np

    if shell.truncated or layer not in shell.layer_z:
        return None
    z = shell.layer_z[layer]
    last = max(shell.layer_z)
    first = min(shell.layer_z)
    ghost_keep = bands(shell.layer_z, layer + 1, last, ghost_stride, z)
    if not ghost_keep:
        return None
    eye = cam.camera_center()
    top = float(cam.world(np.array([[0.0, 0.0, z]]), z)[0, 2])
    if eye[2] < top:
        # a camera below the current top looks up past the printed walls: they occlude
        printed = bands(shell.layer_z, first, layer, printed_stride, 0.0)
    else:
        # Printed walls lie at or below the top; every sight line from a camera above it to
        # a ghost point stays above it, so they can hide nothing. Paint only the band printed
        # now, for its glow.
        printed = {layer: (max(0.0, z - NOW_BAND_MM), z)}
    qp, tp, lp = wall_quads(shell, printed)
    qg, tg, _ = wall_quads(shell, ghost_keep)
    if not len(qg):
        return None
    q = np.concatenate([qp, qg]).astype(float)
    tools = np.concatenate([tp, tg]).astype(np.int64)
    is_ghost = np.concatenate([np.zeros(len(qp), bool), np.ones(len(qg), bool)])
    is_now = np.concatenate([lp == layer, np.zeros(len(qg), bool)])

    h, w = shape
    uv = cam.project(q.reshape(-1, 3), z).reshape(-1, 4, 2) * [
        w / cam.width,
        h / cam.height,
    ]
    # far-to-near order by the distance from the camera to each quad's centre
    centre = cam.world(q.mean(axis=1), z)
    depth = np.linalg.norm(centre - eye, axis=1)
    idx = _paint(uv, depth, (h, w))

    # shade by how squarely each wall faces the camera (reads as 3D without a light model)
    d = q[:, 1, :2] - q[:, 0, :2]
    normal = (
        np.column_stack([d[:, 1], -d[:, 0]])
        / np.maximum(1e-9, np.hypot(d[:, 0], d[:, 1]))[:, None]
    )
    view = eye[:2] - centre[:, :2]
    view /= np.maximum(1e-9, np.linalg.norm(view, axis=1, keepdims=True))
    shade = 0.62 + 0.45 * np.abs((normal * view).sum(axis=1))

    g = _pad(is_ghost, False)[idx + 1]
    now = _pad(is_now, False)[idx + 1]
    if not g.any():
        return None
    ys, xs = np.nonzero(g | now)
    y0, y1 = max(0, ys.min() - 8), min(h, ys.max() + 9)
    x0, x1 = max(0, xs.min() - 8), min(w, xs.max() + 9)
    ci = idx[y0:y1, x0:x1]
    g, now = g[y0:y1, x0:x1], now[y0:y1, x0:x1]
    dep = np.where(ci >= 0, _pad(depth, 0.0)[ci + 1], 0.0)
    pal = palette(shell.colours, int(tools.max()) + 1 if len(tools) else 1)
    col = (
        pal[np.clip(_pad(tools, 0)[ci + 1], 0, len(pal) - 1)]
        * _pad(shade, 1.0)[ci + 1][..., None]
    )
    alpha, col = _style(g, now, dep, col, (x0, y0))
    keep = alpha > 1 / 256
    rows, cols = np.nonzero(keep)
    a = alpha[keep]
    c = col[keep]
    index = (rows + y0).astype(np.int64) * w + (cols + x0)
    a256 = np.round(a * 256).astype(np.uint16)
    return Ghost(
        shape=(h, w),
        index=index,
        inv_alpha=(256 - a256)[:, None],
        premult=np.round(c * a[:, None] * 256).astype(np.uint16),
        layer=layer,
    )


def _pad(values: np.ndarray, empty: object) -> np.ndarray:
    """`values` with `empty` prepended, so `_pad(v, e)[ids + 1]` maps id -1 (no quad) to e."""
    import numpy as np

    return np.concatenate([np.asarray([empty], dtype=values.dtype), values])


def _paint(uv: np.ndarray, depth: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Quad ids (-1 = none) after painting every finite quad far-to-near."""
    import numpy as np
    from PIL import Image, ImageDraw

    h, w = shape
    img = Image.new("I", (w, h), 0)
    draw = ImageDraw.Draw(img)
    ok = np.all(np.isfinite(uv), axis=(1, 2))
    flat = uv.reshape(len(uv), 8).tolist()  # one list per quad: x0, y0, ... x3, y3
    for i in np.argsort(-depth, kind="stable").tolist():
        if ok[i]:
            draw.polygon(flat[i], fill=i + 1)
    return np.asarray(img, dtype=np.int64) - 1


def _style(
    ghost: np.ndarray,
    now: np.ndarray,
    dep: np.ndarray,
    col: np.ndarray,
    origin: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel alpha and colour of the ghost crop: depth fade, edges, hatch, glow."""
    import numpy as np
    from PIL import Image, ImageFilter

    gd = dep[ghost]
    near, far = np.percentile(gd, 3), np.percentile(gd, 97)
    fade = np.clip((dep - near) / max(1e-6, far - near), 0, 1)
    alpha = np.where(ghost, OPACITY_NEAR - (OPACITY_NEAR - OPACITY_FAR) * fade, 0.0)
    inner = (
        np.asarray(
            Image.fromarray(ghost.astype(np.uint8) * 255).filter(
                ImageFilter.MinFilter(5)  # 2 px silhouette line
            )
        )
        > 0
    )
    dx = np.abs(np.diff(dep, axis=1, prepend=dep[:, :1]))
    dy = np.abs(np.diff(dep, axis=0, prepend=dep[:1]))
    edge = ghost & (~inner | (dx > DEPTH_STEP_MM) | (dy > DEPTH_STEP_MM))
    col = np.where(edge[..., None], col * 0.35 + 255 * 0.65, col)
    alpha = np.where(edge, np.maximum(alpha, EDGE_OPACITY - 0.3 * fade), alpha)
    yy, xx = np.mgrid[0 : ghost.shape[0], 0 : ghost.shape[1]]
    hatch = ghost & (((xx + yy + origin[0] + origin[1]) % HATCH_PERIOD) < 2)
    col = np.where(hatch[..., None], col * 0.75 + 255 * 0.25, col)
    alpha = (
        np.asarray(
            Image.fromarray(np.round(alpha * 255).astype(np.uint8)).filter(
                ImageFilter.GaussianBlur(0.6)
            ),
            np.float32,
        )
        / 255.0
    )
    # the band printed now: a soft glow plus a light core, over the ghost
    glow_img = Image.fromarray(now.astype(np.uint8) * 255).filter(
        ImageFilter.MaxFilter(3)
    )
    glow = np.clip(
        np.asarray(glow_img.filter(ImageFilter.GaussianBlur(5)), np.float32)
        / 255.0
        * 2.2,
        0,
        0.85,
    )
    core = (np.asarray(glow_img, np.float32) / 255.0) * 0.95  # >= 3 px line
    for weight, rgb in ((glow, GLOW), (core, GLOW_CORE)):
        # composite "over": c = (c*a*(1-w) + rgb*w) / a', a' = a + w*(1-a)
        new_a = alpha + weight * (1 - alpha)
        mix = (
            col * (alpha * (1 - weight))[..., None]
            + np.array(rgb, np.float32) * weight[..., None]
        )
        col = np.where(
            new_a[..., None] > 0, mix / np.maximum(new_a, 1e-6)[..., None], col
        )
        alpha = new_a
    return alpha, np.clip(col, 0, 255)
