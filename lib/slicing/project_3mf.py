"""project_3mf.py — edit an Orca/Bambu project 3MF in memory: list, pick, orient, arrange, check.

WHAT: the backend of `3d 3mf`. A `Project` holds every zip member of a 3MF; the edits rewrite
  only what they must — build items and resource objects of `3D/3dmodel.model`, the objects,
  instances and plates of `Metadata/model_settings.config`, the object files no kept object
  references, and the per-object metadata files Orca keys by object index — and copy every
  other member byte for byte. So the project's presets (`project_settings.config`), filaments,
  mixed-filament definitions and per-object extruders survive a pick, an orient or an arrange.

WHY text edits, not an XML rewrite: Orca's 3MF reader matches the literal qualified names
  (`p:path`, `p:UUID`, `BambuStudio:…`); ElementTree would rename the prefixes. The model is
  parsed (ElementTree) to read it, and changed with targeted substitutions keyed by object id.

Object index: Orca creates a model object per build item, in build order (first appearance of
  an object id; `_BBS_3MF_Importer::_handle_end_item`), and keys
  `layer_heights_profile.txt`, `layer_config_ranges.xml`, `brim_ear_points.txt` and
  `cut_information.xml` by that 1-based index (`object.second + 1`). `pick` renumbers them.

Geometry: an object's triangles come from its mesh or its components (in this or another
  model part, `p:path`), each with its transform; the build item's transform puts it on the bed.
  Transforms are 3MF 4x3 row-major (p' = p·M); held here as 4x4 column-vector matrices.
"""

from __future__ import annotations

import fnmatch
import io
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

MODEL = "3D/3dmodel.model"
MODEL_RELS = "3D/_rels/3dmodel.model.rels"
SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
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

# Orca's per-object metadata files, keyed by the 1-based object index.
LINE_INDEXED = ("Metadata/layer_heights_profile.txt", "Metadata/brim_ear_points.txt")
XML_INDEXED = ("Metadata/layer_config_ranges.xml", "Metadata/cut_information.xml")
# Per-object data in the object's own Z / XY frame: a rotation would move it off its geometry.
ORIENT_BLOCKERS = {
    "Metadata/layer_heights_profile.txt": "a variable layer height profile",
    "Metadata/layer_config_ranges.xml": "height range modifiers",
    "Metadata/brim_ear_points.txt": "brim ears",
}
# Orca plate grid: stride = bed * (1 + 1/5) (PartPlate LOGICAL_PART_PLATE_GAP).
_PLATE_GAP_FRACTION = 1.0 / 5.0
_PLATE_FILE_RE = re.compile(
    r"^Metadata/(?:plate|plate_no_light|top|pick)_(\d+)(?:_small)?\.[\w.]+$"
)

ON_BED_TOL_MM = 0.05
FIT_TOL_MM = 0.01
OVERHANG_DEG = 45.0  # from vertical, as `3d printability` (fdm-printability skill)
OVERHANG_WARN_MM2 = 100.0  # the area `3d gcode-overhangs` FAILs when printed on nothing

FACES = {
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
}
FACE_CHOICES = (*FACES, "largest")


class ProjectError(ValueError):
    """The 3MF cannot be read or the edit cannot be made (the message says why)."""


class SelectorError(ProjectError):
    """A selector matched no object; carries the names that exist."""

    def __init__(self, selector: str, names: Sequence[str]) -> None:
        super().__init__(f"no object matches {selector!r}")
        self.selector = selector
        self.names = list(names)


@dataclass
class Item:
    """One build item: an object instance placed on the bed."""

    pos: int  # position among the build items (0-based)
    object_id: str
    name: str
    index: int  # Orca's 1-based object index (build order of first appearance)
    transform: np.ndarray  # 4x4, column vectors
    plate: (
        int | None
    )  # 1-based plate from model_settings, None when the 3MF has no plates
    extruder: str | None


@dataclass
class Mesh:
    vertices: np.ndarray  # (n, 3) mm, bed coordinates
    faces: np.ndarray  # (m, 3) int

    @property
    def lo(self) -> np.ndarray:
        return np.asarray(self.vertices.min(axis=0))

    @property
    def hi(self) -> np.ndarray:
        return np.asarray(self.vertices.max(axis=0))


@dataclass
class Bed:
    width: float
    depth: float
    height: float | None
    source: str


@dataclass
class CheckLine:
    gate: str
    status: str  # PASS / WARN / FAIL / SKIP
    detail: str
    object: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "object": self.object,
            "detail": self.detail,
        }


@dataclass
class CheckReport:
    lines: list[CheckLine] = field(default_factory=list)

    @property
    def overall(self) -> str:
        statuses = {ln.status for ln in self.lines}
        return (
            "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
        )

    def render(self) -> str:
        rows = [
            f"{ln.status:<4}  {ln.gate:<10} {(ln.object + ': ') if ln.object else ''}{ln.detail}"
            for ln in self.lines
        ]
        return "\n".join([*rows, f"3mf check: {self.overall}"])

    def to_dict(self) -> dict[str, Any]:
        return {"overall": self.overall, "checks": [ln.to_dict() for ln in self.lines]}


# ---------------------------------------------------------------------------- matrices
def parse_transform(attr: str | None) -> np.ndarray:
    m = np.eye(4)
    if attr:
        try:
            v = [float(t) for t in attr.split()]
        except ValueError as exc:
            raise ProjectError(f"3MF transform {attr!r} is not numeric") from exc
        if len(v) != 12:
            raise ProjectError(f"3MF transform {attr!r} must have 12 numbers")
        m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
        m[:3, 3] = v[9:]
    return m


def format_transform(m: np.ndarray) -> str:
    vals = [*np.asarray(m[:3, :3]).T.reshape(-1), *np.asarray(m[:3, 3])]
    return " ".join(_num(v) for v in vals)


def _num(v: float) -> str:
    v = 0.0 if abs(v) < 1e-12 else float(v)
    return f"{v:.9g}"


def translation(dx: float, dy: float, dz: float) -> np.ndarray:
    t = np.eye(4)
    t[:3, 3] = (dx, dy, dz)
    return t


def rotation_xyz(rx: float, ry: float, rz: float) -> np.ndarray:
    """Rotation by RX about X, then RY about Y, then RZ about Z (degrees, bed axes)."""
    ax, ay, az = (math.radians(a) for a in (rx, ry, rz))
    x = np.array(
        [[1, 0, 0], [0, math.cos(ax), -math.sin(ax)], [0, math.sin(ax), math.cos(ax)]]
    )
    y = np.array(
        [[math.cos(ay), 0, math.sin(ay)], [0, 1, 0], [-math.sin(ay), 0, math.cos(ay)]]
    )
    z = np.array(
        [[math.cos(az), -math.sin(az), 0], [math.sin(az), math.cos(az), 0], [0, 0, 1]]
    )
    r = np.eye(4)
    r[:3, :3] = z @ y @ x
    return r


def rotation_down(direction: Sequence[float]) -> np.ndarray:
    """The rotation that turns the outward DIRECTION to -Z (that face onto the bed)."""
    d = np.asarray(direction, float)
    d = d / np.linalg.norm(d)
    down = np.array([0.0, 0.0, -1.0])
    r = np.eye(4)
    c = float(np.dot(d, down))
    if c > 1 - 1e-9:
        return r
    if c < -1 + 1e-9:  # facing up: half a turn about X
        r[:3, :3] = np.diag([1.0, -1.0, -1.0])
        return r
    axis = np.cross(d, down)
    s = float(np.linalg.norm(axis))
    k = axis / s
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    r[:3, :3] = np.eye(3) + s * kx + (1 - c) * (kx @ kx)
    return r


def largest_flat_face(mesh: Mesh) -> tuple[float, float, float]:
    """Outward normal of the largest flat face of the convex hull: the face the part rests on
    most stably (coplanar hull triangles are summed)."""
    from scipy.spatial import ConvexHull, QhullError

    try:
        hull = ConvexHull(mesh.vertices)
    except (QhullError, ValueError) as exc:
        raise ProjectError(
            f"cannot find a flat face: the part is degenerate ({exc})"
        ) from exc
    tri = mesh.vertices[hull.simplices]
    areas = 0.5 * np.linalg.norm(
        np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1
    )
    groups: dict[tuple[int, ...], float] = {}
    normals: dict[tuple[int, ...], np.ndarray] = {}
    for eq, a in zip(hull.equations, areas, strict=True):
        key = tuple(int(round(x * 1000)) for x in (*eq[:3], eq[3] / 10))
        groups[key] = groups.get(key, 0.0) + float(a)
        normals.setdefault(key, eq[:3])
    best = max(groups, key=lambda k: (groups[k], k))
    n = normals[best]
    return (float(n[0]), float(n[1]), float(n[2]))


# ---------------------------------------------------------------------------- selectors
def matches(name: str, object_id: str, selector: str) -> bool:
    """A selector matches an object by its id, its whole name, or the first word of its name,
    with shell-glob wildcards (`SW*`, `wall_?`); case-sensitive."""
    if selector == object_id:
        return True
    words = name.split()
    return fnmatch.fnmatchcase(name, selector) or (
        bool(words) and fnmatch.fnmatchcase(words[0], selector)
    )


def split_selectors(spec: Iterable[str]) -> list[str]:
    out = [s.strip() for chunk in spec for s in chunk.split(",")]
    return [s for s in out if s]


# ---------------------------------------------------------------------------- the project
# A build item with its line's indent and newline, so a removed item leaves no blank line.
_ITEM_RE = re.compile(r"[ \t]*<item\b[^>]*?(?:/>|>.*?</item>)[ \t]*\n?", re.S)
_COMPONENT_RE = re.compile(r"<component\b[^>]*>")
_OBJ_ID_ATTR = re.compile(r'\bobjectid="([^"]*)"')
_TRANSFORM_ATTR = re.compile(r'\btransform="[^"]*"')
_SETTINGS_OBJECT_RE = re.compile(r'[ \t]*<object id="([^"]*)">.*?</object>\n?', re.S)
_INSTANCE_RE = re.compile(r"[ \t]*<model_instance>.*?</model_instance>\n?", re.S)
_INSTANCE_OBJ = re.compile(r'key="object_id"\s+value="([^"]*)"')
_PLATE_RE = re.compile(r"[ \t]*<plate>.*?</plate>\n?", re.S)
_PLATER_ID = re.compile(r'key="plater_id"\s+value="(\d+)"')
_VERTEX_RE = re.compile(r'<vertex\s+x="([^"]*)"\s+y="([^"]*)"\s+z="([^"]*)"')
_TRIANGLE_RE = re.compile(r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"')


def _object_block_re(oid: str) -> re.Pattern[str]:
    return re.compile(
        rf'[ \t]*<object\b[^>]*\bid="{re.escape(oid)}"[^>]*?(?:/>|>.*?</object>)\n?',
        re.S,
    )


class Project:
    """A 3MF held in memory; edits change the members they touch, `to_bytes` writes it back."""

    def __init__(self, data: bytes, *, label: str = "3MF") -> None:
        self.label = label
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            self._infos = zf.infolist()
            self.files: dict[str, bytes] = {i.filename: zf.read(i) for i in self._infos}
        except (zipfile.BadZipFile, OSError, KeyError) as exc:
            raise ProjectError(f"{label}: not a 3MF (zip) file ({exc})") from exc
        if MODEL not in self.files:
            raise ProjectError(f"{label}: no {MODEL} inside; not a 3MF")
        self._texts: dict[str, str] = {}
        self._roots: dict[str, ET.Element] = {}
        self._mesh_cache: dict[tuple[str, str], Mesh] = {}

    # ---- members
    def text(self, part: str) -> str:
        part = part.lstrip("/")
        if part not in self._texts:
            if part not in self.files:
                raise ProjectError(f"{self.label}: missing model part {part}")
            self._texts[part] = self.files[part].decode("utf-8")
        return self._texts[part]

    def set_text(self, part: str, text: str) -> None:
        part = part.lstrip("/")
        self._texts[part] = text
        self.files[part] = text.encode("utf-8")
        self._roots.pop(part, None)

    def remove(self, part: str) -> None:
        part = part.lstrip("/")
        self.files.pop(part, None)
        self._texts.pop(part, None)
        self._roots.pop(part, None)

    def root(self, part: str) -> ET.Element:
        part = part.lstrip("/")
        if part not in self._roots:
            try:
                self._roots[part] = ET.fromstring(self.text(part))
            except ET.ParseError as exc:
                raise ProjectError(
                    f"{self.label}: {part} is not valid XML ({exc})"
                ) from exc
        return self._roots[part]

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            written = set()
            for info in self._infos:
                if info.filename in self.files:
                    out.writestr(_info_like(info), self.files[info.filename])
                    written.add(info.filename)
            for name, data in self.files.items():
                if name not in written:
                    out.writestr(name, data)
        return buf.getvalue()

    # ---- reading
    def project_settings(self) -> dict[str, Any]:
        raw = self.files.get(PROJECT_SETTINGS)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def printer_model(self) -> str | None:
        v = self.project_settings().get("printer_model")
        return v if isinstance(v, str) and v else None

    def bed(self) -> Bed | None:
        """The printable area of the project's printer preset (project_settings.config)."""
        s = self.project_settings()
        area = s.get("printable_area")
        if not isinstance(area, list) or not area:
            return None
        try:
            pts = [tuple(float(c) for c in str(p).split("x")) for p in area]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        except (ValueError, IndexError):
            return None
        height: float | None
        try:
            height = float(s["printable_height"])
        except (KeyError, TypeError, ValueError):
            height = None
        return Bed(
            max(xs) - min(xs),
            max(ys) - min(ys),
            height,
            f"{PROJECT_SETTINGS} printable_area",
        )

    def _settings_text(self) -> str | None:
        return self.text(SETTINGS) if SETTINGS in self.files else None

    def _settings_objects(self) -> dict[str, dict[str, str]]:
        """object id -> {name, extruder} from model_settings.config."""
        text = self._settings_text()
        if text is None:
            return {}
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ProjectError(
                f"{self.label}: {SETTINGS} is not valid XML ({exc})"
            ) from exc
        out: dict[str, dict[str, str]] = {}
        for obj in root.findall("object"):
            meta = {
                m.get("key", ""): m.get("value", "") for m in obj.findall("metadata")
            }
            out[obj.get("id", "")] = meta
        return out

    def plates(self) -> dict[str, int]:
        """object id -> 1-based plate number, from model_settings.config `<plate>` blocks."""
        text = self._settings_text()
        if text is None:
            return {}
        out: dict[str, int] = {}
        for k, block in enumerate(_PLATE_RE.findall(text), 1):
            m = _PLATER_ID.search(block)
            plate = int(m.group(1)) if m else k
            for inst in _INSTANCE_RE.findall(block):
                oid = _INSTANCE_OBJ.search(inst)
                if oid:
                    out.setdefault(oid.group(1), plate)
        return out

    def plate_count(self) -> int:
        text = self._settings_text()
        return len(_PLATE_RE.findall(text)) if text is not None else 0

    def items(self) -> list[Item]:
        root = self.root(MODEL)
        build = root.find(f"{{{CORE}}}build")
        if build is None:
            raise ProjectError(f"{self.label}: {MODEL} has no <build>")
        settings = self._settings_objects()
        plates = self.plates()
        names_attr = {
            o.get("id", ""): o.get("name") for o in root.iter(f"{{{CORE}}}object")
        }
        items: list[Item] = []
        index: dict[str, int] = {}
        for pos, it in enumerate(build.findall(f"{{{CORE}}}item")):
            oid = it.get("objectid", "")
            index.setdefault(oid, len(index) + 1)
            meta = settings.get(oid, {})
            name = meta.get("name") or names_attr.get(oid) or f"object{oid}"
            items.append(
                Item(
                    pos,
                    oid,
                    name,
                    index[oid],
                    parse_transform(it.get("transform")),
                    plates.get(oid),
                    meta.get("extruder"),
                )
            )
        return items

    def object_index(self) -> dict[str, int]:
        return {it.object_id: it.index for it in self.items()}

    # ---- geometry
    def mesh(self, item: Item) -> Mesh:
        local = self._object_mesh(MODEL, item.object_id, depth=0)
        v = local.vertices @ item.transform[:3, :3].T + item.transform[:3, 3]
        return Mesh(v * self._scale(MODEL), local.faces)

    def _scale(self, part: str) -> float:
        return UNITS.get(self.root(part).get("unit", "millimeter"), 1.0)

    def _object_mesh(self, part: str, oid: str, *, depth: int) -> Mesh:
        """An object's triangles in its own frame (root model units), components included."""
        key = (part.lstrip("/"), oid)
        if key in self._mesh_cache:
            return self._mesh_cache[key]
        if depth > 16:
            raise ProjectError(f"{self.label}: component nesting too deep (a cycle?)")
        block_m = _object_block_re(oid).search(self.text(part))
        if block_m is None:
            raise ProjectError(
                f"{self.label}: object id {oid} not found in {part.lstrip('/')}"
            )
        block = block_m.group(0)
        verts: list[np.ndarray] = []
        faces: list[np.ndarray] = []
        offset = 0
        if "<mesh" in block:
            v, f = _parse_mesh(block, f"{part}#{oid}")
            ratio = self._scale(part) / self._scale(MODEL)
            verts.append(v * ratio)
            faces.append(f)
            offset = len(v)
        for sub, cid, transform in _components(block):
            cm = self._object_mesh(sub or part, cid, depth=depth + 1)
            tf = parse_transform(transform)
            verts.append(cm.vertices @ tf[:3, :3].T + tf[:3, 3])
            faces.append(cm.faces + offset)
            offset += len(cm.vertices)
        if not verts:
            raise ProjectError(f"{self.label}: object {oid} has no triangles")
        mesh = Mesh(np.vstack(verts), np.vstack(faces).astype(np.int64))
        self._mesh_cache[key] = mesh
        return mesh

    # ---- editing: pick
    def pick(self, selectors: Sequence[str]) -> list[Item]:
        """Keep only the objects a selector matches; returns the kept items."""
        items = self.items()
        keep_ids: set[str] = set()
        for sel in selectors:
            hit = {it.object_id for it in items if matches(it.name, it.object_id, sel)}
            if not hit:
                raise SelectorError(sel, sorted({it.name for it in items}))
            keep_ids |= hit
        drop_ids = {it.object_id for it in items} - keep_ids
        if drop_ids:
            self._drop_objects(items, drop_ids)
        return self.items()

    def _drop_objects(self, items: list[Item], drop_ids: set[str]) -> None:
        old_index = {it.object_id: it.index for it in items}
        kept_order = [
            oid
            for oid in dict.fromkeys(it.object_id for it in items)
            if oid not in drop_ids
        ]
        renumber = {old_index[oid]: k for k, oid in enumerate(kept_order, 1)}

        model = self.text(MODEL)
        model = _ITEM_RE.sub(
            lambda m: "" if _item_oid(m.group(0)) in drop_ids else m.group(0), model
        )
        self.set_text(MODEL, model)
        self._remove_unreferenced(kept_order)

        settings = self._settings_text()
        if settings is not None:
            settings = _SETTINGS_OBJECT_RE.sub(
                lambda m: "" if m.group(1) in drop_ids else m.group(0), settings
            )
            settings = _INSTANCE_RE.sub(
                lambda m: "" if _instance_oid(m.group(0)) in drop_ids else m.group(0),
                settings,
            )
            self.set_text(SETTINGS, settings)
        self._renumber_indexed(renumber)

    def _remove_unreferenced(self, kept_ids: list[str]) -> None:
        """Drop root-model objects and object files that no kept build item reaches."""
        reach: set[tuple[str, str]] = set()
        stack = [(MODEL, oid) for oid in kept_ids]
        while stack:
            part, oid = stack.pop()
            if (part, oid) in reach:
                continue
            reach.add((part, oid))
            block = _object_block_re(oid).search(self.text(part))
            if block is None:
                continue
            for sub, cid, _ in _components(block.group(0)):
                stack.append((sub or part, cid))
        root_ids = [o.get("id", "") for o in self.root(MODEL).iter(f"{{{CORE}}}object")]
        model = self.text(MODEL)
        for oid in root_ids:
            if (MODEL, oid) not in reach:
                model = _object_block_re(oid).sub("", model, count=1)
        self.set_text(MODEL, model)
        used_parts = {p for p, _ in reach}
        rels = self.files.get(MODEL_RELS)
        rels_text = rels.decode("utf-8") if rels is not None else None
        for name in list(self.files):
            if (
                name.startswith("3D/")
                and name.endswith(".model")
                and name != MODEL
                and name not in used_parts
            ):
                self.remove(name)
                if rels_text is not None:
                    rels_text = re.sub(
                        rf'[ \t]*<Relationship\b[^>]*Target="/?{re.escape(name)}"[^>]*/>\n?',
                        "",
                        rels_text,
                    )
        if rels_text is not None:
            self.set_text(MODEL_RELS, rels_text)

    def _renumber_indexed(self, renumber: dict[int, int]) -> None:
        for name in LINE_INDEXED:
            if name in self.files:
                out = []
                for line in self.text(name).splitlines(keepends=True):
                    m = re.match(r"object_id=(\d+)\|", line)
                    if m is None:
                        out.append(line)
                    elif int(m.group(1)) in renumber:
                        out.append(
                            f"object_id={renumber[int(m.group(1))]}|{line[m.end() :]}"
                        )
                self.set_text(name, "".join(out))
        for name in XML_INDEXED:
            if name in self.files:

                def sub(m: re.Match[str]) -> str:
                    old = int(m.group(2))
                    return (
                        ""
                        if old not in renumber
                        else f'{m.group(1)}"{renumber[old]}"{m.group(3)}'
                    )

                self.set_text(
                    name,
                    re.sub(
                        r'([ \t]*<object\s+id=)"(\d+)"(.*?</object>\n?)',
                        sub,
                        self.text(name),
                        flags=re.S,
                    ),
                )

    # ---- editing: transforms
    def set_transform(self, item: Item, m: np.ndarray) -> None:
        """Rewrite the transform of build item ITEM (by position)."""
        model = self.text(MODEL)
        found = list(_ITEM_RE.finditer(model))
        if item.pos >= len(found):
            raise ProjectError(f"{self.label}: build item {item.pos} is gone")
        span = found[item.pos]
        tag = span.group(0)
        attr = f'transform="{format_transform(m)}"'
        new = (
            _TRANSFORM_ATTR.sub(attr, tag, count=1)
            if _TRANSFORM_ATTR.search(tag)
            else tag.replace("<item", f"<item {attr}", 1)
        )
        self.set_text(MODEL, model[: span.start()] + new + model[span.end() :])
        item.transform = m

    def orient_blockers(self, items: Sequence[Item]) -> list[str]:
        """Why some of ITEMS cannot be rotated: per-object data in the object's own frame."""
        idx = {it.index: it.name for it in items}
        out = []
        for name, what in ORIENT_BLOCKERS.items():
            if name not in self.files:
                continue
            text = self.text(name)
            ids = {int(x) for x in re.findall(r"object_id=(\d+)\|", text)} | {
                int(x) for x in re.findall(r'<object\s+id="(\d+)"', text)
            }
            out += [f"{idx[i]} has {what} ({name})" for i in sorted(ids & idx.keys())]
        return out

    def orient(self, item: Item, rot: np.ndarray) -> Mesh:
        """Turn ITEM by ROT about its bed-space bbox centre, then drop it onto the bed."""
        mesh = self.mesh(item)
        c = (mesh.lo + mesh.hi) / 2 / self._scale(MODEL)
        m = translation(*c) @ rot @ translation(*(-c)) @ item.transform
        self.set_transform(item, m)
        turned = self.mesh(item)
        m = translation(0, 0, -float(turned.lo[2]) / self._scale(MODEL)) @ m
        self.set_transform(item, m)
        return self.mesh(item)

    # ---- editing: arrange
    def collapse_plates(self) -> None:
        """Every instance onto the first plate; the other plates and their files go."""
        text = self._settings_text()
        if text is None:
            return
        blocks = list(_PLATE_RE.finditer(text))
        if len(blocks) <= 1:
            return
        instances = [inst for b in blocks for inst in _INSTANCE_RE.findall(b.group(0))]
        first = _INSTANCE_RE.sub("", blocks[0].group(0))
        close = first.rfind("</plate>")
        indent = re.match(r"[ \t]*", first)
        first_new = (
            first[:close]
            + "".join(instances)
            + (indent.group(0) if indent else "")
            + first[close:].lstrip(" \t")
        )
        out = text[: blocks[0].start()] + first_new
        prev = blocks[0].end()
        for b in blocks[1:]:
            out += text[prev : b.start()]
            prev = b.end()
        out += text[prev:]
        self.set_text(SETTINGS, out)
        for name in list(self.files):
            m = _PLATE_FILE_RE.match(name)
            if m and int(m.group(1)) > 1:
                self.remove(name)

    def plate_origin(self, plate: int | None, bed: Bed) -> tuple[float, float]:
        """Bed-space origin of PLATE in Orca's plate grid (plate 1 at 0,0)."""
        if not plate or plate <= 1:
            return (0.0, 0.0)
        from orca_project_3mf import plate_columns

        cols = plate_columns(max(self.plate_count(), plate))
        row, col = divmod(plate - 1, cols)
        return (
            col * bed.width * (1 + _PLATE_GAP_FRACTION),
            -row * bed.depth * (1 + _PLATE_GAP_FRACTION),
        )


def _instance_oid(block: str) -> str:
    m = _INSTANCE_OBJ.search(block)
    return m.group(1) if m else ""


def _components(block: str) -> list[tuple[str | None, str, str | None]]:
    """(model part or None for this part, object id, transform) of each <component>."""
    out = []
    for tag in _COMPONENT_RE.findall(block):
        path = re.search(r'\bp:path="([^"]*)"', tag)
        cid = re.search(r'\bobjectid="([^"]*)"', tag)
        tf = re.search(r'\btransform="([^"]*)"', tag)
        if cid:
            out.append(
                (
                    path.group(1).lstrip("/") if path else None,
                    cid.group(1),
                    tf.group(1) if tf else None,
                )
            )
    return out


def _item_oid(tag: str) -> str:
    m = _OBJ_ID_ATTR.search(tag)
    return m.group(1) if m else ""


def _info_like(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    z = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    z.compress_type = zipfile.ZIP_DEFLATED
    z.external_attr = info.external_attr
    return z


def _parse_mesh(block: str, where: str) -> tuple[np.ndarray, np.ndarray]:
    vs = _VERTEX_RE.findall(block)
    ts = _TRIANGLE_RE.findall(block)
    if len(vs) != block.count("<vertex") or len(ts) != block.count("<triangle"):
        vs, ts = _parse_mesh_xml(block, where)
    try:
        v = np.array(vs, dtype=float).reshape(-1, 3)
        f = np.array(ts, dtype=np.int64).reshape(-1, 3)
    except ValueError as exc:
        raise ProjectError(
            f"{where}: vertex or triangle values are not numeric"
        ) from exc
    if len(f) and (f.min() < 0 or f.max() >= len(v)):
        raise ProjectError(f"{where}: a triangle names a vertex that does not exist")
    return v, f


def _parse_mesh_xml(
    block: str, where: str
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    m = re.search(r"<mesh\b.*?</mesh>", block, re.S)
    if m is None:
        raise ProjectError(f"{where}: <mesh> is not closed")
    try:
        # element and attribute prefixes (slic3rpe:, p:) are undeclared in the cut-out block
        plain = re.sub(
            r"(\s)[\w.-]+:([\w.-]+=)",
            r"\1\2",
            re.sub(r"<(/?)[\w.-]+:", r"<\1", m.group(0)),
        )
        el = ET.fromstring(plain)
    except ET.ParseError as exc:
        raise ProjectError(f"{where}: <mesh> is not valid XML ({exc})") from exc
    vs = [(v.get("x", ""), v.get("y", ""), v.get("z", "")) for v in el.iter("vertex")]
    ts = [
        (t.get("v1", ""), t.get("v2", ""), t.get("v3", "")) for t in el.iter("triangle")
    ]
    return vs, ts


# ---------------------------------------------------------------------------- arrange
@dataclass
class Placed:
    name: str
    x: float  # lower-left corner of the footprint on the bed
    y: float


def arrange(project: Project, bed: Bed, *, gap: float, margin: float) -> list[Placed]:
    """Pack every build item onto plate 1 of BED (shelf packer of `3d arrange`), centred.
    Raises ProjectError naming the parts that do not fit on one plate."""
    from arrange_pack import OversizePart, PartBox, centering_offset, shelf_pack

    items = project.items()
    meshes = [project.mesh(it) for it in items]
    usable_w, usable_d = bed.width - 2 * margin, bed.depth - 2 * margin
    usable = min(usable_w, usable_d)
    boxes = [
        PartBox(it.name, k, float(m.hi[0] - m.lo[0]), float(m.hi[1] - m.lo[1]))
        for k, (it, m) in enumerate(zip(items, meshes, strict=True))
    ]
    try:
        plates = shelf_pack(boxes, usable=usable, gap=gap)
    except OversizePart as exc:
        raise ProjectError(
            f"{exc} on a {bed.width:g} x {bed.depth:g} mm bed ({bed.source})"
        ) from exc
    if len(plates) > 1:
        over = [pl.name for p in plates[1:] for pl in p.placements]
        raise ProjectError(
            f"{len(items)} objects need {len(plates)} plates of {bed.width:g} x {bed.depth:g} mm "
            f"(margin {margin:g}, gap {gap:g}); these do not fit on one: {', '.join(over)}"
        )
    if not plates:
        raise ProjectError("no objects to arrange")
    project.collapse_plates()
    plate = plates[0]
    ox, oy = centering_offset(plate, bed=usable)
    scale = project._scale(MODEL)
    placed = []
    for pl in plate.placements:
        it, m = items[pl.mesh_index], meshes[pl.mesh_index]
        x = margin + (usable_w - usable) / 2 + ox + pl.x
        y = margin + (usable_d - usable) / 2 + oy + pl.y
        dx, dy, dz = x - float(m.lo[0]), y - float(m.lo[1]), -float(m.lo[2])
        project.set_transform(
            it, translation(dx / scale, dy / scale, dz / scale) @ it.transform
        )
        placed.append(Placed(it.name, x, y))
    return placed


# ---------------------------------------------------------------------------- check
def overhang_area(mesh: Mesh) -> float:
    """mm² of downward faces steeper than OVERHANG_DEG from vertical, not resting on the bed."""
    tri = mesh.vertices[mesh.faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area2 = np.linalg.norm(cross, axis=1)
    ok = area2 > 1e-12
    nz = np.zeros(len(tri))
    nz[ok] = cross[ok, 2] / area2[ok]
    limit = -math.cos(math.radians(90.0 - OVERHANG_DEG))
    on_bed = tri[:, :, 2].max(axis=1) <= float(mesh.lo[2]) + ON_BED_TOL_MM
    return float(0.5 * area2[(nz < limit) & ~on_bed].sum())


def check(
    project: Project, bed: Bed | None, *, printer_model: str | None
) -> CheckReport:
    report = CheckReport()
    own = project.printer_model()
    if printer_model is None:
        report.lines.append(
            CheckLine(
                "printer",
                "SKIP",
                f"no --printer; the project is set up for {own or 'no printer'}",
            )
        )
    elif own is None:
        report.lines.append(
            CheckLine(
                "printer",
                "SKIP",
                f"no printer_model in the 3MF (not a slicer project); target {printer_model}",
            )
        )
    elif own == printer_model:
        report.lines.append(
            CheckLine("printer", "PASS", f"the project is set up for {own}")
        )
    else:
        report.lines.append(
            CheckLine(
                "printer",
                "FAIL",
                f"the project is set up for {own}, the target is {printer_model}: its presets slice for the wrong "
                "machine; switch the printer in the slicer and save the project",
            )
        )
    items = project.items()
    if not items:
        report.lines.append(CheckLine("objects", "FAIL", "the build has no objects"))
        return report
    plates = sorted({it.plate for it in items if it.plate})
    if len(plates) > 1:
        report.lines.append(
            CheckLine(
                "plates",
                "WARN",
                f"objects on plates {', '.join(map(str, plates))}: "
                "`3d slice` slices each plate; `3d 3mf arrange` puts them on one",
            )
        )
    for it in items:
        mesh = project.mesh(it)
        lo, hi = mesh.lo, mesh.hi
        if bed is None:
            report.lines.append(
                CheckLine(
                    "bed fit", "SKIP", "no bed size: pass --printer NAME", it.name
                )
            )
        else:
            ox, oy = project.plate_origin(it.plate, bed)
            x0, y0, x1, y1 = lo[0] - ox, lo[1] - oy, hi[0] - ox, hi[1] - oy
            box = f"X {x0:.1f}..{x1:.1f}, Y {y0:.1f}..{y1:.1f} on {bed.width:g} x {bed.depth:g} mm"
            inside = (
                x0 >= -FIT_TOL_MM
                and y0 >= -FIT_TOL_MM
                and x1 <= bed.width + FIT_TOL_MM
                and y1 <= bed.depth + FIT_TOL_MM
            )
            report.lines.append(
                CheckLine(
                    "bed fit",
                    "PASS" if inside else "FAIL",
                    box if inside else f"off the bed: {box}",
                    it.name,
                )
            )
            if bed.height is not None:
                tall = hi[2] - lo[2] > bed.height + FIT_TOL_MM
                report.lines.append(
                    CheckLine(
                        "height",
                        "FAIL" if tall else "PASS",
                        f"{hi[2] - lo[2]:.1f} mm of {bed.height:g} mm",
                        it.name,
                    )
                )
        z = float(lo[2])
        if abs(z) <= ON_BED_TOL_MM:
            report.lines.append(
                CheckLine(
                    "on bed", "PASS", f"lowest point z={round(z, 2) + 0.0:.2f}", it.name
                )
            )
        elif z < 0:
            report.lines.append(
                CheckLine(
                    "on bed",
                    "FAIL",
                    f"{-z:.2f} mm below the bed: the slicer cuts it off; "
                    "`3d 3mf orient` drops parts onto the bed",
                    it.name,
                )
            )
        else:
            report.lines.append(
                CheckLine("on bed", "WARN", f"floats {z:.2f} mm above the bed", it.name)
            )
        over = overhang_area(mesh)
        report.lines.append(
            CheckLine(
                "overhang",
                "WARN" if over > OVERHANG_WARN_MM2 else "PASS",
                f"{over:.0f} mm² steeper than {OVERHANG_DEG:g}° from vertical"
                + (
                    " (needs supports, or turn another face down)"
                    if over > OVERHANG_WARN_MM2
                    else ""
                ),
                it.name,
            )
        )
    return report
