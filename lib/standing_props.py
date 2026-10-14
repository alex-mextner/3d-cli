#!/usr/bin/env python3
"""standing_props.py — snap-off props for a part printed standing, and its stability gate.

A tall thin part printed upright (a wall panel, roof, rim, sign) rocks at tool changes and
z-hops and may tip over mid-print. This tool adds sloped ribs on its hidden back side and
checks that the part with its ribs stands stably.

Geometry (print frame: z up, the part's thickness along y, back = +y for --back +y):
  stations  n = max(2, round((L - 20) / spacing) + 1) ribs, evenly from x0 + 10 to x1 - 10
  profile   at each station the back of the part (the hindmost y of the part sliced through
            a window 1 mm wider than the rib) is read every 1 mm of z up to height·H
  rib       a RIB-thick plate in the (y, z) plane: inner edge straight at t_max + gap (t_max
            = hindmost point of the back at that station, so a hollow or ribbed back does not
            pull the rib into its cells), outer edge sloping down at `angle` to the bed
  tabs      up to three TAB-square bridges per rib to the back, only at heights where the back
            reaches within 0.5 mm of t_max, nearest the bottom, middle and top of the rib
  foot      a FOOT-high strip on the bed joining all ribs, 1 mm behind their inner edges
  props     (ribs ∪ foot) − (part dilated by ±gap in y and z), then ∪ tabs

Stability gate: ratio = H / (part depth at the bed + rib reach) must be <= --max-ratio. Rib
reach is the distance from the hindmost back point to the rib's outer edge at the bed.

Ported from babushkin-dom gen/standing_props.py (constants and algorithm unchanged). One
deliberate difference: the part depth in the ratio is measured at the bed (the first-layer
slice), as the gate describes, not over the whole bounding box.

Input: an STL (one part) or a 3MF (one object, any number of parts; with several objects
pick one with --object). Output (-o): a 3MF with the props as an extra part of the same
object (other objects carried over), or an STL with the props alone.

Exit: 0 PASS, 1 FAIL (unstable), 2 usage / load error.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

import numpy as np

RIB, TAB, FOOT = 1.6, 0.8, 0.6               # mm: rib plate, tab square, foot strip height
END_INSET = 10.0                              # mm: first/last station in from the part ends
PROFILE_STEP = 1.0                            # mm: back profile sampling in z
TAB_REACH = 0.5                               # mm: back within this of t_max gets a tab
TAB_EMBED = 0.3                               # mm: tab overlap into the part and into the rib
TAB_LOW, TAB_HIGH = 5.0, 3.0                  # mm: lowest tab aims this far above the bed, highest this far under the rib top
FOOT_OFFSET = 1.0                             # mm: foot strip starts this far behind the rib edge
BED_SLICE = 0.01                              # mm: the bed footprint is the part's slice this far above its bottom
DEFAULTS = {"spacing": 50.0, "height": 0.6, "angle": 55.0, "gap": 0.25, "max_ratio": 3.0}

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
REL_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
SETTINGS = "Metadata/model_settings.config"


class InputError(ValueError):
    """the input cannot be loaded or does not describe one closed part"""


@dataclass
class Body:
    """one mesh of an object, in world coordinates (mm)"""
    name: str
    vertices: np.ndarray                      # (n, 3) float
    faces: np.ndarray                         # (m, 3) int
    extruder: int | None = None


@dataclass
class Obj:
    """one object of the build: its bodies (colour parts) share the object"""
    name: str
    bodies: list[Body]
    extruder: int | None = None
    printable: bool = True


@dataclass
class PropsResult:
    mesh: Any                                 # manifold3d.Manifold of the props, None without ribs
    tabs: list[tuple[float, ...]] = field(default_factory=list)   # tab boxes (x0 y0 z0 x1 y1 z1)
    info: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- geometry


def _manifold(vertices: np.ndarray, faces: np.ndarray) -> Any:
    import trimesh
    from manifold3d import Manifold, Mesh

    tm = trimesh.Trimesh(np.asarray(vertices, float), np.asarray(faces, np.int64), process=False)
    tm.merge_vertices()
    return Manifold(Mesh(vert_properties=np.asarray(tm.vertices, np.float32),
                         tri_verts=np.asarray(tm.faces, np.uint32)))


def _box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> Any:
    from manifold3d import Manifold

    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def part_manifold(bodies: list[Body]) -> Any:
    """union of the bodies; InputError when a body is not a closed manifold"""
    from manifold3d import Error, Manifold, OpType

    mans = []
    for b in bodies:
        m = _manifold(b.vertices, b.faces)
        if m.status() != Error.NoError or m.is_empty():
            raise InputError(f"part '{b.name}' is not a closed manifold mesh ({m.status().name}); "
                             f"repair it first (3d mesh {b.name} reports the defects)")
        mans.append(m)
    return Manifold.batch_boolean(mans, OpType.Add)


def stations(x0: float, x1: float, spacing: float) -> np.ndarray:
    """rib x positions: at least two, END_INSET in from each end; a part too short for two
    distinct stations gets one in the middle"""
    inset = min(END_INSET, (x1 - x0) / 2)
    n = max(2, int(round((x1 - x0 - 2 * END_INSET) / spacing)) + 1)
    return np.unique(np.round(np.linspace(x0 + inset, x1 - inset, n), 6))


def props(part: Any, k: int, *, spacing: float = 50.0, height: float = 0.6, angle: float = 55.0,
          gap: float = 0.25, max_ratio: float = 3.0) -> PropsResult:
    """part: manifold3d.Manifold in the print frame; k: back direction (+1: +y, -1: -y)."""
    from manifold3d import CrossSection, Manifold, OpType

    x0, y0, z0, x1, y1, z1 = part.bounding_box()
    H = z1 - z0
    hr = height * H
    cot = 1.0 / math.tan(math.radians(angle))
    st = stations(x0, x1, spacing)
    # the part dilated by the snap-off gap (in y and z) is what the props must stay clear of
    keep_out = Manifold.batch_boolean(
        [part.translate((0, dy, dz)) for dy in (-gap, 0, gap) for dz in (-gap, 0, gap)], OpType.Add)
    ribs: list[Any] = []
    tabs: list[tuple[float, ...]] = []
    reach, t_max_all, used = 0.0, -math.inf, []
    zs = np.arange(z0 + 0.1, z0 + hr, PROFILE_STEP)
    for xc in st:
        win = _box(xc - RIB / 2 - 0.5, y0 - 1, z0, xc + RIB / 2 + 0.5, y1 + 1, z0 + hr)
        piece = part ^ win
        if piece.is_empty() or len(zs) == 0:
            continue
        back: list[float | None] = []
        for z in zs:
            sl = piece.slice(float(z))
            if sl.is_empty():
                back.append(None)
                continue
            b = sl.bounds()                                   # (xmin, ymin, xmax, ymax)
            back.append(b[3] if k > 0 else -b[1])             # in t = k·y
        known = [v for v in back if v is not None]
        if not known:
            continue
        last = known[0]
        filled: list[float] = []
        for v in back:                                        # gaps take the value below them
            last = v if v is not None else last
            filled.append(last)
        t_max = max(filled)
        # straight inner edge behind the hindmost point of the back (a hollow underside or
        # ribbed back would otherwise pull the prop into its cells, where it cannot snap off)
        tin = t_max + gap
        outer = [(tin + 1.0 + (z0 + hr - z) * cot, float(z)) for z in zs[::-1]]
        pts = [(tin, z0), (tin, z0 + hr)] + outer + [(outer[-1][0], z0)]
        ring = [(t * k, z) for t, z in pts]                   # plane (y, z)
        cs = CrossSection([ring])
        if cs.area() <= 0:
            cs = CrossSection([ring[::-1]])                   # either winding
        # extruded (X = y, Y = z, Z = thickness) -> part frame (x = Z, y = X, z = Y) at the station
        rib = Manifold.extrude(cs, RIB).transform(
            np.array([[0, 0, 1, xc - RIB / 2], [1, 0, 0, 0], [0, 1, 0, 0.0]]))
        ribs.append(rib)
        used.append(float(xc))
        reach = max(reach, outer[-1][0] - t_max)
        t_max_all = max(t_max_all, t_max)
        # tabs where the back reaches the rib, aimed at the bottom, middle and top of the rib;
        # each embeds into the back as sampled at its own height (not the station's t_max,
        # which a back up to TAB_REACH lower would never meet), and none dips under the bed
        near = {float(z): t for t, z in zip(filled, zs) if t >= t_max - TAB_REACH}
        for target in (z0 + TAB_LOW, z0 + hr / 2, z0 + hr - TAB_HIGH):
            zt = min(near, key=lambda z: abs(z - target))
            ya, yb = sorted(((near[zt] - TAB_EMBED) * k, (tin + TAB_EMBED) * k))
            zb = max(z0, zt - TAB / 2)
            box = (xc - TAB / 2, ya, zb, xc + TAB / 2, yb, zb + TAB)
            if box not in tabs:                               # targets snapped to the same z share one tab
                tabs.append(box)

    # bed footprint: an empty first slice (a degenerate bottom) means no footprint to stand on
    bed = part.slice(z0 + BED_SLICE)
    depth = (bed.bounds()[3] - bed.bounds()[1]) if not bed.is_empty() else 0.0
    ratio = H / max(depth + reach, 1e-3)                      # nothing to stand on: huge ratio, FAIL
    info: dict[str, Any] = dict(stations=len(ribs), station_x=[round(x, 2) for x in used],
                                height=round(float(hr), 1), reach=round(float(reach), 1),
                                depth=round(float(depth), 1), tabs=len(tabs), ratio=round(float(ratio), 2),
                                max_ratio=max_ratio, ok=bool(ratio <= max_ratio))
    if not ribs:
        return PropsResult(None, [], info)
    ta = t_max_all + gap + FOOT_OFFSET
    tb = ta + 0.5 * hr * cot
    ya, yb = sorted((ta * k, tb * k))
    foot = _box(used[0] - RIB / 2, ya, z0, used[-1] + RIB / 2, yb, z0 + FOOT)
    pr = Manifold.batch_boolean(ribs + [foot], OpType.Add) - keep_out
    pr = Manifold.batch_boolean([pr] + [_box(*t) for t in tabs], OpType.Add)
    return PropsResult(pr, tabs, info)


def mesh_arrays(m: Any) -> tuple[np.ndarray, np.ndarray]:
    g = m.to_mesh()
    return np.asarray(g.vert_properties, float)[:, :3], np.asarray(g.tri_verts, np.int64)


# ---------------------------------------------------------------- 3MF / STL input


def _matrix(spec: str | None) -> np.ndarray:
    """3MF transform (12 numbers, row-vector convention) -> 4x4 column-vector matrix"""
    M = np.eye(4)
    if spec:
        v = [float(t) for t in spec.split()]
        if len(v) != 12:
            raise InputError(f"bad 3MF transform '{spec}'")
        M[:3, :3] = np.array(v[:9]).reshape(3, 3).T
        M[:3, 3] = v[9:12]
    return M


def _root_model(zf: zipfile.ZipFile) -> str:
    try:
        rels = ET.fromstring(zf.read("_rels/.rels"))
        for r in rels:
            if r.get("Type") == REL_3DMODEL and r.get("Target"):
                return str(r.get("Target")).lstrip("/")
    except KeyError:
        pass
    return "3D/3dmodel.model"


def _settings(zf: zipfile.ZipFile) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Orca/Bambu model_settings.config -> (object id -> metadata, part id -> metadata)"""
    objs: dict[str, dict[str, str]] = {}
    parts: dict[str, dict[str, str]] = {}
    if SETTINGS not in zf.namelist():
        return objs, parts
    root = ET.fromstring(zf.read(SETTINGS))
    for o in root.iter("object"):
        objs[o.get("id", "")] = {m.get("key", ""): m.get("value", "") for m in o.findall("metadata")}
        for p in o.findall("part"):
            parts[p.get("id", "")] = {m.get("key", ""): m.get("value", "") for m in p.findall("metadata")}
    return objs, parts


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v else None
    except ValueError:
        return None


def load_3mf(path: str) -> list[Obj]:
    """every build item of a 3MF as an Obj with its mesh bodies in world coordinates;
    InputError for anything that is not a readable 3MF"""
    try:
        return _load_3mf(path)
    except InputError:
        raise
    except (ValueError, IndexError, ET.ParseError, zipfile.BadZipFile) as e:
        raise InputError(f"{path}: malformed 3MF: {e}") from e


def _load_3mf(path: str) -> list[Obj]:
    with zipfile.ZipFile(path) as zf:
        root_path = _root_model(zf)
        models: dict[str, tuple[ET.Element, float]] = {}

        def model(p: str) -> tuple[ET.Element, float]:
            p = p.lstrip("/")
            if p not in models:
                try:
                    el = ET.fromstring(zf.read(p))
                except KeyError as e:
                    raise InputError(f"{path}: missing model part {p}") from e
                models[p] = (el, UNITS.get(el.get("unit", "millimeter"), 1.0))
            return models[p]

        def find(p: str, oid: str) -> ET.Element:
            el, _ = model(p)
            for o in el.iter(f"{{{CORE}}}object"):
                if o.get("id") == oid:
                    return o
            raise InputError(f"{path}: object {oid} not found in {p}")

        o_meta, p_meta = _settings(zf)
        root_el, scale = model(root_path)
        S = np.diag([scale, scale, scale, 1.0])

        def leaves(p: str, oid: str, T: np.ndarray, depth: int = 0) -> list[Body]:
            if depth > 16:
                raise InputError(f"{path}: component nesting too deep (cycle?)")
            o = find(p, oid)
            mesh = o.find(f"{{{CORE}}}mesh")
            if mesh is not None:
                vs = mesh.find(f"{{{CORE}}}vertices")
                ts = mesh.find(f"{{{CORE}}}triangles")
                if vs is None or ts is None:
                    return []
                V = np.array([[float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))]
                              for v in vs], float).reshape(-1, 3)
                F = np.array([[int(t.get("v1", 0)), int(t.get("v2", 0)), int(t.get("v3", 0))]
                              for t in ts], np.int64).reshape(-1, 3)
                # a production-extension model file may have its own unit: bring its
                # coordinates to the root model's unit, in which the transforms are written
                V = V * (model(p)[1] / scale)
                V = (T @ np.c_[V, np.ones(len(V))].T).T[:, :3]
                if np.linalg.det(T[:3, :3]) < 0:              # a mirroring transform turns the mesh inside out
                    F = F[:, ::-1]
                meta = p_meta.get(oid, {})
                name = meta.get("name") or o.get("name") or f"part{oid}"
                return [Body(name, V, F, _int(meta.get("extruder")))]
            out: list[Body] = []
            comps = o.find(f"{{{CORE}}}components")
            for c in comps if comps is not None else []:
                cp = c.get(f"{{{PROD}}}path") or p
                out += leaves(cp, c.get("objectid", ""), T @ _matrix(c.get("transform")), depth + 1)
            return out

        objs: list[Obj] = []
        build = root_el.find(f"{{{CORE}}}build")
        for item in build if build is not None else []:
            oid = item.get("objectid", "")
            ip = item.get(f"{{{PROD}}}path") or root_path
            bodies = leaves(ip, oid, S @ _matrix(item.get("transform")))
            meta = o_meta.get(oid, {})
            name = meta.get("name") or find(ip, oid).get("name") or f"object{oid}"
            if bodies:
                objs.append(Obj(name, bodies, _int(meta.get("extruder")), item.get("printable", "1") != "0"))
    if not objs:
        raise InputError(f"{path}: no mesh objects in the 3MF build")
    return objs


def load_stl(path: str) -> list[Obj]:
    import trimesh

    try:
        tm = trimesh.load(path, file_type="stl", force="mesh", process=False)
    except Exception as e:
        raise InputError(f"{path}: cannot read STL: {e}") from e
    if not isinstance(tm, trimesh.Trimesh) or tm.is_empty:
        raise InputError(f"{path}: empty STL")
    name = os.path.splitext(os.path.basename(path))[0]
    return [Obj(name, [Body(name, np.asarray(tm.vertices, float), np.asarray(tm.faces, np.int64))])]


# ---------------------------------------------------------------- output


def _fmt(v: float) -> str:
    return repr(float(v))


def _mesh_object_xml(oid: int, b: Body) -> str:
    rows = [f'  <object id="{oid}" type="model">', "   <mesh>", "    <vertices>"]
    rows += [f'     <vertex x="{_fmt(x)}" y="{_fmt(y)}" z="{_fmt(z)}"/>' for x, y, z in b.vertices]
    rows += ["    </vertices>", "    <triangles>"]
    rows += [f'     <triangle v1="{a}" v2="{c}" v3="{d}"/>' for a, c, d in b.faces]
    rows += ["    </triangles>", "   </mesh>", "  </object>"]
    return "\n".join(rows)


def write_3mf(path: str, objs: list[Obj]) -> None:
    """3MF with one multi-part object per Obj (core components, bodies in world coordinates)
    and an Orca/Bambu model_settings.config naming each part and its extruder"""
    from xml.sax.saxutils import quoteattr

    res: list[str] = []
    items: list[str] = []
    cfg = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    nid = 1
    for o in objs:
        ids = []
        for b in o.bodies:
            res.append(_mesh_object_xml(nid, b))
            ids.append(nid)
            nid += 1
        oid = nid
        nid += 1
        res.append(f'  <object id="{oid}" type="model"><components>'
                   + "".join(f'<component objectid="{i}"/>' for i in ids) + "</components></object>")
        items.append(f'  <item objectid="{oid}" printable="{1 if o.printable else 0}"/>')
        cfg.append(f'  <object id="{oid}">')
        cfg.append(f'    <metadata key="name" value={quoteattr(o.name)}/>')
        if o.extruder is not None:
            cfg.append(f'    <metadata key="extruder" value="{o.extruder}"/>')
        for i, b in zip(ids, o.bodies):
            cfg.append(f'    <part id="{i}" subtype="normal_part">')
            cfg.append(f'      <metadata key="name" value={quoteattr(b.name)}/>')
            if b.extruder is not None:
                cfg.append(f'      <metadata key="extruder" value="{b.extruder}"/>')
            cfg.append("    </part>")
        cfg.append("  </object>")
    cfg.append("</config>")
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE}">\n <resources>\n'
             + "\n".join(res) + "\n </resources>\n <build>\n" + "\n".join(items) + "\n </build>\n</model>\n")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                    ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                    ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                    ' <Default Extension="config" ContentType="text/xml"/>\n'
                    "</Types>")
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                    f' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{REL_3DMODEL}"/>\n'
                    "</Relationships>")
        zf.writestr("3D/3dmodel.model", model)
        zf.writestr(SETTINGS, "\n".join(cfg))


def write_stl(path: str, vertices: np.ndarray, faces: np.ndarray) -> None:
    import trimesh

    trimesh.Trimesh(vertices, faces, process=False).export(path, file_type="stl")


def dominant_extruder(o: Obj) -> int | None:
    """extruder of the largest-volume body (falls back to the object's extruder)"""
    best = max(o.bodies, key=lambda b: abs(_signed_volume(b.vertices, b.faces)))
    return best.extruder if best.extruder is not None else o.extruder


def _signed_volume(V: np.ndarray, F: np.ndarray) -> float:
    if len(F) == 0:
        return 0.0
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


# ---------------------------------------------------------------- CLI


def _bounded(name: str, lo: float = 0.0, hi: float = math.inf, lo_open: bool = True) -> Any:
    def parse(s: str) -> float:
        try:
            v = float(s)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"{name} must be a number, got '{s}'") from e
        if not math.isfinite(v) or (v <= lo if lo_open else v < lo) or v > hi:
            bound = f"> {lo}" if lo_open else f">= {lo}"
            raise argparse.ArgumentTypeError(f"{name} must be {bound}" + (f" and <= {hi}" if hi < math.inf else "")
                                             + f", got {s}")
        return v
    return parse


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="3d props", description="snap-off props for a part printed standing")
    ap.add_argument("input", help="part .stl or .3mf in the print frame (z up, thickness along y)")
    ap.add_argument("--back", required=True, choices=("+y", "-y"), help="hidden back side")
    ap.add_argument("--object", help="3MF object to prop (required when the 3MF holds several)")
    ap.add_argument("--spacing", type=_bounded("--spacing"), default=DEFAULTS["spacing"])
    ap.add_argument("--height", type=_bounded("--height", hi=1.0), default=DEFAULTS["height"])
    ap.add_argument("--angle", type=_bounded("--angle", hi=89.0), default=DEFAULTS["angle"])
    ap.add_argument("--gap", type=_bounded("--gap", lo_open=False), default=DEFAULTS["gap"])
    ap.add_argument("--max-ratio", type=_bounded("--max-ratio"), default=DEFAULTS["max_ratio"])
    ap.add_argument("-o", "--output", help="write OUT.3mf (props as an extra part) or OUT.stl (props only)")
    ap.add_argument("--json", action="store_true", help="JSON report on stdout")
    return ap


def _pick(objs: list[Obj], name: str | None, path: str) -> Obj:
    if name is not None:
        hits = [o for o in objs if o.name == name]
        if len(hits) != 1:
            names = ", ".join(o.name for o in objs)
            raise InputError(f"{path}: {'no' if not hits else 'several'} objects named '{name}' (objects: {names})")
        return hits[0]
    if len(objs) > 1:
        raise InputError(f"{path} holds {len(objs)} objects ({', '.join(o.name for o in objs)}); "
                         "pick the standing one with --object NAME")
    return objs[0]


def _glue_back(argv: list[str]) -> list[str]:
    """`--back -y` would read as a flag to argparse: glue the value to its option"""
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--back" and i + 1 < len(argv) and argv[i + 1] in ("+y", "-y"):
            out.append(f"--back={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main(argv: list[str]) -> int:
    args = _parser().parse_args(_glue_back(argv))
    k = -1 if args.back == "-y" else 1
    out = args.output
    if out and os.path.splitext(out)[1].lower() not in (".3mf", ".stl"):
        print(f"3d props: -o must end in .3mf or .stl, got {out}", file=sys.stderr)
        return 2
    try:
        ext = os.path.splitext(args.input)[1].lower()
        if ext == ".3mf":
            objs = load_3mf(args.input)
        elif ext == ".stl":
            if args.object is not None:
                raise InputError("--object applies to 3MF input only")
            objs = load_stl(args.input)
        else:
            raise InputError(f"{args.input}: expected a .stl or .3mf file")
        target = _pick(objs, args.object, args.input)
        part = part_manifold(target.bodies)
    except InputError as e:
        print(f"3d props: {e}", file=sys.stderr)
        return 2
    r = props(part, k, spacing=args.spacing, height=args.height, angle=args.angle, gap=args.gap,
              max_ratio=args.max_ratio)
    info = r.info
    written = None
    if info["ok"] and out and r.mesh is not None:
        V, F = mesh_arrays(r.mesh)
        if out.lower().endswith(".stl"):
            write_stl(out, V, F)
        else:
            body = Body(f"{target.name} props", V, F, dominant_extruder(target))
            target.bodies.append(body)
            write_3mf(out, objs)
        written = out
    report = dict(input=args.input, object=target.name, back=f"{'+' if k > 0 else '-'}y", **info,
                  status="PASS" if info["ok"] else "FAIL", output=written)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report, out)
    return 0 if info["ok"] else 1


def _print_report(r: dict[str, Any], out: str | None) -> None:
    print(f"props  {r['input']}  object {r['object']}  back {r['back']}")
    if r["stations"]:
        xs = ", ".join(f"{x:g}" for x in r["station_x"])
        print(f"  ribs     {r['stations']} at x = {xs} mm; {r['tabs']} tabs")
        print(f"  rib      height {r['height']} mm, reach {r['reach']} mm behind the back")
    else:
        print("  ribs     none: the part has no material at any station")
    print(f"  base     depth at the bed {r['depth']} mm + reach {r['reach']} mm")
    print(f"  ratio    height / base = {r['ratio']} (max {r['max_ratio']:g})")
    if r["ok"]:
        tail = f"; wrote {r['output']}" if r["output"] else ("; no props to write" if out else "")
        print(f">>> PROPS: PASS{tail}")
    else:
        print(f">>> PROPS: FAIL height/base ratio {r['ratio']} > {r['max_ratio']:g}: the part may tip over"
              " mid-print; print it lying down, add a wider base, or raise --height / lower --angle"
              + ("; nothing written" if out else ""))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
