#!/usr/bin/env python3
"""visible_faces.py — the visible side of a part must print clean: no support, no sag.

Decorative parts (facades, panels, figurines) have a side everyone looks at. A global
overhang fraction cannot tell a hidden back overhang from a sag on that face, so this gate
takes the visible side as a half-space of the print frame (`y<4.8`, `x>10`, or `all`),
optionally minus hidden (u, z) polygons (u = the other bed axis: what another part covers)
and with declared openings (windows) whose short bridges are allowed, and checks it twice:

MESH (slicer view). The union of the part's volumes (colour interfaces are not surfaces) is
  sliced every layer height. A layer is the union of three planes 0.01 mm apart, so a CSG
  hairline (a horizontal face lying in the plane, a slit under 0.02 mm) is not printable
  geometry. On the visible side, the part of layer N outside layer N-1 grown by the
  self-supporting reach (layer height x tan 45 + 0.1 mm for the stair quantisation of a 45
  slope) is an unsupported piece; pieces over --min-area that are wider than 0.1 mm somewhere
  FAIL. A piece inside a declared opening whose both ends rest on the layer below and whose
  span (piece + the reach eaten off each side) is <= --bar-span is a bridge by design.

G-CODE (actual slice). Per object: a Support extrusion above the raft (support below the
  object's first own layer) on the visible side FAILS. Each layer is rasterised at 0.05 mm
  (line centres dilated 4 px = a 0.4 mm line); the layer below becomes a footprint (closing
  24 px + fill holes, so sparse infill gaps and enclosed voids count as carried). An island
  of the layer more than 0.35 mm (7 px) outside that footprint that reaches the outer 0.6 mm
  skin band of its layer, on the visible side, FAILS: a modelled 45 chamfer steps 0.14 mm per
  0.12 mm layer, a dropped wall sliver or a sag steps >= 0.4 mm. Islands inside an opening
  anchored within 1 mm past both ends and no longer than --bar-span are bridges by design.

Frames: the mesh pass works in the input's coordinates (STL as given; 3MF build items with
their transforms, i.e. bed coordinates), the G-code pass in bed coordinates.

Exit: 0 PASS, 1 FAIL, 2 usage / load error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from array import array
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import contextlib

from gcode_labels import ObjectLabels, feature, undecorated

MIN_AREA = 0.5  # mm²: smaller pieces / islands are noise
LAYER = 0.12  # mm
BAR_SPAN = 10.0  # mm: longest bridge allowed inside a declared opening
REACH_PAD = 0.10  # mm added to layer x tan 45 (stair quantisation of a 45 slope)
SLIVER = 0.05  # mm: a piece that vanishes under offset(-SLIVER) is a CSG sliver
PLANES = (
    0.0013,
    -0.0097,
    0.0113,
)  # three slice planes per layer (absorb CSG hairlines)
OPENING_TOL = 0.3  # mm slack around a declared opening
PX = 0.05  # G-code raster pixel, mm
LINE_PX = 4  # half line width in px (0.4 mm line)
CLOSE_PX = 24  # closing of a layer into its footprint
SKIN_PX = 12  # outer skin band (0.6 mm)
STEP_PX = 7  # allowed step-out per layer (0.35 mm)
ANCHOR_MM = 1.0  # a G-code bridge is anchored within this distance past each end
MAX_RASTER_PIXELS = 60_000_000
SKIP_TYPES = ("brim", "skirt", "skirt/brim", "prime tower", "wipe tower", "custom")
BIG = 1e4

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
UNITS = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

Mesh = tuple[np.ndarray, np.ndarray]  # (V float (n,3), F int (m,3))


class InputError(ValueError):
    """an input file or option value cannot be used (exit 2)"""


# ---------------------------------------------------------------- the visible side
@dataclass(frozen=True)
class Visible:
    """visible half-space `axis < value` / `axis > value`; axis None = every face is visible"""

    axis: str | None
    less: bool = True
    value: float = 0.0

    @property
    def u(self) -> int:
        """index of the along-face bed axis used by hidden polygons and openings (x for y/all)"""
        return 1 if self.axis == "x" else 0

    def contains(self, x: float, y: float) -> bool:
        if self.axis is None:
            return True
        c = x if self.axis == "x" else y
        return c < self.value if self.less else c > self.value

    def text(self) -> str:
        return (
            "all"
            if self.axis is None
            else f"{self.axis}{'<' if self.less else '>'}{self.value:g}"
        )


_VIS_RE = re.compile(
    r"^\s*([xy])\s*([<>])\s*([-+]?(?:\d+\.?\d*|\.\d+))\s*$", re.IGNORECASE
)


def parse_visible(spec: str) -> Visible:
    if spec.strip().lower() == "all":
        return Visible(None)
    m = _VIS_RE.match(spec)
    if not m:
        raise InputError(
            f"--visible {spec!r}: expected x<V, x>V, y<V, y>V or all (V in mm)"
        )
    return Visible(m.group(1).lower(), m.group(2) == "<", float(m.group(3)))


@dataclass
class FaceSpec:
    visible: Visible
    hidden: list[np.ndarray] = field(default_factory=list)  # (u, z) polygons
    openings: list[tuple[float, float, float, float]] = field(
        default_factory=list
    )  # u0, u1, z0, z1

    def hidden_at(self, z: float) -> list[tuple[float, float]]:
        """u intervals of the hidden polygons on the line at height z (even-odd scanline)"""
        out: list[tuple[float, float]] = []
        for poly in self.hidden:
            p, q = poly, np.roll(poly, -1, axis=0)
            hit = (p[:, 1] <= z) != (q[:, 1] <= z)
            if not hit.any():
                continue
            us = np.sort(
                p[hit, 0]
                + (z - p[hit, 1]) * (q[hit, 0] - p[hit, 0]) / (q[hit, 1] - p[hit, 1])
            )
            out += [(float(us[i]), float(us[i + 1])) for i in range(0, len(us) - 1, 2)]
        return out

    def is_hidden(self, u: float, z: float) -> bool:
        return any(a <= u <= b for a, b in self.hidden_at(z))

    def visible_at(self, x: float, y: float, z: float) -> bool:
        if not self.visible.contains(x, y):
            return False
        return not self.is_hidden(x if self.visible.u == 0 else y, z)

    def first_visible(self, pts: np.ndarray, z: float) -> np.ndarray | None:
        """the first of the (n, 2) points on the visible side at height z, or None"""
        v = self.visible
        if v.axis is None:
            cand = pts
        else:
            c = pts[:, 0 if v.axis == "x" else 1]
            cand = pts[c < v.value] if v.less else pts[c > v.value]
        if not len(cand):
            return None
        spans = self.hidden_at(z)
        if not spans:
            return cand[0]
        u = cand[:, v.u]
        hidden = np.zeros(len(cand), bool)
        for a, b in spans:
            hidden |= (u >= a) & (u <= b)
        free = cand[~hidden]
        return free[0] if len(free) else None

    def opening_for(self, ua: float, ub: float, z: float) -> bool:
        return any(
            o0 - OPENING_TOL <= ua
            and ub <= o1 + OPENING_TOL
            and z0 <= z <= z1 + OPENING_TOL
            for o0, o1, z0, z1 in self.openings
        )


def _array(q: Any) -> np.ndarray:
    try:
        return np.asarray(q, float)
    except (TypeError, ValueError):
        return np.zeros((0,))


def load_hidden(path: str) -> list[np.ndarray]:
    data = _load_json(path, "--hidden")
    if not isinstance(data, list):
        raise InputError(
            f"--hidden {path}: expected a JSON list of polygons [[[u, z], ...], ...]"
        )
    polys = []
    for i, q in enumerate(data):
        a = _array(q)
        if a.ndim != 2 or a.shape[1] != 2 or len(a) < 3 or not np.isfinite(a).all():
            raise InputError(
                f"--hidden {path}: polygon {i} must be a list of >= 3 [u, z] points"
            )
        polys.append(a)
    return polys


def load_openings(path: str) -> list[tuple[float, float, float, float]]:
    data = _load_json(path, "--openings")
    if not isinstance(data, list):
        raise InputError(f"--openings {path}: expected a JSON list of [u0, u1, z0, z1]")
    out = []
    for i, o in enumerate(data):
        a = _array(o)
        if a.shape != (4,) or not np.isfinite(a).all() or a[0] > a[1] or a[2] > a[3]:
            raise InputError(
                f"--openings {path}: entry {i} must be [u0, u1, z0, z1] with u0 <= u1, z0 <= z1"
            )
        out.append((float(a[0]), float(a[1]), float(a[2]), float(a[3])))
    return out


def _load_json(path: str, flag: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as e:
        raise InputError(f"{flag} {path}: {e.strerror}") from e
    except json.JSONDecodeError as e:
        raise InputError(f"{flag} {path}: not valid JSON ({e})") from e


def _split_target(arg: str, is_spec: bool) -> tuple[str | None, str]:
    """'OBJ:VALUE' -> (OBJ, VALUE); a bare value (or an existing path) -> (None, VALUE)"""
    if is_spec:
        obj, sep, val = arg.rpartition(":")
        return (obj, val) if sep and obj else (None, arg)
    if ":" in arg and not os.path.exists(arg):
        obj, _, val = arg.partition(":")
        return obj or None, val
    return None, arg


@dataclass
class Specs:
    """--visible/--hidden/--openings, each either a default or bound to an object pattern"""

    visible: list[tuple[str | None, Visible]] = field(default_factory=list)
    hidden: list[tuple[str | None, list[np.ndarray]]] = field(default_factory=list)
    openings: list[tuple[str | None, list[tuple[float, float, float, float]]]] = field(
        default_factory=list
    )

    @staticmethod
    def _pick(
        entries: list[tuple[str | None, Any]], names: list[str]
    ) -> tuple[bool, Any]:
        for pat, val in entries:
            if pat is not None and any(fnmatch.fnmatchcase(n, pat) for n in names):
                return True, val
        for pat, val in entries:
            if pat is None:
                return True, val
        return False, None

    def resolve(self, names: list[str]) -> FaceSpec | None:
        found, vis = self._pick(self.visible, names)
        if not found:
            return None
        return FaceSpec(
            vis,
            self._pick(self.hidden, names)[1] or [],
            self._pick(self.openings, names)[1] or [],
        )

    def patterns(self) -> list[str]:
        return [p for p, _ in self.visible if p is not None]


def object_names(name: str) -> list[str]:
    """names an object pattern is matched against: the full slicer label, the part before
    Orca's `_—_` (Bambu's ` — `) separator and the label without its id/copy suffix"""
    out = [name, re.split("_—_| — ", name)[0], undecorated(name)]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- mesh loading
def _stl(path: str) -> Mesh:
    import trimesh

    try:
        t = trimesh.load(path, force="mesh", process=False)
    except Exception as e:  # trimesh raises many types for a broken file
        raise InputError(f"{path}: cannot read mesh ({e})") from e
    if not isinstance(t, trimesh.Trimesh) or len(t.faces) == 0:
        raise InputError(f"{path}: no triangles")
    t.merge_vertices()
    return np.asarray(t.vertices, float), np.asarray(t.faces, np.int64)


def _transform(attr: str | None) -> np.ndarray:
    m = np.eye(4)
    if attr:
        v = [float(t) for t in attr.split()]
        if len(v) != 12:
            raise InputError(f"3MF transform {attr!r} must have 12 numbers")
        m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
        m[:3, 3] = v[9:]
    return m


def load_3mf(path: str) -> list[tuple[str, list[Mesh]]]:
    """build items of a 3MF as (object name, meshes in bed coordinates); each object's
    components (colour volumes) stay separate meshes for the union"""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise InputError(f"{path}: not a 3MF (zip) file ({e})") from e
    roots: dict[str, ET.Element] = {}

    def model(part: str) -> ET.Element:
        part = part.lstrip("/")
        if part not in roots:
            try:
                roots[part] = ET.fromstring(z.read(part))
            except KeyError as e:
                raise InputError(f"{path}: missing model part {part}") from e
            except ET.ParseError as e:
                raise InputError(f"{path}: {part} is not valid XML ({e})") from e
        return roots[part]

    main = "3D/3dmodel.model"
    root = model(main)
    scale = UNITS.get(root.get("unit", "millimeter"), 1.0)

    def obj(part: str, oid: str) -> ET.Element:
        for o in model(part).iter(f"{{{CORE}}}object"):
            if o.get("id") == oid:
                return o
        raise InputError(f"{path}: object id {oid} not found in {part}")

    def meshes(part: str, oid: str, tf: np.ndarray, depth: int = 0) -> list[Mesh]:
        if depth > 16:
            raise InputError(f"{path}: component nesting too deep (cycle?)")
        o = obj(part, oid)
        out: list[Mesh] = []
        m = o.find(f"{{{CORE}}}mesh")
        if m is not None:
            vs = m.find(f"{{{CORE}}}vertices")
            ts = m.find(f"{{{CORE}}}triangles")
            if vs is not None and ts is not None:
                V = np.array(
                    [[float(v.get(k, "0")) for k in "xyz"] for v in vs], float
                ).reshape(-1, 3)
                F = np.array(
                    [[int(t.get(k, "0")) for k in ("v1", "v2", "v3")] for t in ts],
                    np.int64,
                ).reshape(-1, 3)
                if len(F):
                    # a production-extension model file may declare its own unit: bring its vertices
                    # to the root unit, in which the transforms (translations included) are written,
                    # then scale the result to mm
                    V = V * (
                        UNITS.get(model(part).get("unit", "millimeter"), 1.0) / scale
                    )
                    out.append((((V @ tf[:3, :3].T) + tf[:3, 3]) * scale, F))
        comps = o.find(f"{{{CORE}}}components")
        for c in comps if comps is not None else []:
            sub = c.get(f"{{{PROD}}}path") or part
            out += meshes(
                sub,
                c.get("objectid", ""),
                tf @ _transform(c.get("transform")),
                depth + 1,
            )
        return out

    items = []
    build = root.find(f"{{{CORE}}}build")
    for k, it in enumerate(build if build is not None else []):
        part = it.get(f"{{{PROD}}}path") or main
        oid = it.get("objectid", "")
        name = obj(part, oid).get("name") or f"object{oid or k}"
        ms = meshes(part, oid, _transform(it.get("transform")))
        if ms:
            items.append((name, ms))
    if not items:
        raise InputError(f"{path}: no build items with triangles")
    return items


def union_solid(meshes: list[Mesh]) -> Any:
    """outer solid: union of the volumes (interfaces between colours are not surfaces)"""
    from manifold3d import Error, Manifold, Mesh as MMesh, OpType

    ms = []
    for V, F in meshes:
        m = Manifold(
            MMesh(
                vert_properties=np.asarray(V, np.float32),
                tri_verts=np.asarray(F, np.uint32),
            )
        )
        if m.status() != Error.NoError:
            raise InputError(
                f"a volume is not a closed manifold mesh ({m.status()}); repair it first (3d mesh)"
            )
        ms.append(m)
    return Manifold.batch_boolean(ms, OpType.Add)


# ---------------------------------------------------------------- mesh pass
def mesh_report(
    name: str,
    solid: Any,
    spec: FaceSpec,
    layer: float = LAYER,
    min_area: float = MIN_AREA,
    bar_span: float = BAR_SPAN,
) -> dict[str, Any]:
    """layer by layer, the part of a layer on the visible side not carried by the layer below
    grown by the self-supporting reach; every piece over min_area is a sag or a bridge"""
    from manifold3d import CrossSection, JoinType

    reach = layer + REACH_PAD
    x0b, y0b, z0b, x1b, y1b, z1b = solid.bounding_box()
    # layers from the bed (z 0) up: a part that starts above the bed has nothing under its
    # first layer and fails, as the slicer would need support for it
    zs = np.arange(min(z0b, 0.0) + layer / 2, z1b, layer)
    vis_ax, ui = spec.visible, spec.visible.u
    if vis_ax.axis is None:
        face = CrossSection.square((2 * BIG, 2 * BIG)).translate((-BIG, -BIG))
    else:
        edge = vis_ax.value + 0.02 if vis_ax.less else vis_ax.value - 0.02
        lo = edge - 2 * BIG if vis_ax.less else edge
        face = CrossSection.square((2 * BIG, 2 * BIG)).translate(
            (lo, -BIG) if vis_ax.axis == "x" else (-BIG, lo)
        )

    def strip(a: float, b: float, w: float = 2 * BIG, c: float = -BIG) -> Any:
        """band a <= u <= b across the face (v from c, width w)"""
        return (
            CrossSection.square((b - a, w)).translate((a, c))
            if ui == 0
            else CrossSection.square((w, b - a)).translate((c, a))
        )

    pieces: list[dict[str, Any]] = []
    bridges: list[dict[str, Any]] = []
    prev = None
    for z in zs:
        z = float(z)
        cur = solid.slice(z + PLANES[0])
        for dz in PLANES[1:]:
            cur = cur + solid.slice(z + dz)
        if prev is not None and not cur.is_empty():
            vis = face
            for a, b in spec.hidden_at(z):
                if b > a:
                    vis = vis - strip(a, b)
            new = (cur ^ vis) - prev.offset(reach, JoinType.Miter)
            for g in new.decompose():
                if g.area() <= min_area or g.offset(-SLIVER, JoinType.Miter).is_empty():
                    continue
                bx0, by0, bx1, by1 = g.bounds()
                ua, ub = (bx0, bx1) if ui == 0 else (by0, by1)
                va, vb = (by0, by1) if ui == 0 else (bx0, bx1)
                span = (ub - ua) + 2 * reach
                # a bridge: the layer below carries both of its ends (0.4 mm probes past the reach)
                ends = all(
                    not (prev ^ strip(ue, ue + 0.4, vb - va + 0.2, va - 0.1)).is_empty()
                    for ue in (ua - reach - 0.4, ub + reach)
                )
                rec = {
                    "area_mm2": round(g.area(), 3),
                    "x": round((bx0 + bx1) / 2, 2),
                    "y": round((by0 + by1) / 2, 2),
                    "z": round(z, 3),
                    "bbox": [
                        round(bx0, 2),
                        round(by0, 2),
                        round(bx1, 2),
                        round(by1, 2),
                    ],
                }
                if spec.opening_for(ua, ub, z) and ends and span <= bar_span:
                    bridges.append(dict(rec, span_mm=round(span, 2)))
                else:
                    pieces.append(rec)
        prev = cur
    pieces.sort(key=lambda r: -r["area_mm2"])
    return {
        "name": name,
        "kind": "mesh",
        "status": "FAIL" if pieces else "PASS",
        "visible": vis_ax.text(),
        "layers": len(zs),
        "pieces": pieces,
        "bridges": bridges,
        "total_area_mm2": round(sum(p["area_mm2"] for p in pieces), 3),
    }


# ---------------------------------------------------------------- G-code pass
class Layer:
    """one object layer of extrusion, kept compact: a big plate is millions of moves, and a
    float array of samples per move took 5 GB. Straight moves are stored as (x, y, nx, ny)
    plus their sample count and expanded on demand (`points()`, one layer at a time); arcs
    (rare) keep their samples."""

    __slots__ = ("arcs", "counts", "moves")

    def __init__(self) -> None:
        self.moves = array("d")  # x, y, nx, ny per straight move
        self.counts = array("q")  # samples - 1 per straight move
        self.arcs: list[np.ndarray] = []

    def add_move(self, x: float, y: float, nx: float, ny: float) -> None:
        self.moves.extend((x, y, nx, ny))
        self.counts.append(max(1, int(math.hypot(nx - x, ny - y) / (PX / 2))))

    def points(self) -> np.ndarray:
        """(n, 2) samples every PX/2 along each move (straight moves first, then arcs)"""
        parts = list(self.arcs)
        if self.counts:
            m = np.frombuffer(self.moves, float).reshape(-1, 4)
            parts.insert(0, _segments(m, np.frombuffer(self.counts, np.int64)))
        return np.vstack(parts)

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """exact min / max of points() without expanding the moves: the samples of a move
        are monotone between its first (x) and last (x + (nx - x)) sample"""
        lo, hi = [], []
        if self.counts:
            m = np.frombuffer(self.moves, float).reshape(-1, 4)
            ends = np.vstack((m[:, :2], m[:, :2] + (m[:, 2:] - m[:, :2])))
            lo.append(ends.min(0))
            hi.append(ends.max(0))
        for a in self.arcs:
            lo.append(a.min(0))
            hi.append(a.max(0))
        return np.min(lo, axis=0), np.max(hi, axis=0)


@dataclass
class GObject:
    name: str
    spec: FaceSpec
    layers: dict[float, Layer] = field(default_factory=dict)
    support: list[tuple[str, np.ndarray, float]] = field(
        default_factory=list
    )  # (type, points, z)

    def layer(self, z: float) -> Layer:
        lay = self.layers.get(z)
        if lay is None:
            lay = self.layers[z] = Layer()
        return lay

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        b = [lay.bounds() for lay in self.layers.values()]
        return np.min([q[0] for q in b], axis=0), np.max([q[1] for q in b], axis=0)


_WORD = re.compile(r"([XYZEIJ])\s*(-?(?:\d+\.?\d*|\.\d+))", re.IGNORECASE)


def _arc(
    x: float, y: float, nx: float, ny: float, i: float, j: float, cw: bool
) -> np.ndarray:
    cx, cy = x + i, y + j
    r = math.hypot(i, j)
    a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
    sweep = (a0 - a1) if cw else (a1 - a0)
    sweep %= 2 * math.pi
    if sweep < 1e-9:
        sweep = 2 * math.pi  # start == end: a full circle
    n = max(2, int(r * sweep / (PX / 2)) + 1)
    t = np.linspace(0.0, sweep, n) * (-1 if cw else 1) + a0
    pts = np.c_[cx + r * np.cos(t), cy + r * np.sin(t)]
    pts[-1] = (nx, ny)
    return pts


def _segment(x: float, y: float, nx: float, ny: float) -> np.ndarray:
    n = max(1, int(math.hypot(nx - x, ny - y) / (PX / 2)))
    s = np.linspace(0.0, 1.0, n + 1)
    return np.c_[x + (nx - x) * s, y + (ny - y) * s]


def _segments(m: np.ndarray, n: np.ndarray) -> np.ndarray:
    """_segment() of every move (rows x, y, nx, ny; n = samples - 1) at once, bit for bit:
    np.linspace(0, 1, n + 1) is arange(n + 1) * (1 / n) with its last value set to 1"""
    cnt = n + 1
    end = np.cumsum(cnt)
    t = (np.arange(end[-1]) - np.repeat(end - cnt, cnt)) * np.repeat(1.0 / n, cnt)
    t[end - 1] = 1.0
    x, y = np.repeat(m[:, 0], cnt), np.repeat(m[:, 1], cnt)
    return np.c_[
        x + np.repeat(m[:, 2] - m[:, 0], cnt) * t,
        y + np.repeat(m[:, 3] - m[:, 1], cnt) * t,
    ]


def parse_gcode(path: str, specs: Specs) -> tuple[dict[str, GObject], list[str]]:
    """extrusion per gated object and layer (sampled every PX/2 along each move), plus its
    support moves. Objects come from the G-code's object labels (gcode_labels: EXCLUDE_OBJECT,
    Bambu `; start printing object` blocks, ...); a file without them is one object named
    `plate`. Support printed outside any object block is given to every gated object whose
    extrusion bbox (+1 mm) it touches. Returns (gated objects, all labels seen)."""
    objs: dict[str, GObject] = {}
    seen: list[str] = []
    gated: dict[str, GObject | None] = {}
    orphans: list[tuple[str, np.ndarray, float]] = []
    marked = False
    cur: GObject | None = None
    labels = ObjectLabels()

    def enter(label: str) -> GObject | None:
        if label not in gated:
            seen.append(label)
            spec = specs.resolve(object_names(label))
            gated[label] = (
                objs.setdefault(label, GObject(label, spec)) if spec else None
            )
        return gated[label]

    cur = enter("plate")
    typ, z, zc = "", 0.0, None
    x = y = 0.0
    e_abs, e_last = False, 0.0
    try:
        fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115 — `with fh:` below; open errors map to InputError here
    except OSError as e:
        raise InputError(f"{path}: {e.strerror}") from e
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if labels.feed(line):
                if labels.current is not None:
                    if not marked:
                        marked = True
                        plate = objs.pop("plate", None)
                        gated.pop("plate", None)
                        seen.remove("plate")
                        if plate is not None:
                            orphans += plate.support
                    cur = enter(labels.current)
                elif marked:
                    cur = None
                continue
            if line[0] == ";":
                body = line[1:].strip()
                if body.startswith("Z:") or body.startswith("Z_HEIGHT:"):
                    with contextlib.suppress(ValueError):
                        zc = round(float(body.split(":", 1)[1]), 3)
                elif (tag := feature(line)) is not None:
                    typ = tag
                continue
            word = line.split(None, 1)[0].upper()
            if word in ("M82", "M83"):
                e_abs = word == "M82"
                continue
            code = line.split(";", 1)[0]
            if word == "G92":
                w = {k.upper(): float(v) for k, v in _WORD.findall(code)}
                if "E" in w:
                    e_last = w["E"]
                continue
            if word not in ("G0", "G1", "G2", "G3", "G00", "G01", "G02", "G03"):
                continue
            w = {k.upper(): float(v) for k, v in _WORD.findall(code)}
            nx, ny = w.get("X", x), w.get("Y", y)
            if "Z" in w:
                z = w["Z"]
            extr = False
            if "E" in w:
                extr = (w["E"] - e_last > 1e-9) if e_abs else w["E"] > 0
                if e_abs:
                    e_last = w["E"]
            arc = word in ("G2", "G3", "G02", "G03") and ("I" in w or "J" in w)
            outside = cur is None and marked
            if extr and (cur is not None or outside) and (nx != x or ny != y or arc):
                lz = zc if zc is not None else round(z, 3)
                t = typ.lower()
                if t.startswith("support"):
                    pts = (
                        _arc(
                            x,
                            y,
                            nx,
                            ny,
                            w.get("I", 0.0),
                            w.get("J", 0.0),
                            word in ("G2", "G02"),
                        )
                        if arc
                        else _segment(x, y, nx, ny)
                    )
                    (orphans if cur is None else cur.support).append((typ, pts, lz))
                elif cur is not None and t not in SKIP_TYPES:
                    if arc:
                        cur.layer(lz).arcs.append(
                            _arc(
                                x,
                                y,
                                nx,
                                ny,
                                w.get("I", 0.0),
                                w.get("J", 0.0),
                                word in ("G2", "G02"),
                            )
                        )
                    else:
                        cur.layer(lz).add_move(x, y, nx, ny)
            x, y = nx, ny
    for o in objs.values():
        if o.layers:
            lo, hi = o.bounds()
            lo, hi = lo - 1.0, hi + 1.0
            o.support += [
                s for s in orphans if ((s[1] >= lo) & (s[1] <= hi)).all(1).any()
            ]
    return objs, seen


def _fill_holes(a: np.ndarray) -> np.ndarray:
    """ndimage.binary_fill_holes(a) with one labelling instead of an iterated propagation:
    a hole is a background region (4-connected, as there) that does not reach the raster edge"""
    from scipy import ndimage

    lab, n = ndimage.label(~a)
    keep = np.ones(n + 1, bool)
    keep[lab[0]] = keep[lab[-1]] = keep[lab[:, 0]] = keep[lab[:, -1]] = False
    keep[0] = True
    return keep[lab]


def gcode_report(
    o: GObject,
    layer: float = LAYER,
    min_area: float = MIN_AREA,
    bar_span: float = BAR_SPAN,
) -> dict[str, Any]:
    from scipy import ndimage

    spec = o.spec
    rep: dict[str, Any] = {
        "name": o.name,
        "kind": "gcode",
        "visible": spec.visible.text(),
        "layers": len(o.layers),
        "support": [],
        "islands": [],
        "bridges": [],
    }
    if not o.layers:
        rep.update(
            status="FAIL",
            error="no extrusion of this object in the G-code (truncated slice?)",
        )
        return rep
    zs = sorted(o.layers)
    # support below the object's first own layer is the raft; over a raft the G-code z is
    # higher than the model z (hidden zones, openings): the first object layer is model z = layer
    raft = any(z < zs[0] - 1e-6 for _, _, z in o.support)
    zoff = zs[0] - layer if raft else 0.0
    for typ, pts, z in o.support:
        if z < zs[0] - 1e-6:
            continue
        vis = spec.first_visible(pts, z - zoff)
        if vis is not None:
            rep["support"].append(
                {
                    "type": typ,
                    "x": round(float(vis[0]), 2),
                    "y": round(float(vis[1]), 2),
                    "z": z,
                }
            )
    lo, hi = o.bounds()
    # margin wider than the closing: erosion treats outside the raster as empty, so a margin
    # under CLOSE_PX would eat the outermost lines of the footprint (false islands at the edge)
    margin = (LINE_PX + CLOSE_PX + STEP_PX + 4) * PX
    x0, y0 = lo - margin
    W, H = ((hi + margin - (x0, y0)) / PX).astype(int) + 1
    if W * H > MAX_RASTER_PIXELS:
        rep.update(
            status="FAIL", error=f"object raster {W}x{H} px at {PX} mm is too large"
        )
        return rep
    org = np.array([x0, y0])
    ui = spec.visible.u
    m1 = int(round(ANCHOR_MM / PX))
    # Each layer is processed in its own window: its cells + `crop` px, inside the raster. The
    # closed layer (dilation LINE_PX + CLOSE_PX) stays inside the window with an empty ring
    # around it, so closing, hole filling, the skin erosion and the labels in the window equal
    # those on the whole raster (outside the window the layer is empty in both).
    crop = LINE_PX + CLOSE_PX + 2
    size = np.array([W, H])
    prev_body: np.ndarray | None = None  # the whole raster: the next layer's footprint
    zprev = 0.0
    for k in zs:
        ij = ((o.layers[k].points() - org) / PX).astype(int)
        a0, a1 = np.maximum(ij.min(0) - crop, 0), np.minimum(ij.max(0) + crop + 1, size)
        win = (slice(a0[0], a1[0]), slice(a0[1], a1[1]))
        g = np.zeros(a1 - a0, bool)
        g[ij[:, 0] - a0[0], ij[:, 1] - a0[1]] = True
        del ij
        g = ndimage.binary_dilation(g, iterations=LINE_PX)
        body_w = _fill_holes(ndimage.binary_closing(g, iterations=CLOSE_PX))
        body = np.zeros((W, H), bool)
        body[win] = body_w
        if prev_body is not None:
            # a skipped layer: nothing carries this one
            carried = k - zprev <= 1.5 * layer
            foot = prev_body if carried else np.zeros_like(body)
            un = g
            if carried:
                # the footprint grown by STEP_PX, in the window (from the footprint around it)
                b0, b1 = np.maximum(a0 - STEP_PX, 0), np.minimum(a1 + STEP_PX, size)
                grown = ndimage.binary_dilation(
                    foot[b0[0] : b1[0], b0[1] : b1[1]], iterations=STEP_PX
                )
                d0, d1 = a0 - b0, a1 - b0
                un = g & ~grown[d0[0] : d1[0], d0[1] : d1[1]]
            lab, n = ndimage.label(un)
            if n:
                # only islands reaching the skin of this layer count: a top skin over infill or a
                # ceiling over an enclosed void is inside the part and never seen
                skin = body_w & ~ndimage.binary_erosion(body_w, iterations=SKIN_PX)
                on_skin = np.zeros(n + 1, bool)
                on_skin[lab[skin]] = True
                for q, box_ in enumerate(ndimage.find_objects(lab), start=1):
                    if not on_skin[q]:
                        continue
                    cells = np.argwhere(lab[box_] == q) + (
                        box_[0].start + a0[0],
                        box_[1].start + a0[1],
                    )
                    a = len(cells) * PX * PX
                    cx, cy = cells.mean(0) * PX + org
                    if a <= min_area or not spec.visible_at(cx, cy, k - zoff):
                        continue
                    ia, ib = cells[:, ui].min(), cells[:, ui].max()
                    ua, ub = ia * PX + org[ui], ib * PX + org[ui]
                    va, vb = cells[:, 1 - ui].min(), cells[:, 1 - ui].max()

                    lo_end, hi_end = (max(ia - m1, 0), ia), (ib + 1, ib + 1 + m1)
                    vband = slice(va, vb + 1)
                    anchored = all(
                        foot[
                            (slice(*e), vband) if ui == 0 else (vband, slice(*e))
                        ].any()
                        for e in (lo_end, hi_end)
                    )
                    rec = {
                        "area_mm2": round(a, 3),
                        "x": round(float(cx), 2),
                        "y": round(float(cy), 2),
                        "z": k,
                    }
                    if (
                        anchored
                        and ub - ua <= bar_span
                        and spec.opening_for(ua, ub, k - zoff)
                    ):
                        rep["bridges"].append(
                            dict(rec, span_mm=round(float(ub - ua), 2))
                        )
                    else:
                        rep["islands"].append(rec)
        prev_body, zprev = body, k
    rep["islands"].sort(key=lambda r: -r["area_mm2"])
    rep["status"] = "FAIL" if rep["islands"] or rep["support"] else "PASS"
    return rep


# ---------------------------------------------------------------- driver
@dataclass
class Options:
    layer: float = LAYER
    min_area: float = MIN_AREA
    bar_span: float = BAR_SPAN


def check(
    parts: list[str], gcode: str | None, specs: Specs, opts: Options
) -> dict[str, Any]:
    """run both passes; parts are STL groups (`a.stl,b.stl` = one part) or 3MF files"""
    mesh: list[dict[str, Any]] = []
    skipped: list[str] = []
    names: list[str] = []
    for group in parts:
        files = [f for f in group.split(",") if f]
        if len(files) == 1 and files[0].lower().endswith(".3mf"):
            named = load_3mf(files[0])
        else:
            named = [
                (
                    os.path.splitext(os.path.basename(files[0]))[0],
                    [_stl(f) for f in files],
                )
            ]
        for name, meshes in named:
            names.append(name)
            spec = specs.resolve(object_names(name))
            if spec is None:
                skipped.append(name)
                continue
            mesh.append(
                mesh_report(
                    name,
                    union_solid(meshes),
                    spec,
                    opts.layer,
                    opts.min_area,
                    opts.bar_span,
                )
            )
    gc: list[dict[str, Any]] = []
    # an object-bound --visible must match something: in the G-code when one is given (a planned
    # object missing from the slice), else among the mesh parts (a typo would silently skip it)
    if gcode:
        objs, seen = parse_gcode(gcode, specs)
        gc = [
            gcode_report(o, opts.layer, opts.min_area, opts.bar_span)
            for o in objs.values()
        ]
        skipped += [s for s in seen if s not in objs]
        for pat in specs.patterns():
            if not any(
                fnmatch.fnmatchcase(n, pat) for s in seen for n in object_names(s)
            ):
                gc.append(
                    {
                        "name": pat,
                        "kind": "gcode",
                        "status": "FAIL",
                        "visible": "",
                        "layers": 0,
                        "support": [],
                        "islands": [],
                        "bridges": [],
                        "error": "no object in the G-code matches this name",
                    }
                )
    else:
        mesh.extend(
            {
                "name": pat,
                "kind": "mesh",
                "status": "FAIL",
                "visible": "",
                "layers": 0,
                "pieces": [],
                "bridges": [],
                "total_area_mm2": 0.0,
                "error": "no part matches this name",
            }
            for pat in specs.patterns()
            if not any(
                fnmatch.fnmatchcase(n, pat) for s in names for n in object_names(s)
            )
        )
    if not mesh and not gc:
        raise InputError(
            "nothing to check: no part or G-code object has a --visible side "
            f"(objects seen: {', '.join(skipped) or 'none'})"
        )
    status = "FAIL" if any(r["status"] == "FAIL" for r in mesh + gc) else "PASS"
    return {
        "status": status,
        "layer_mm": opts.layer,
        "min_area_mm2": opts.min_area,
        "bar_span_mm": opts.bar_span,
        "reach_mm": round(opts.layer + REACH_PAD, 3),
        "mesh": mesh,
        "gcode": gc,
        "skipped": skipped,
    }


def _bridge_note(br: list[dict[str, Any]]) -> str:
    return (
        (
            f"; bridges in openings: {len(br)}, longest {max(b['span_mm'] for b in br):.1f} mm"
        )
        if br
        else ""
    )


def text_report(res: dict[str, Any]) -> list[str]:
    out = []
    if res["mesh"]:
        nf = sum(r["status"] == "FAIL" for r in res["mesh"])
        out.append(
            f"visible-faces (mesh, layer {res['layer_mm']} mm): {'PASS' if not nf else f'FAIL in {nf} parts'}"
        )
        for r in res["mesh"]:
            if r.get("error"):
                out.append(f"  {r['name']:8s} FAIL {r['error']}")
            elif r["pieces"]:
                out.append(
                    f"  {r['name']:8s} FAIL {len(r['pieces'])} unsupported pieces on the visible side "
                    f"({r['visible']}), {r['total_area_mm2']:.1f} mm² in all{_bridge_note(r['bridges'])}"
                )
                for p in r["pieces"][:15]:
                    b = p["bbox"]
                    out.append(
                        f"           {p['area_mm2']:6.2f} mm² at x {p['x']:.1f} y {p['y']:.1f} z {p['z']:.2f} "
                        f"(x {b[0]:.1f}..{b[2]:.1f}, y {b[1]:.1f}..{b[3]:.1f})"
                    )
                if len(r["pieces"]) > 15:
                    out.append(
                        f"           ... {len(r['pieces']) - 15} more (--json lists all)"
                    )
            else:
                out.append(
                    f"  {r['name']:8s} PASS ({r['layers']} layers, no unsupported piece over "
                    f"{res['min_area_mm2']} mm² on the visible side ({r['visible']}){_bridge_note(r['bridges'])})"
                )
    if res["gcode"]:
        nf = sum(r["status"] == "FAIL" for r in res["gcode"])
        out.append(
            f"visible-faces (G-code): {'PASS' if not nf else f'FAIL in {nf} objects'}"
        )
        for r in res["gcode"]:
            if r.get("error"):
                out.append(f"  {r['name']:8s} FAIL {r['error']}")
            elif r["status"] == "FAIL":
                out.append(
                    f"  {r['name']:8s} FAIL {len(r['support'])} support moves and {len(r['islands'])} "
                    f"unsupported islands on the visible side ({r['visible']}){_bridge_note(r['bridges'])}"
                )
                out.extend(
                    f"           support ({s['type']}) at x {s['x']:.1f} y {s['y']:.1f} z {s['z']:.2f}"
                    for s in r["support"][:5]
                )
                out.extend(
                    f"           island {i['area_mm2']:6.2f} mm² at x {i['x']:.1f} y {i['y']:.1f} z {i['z']:.2f}"
                    for i in r["islands"][:10]
                )
            else:
                out.append(
                    f"  {r['name']:8s} PASS ({r['layers']} layers: no support, no island without the layer "
                    f"below on the visible side ({r['visible']}){_bridge_note(r['bridges'])})"
                )
    if res["skipped"]:
        out.append(f"not gated (no --visible for them): {', '.join(res['skipped'])}")
    out.append(f">>> VISIBLE-FACES: {res['status']}")
    return out


def build_specs(visible: list[str], hidden: list[str], openings: list[str]) -> Specs:
    sp = Specs()
    for v in visible:
        obj, val = _split_target(v, True)
        sp.visible.append((obj, parse_visible(val)))
    for h in hidden:
        obj, val = _split_target(h, False)
        sp.hidden.append((obj, load_hidden(val)))
    for o in openings:
        obj, val = _split_target(o, False)
        sp.openings.append((obj, load_openings(val)))
    return sp


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="3d visible-faces",
        description="no unsupported layer piece or support on the visible side of a part",
    )
    ap.add_argument("parts", nargs="*", help="PART.stl[,VOLUME2.stl...] or PLATE.3mf")
    ap.add_argument("--visible", action="append", default=[], metavar="[OBJ:]SPEC")
    ap.add_argument("--hidden", action="append", default=[], metavar="[OBJ:]POLYS.json")
    ap.add_argument(
        "--openings", action="append", default=[], metavar="[OBJ:]OPENINGS.json"
    )
    ap.add_argument("--gcode", metavar="PLATE.gcode")
    ap.add_argument("--layer", type=float, default=LAYER)
    ap.add_argument("--min-area", type=float, default=MIN_AREA)
    ap.add_argument("--bar-span", type=float, default=BAR_SPAN)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        bad = [
            f"--{k} {v}"
            for k, v in (
                ("layer", a.layer),
                ("min-area", a.min_area),
                ("bar-span", a.bar_span),
            )
            if not (math.isfinite(v) and v > 0)
        ]
        if bad:
            raise InputError(
                "invalid option: " + "; ".join(bad) + " (must be finite and > 0)"
            )
        if not a.visible:
            raise InputError(
                "--visible is required (e.g. --visible 'y<4.8', --visible 'WALL:x>2', --visible all)"
            )
        if not a.parts and not a.gcode:
            raise InputError("give a part (STL/3MF) and/or --gcode PLATE.gcode")
        res = check(
            a.parts,
            a.gcode,
            build_specs(a.visible, a.hidden, a.openings),
            Options(a.layer, a.min_area, a.bar_span),
        )
    except InputError as e:
        print(f"visible-faces: error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print("\n".join(text_report(res)))
    return 1 if res["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
