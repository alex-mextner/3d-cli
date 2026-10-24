"""dimension_model.py — read a model file into raw geometry WITH its colours, for `3d dimension`.

WHAT: `load_raw(path, defines)` returns a `RawModel`: one vertex/face soup plus, per face, the colour
  GROUP it belongs to — (object name, material/colour label, authored #RRGGBB or None). `dimension_parts.py`
  turns the groups into named parts, resolves their colours and builds the mesh that gets painted.
  Sources of colour:
    .3mf   `basematerials` / `colorgroup` properties, per triangle (pid/p1) or per object (pid/pindex),
           build items + component trees with their transforms, the model unit; object names from the
           model (or Orca's `Metadata/model_settings.config`) and the part-role metadata (`3d:role`).
    .obj   `o`/`g` names, `usemtl` + the `mtllib` file's `Kd`.
    .scad  exported by openscad to 3MF (which keeps every `color()` as a per-triangle base material).
    other  trimesh (STL, PLY, …): one uncoloured group.

WHY: the renderer used to take `trimesh.load(..., force="mesh")` for everything, which drops colours and
  materials and needs `lxml` for 3MF (not a dependency of this repo): every part came out the same blue
  (alex-mextner/3d-cli#580).

INVARIANTS: the 3MF and OBJ readers are stdlib + numpy only (zipfile + xml.etree) — no lxml, no
  networkx. A 3MF/OBJ that cannot be read raises `RenderError` (a message the user can act on), never a
  traceback. The returned arrays are in millimetres (3MF unit applied).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

import numpy as np

UNIT_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}
MAX_COMPONENT_DEPTH = 16
# "Color 3", "Material", "default": a generated material name says nothing about the part.
GENERIC_LABEL = re.compile(
    r"^(color|colour|material|default|mat)[ _.-]?\d*$", re.IGNORECASE
)
# The base material OpenSCAD gives geometry without color().
OPENSCAD_DEFAULT_MATERIAL = "Default"


class RenderError(Exception):
    """A failure the user can act on; `main` prints it and exits with `exit_code`."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class RawGroup:
    """One run of faces that share an object and a colour."""

    object_name: str  # "" when the file has no usable object name
    label: str  # material name, or the colour hex for a generic one; "" when uncoloured
    color: str | None  # authored "#RRGGBB"
    meta: tuple[tuple[str, str], ...] = ()  # object metadata (3d:role, …)
    object_id: str = (
        ""  # identity of the object instance; "" = the file has no object structure
    )


@dataclass
class RawModel:
    vertices: np.ndarray  # (N, 3) mm
    faces: np.ndarray  # (F, 3) indices
    face_group: np.ndarray  # (F,) index into groups
    groups: list[RawGroup]
    warnings: list[str] = field(default_factory=list)


def to_hex(r: float, g: float, b: float) -> str:
    """Channels in 0..1 -> '#RRGGBB'."""
    return "#" + "".join(
        f"{int(round(min(max(c, 0.0), 1.0) * 255)):02X}" for c in (r, g, b)
    )


def from_hex(color: str) -> tuple[float, float, float]:
    """'#RRGGBB' -> channels in 0..1."""
    return (
        int(color[1:3], 16) / 255.0,
        int(color[3:5], 16) / 255.0,
        int(color[5:7], 16) / 255.0,
    )


def parse_hex(raw: str | None) -> str | None:
    """'#RRGGBB' or '#RRGGBBAA' (any case) -> '#RRGGBB'; anything else -> None."""
    if raw is None:
        return None
    text = raw.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text):
        return None
    return text[:7].upper()


def colour_label(material: str, color: str | None) -> str:
    """The name a colour group shows: its material name, or the hex for a generated one."""
    if color is None:
        return ""
    return color if not material or GENERIC_LABEL.match(material) else material


def _strip_ns(root: ET.Element) -> None:
    """Drop XML namespaces in place, so lookups use plain tag/attribute names."""
    for el in root.iter():
        el.tag = el.tag.rpartition("}")[2]
        if any("}" in key for key in el.attrib):
            el.attrib = {k.rpartition("}")[2]: v for k, v in el.attrib.items()}


def _parse_xml(data: bytes, what: str) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RenderError(f"3d dimension: {what} is not valid XML: {exc}") from exc
    _strip_ns(root)
    return root


def _matrix(raw: str | None) -> np.ndarray:
    """A 3MF transform (12 numbers, row-vector convention) as a 4x4 matrix."""
    out = np.eye(4)
    if not raw:
        return out
    try:
        v = [float(x) for x in raw.split()]
    except ValueError as exc:
        raise RenderError(f"3d dimension: bad 3MF transform {raw!r}") from exc
    if len(v) != 12:
        raise RenderError(
            f"3d dimension: a 3MF transform needs 12 numbers, got {len(v)}"
        )
    out[:3, :3] = np.array(v[:9]).reshape(3, 3)
    out[3, :3] = v[9:]
    return out


@dataclass
class _Props:
    names: list[str]
    colors: list[str | None]


@dataclass
class _Obj:
    name: str
    pid: str | None
    pindex: int
    vertices: np.ndarray | None
    triangles: list[Any]
    components: list[tuple[str, np.ndarray]]
    meta: dict[str, str]


def _model_part(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if "_rels/.rels" in names:
        rels = _parse_xml(zf.read("_rels/.rels"), "3MF _rels/.rels")
        for rel in rels.iter("Relationship"):
            if rel.get("Type", "").endswith("/3dmodel") and rel.get("Target"):
                target = (rel.get("Target") or "").lstrip("/")
                if target in names:
                    return target
    for name in names:
        if name.lower().endswith(".model"):
            return name
    raise RenderError("3d dimension: the 3MF has no 3D model part")


def _settings_names(zf: zipfile.ZipFile) -> dict[str, str]:
    """Object names of a slicer project (`Metadata/model_settings.config`), by object id."""
    if "Metadata/model_settings.config" not in zf.namelist():
        return {}
    try:
        cfg = ET.fromstring(zf.read("Metadata/model_settings.config"))
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for obj in cfg.iter("object"):
        for md in obj.findall("metadata"):
            if md.get("key") == "name" and md.get("value"):
                out[obj.get("id", "")] = md.get("value", "")
    return out


def _metadata(el: ET.Element) -> dict[str, str]:
    meta: dict[str, str] = {}
    for md in [*el.findall("metadatagroup/metadata"), *el.findall("metadata")]:
        key = md.get("name")
        if key:
            meta[key] = (md.text or md.get("value") or "").strip()
    return meta


def _read_object(el: ET.Element, settings: dict[str, str], from_openscad: bool) -> _Obj:
    oid = el.get("id", "")
    name = el.get("name") or settings.get(oid, "")
    if from_openscad:
        name = ""  # "OpenSCAD Model" names the exporter, not a part
    verts: np.ndarray | None = None
    tris: list[Any] = []
    mesh = el.find("mesh")
    if mesh is not None:
        vertex_els = mesh.findall("vertices/vertex")
        try:
            verts = np.array(
                [
                    [float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))]
                    for v in vertex_els
                ],
                dtype=float,
            ).reshape(-1, 3)
            tris = list(mesh.findall("triangles/triangle"))
        except ValueError as exc:
            raise RenderError(
                f"3d dimension: object {oid} of the 3MF has a bad vertex: {exc}"
            ) from exc
    comps = [
        (c.get("objectid", ""), _matrix(c.get("transform")))
        for c in el.findall("components/component")
    ]
    try:
        pindex = int(el.get("pindex", "0"))
    except ValueError:
        pindex = 0
    return _Obj(name, el.get("pid"), pindex, verts, tris, comps, _metadata(el))


def _properties(resources: ET.Element) -> dict[str, _Props]:
    props: dict[str, _Props] = {}
    for el in resources:
        if el.tag == "basematerials":
            props[el.get("id", "")] = _Props(
                [b.get("name", "") for b in el.findall("base")],
                [parse_hex(b.get("displaycolor")) for b in el.findall("base")],
            )
        elif el.tag == "colorgroup":
            colors = [parse_hex(c.get("color")) for c in el.findall("color")]
            props[el.get("id", "")] = _Props([""] * len(colors), colors)
    return props


def read_3mf(path: str) -> RawModel:
    """A 3MF as raw geometry + colour groups; an OpenSCAD export's first base material means "no color()"."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RenderError(
            f"3d dimension: {path} is not a readable 3MF archive: {exc}"
        ) from exc
    with zf:
        root = _parse_xml(zf.read(_model_part(zf)), "the 3MF model")
        settings = _settings_names(zf)
    application = next(
        (
            m.text or ""
            for m in root.findall("metadata")
            if m.get("name") == "Application"
        ),
        "",
    )
    from_openscad = application.startswith("OpenSCAD")
    unit = root.get("unit", "millimeter")
    if unit not in UNIT_MM:
        raise RenderError(
            f"3d dimension: unknown 3MF unit {unit!r} (expected one of {', '.join(UNIT_MM)})"
        )
    resources = root.find("resources")
    if resources is None:
        raise RenderError("3d dimension: the 3MF has no resources")
    props = _properties(resources)
    objects = {
        o.get("id", ""): _read_object(o, settings, from_openscad)
        for o in resources.findall("object")
    }
    build = root.find("build")
    items = [] if build is None else build.findall("item")
    if items:
        roots = [(i.get("objectid", ""), _matrix(i.get("transform"))) for i in items]
    else:
        referenced = {cid for o in objects.values() for cid, _ in o.components}
        roots = [(oid, np.eye(4)) for oid in objects if oid not in referenced]

    verts_out: list[np.ndarray] = []
    faces_out: list[np.ndarray] = []
    index: dict[RawGroup, int] = {}
    face_group: list[np.ndarray] = []
    offset = 0
    item_no = [0]

    def group_of(top: _Obj, leaf: _Obj, leaf_oid: str, pid: str | None, p1: int) -> int:
        color: str | None = None
        material = ""
        table = props.get(pid or "")
        if table is not None and 0 <= p1 < len(table.colors):
            color, material = table.colors[p1], table.names[p1]
            if from_openscad and p1 == 0 and material == OPENSCAD_DEFAULT_MATERIAL:
                color, material = None, ""
        meta = {**top.meta, **leaf.meta}
        group = RawGroup(
            leaf.name or top.name,
            colour_label(material, color),
            color,
            tuple(sorted(meta.items())),
            f"{item_no[0]}:{leaf_oid}",  # two objects of one name stay two parts
        )
        return index.setdefault(group, len(index))

    def emit(oid: str, matrix: np.ndarray, top: _Obj | None, depth: int) -> None:
        nonlocal offset
        if depth > MAX_COMPONENT_DEPTH:
            raise RenderError(
                "3d dimension: the 3MF component tree is nested too deeply (or cyclic)"
            )
        obj = objects.get(oid)
        if obj is None:
            raise RenderError(
                f"3d dimension: the 3MF references the missing object {oid!r}"
            )
        top = top or obj
        if obj.vertices is not None and len(obj.triangles):
            v = obj.vertices
            n = len(v)
            homogeneous = np.hstack([v, np.ones((n, 1))]) @ matrix
            tri = np.empty((len(obj.triangles), 3), dtype=np.int64)
            gid = np.empty(len(obj.triangles), dtype=np.int64)
            cache: dict[tuple[str | None, int], int] = {}
            for k, t in enumerate(obj.triangles):
                try:
                    tri[k] = (
                        int(t.get("v1", "")),
                        int(t.get("v2", "")),
                        int(t.get("v3", "")),
                    )
                    pid = t.get("pid", obj.pid)
                    p1 = int(t.get("p1", obj.pindex))
                except ValueError as exc:
                    raise RenderError(
                        f"3d dimension: object {oid} of the 3MF has a bad triangle"
                    ) from exc
                if (pid, p1) not in cache:
                    cache[pid, p1] = group_of(top, obj, oid, pid, p1)
                gid[k] = cache[pid, p1]
            if tri.min() < 0 or tri.max() >= n:
                raise RenderError(
                    f"3d dimension: object {oid} of the 3MF has a triangle outside its vertices"
                )
            verts_out.append(homogeneous[:, :3] * UNIT_MM[unit])
            faces_out.append(tri + offset)
            face_group.append(gid)
            offset += n
        for cid, cmatrix in obj.components:
            emit(cid, cmatrix @ matrix, top, depth + 1)

    for n, (oid, matrix) in enumerate(roots):
        item_no[0] = n
        emit(oid, matrix, None, 0)
    if not faces_out:
        raise RenderError(f"3d dimension: no triangles in {path}")
    return RawModel(
        np.vstack(verts_out),
        np.vstack(faces_out),
        np.concatenate(face_group),
        list(index),
    )


def _read_mtl(path: str) -> dict[str, str]:
    """material name -> '#RRGGBB' (from Kd) of one .mtl file."""
    out: dict[str, str] = {}
    current: str | None = None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            key, rest = parts[0].lower(), parts[1].strip()
            if key == "newmtl":
                current = rest
            elif key == "kd" and current is not None:
                try:
                    r, g, b = (float(x) for x in rest.split()[:3])
                except ValueError:
                    continue
                out[current] = to_hex(r, g, b)
    return out


def read_obj(path: str) -> RawModel:
    """An OBJ as raw geometry; colour groups are (o/g name, usemtl material) with the MTL's Kd."""
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    face_group: list[int] = []
    groups: dict[tuple[str, str], int] = {}
    materials: dict[str, str] = {}
    warnings: list[str] = []
    obj_name, material = "", ""
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            tokens = line.split()
            if not tokens or tokens[0].startswith("#"):
                continue
            key = tokens[0]
            arg = line.split(None, 1)[1].strip() if len(tokens) > 1 else ""
            try:
                if key == "v":
                    verts.append((float(tokens[1]), float(tokens[2]), float(tokens[3])))
                elif key == "f":
                    idx = []
                    for tok in tokens[1:]:
                        i = int(tok.split("/")[0])
                        idx.append(i - 1 if i > 0 else len(verts) + i)
                    gid = groups.setdefault((obj_name, material), len(groups))
                    for k in range(1, len(idx) - 1):  # fan-triangulate polygons
                        faces.append((idx[0], idx[k], idx[k + 1]))
                        face_group.append(gid)
                elif key in ("o", "g"):
                    obj_name = "" if arg.lower() == "default" else arg
                elif key == "usemtl":
                    material = arg
                elif key == "mtllib":
                    for name in arg.split():
                        mtl = os.path.join(base, name)
                        if os.path.isfile(mtl):
                            materials.update(_read_mtl(mtl))
                        else:
                            warnings.append(
                                f"{os.path.basename(path)}: material library {name} not found"
                            )
            except (IndexError, ValueError) as exc:
                raise RenderError(
                    f"3d dimension: {path}:{line_no}: cannot read {key!r} line"
                ) from exc
    if not faces:
        raise RenderError(f"3d dimension: no triangles in {path}")
    vertices = np.array(verts, dtype=float).reshape(-1, 3)
    face_arr = np.array(faces, dtype=np.int64)
    if face_arr.min() < 0 or face_arr.max() >= len(vertices):
        raise RenderError(f"3d dimension: {path} has a face outside its vertices")
    raw_groups: list[RawGroup] = []
    for name, mat in groups:
        color = materials.get(mat)
        raw_groups.append(RawGroup(name, colour_label(mat, color), color, (), name))
    return RawModel(
        vertices, face_arr, np.array(face_group, dtype=np.int64), raw_groups, warnings
    )


def _read_mesh_file(path: str) -> RawModel:
    """STL and every other format trimesh reads: one uncoloured group."""
    import trimesh

    try:
        mesh: Any = trimesh.load(path, force="mesh")
    except Exception as exc:  # noqa: BLE001 — trimesh raises a different class per format
        raise RenderError(
            f"3d dimension: cannot read {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if mesh.is_empty or len(mesh.faces) == 0:
        raise RenderError(f"3d dimension: no triangles in {path}")
    return RawModel(
        np.asarray(mesh.vertices, dtype=float),
        np.asarray(mesh.faces, dtype=np.int64),
        np.zeros(len(mesh.faces), dtype=np.int64),
        [RawGroup("", "", None)],
    )


def _scad_to_3mf(path: str, out: str, defines: list[str]) -> None:
    cmd = ["openscad", "-o", out]
    for define in defines:
        cmd += ["-D", define]
    try:
        done = subprocess.run([*cmd, path], capture_output=True, text=True)
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


def load_raw(path: str, defines: list[str] | None = None) -> RawModel:
    """Read `path` (3mf / obj / scad / anything trimesh reads) into colour-grouped raw geometry."""
    lower = path.lower()
    if defines and not lower.endswith(".scad"):
        raise RenderError("3d dimension: -D NAME=VALUE only applies to a .scad input")
    if lower.endswith(".scad"):
        fd, tmp = tempfile.mkstemp(suffix=".3mf")
        os.close(fd)
        try:
            _scad_to_3mf(path, tmp, defines or [])
            return read_3mf(tmp)
        finally:
            os.unlink(tmp)
    if lower.endswith(".3mf"):
        return read_3mf(path)
    if lower.endswith(".obj"):
        return read_obj(path)
    return _read_mesh_file(path)
