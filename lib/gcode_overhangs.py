"""gcode_overhangs.py — extrusion that nothing holds up, found in the sliced G-code itself.

WHAT: for every object of a plate (EXCLUDE_OBJECT_START / M486 / `; printing object` labels; a
  file without labels is one object, "plate"), layer by layer, the object's extrusion that no
  material of the layer below carries:

    carried   the layer below (the object's own extrusion plus its support/raft, closed
              and with enclosed gaps up to --bridge-max wide filled — infill under a top skin,
              a ceiling over a short span) grown by the 45-degree reach dz * tan(45) plus half
              a line width; the bed carries every line at or below the first-layer height;
    bridged   an uncarried piece that a straight span reaches: on some row, column or
              diagonal its run is at most --bridge-max long and ends on carried material on
              both sides (a ceiling over a pocket, a bridge between walls). A strip growing
              out of one edge — a shallow underside, a cantilever — is anchored on one side
              only and stays uncarried. The slicer's own `;TYPE:Bridge` / `Overhang wall`
              labels are not trusted: it labels a shallow underside printed in the air the same;
    island    a connected piece of uncarried, unbridged extrusion over --min-area mm2 (smaller
              = raster noise); reported with its layer z, centre x/y and area.

  An object FAILs when its islands add up to more than --max-unsupported mm2. The first layer
  "in the air" is the same rule: an object whose lowest layer is above the first-layer height
  has only its supports/raft below it — with none, its whole first layer is one island.

WHY: the slicer is silent about overhangs when supports are off. A K1 overlay plate with a
  non-flat bottom printed pieces of its underside in the air; every other gate was green.
  `3d visible-faces --gcode` applies the stricter visible-side rule (any island reaching a
  declared visible skin) on top of this; this gate covers every face and every object.

OUTPUT: text or --json, and a top-down PNG (--png, default under the cache dir) with the
  objects' footprints in grey and the islands in red, darker = higher.

SPEED: only the carrier of each layer is a full raster (lines stamped as disks by shifted
  ORs; enclosed gaps measured on 0.5 mm blocks). The tests run on the pixels that stick out
  of it: reach and step-out distance by a k-d tree on the carrier's edge, bridges on sorted
  runs, islands as sparse connected components. Layers are independent given the layer
  below, so they are split into chunks over forked workers (cli.resources.pool_size). A
  30 MB K1 SE plate: ~3 s parse + ~4 s analysis (was ~60 s with full-grid distance maps).

Run: python3 lib/gcode_overhangs.py FILE.gcode [--max-unsupported MM2] [--bridge-max MM]
       [--min-area MM2] [--png PATH] [--json]
Heavy deps (numpy, scipy) are imported here only; `3d gcode-overhangs` reaches this via
cli.pyrun and `3d print-preflight` runs it as a subprocess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
import zlib
from array import array
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

MAX_UNSUPPORTED_MM2 = (
    100.0  # calibrated: BASE2/3/4 ceilings and grass tips stay below, OVL1 is ~3100
)
BRIDGE_MAX_MM = 15.0
MIN_AREA_MM2 = 2.0  # grass-blade tips and raster noise are smaller
LEDGE_MAX_MM = (
    0.6  # a step-out this narrow is printed as an overhang perimeter: WARN, not FAIL
)
OVERHANG_DEG = 45.0
PX_MM = 0.1
_MAX_CELLS = 6_000_000
_TAIL_BYTES = 1 << 20
_Z_QUANT = 0.001
_PLATE = "plate"
_KEEP = 20  # islands listed per object (the total area counts all of them)
_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_MOVE = re.compile(r"G0*([0-3])(?![\d.])")
_CONFIG = re.compile(r"^;\s*([A-Za-z_][A-Za-z0-9_ \[\]]*?)\s*=\s*(.*)$")
_NAME = re.compile(r"NAME=(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE)
_EXCLUDED_TYPES = ("skirt", "brim", "custom", "prime", "wipe", "purge", "ironing")
_SUPPORT_TYPES = ("support", "raft")
OWN, SUPPORT = 0, 1


@dataclass
class Island:
    z: float
    x: float
    y: float
    area_mm2: float
    first_layer: bool = False
    kind: str = "island"  # "island" or "ledge" (a strip no wider than --ledge-max: an edge step-out)


@dataclass
class ObjectResult:
    name: str
    status: str
    detail: str
    layers: int = 0
    base_z: float | None = None
    unsupported_mm2: float = 0.0
    ledge_mm2: float = 0.0
    islands: list[Island] = field(default_factory=list)
    island_count: int = 0


class _Obj:
    """Extrusion of one object as compact arrays: (z, kind) -> x0, y0, x1, y1, width per line."""

    def __init__(self) -> None:
        self.lines: dict[tuple[float, int], array[float]] = defaultdict(
            lambda: array("d")
        )

    def add(
        self, z: float, kind: int, x0: float, y0: float, x1: float, y1: float, w: float
    ) -> None:
        self.lines[(z, kind)].extend((x0, y0, x1, y1, w))

    def get(self, z: float, kind: int) -> NDArray[np.float64]:
        import numpy as np

        a = self.lines.get((z, kind))
        return np.frombuffer(a, float).reshape(-1, 5) if a else np.zeros((0, 5))

    def zs(self, kind: int | None = None) -> list[float]:
        return sorted({z for z, k in self.lines if kind is None or k == kind})


@dataclass
class Plate:
    objects: dict[str, _Obj]
    defined: list[str]
    config: dict[str, str]
    first_z: float | None


def read_config(path: str) -> dict[str, str]:
    """Slicer settings from `; key = value` comments at the file's head and tail."""
    size = os.path.getsize(path)
    chunks: list[bytes] = []
    with open(path, "rb") as fh:
        chunks.append(fh.read(min(size, 256 * 1024)))
        if size > 256 * 1024:
            fh.seek(max(256 * 1024, size - _TAIL_BYTES))
            chunks.append(fh.read())
    config: dict[str, str] = {}
    for chunk in chunks:
        for raw in chunk.decode("utf-8", "replace").splitlines():
            if raw.startswith(";"):
                m = _CONFIG.match(raw)
                if m:
                    config.setdefault(m.group(1).strip(), m.group(2).strip())
    return config


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        v = float(value.split(",")[0].strip().rstrip("%"))
    except ValueError:
        return None
    return v if v > 0 else None


def default_width(config: dict[str, str]) -> float:
    for key in (
        "line_width",
        "extrusion_width",
        "outer_wall_line_width",
        "perimeter_extrusion_width",
    ):
        v = _float(config.get(key))
        if v is not None and v < 3:  # a percentage of the nozzle reads as e.g. 112.5
            return v
    return (_float(config.get("nozzle_diameter")) or 0.4) * 1.125


def _label(line: str, current: str | None) -> tuple[bool, str | None]:
    """(is a label line, object now printing); None = between objects."""
    up = line[:24].upper()
    if up.startswith("EXCLUDE_OBJECT_START"):
        m = _NAME.search(line)
        return True, m.group(1).strip("\"'") if m else "?"
    if up.startswith("EXCLUDE_OBJECT_END"):
        return True, None
    if up.startswith("M486"):
        words = dict(_WORD.findall(line.split(";", 1)[0].upper()[4:]))
        if "S" in words:
            n = int(float(words["S"]))
            return True, None if n < 0 else f"object {n}"
        return True, current
    if line.startswith("; printing object "):
        return True, line[len("; printing object ") :].strip()
    if line.startswith("; stop printing object"):
        return True, None
    return False, current


def parse(path: str) -> Plate:
    """Every extruding XY line of the plate after the first layer marker (start G-code purges
    are not the plate; a file without markers is read from the start), per object and z."""
    from printer_ops.move_bounds import Move

    config = read_config(path)
    width_default = default_width(config)
    objects: dict[str, _Obj] = defaultdict(_Obj)
    pre: dict[str, _Obj] = defaultdict(_Obj)
    defined: list[str] = []
    labeled = marked = False
    obj: str | None = None
    ftype = ""
    width = width_default
    x: float | None = None  # unknown until the first absolute move, and again after G28
    y: float | None = None
    z = e = 0.0
    rel, abs_e = False, True
    first_z: float | None = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            c0 = line[0]
            if c0 == ";":
                if line.startswith(";TYPE:"):
                    ftype = line[6:].strip().lower()
                elif line.startswith((";WIDTH:", "; WIDTH:")):
                    width = _float(line.split(":", 1)[1]) or width_default
                elif line.startswith((";LAYER:", ";LAYER_CHANGE", "; CHANGE_LAYER")):
                    marked = True
                else:
                    is_label, obj = _label(line, obj)
                    labeled = labeled or is_label
                continue
            up = line.split(";", 1)[0].strip().upper()
            if up.startswith("EXCLUDE_OBJECT_DEFINE"):
                m = _NAME.search(line)
                if m:
                    defined.append(m.group(1).strip("\"'"))
                labeled = True
                continue
            if up.startswith(("EXCLUDE_OBJECT", "M486")):
                is_label, obj = _label(line, obj)
                labeled = labeled or is_label
                continue
            if c0 in "Mm":
                head = up.split(" ", 1)[0]
                if head in ("M82", "M83"):
                    abs_e = head == "M82"
                continue
            if c0 not in "Gg":
                continue
            if up.startswith(("G90", "G91", "G92", "G28")) and not up[3:4].isdigit():
                if up.startswith("G90"):
                    rel = False
                elif up.startswith("G91"):
                    rel = True
                elif up.startswith("G28"):
                    homed = set(re.findall(r"[XYZ]", up[3:]))
                    if not homed or "X" in homed:
                        x = None
                    if not homed or "Y" in homed:
                        y = None
                else:
                    words = dict(_WORD.findall(up[3:]))
                    if "E" in words or not words:
                        e = float(words.get("E", 0))
                continue
            m = _MOVE.match(up)
            if not m:
                continue
            words = dict(_WORD.findall(up[m.end() :]))
            g = int(m.group(1))
            nx, ny = x, y
            if "X" in words:
                nx = (
                    float(words["X"])
                    if not rel
                    else (x + float(words["X"]) if x is not None else None)
                )
            if "Y" in words:
                ny = (
                    float(words["Y"])
                    if not rel
                    else (y + float(words["Y"]) if y is not None else None)
                )
            if "Z" in words:
                z = z + float(words["Z"]) if rel else float(words["Z"])
            extruding = False
            if "E" in words:
                ev = float(words["E"])
                if rel or not abs_e:
                    extruding, e = ev > 1e-6, e + ev
                else:
                    extruding, e = ev > e + 1e-6, ev
            owner = obj if labeled else _PLATE
            if (
                extruding
                and owner is not None
                and x is not None
                and y is not None
                and nx is not None
                and ny is not None
                and (nx != x or ny != y or g in (2, 3))
                and not any(t in ftype for t in _EXCLUDED_TYPES)
            ):
                kind = SUPPORT if any(t in ftype for t in _SUPPORT_TYPES) else OWN
                zq = round(round(z / _Z_QUANT) * _Z_QUANT, 4)
                if marked and (first_z is None or zq < first_z):
                    first_z = zq  # the plate's first layer: object, support or raft
                store = (objects if marked else pre)[owner]
                if g in (2, 3):
                    ci, cj = float(words.get("I", 0)), float(words.get("J", 0))
                    for c in (
                        Move.arc_move(x, y, nx, ny, ci, cj, g == 2).chords(0.2) or []
                    ):
                        store.add(zq, kind, c.x0, c.y0, c.x1, c.y1, width)
                else:
                    store.add(zq, kind, x, y, nx, ny, width)
            x, y = nx, ny
    if not marked:
        objects = pre
        every = [z for o in objects.values() for z in o.zs()]
        first_z = min(every) if every else None
    return Plate(dict(objects), defined, config, first_z)


class _Raster:
    """One object's raster: origin, pixel size, shape; thick lines drawn into windows of it."""

    def __init__(self, lines: NDArray[np.float64]) -> None:
        import numpy as np

        self.np = np
        margin = 2.0 + float(lines[:, 4].max())
        lo = np.minimum(lines[:, [0, 1]].min(0), lines[:, [2, 3]].min(0)) - margin
        hi = np.maximum(lines[:, [0, 1]].max(0), lines[:, [2, 3]].max(0)) + margin
        w, h = hi - lo
        self.px = max(PX_MM, math.sqrt(w * h / _MAX_CELLS))
        self.origin = lo
        self.shape = (
            int(math.ceil(w / self.px)) + 1,
            int(math.ceil(h / self.px)) + 1,
        )  # (x, y)

    def window(self, lines: NDArray[np.float64], pad_mm: float) -> tuple[slice, slice]:
        np = self.np
        wmax = lines[:, 4].max()
        lo = np.minimum(lines[:, [0, 1]].min(0), lines[:, [2, 3]].min(0)) - pad_mm - wmax
        hi = np.maximum(lines[:, [0, 1]].max(0), lines[:, [2, 3]].max(0)) + pad_mm + wmax
        a = np.maximum(((lo - self.origin) / self.px).astype(int), 0)
        b = np.minimum(((hi - self.origin) / self.px).astype(int) + 2, self.shape)
        return slice(int(a[0]), int(b[0])), slice(int(a[1]), int(b[1]))

    def draw(
        self, lines: NDArray[np.float64], win: tuple[slice, slice]
    ) -> NDArray[np.bool_]:
        """Union of the lines as stadiums of their width, in window `win`: centre lines
        sampled less than a pixel apart, dilated by a disk per width."""
        np = self.np
        shape = (win[0].stop - win[0].start, win[1].stop - win[1].start)
        out = np.zeros(shape, dtype=bool)
        if not len(lines):
            return out
        off = self.origin + self.px * np.array([win[0].start, win[1].start])
        radius = np.maximum(np.rint(lines[:, 4] / 2 / self.px), 0).astype(int)
        for r in np.unique(radius):
            a = lines[radius == r]
            length = np.hypot(a[:, 2] - a[:, 0], a[:, 3] - a[:, 1])
            # steps under one pixel: any rounding then moves at most one pixel per sample, so
            # the centre line has no holes (at exactly one pixel, float error on .5 positions
            # skipped every other pixel of an axis-aligned line)
            n = np.ceil(length / (0.9 * self.px)).astype(int) + 1
            idx = np.repeat(np.arange(len(a)), n)
            t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.maximum(
                np.repeat(n - 1, n), 1
            )
            i = np.rint((a[idx, 0] + (a[idx, 2] - a[idx, 0]) * t - off[0]) / self.px).astype(np.int32)
            j = np.rint((a[idx, 1] + (a[idx, 3] - a[idx, 1]) * t - off[1]) / self.px).astype(np.int32)
            ok = (i >= 0) & (i < shape[0]) & (j >= 0) & (j < shape[1])
            centre = np.zeros(shape, dtype=bool)
            centre[i[ok], j[ok]] = True
            out |= _dilate_disk(centre, int(r))
        return out


def _shift_or(dst: NDArray[np.bool_], src: NDArray[np.bool_], k: int, axis: int) -> None:
    """dst |= src shifted by k pixels along axis (zeros shifted in)."""
    if k == 0:
        dst |= src
    elif axis == 0:
        if k > 0:
            dst[k:] |= src[:-k]
        else:
            dst[:k] |= src[-k:]
    elif k > 0:
        dst[:, k:] |= src[:, :-k]
    else:
        dst[:, :k] |= src[:, -k:]


def _dilate_disk(a: NDArray[np.bool_], r: int) -> NDArray[np.bool_]:
    """Binary dilation by the disk x*x + y*y <= r*r + 0.25, as row runs: O(r) shifted ORs
    instead of one pass per disk pixel."""
    if r <= 0:
        return a
    half = [int(math.floor(math.sqrt(r * r + 0.25 - dy * dy))) for dy in range(r + 1)]
    rows = {0: a}
    cur = a.copy()
    for k in range(1, half[0] + 1):
        _shift_or(cur, a, k, 1)
        _shift_or(cur, a, -k, 1)
        rows[k] = cur.copy()
    out = rows[half[0]].copy()
    for dy in range(1, r + 1):
        _shift_or(out, rows[half[dy]], dy, 0)
        _shift_or(out, rows[half[dy]], -dy, 0)
    return out


def _cross(a: NDArray[np.bool_], grow: bool) -> NDArray[np.bool_]:
    """One step of dilation (grow) or erosion by the 4-neighbour cross; erosion treats the
    outside as set (the windows carry a margin, nothing sits on their edge)."""
    o = a.copy()
    if grow:
        o[1:] |= a[:-1]
        o[:-1] |= a[1:]
        o[:, 1:] |= a[:, :-1]
        o[:, :-1] |= a[:, 1:]
    else:
        o[1:] &= a[:-1]
        o[:-1] &= a[1:]
        o[:, 1:] &= a[:, :-1]
        o[:, :-1] &= a[:, 1:]
    return o


def _carrying_body(
    layer: NDArray[np.bool_], px: float, bridge_max: float
) -> NDArray[np.bool_]:
    """The layer as a carrier: raster gaps closed, enclosed gaps no wider than a bridge filled
    (a top skin over sparse infill, a short ceiling); wider enclosed voids stay open. A void's
    width is twice its deepest point, measured on 0.5 mm blocks (+ half a block)."""
    import numpy as np
    from scipy import ndimage

    body = _cross(_cross(_cross(_cross(layer, True), True), False), False) | layer
    holes, n = ndimage.label(~body)
    if n < 2:
        return body
    fill = np.ones(n + 1, dtype=bool)
    fill[0] = False
    fill[np.unique(np.concatenate([holes[0], holes[-1], holes[:, 0], holes[:, -1]]))] = False
    if not fill.any():
        return body
    inner = fill[holes]
    f = max(1, int(round(0.5 / px)))
    h, w = inner.shape[0] // f, inner.shape[1] // f
    if h and w:
        blocks = inner[: h * f, : w * f].reshape(h, f, w, f).all(axis=(1, 3))
        if blocks.any():
            depth = ndimage.distance_transform_edt(blocks) * (px * f) + px * f / 2
            ii, jj = np.nonzero(depth > bridge_max / 2)
            fill[holes[ii * f, jj * f]] = False
    return body | fill[holes]


def _runs_bridged(
    fi: NDArray[np.intp],
    fj: NDArray[np.intp],
    anchored: Any,
    span_px: float,
) -> NDArray[np.bool_]:
    """Free pixels (fi, fj) that a straight bridge reaches: on some line (row, column or
    diagonal) the free run through them is at most `span_px` long and both pixels past its
    ends are anchored — a ceiling over a pocket, a bridge between walls. A strip growing out
    of one edge (a shallow underside, a cantilever) is anchored on one side only and stays
    free. Works on the free pixels only (sorted runs), never on the whole grid."""
    import numpy as np

    out = np.zeros(len(fi), dtype=bool)
    diag = span_px / math.sqrt(2)
    for di, dj, key, along, limit in (
        (0, 1, fi, fj, span_px),
        (1, 0, fj, fi, span_px),
        (1, -1, fi + fj, fi, diag),
        (1, 1, fi - fj, fi, diag),
    ):
        order = np.lexsort((along, key))
        k, t = key[order], along[order]
        brk = (np.diff(k) != 0) | (np.diff(t) != 1)
        cut = np.flatnonzero(brk)
        starts = np.concatenate(([0], cut + 1))
        ends = np.concatenate((cut, [len(order) - 1]))
        s, e = order[starts], order[ends]
        ok = (ends - starts + 1) <= limit
        ok &= anchored(fi[s] - di, fj[s] - dj) & anchored(fi[e] + di, fj[e] + dj)
        run = np.concatenate(([0], np.cumsum(brk)))
        out[order] |= ok[run]
    return out


def _components(fi: NDArray[np.intp], fj: NDArray[np.intp]) -> tuple[int, NDArray[np.intp]]:
    """8-connected components of a sparse pixel set: (count, label per pixel)."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    stride = int(fj.max()) + 3
    ids = fi.astype(np.int64) * stride + (fj + 1)
    order = np.argsort(ids)
    sid = ids[order]
    rows, cols = [], []
    for off in (1, stride - 1, stride, stride + 1):
        pos = np.searchsorted(sid, sid + off)
        hit = pos < len(sid)
        hit[hit] = sid[pos[hit]] == sid[hit] + off
        rows.append(order[hit])
        cols.append(order[pos[hit]])
    r, c = np.concatenate(rows), np.concatenate(cols)
    graph = coo_matrix((np.ones(len(r), dtype=np.int8), (r, c)), shape=(len(fi), len(fi)))
    n, labels = connected_components(graph, directed=False)
    return int(n), labels


@dataclass
class _LayerOut:
    islands: list[Island]
    unsupported: float
    ledges: float
    marks: list[tuple[NDArray[np.intp], NDArray[np.intp], float]]  # raster pixels per island


class _Layers:
    """The per-layer state of one object: lines by z, raster, rule parameters."""

    def __init__(
        self,
        o: _Obj,
        bed_z: float,
        bridge_max: float,
        min_area: float,
        ledge_max: float,
        keep_marks: bool,
    ) -> None:
        import numpy as np

        self.o = o
        self.zs = o.zs()
        self.base = o.zs(OWN)[0]
        every = np.vstack([o.get(z, k) for z in self.zs for k in (OWN, SUPPORT) if len(o.get(z, k))])
        self.ras = _Raster(every)
        self.bed_z = bed_z
        self.bridge_max = bridge_max
        self.min_area = min_area
        self.ledge_max = ledge_max
        self.keep_marks = keep_marks

    def drawn(self, idx: int) -> tuple[NDArray[np.bool_], NDArray[np.bool_], tuple[slice, slice], float]:
        """(own, own + support, window, reach) of layer idx."""
        import numpy as np

        z = self.zs[idx]
        own, sup = self.o.get(z, OWN), self.o.get(z, SUPPORT)
        lines = np.vstack([own, sup])
        dz = z - self.zs[idx - 1] if idx else z
        reach = dz * math.tan(math.radians(OVERHANG_DEG)) + float(lines[:, 4].max()) / 2
        win = self.ras.window(lines, reach + 1.0)
        mine = self.ras.draw(own, win)
        return mine, mine | self.ras.draw(sup, win), win, reach

    def run(self, first: int, stop: int) -> tuple[list[_LayerOut], NDArray[np.bool_] | None]:
        """Layers first..stop-1; the carrier of layer first-1 is rebuilt to start from."""
        import numpy as np

        ras = self.ras
        full_prev = np.zeros(ras.shape, dtype=bool)
        footprint = np.zeros(ras.shape, dtype=bool) if self.keep_marks else None
        if first > 0:
            _, drawn, win, _ = self.drawn(first - 1)
            full_prev[win] = _carrying_body(drawn, ras.px, self.bridge_max)
        outs: list[_LayerOut] = []
        for idx in range(first, stop):
            mine, drawn, win, reach = self.drawn(idx)
            z = self.zs[idx]
            if z > self.bed_z + 1e-6 and mine.any():
                outs.append(self.layer(z, mine, full_prev[win], win, reach))
            else:
                outs.append(_LayerOut([], 0.0, 0.0, []))
            if footprint is not None:
                footprint[win] |= drawn
            full_prev[:] = False
            full_prev[win] = _carrying_body(drawn, ras.px, self.bridge_max)
        return outs, footprint

    def layer(
        self,
        z: float,
        mine: NDArray[np.bool_],
        prev: NDArray[np.bool_],
        win: tuple[slice, slice],
        reach: float,
    ) -> _LayerOut:
        import numpy as np
        from scipy import ndimage
        from scipy.spatial import cKDTree

        px = self.ras.px
        cell = px * px
        oi, oj = np.nonzero(mine & ~prev)  # only what sticks out of the carrier needs the test
        if not len(oi):
            return _LayerOut([], 0.0, 0.0, [])
        far = (reach + self.ledge_max) / px + 2
        edge = np.argwhere(prev & ~_cross(prev, False))
        tree = cKDTree(edge) if len(edge) else None

        def dist_px(i: NDArray[np.intp], j: NDArray[np.intp]) -> NDArray[np.float64]:
            if tree is None:
                return np.full(len(i), np.inf)
            d, _ = tree.query(np.column_stack((i, j)), distance_upper_bound=far)
            return np.asarray(d, dtype=float)

        h, w = prev.shape

        def anchored(i: NDArray[np.intp], j: NDArray[np.intp]) -> NDArray[np.bool_]:
            inside = (i >= 0) & (i < h) & (j >= 0) & (j < w)
            res = np.zeros(len(i), dtype=bool)
            ii, jj = i[inside], j[inside]
            on = prev[ii, jj]
            near = on.copy()
            near[~on] = dist_px(ii[~on], jj[~on]) * px <= reach
            res[inside] = near
            return res

        d = dist_px(oi, oj) * px
        free = d > reach
        fi, fj, fd = oi[free], oj[free], d[free]
        if not len(fi):
            return _LayerOut([], 0.0, 0.0, [])
        keep = ~_runs_bridged(fi, fj, anchored, self.bridge_max / px)
        fi, fj, fd = fi[keep], fj[keep], fd[keep]
        # a step-out no more than --ledge-max past the reach (an edge one line wider than the
        # layer below) is an overhang perimeter: counted apart, WARN only
        ledge = fd <= reach + self.ledge_max
        ledges = float(ledge.sum()) * cell
        fi, fj = fi[~ledge], fj[~ledge]
        out = _LayerOut([], 0.0, ledges, [])
        if not len(fi):
            return out
        n, labels = _components(fi, fj)
        sizes = np.bincount(labels, minlength=n) * cell
        ci = np.bincount(labels, weights=fi, minlength=n) / np.maximum(np.bincount(labels, minlength=n), 1)
        cj = np.bincount(labels, weights=fj, minlength=n) / np.maximum(np.bincount(labels, minlength=n), 1)
        ras = self.ras
        for q in np.flatnonzero(sizes > self.min_area):
            pi, pj = fi[labels == q], fj[labels == q]
            # thickness of the piece: twice its deepest pixel from its edge
            i0, j0 = int(pi.min()) - 1, int(pj.min()) - 1
            mask = np.zeros((int(pi.max()) - i0 + 2, int(pj.max()) - j0 + 2), dtype=bool)
            mask[pi - i0, pj - j0] = True
            depth = float(ndimage.distance_transform_edt(mask).max()) * px
            kind = "ledge" if 2 * depth <= self.ledge_max else "island"
            area = float(sizes[q])
            out.islands.append(
                Island(
                    z=z,
                    x=round(float(ras.origin[0] + (win[0].start + ci[q]) * px), 2),
                    y=round(float(ras.origin[1] + (win[1].start + cj[q]) * px), 2),
                    area_mm2=round(area, 2),
                    first_layer=z == self.base,
                    kind=kind,
                )
            )
            if kind == "ledge":
                out.ledges += area
            else:
                out.unsupported += area
            if self.keep_marks:
                out.marks.append((pi + win[0].start, pj + win[1].start, z))
        return out


_JOB: _Layers | None = None  # the object a forked worker analyses (set before the pool forks)


def _run_chunk(bounds: tuple[int, int]) -> tuple[list[_LayerOut], NDArray[np.bool_] | None]:
    assert _JOB is not None
    return _JOB.run(*bounds)


def _run_layers(job: _Layers) -> tuple[list[_LayerOut], NDArray[np.bool_] | None]:
    """All layers of one object; split into contiguous chunks over forked workers (each
    chunk rebuilds the carrier of the layer before it), serial where fork is unavailable."""
    import multiprocessing as mp

    from cli.resources import pool_size

    n = len(job.zs)
    workers = pool_size(max(1, n // 8))
    if workers < 2 or "fork" not in mp.get_all_start_methods():
        return job.run(0, n)
    global _JOB
    _JOB = job
    step = -(-n // workers)
    bounds = [(a, min(n, a + step)) for a in range(0, n, step)]
    try:
        with mp.get_context("fork").Pool(len(bounds)) as pool:
            parts = pool.map(_run_chunk, bounds)
    finally:
        _JOB = None
    outs = [o for part, _ in parts for o in part]
    feet = [f for _, f in parts if f is not None]
    footprint = None
    if feet:
        footprint = feet[0]
        for f in feet[1:]:
            footprint |= f
    return outs, footprint


def analyze_object(
    name: str,
    o: _Obj,
    bed_z: float,
    max_unsupported: float,
    bridge_max: float,
    min_area: float,
    canvas: _Canvas | None = None,
    ledge_max: float = LEDGE_MAX_MM,
) -> ObjectResult:
    own_zs = o.zs(OWN)
    if not own_zs:
        return ObjectResult(name, "WARN", "only support extrusion, no object lines")
    job = _Layers(o, bed_z, bridge_max, min_area, ledge_max, canvas is not None)
    outs, footprint = _run_layers(job)
    islands = [i for out in outs for i in out.islands]
    total = sum(out.unsupported for out in outs)
    ledges = sum(out.ledges for out in outs)
    if canvas is not None:
        for out in outs:
            for ii, jj, z in out.marks:
                canvas.mark(job.ras, ii, jj, z)
        if footprint is not None:
            canvas.base(job.ras, footprint)
    base = job.base
    islands.sort(key=lambda i: -i.area_mm2)
    res = ObjectResult(
        name,
        "PASS",
        "",
        layers=len(own_zs),
        base_z=base,
        unsupported_mm2=round(total, 2),
        ledge_mm2=round(ledges, 2),
        islands=islands[:_KEEP],
        island_count=len(islands),
    )
    real = [i for i in islands if i.kind == "island"]
    in_air = [i for i in real if i.first_layer]
    where = f"base z={base:g}" + (
        "" if base <= bed_z + 1e-6 else f", above the first layer z={bed_z:g}"
    )
    ledge_note = (
        f"; edge step-outs <= {ledge_max:g} mm wide: {ledges:.1f} mm2" if ledges else ""
    )
    if total > max_unsupported:
        res.status = "FAIL"
        zs = sorted({i.z for i in real})
        res.detail = (
            f"{total:.1f} mm2 printed on nothing in {len(real)} island(s), z {zs[0]:g}–{zs[-1]:g}"
            + (" (its first layer is in the air)" if in_air else "")
            + ledge_note
            + f"; {where}"
        )
    elif islands or ledges > min_area:
        res.status = "WARN"
        res.detail = (
            f"{total:.1f} mm2 in {len(real)} island(s) (<= {max_unsupported:g} mm2: ceilings, tips)"
            + ledge_note
            + f"; {where}"
        )
    else:
        res.detail = f"every line carried; {where}"
    return res


class _Canvas:
    """Top-down PNG of the plate: footprints grey, islands red (darker = higher)."""

    def __init__(self, extent: tuple[float, float, float, float], z_max: float) -> None:
        import numpy as np

        self.np = np
        x0, y0, x1, y1 = extent
        self.px = max(0.1, math.sqrt((x1 - x0) * (y1 - y0) / 4_000_000))
        self.origin = (x0, y0)
        self.shape = (int((x1 - x0) / self.px) + 1, int((y1 - y0) / self.px) + 1)
        self.foot = np.zeros(self.shape, dtype=bool)
        self.zmap = np.full(self.shape, np.nan)
        self.z_max = max(z_max, 1e-6)

    def _place(self, ras: _Raster, ii: NDArray[np.intp], jj: NDArray[np.intp]) -> tuple[Any, Any]:
        np = self.np
        x = ras.origin[0] + ii * ras.px
        y = ras.origin[1] + jj * ras.px
        i = np.clip(((x - self.origin[0]) / self.px).astype(int), 0, self.shape[0] - 1)
        j = np.clip(((y - self.origin[1]) / self.px).astype(int), 0, self.shape[1] - 1)
        return i, j

    def mark(self, ras: _Raster, ii: NDArray[np.intp], jj: NDArray[np.intp], z: float) -> None:
        """Island pixels (raster coordinates of `ras`) printed at height z."""
        i, j = self._place(ras, ii, jj)
        self.zmap[i, j] = self.np.fmax(self.zmap[i, j], z)

    def base(self, ras: _Raster, footprint: NDArray[np.bool_]) -> None:
        i, j = self._place(ras, *self.np.nonzero(footprint))
        self.foot[i, j] = True

    def write(self, path: str) -> None:
        np = self.np
        img = np.full(self.shape + (3,), 255, dtype=np.uint8)
        img[self.foot] = (200, 200, 200)
        hit = ~np.isnan(self.zmap)
        shade = 1 - 0.6 * np.nan_to_num(self.zmap) / self.z_max
        img[hit, 0] = (255 * shade[hit]).astype(np.uint8)
        img[hit, 1] = 0
        img[hit, 2] = 0
        rows = np.flipud(img.transpose(1, 0, 2))  # y up, x right
        write_png(path, rows)


def write_png(path: str, rgb: NDArray[np.uint8]) -> None:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[r].tobytes() for r in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    with open(path, "wb") as fh:
        fh.write(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b"")
        )


def default_png(gcode: str) -> str:
    """Cache path of the overhang map: one per G-code content."""
    h = hashlib.sha256()
    with open(gcode, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    stem = os.path.splitext(os.path.basename(gcode))[0]
    return os.path.join(base, "3d-cli", "overhangs", f"{stem}-{h.hexdigest()[:12]}.png")


def run_check(
    path: str,
    max_unsupported: float = MAX_UNSUPPORTED_MM2,
    bridge_max: float = BRIDGE_MAX_MM,
    min_area: float = MIN_AREA_MM2,
    png: str | None = None,
    ledge_max: float = LEDGE_MAX_MM,
) -> dict[str, Any]:
    plate = parse(path)
    if plate.first_z is None:
        return {
            "check": "overhangs",
            "status": "SKIP",
            "detail": "no extruding moves in the G-code",
            "objects": [],
        }
    if (plate.config.get("spiral_mode") or "0").strip() not in ("0", "false", ""):
        return {
            "check": "overhangs",
            "status": "SKIP",
            "objects": [],
            "detail": "spiral (vase) mode: the layers are one continuous helix, not checked layer by layer",
        }
    first_h = _float(plate.config.get("initial_layer_print_height")) or _float(
        plate.config.get("first_layer_height")
    )
    bed_z = max(plate.first_z, first_h or plate.first_z)
    canvas = None
    if png is not None:
        import numpy as np

        ext = [
            np.vstack([o.get(z, k) for (z, k) in o.lines])
            for o in plate.objects.values()
            if o.lines
        ]
        allv = np.vstack(ext)
        lo = np.minimum(allv[:, [0, 1]].min(0), allv[:, [2, 3]].min(0)) - 2
        hi = np.maximum(allv[:, [0, 1]].max(0), allv[:, [2, 3]].max(0)) + 2
        z_max = max(max(o.zs()) for o in plate.objects.values() if o.lines)
        canvas = _Canvas(
            (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])), z_max
        )
    objects = [
        analyze_object(
            name, o, bed_z, max_unsupported, bridge_max, min_area, canvas, ledge_max
        )
        for name, o in plate.objects.items()
    ]
    objects += [
        ObjectResult(n, "WARN", "defined but never printed")
        for n in plate.defined
        if n not in plate.objects
    ]
    if canvas is not None and png is not None:
        os.makedirs(os.path.dirname(os.path.abspath(png)), exist_ok=True)
        canvas.write(png)
    states = {o.status for o in objects}
    status = "FAIL" if "FAIL" in states else "WARN" if "WARN" in states else "PASS"
    bad = [o for o in objects if o.status == "FAIL"]
    limits = f"max {max_unsupported:g} mm2 per object, bridges <= {bridge_max:g} mm"
    if bad:
        listed = ", ".join(f"{o.name} {o.unsupported_mm2:g} mm2" for o in bad)
        detail = f"{len(bad)} of {len(objects)} object(s) print on nothing: {listed}; {limits}"
    else:
        worst = max(objects, key=lambda o: o.unsupported_mm2)
        detail = f"{len(objects)} object(s), largest uncarried {worst.unsupported_mm2:g} mm2 ({worst.name}); {limits}"
    return {
        "check": "overhangs",
        "status": status,
        "detail": detail,
        "max_unsupported_mm2": max_unsupported,
        "bridge_max_mm": bridge_max,
        "min_area_mm2": min_area,
        "first_layer_z": bed_z,
        "png": png,
        "objects": [asdict(o) for o in objects],
    }


def format_lines(report: dict[str, Any]) -> list[str]:
    out = []
    for o in report.get("objects", []):
        out.append(f"{o['status']:<4} {o['name']}: {o['detail']}")
        shown = o["islands"][:5]
        if shown and o["status"] != "PASS":
            more = (
                f" (+{o['island_count'] - len(shown)} more)"
                if o["island_count"] > len(shown)
                else ""
            )
            out.append(
                "     islands: "
                + ", ".join(
                    f"{i['kind']} z{i['z']:g} ({i['x']:g}, {i['y']:g}) {i['area_mm2']:g} mm2"
                    for i in shown
                )
                + more
            )
    if report.get("png") and report.get("status") != "PASS":
        out.append(f"map: {report['png']}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="3d gcode-overhangs")
    ap.add_argument("gcode")
    ap.add_argument("--max-unsupported", type=float, default=MAX_UNSUPPORTED_MM2)
    ap.add_argument("--bridge-max", type=float, default=BRIDGE_MAX_MM)
    ap.add_argument("--min-area", type=float, default=MIN_AREA_MM2)
    ap.add_argument("--ledge-max", type=float, default=LEDGE_MAX_MM)
    ap.add_argument("--png")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    for flag, v in (
        ("--max-unsupported", a.max_unsupported),
        ("--bridge-max", a.bridge_max),
        ("--min-area", a.min_area),
        ("--ledge-max", a.ledge_max),
    ):
        if not v >= 0 or (flag == "--bridge-max" and v == 0):
            print(
                f"error: {flag} must be a number of mm{'2' if 'bridge' not in flag else ''} >= 0",
                file=sys.stderr,
            )
            return 2
    if not os.path.isfile(a.gcode):
        print(f"error: no such file: {a.gcode}", file=sys.stderr)
        return 2
    png = None if a.no_png else (a.png or default_png(a.gcode))
    report = run_check(
        a.gcode, a.max_unsupported, a.bridge_max, a.min_area, png, a.ledge_max
    )
    if a.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        print(f"overhangs {a.gcode}: {report['status']} — {report['detail']}")
        for line in format_lines(report):
            print(f"  {line}")
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
