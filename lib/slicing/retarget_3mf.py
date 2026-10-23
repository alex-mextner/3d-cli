"""retarget_3mf.py — turn an Orca-family project .3mf for printer A into one for printer B.

WHAT: reads a slicer project 3MF (Orca / Snapmaker Orca / Bambu: `Metadata/project_settings.config`
  JSON, `Metadata/model_settings.config`, `3D/3dmodel.model` + `3D/Objects/*.model`) and writes a
  copy that opens, slices and prints on another machine:
    - the printer keys (bed, height, nozzle, start/end G-code, limits, ...) come from a machine
      preset; optional process / filament presets and `--set key=value` overrides apply after;
    - multi-extruder / multi-filament assignments collapse to one when the target has one extruder;
    - only the chosen objects are kept (`keep`), and they are moved (and rotated about Z if
      needed) so the footprint sits centred on the new bed with a margin — or it fails, loudly;
    - the result is re-read and checked (bounds, JSON, XML, required keys) before it is called OK.

WHY: re-slicing a part from scratch because its project was made for another printer wastes the
  orientation, supports and tuned settings already in the file. This keeps all of them and
  changes only what the target printer forces.

Everything here is stdlib-only and works on bytes/dicts; `commands/retarget.py` is the CLI shell.

Examples:
  parts, report = retarget(src, machine=PresetSource(name, flat_settings), keep="1", margin=5.0)
  write_package(dst, parts)
"""

from __future__ import annotations

import difflib
import functools
import io
import json
import math
import re
import struct
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from errors import GateFailure, InputNotFound, InvalidArgument

from slicing.bed_fit import Bed, Placement, bounds, closest_fit, convex_hull, fit_to_bed

SETTINGS = "Metadata/project_settings.config"
MODEL_SETTINGS = "Metadata/model_settings.config"
MAIN_MODEL = "3D/3dmodel.model"
MODEL_RELS = "3D/_rels/3dmodel.model.rels"
ROOT_RELS = "_rels/.rels"
LAYER_PROFILE = "Metadata/layer_heights_profile.txt"

_NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_TAG_VERTEX = f"{{{_NS_CORE}}}vertex"
_TAG_TRIANGLE = f"{{{_NS_CORE}}}triangle"
_TAG_OBJECT = f"{{{_NS_CORE}}}object"
_PRESET_IDENTITY_KEYS = ("name", "from", "version", "print_compatible_printers")
_HOST_REACHING_KEYS: dict[str, Any] = {
    "post_process": [],
    "output_filename_format": "{input_filename_base}.gcode",
}

# G-code templates belong to the printer: a macro the target does not define aborts the print.
GCODE_KEYS = frozenset(
    {
        "machine_start_gcode",
        "machine_end_gcode",
        "before_layer_change_gcode",
        "layer_change_gcode",
        "change_filament_gcode",
        "machine_pause_gcode",
        "template_custom_gcode",
        "time_lapse_gcode",
        "printing_by_object_gcode",
        "change_extrusion_role_gcode",
    }
)
# Project-only per-extruder lists (newer multi-nozzle builds) that no system preset spells out.
_EXTRA_PER_EXTRUDER = frozenset(
    {"extruder_type", "printer_extruder_id", "extruder_variant_list"}
)
# Lists whose length is a point count / free-form, never "one entry per extruder".
_POINT_LIST_KEYS = frozenset(
    {"printable_area", "bed_exclude_area", "head_wrap_detect_zone"}
)
# Machine lists whose length is a mode count (machine_max_*: normal/silent) or a size list
# (thumbnails), never the extruder count, even when the two numbers coincide.
_FIXED_LENGTH_PREFIXES = ("machine_max_", "machine_min_", "thumbnails")
# Process keys holding a 1-based filament slot (0 = "current").
_SLOT_KEY_SUFFIX = "_filament"
_REQUIRED_KEYS = (
    "printable_area",
    "printable_height",
    "nozzle_diameter",
    "printer_settings_id",
    "print_settings_id",
    "filament_settings_id",
    "machine_start_gcode",
    "machine_end_gcode",
    "layer_height",
)
_PREVIEW_PREFIXES = (
    "plate_|top_|pick_"  # plate_ also covers plate_no_light_N and plate_N_small
)
_PREVIEW_PART = re.compile(rf"^Metadata/(?:{_PREVIEW_PREFIXES})[^/]*\.(?:png|json)$")
_PREVIEW_REL = re.compile(
    rf'[ \t]*<Relationship\b[^>]*Target="/?Metadata/(?:{_PREVIEW_PREFIXES})[^"]*"[^>]*/>[ \t]*\r?\n?'
)
# A sliced project carries the source printer's G-code: it must not travel to the target.
_STALE_GCODE = re.compile(r"^Metadata/plate_\d+\.gcode(?:\.md5)?$")
_GCODE_REL = re.compile(
    r'[ \t]*<Relationship\b[^>]*Target="/?Metadata/plate_\d+\.gcode[^"]*"[^>]*/>[ \t]*\r?\n?'
)
_PAINT_ATTR = re.compile(r'\s(?:paint_color|mmu_segmentation)="[^"]*"')
_TOL = 1e-6
# Orca's flush-volume tables for exactly one filament (matrix 1x1; vector = purge into/from the tower).
_SINGLE_FILAMENT_FLUSH_MATRIX = ("0",)
_SINGLE_FILAMENT_FLUSH_VECTOR = ("140", "140")

# Named override sets for `--preset`. Values are strings, as in project_settings.config.
PRESETS: dict[str, dict[str, str]] = {
    # Same-material supports that peel off instead of fusing: a 0.2 mm air gap above and below the
    # support, two interface layers on top (none below), sparse rectilinear interface lines, supports
    # switched on. Support type/angle/density and the object's walls and infill are left as authored.
    "easy-release-supports": {
        "enable_support": "1",
        "support_top_z_distance": "0.2",
        "support_bottom_z_distance": "0.2",
        "support_interface_top_layers": "2",
        "support_interface_bottom_layers": "0",
        "support_interface_spacing": "0.5",
        "support_interface_pattern": "rectilinear",
    },
}


@functools.lru_cache(maxsize=1)
def setting_scopes() -> dict[str, frozenset[str]]:
    """`{"machine": keys, "filament": keys}` from `lib/data/orca-setting-scopes.json`."""
    path = Path(__file__).resolve().parent.parent / "data" / "orca-setting-scopes.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "machine": frozenset(raw["machine"]) | GCODE_KEYS | _EXTRA_PER_EXTRUDER,
        "filament": frozenset(raw["filament"]),
    }


# --------------------------------------------------------------------------- data classes
@dataclass(frozen=True)
class PresetSource:
    """A resolved preset: its name and its flattened settings."""

    name: str
    settings: Mapping[str, Any]


@dataclass(frozen=True)
class Change:
    key: str
    old: Any
    new: Any
    origin: str


@dataclass
class Report:
    """What `retarget` did, for the CLI to print and the tests to assert on."""

    source: str = ""
    target: str = ""
    objects: list[str] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    bed: Bed = field(default_factory=lambda: Bed(0.0, 0.0, 0.0, 0.0))
    margin: float = 0.0
    footprint: tuple[float, float] = (0.0, 0.0)
    placement: Placement = field(
        default_factory=lambda: Placement(0.0, (0.0, 0.0), 0.0, 0.0, (0.0, 0.0))
    )
    zmax: float = 0.0
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Item:
    index: int  # 1-based position in <build>
    object_id: int
    instance_id: int
    transform: tuple[float, ...]


# --------------------------------------------------------------------------- 3MF reading
def read_package(path: Path) -> dict[str, bytes]:
    """All parts of the 3MF zip, in archive order."""
    if not path.is_file():
        raise InputNotFound(str(path), command="retarget")
    try:
        with zipfile.ZipFile(path) as zf:
            parts = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
    except zipfile.BadZipFile as exc:
        raise InvalidArgument(
            "input", str(path), ["a .3mf (zip) project file"], command="retarget"
        ) from exc
    if SETTINGS not in parts or MAIN_MODEL not in parts or MODEL_SETTINGS not in parts:
        raise InvalidArgument(
            "input",
            str(path),
            ["an Orca / Snapmaker Orca / Bambu Studio project .3mf"],
            command="retarget",
            extra=(
                f"Missing {SETTINGS}, {MODEL_SETTINGS} or {MAIN_MODEL}: this is not a slicer project "
                "3MF (a plain mesh 3MF has no printer settings to retarget; use `3d slice`)."
            ),
        )
    return parts


def write_package(path: Path, parts: Mapping[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in parts.items():
                zf.writestr(name, data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _load_settings(parts: Mapping[str, bytes]) -> dict[str, Any]:
    data = json.loads(parts[SETTINGS].decode("utf-8"))
    if not isinstance(data, dict):
        raise InvalidArgument("input", SETTINGS, ["a JSON object"], command="retarget")
    return data


def _dump_settings(settings: Mapping[str, Any]) -> bytes:
    return (json.dumps(settings, indent=4, ensure_ascii=False) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- transforms
Matrix = tuple[
    float, ...
]  # 3MF layout: m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz (row vectors)
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def _parse_matrix(text: str | None) -> Matrix:
    if not text:
        return _IDENTITY
    values = tuple(float(v) for v in text.split())
    if len(values) != 12:
        raise InvalidArgument("3MF transform", text, ["12 numbers"], command="retarget")
    return values


def _format_matrix(m: Sequence[float]) -> str:
    return " ".join(format(v, ".9g") for v in m)


def _compose(first: Matrix, then: Matrix) -> Matrix:
    """Apply `first`, then `then` (3MF row-vector convention)."""
    a, b = first, then
    rot = [
        sum(a[3 * r + k] * b[3 * k + c] for k in range(3))
        for r in range(3)
        for c in range(3)
    ]
    trans = [
        sum(a[9 + k] * b[3 * k + c] for k in range(3)) + b[9 + c] for c in range(3)
    ]
    return (*rot, *trans)


def _rotation_about_z(
    angle_deg: float, pivot: tuple[float, float], dx: float, dy: float
) -> Matrix:
    """World transform: rotate CCW by `angle_deg` about `pivot` (XY), then translate (dx, dy)."""
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    px, py = pivot
    # Row-vector form of p' = R(p - pivot) + pivot + d.
    tx = -px * c + py * s + px + dx
    ty = -px * s - py * c + py + dy
    return (c, s, 0.0, -s, c, 0.0, 0.0, 0.0, 1.0, tx, ty, 0.0)


def _apply(m: Matrix, x: float, y: float, z: float) -> tuple[float, float, float]:
    return (
        x * m[0] + y * m[3] + z * m[6] + m[9],
        x * m[1] + y * m[4] + z * m[7] + m[10],
        x * m[2] + y * m[5] + z * m[8] + m[11],
    )


# --------------------------------------------------------------------------- model parsing
_ITEM_RE = re.compile(r"[ \t]*<item\b[^>]*/>[ \t]*\r?\n?")
_OBJECT_RE = re.compile(
    r"[ \t]*<object id=\"(\d+)\"[^>]*>.*?</object>[ \t]*\r?\n?", re.DOTALL
)


def _build_items(main_model: str) -> list[_Item]:
    seen: dict[int, int] = {}
    items: list[_Item] = []
    for n, m in enumerate(_ITEM_RE.finditer(main_model), start=1):
        tag = m.group(0)
        oid = re.search(r'objectid="(\d+)"', tag)
        if oid is None:
            raise InvalidArgument(
                "3MF build item",
                tag.strip(),
                ["an <item objectid=...>"],
                command="retarget",
            )
        transform = re.search(r'transform="([^"]*)"', tag)
        object_id = int(oid.group(1))
        instance = seen.get(object_id, 0)
        seen[object_id] = instance + 1
        items.append(
            _Item(
                n,
                object_id,
                instance,
                _parse_matrix(transform.group(1) if transform else None),
            )
        )
    return items


@dataclass(frozen=True)
class _Leaf:
    """A mesh object reachable from a build item: where it lives and how it is placed in the item."""

    path: str | None  # sub-model part name (no leading slash), None = in the main model
    object_id: int
    transform: Matrix


def _leaves(main_model: str) -> dict[int, list[_Leaf]]:
    """object id -> mesh leaves (components resolved), from the main model's resources."""
    root = ET.fromstring(main_model)
    out: dict[int, list[_Leaf]] = {}
    for obj in root.iter(f"{{{_NS_CORE}}}object"):
        oid = int(obj.attrib["id"])
        leaves: list[_Leaf] = []
        if obj.find(f"{{{_NS_CORE}}}mesh") is not None:
            leaves.append(_Leaf(None, oid, _IDENTITY))
        for comp in obj.iter(f"{{{_NS_CORE}}}component"):
            path = comp.attrib.get(f"{{{_NS_PROD}}}path")
            leaves.append(
                _Leaf(
                    path.lstrip("/") if path else None,
                    int(comp.attrib["objectid"]),
                    _parse_matrix(comp.attrib.get("transform")),
                )
            )
        out[oid] = leaves
    return out


def _mesh_vertices(xml: bytes, object_id: int) -> list[tuple[float, float, float]]:
    """The vertices of mesh object `object_id` in a model part (end events only: one
    callback per element)."""
    verts: list[tuple[float, float, float]] = []
    for _event, elem in ET.iterparse(io.BytesIO(xml), events=("end",)):
        if elem.tag == _TAG_VERTEX:
            verts.append(
                (
                    float(elem.attrib["x"]),
                    float(elem.attrib["y"]),
                    float(elem.attrib["z"]),
                )
            )
        elif elem.tag == _TAG_OBJECT:
            if int(elem.attrib["id"]) == object_id:
                return verts
            verts.clear()
            elem.clear()
    return []


@dataclass(frozen=True)
class _Footprint:
    hull: list[tuple[float, float]]
    zmin: float
    zmax: float


def _footprint(
    parts: Mapping[str, bytes],
    main_model: str,
    items: Sequence[_Item],
    skip: frozenset[int] = frozenset(),
) -> _Footprint:
    """XY convex hull and Z range of the given build items in world space; mesh objects in
    `skip` (modifiers, negative parts, support blockers) do not count."""
    leaves = _leaves(main_model)
    xy: list[tuple[float, float]] = []
    zmin, zmax = math.inf, -math.inf
    cache: dict[tuple[str | None, int], list[tuple[float, float, float]]] = {}
    for item in items:
        for leaf in leaves.get(item.object_id, []):
            if leaf.object_id in skip:
                continue
            key = (leaf.path, leaf.object_id)
            if key not in cache:
                source = (
                    parts.get(leaf.path) if leaf.path else main_model.encode("utf-8")
                )
                if source is None:
                    raise InvalidArgument(
                        "3MF",
                        str(leaf.path),
                        ["a sub-model part present in the archive"],
                        command="retarget",
                    )
                cache[key] = _mesh_vertices(source, leaf.object_id)
                if not cache[key]:
                    raise InvalidArgument(
                        "3MF",
                        f"object {leaf.object_id} in {leaf.path or MAIN_MODEL}",
                        ["a component that points at an existing mesh object"],
                        command="retarget",
                    )
            world = _compose(leaf.transform, item.transform)
            for vx, vy, vz in cache[key]:
                x, y, z = _apply(world, vx, vy, vz)
                xy.append((x, y))
                zmin, zmax = min(zmin, z), max(zmax, z)
    if not xy:
        raise InvalidArgument(
            "--keep",
            str([i.index for i in items]),
            ["objects that contain a mesh"],
            command="retarget",
        )
    return _Footprint(convex_hull(xy), zmin, zmax)


def _non_printing_volumes(model_settings: str) -> frozenset[int]:
    """Volume (mesh object) ids whose subtype is not `normal_part`: modifiers, negative parts,
    support blockers/enforcers shape the print but are not material on the bed."""
    return frozenset(
        int(m.group(1))
        for m in re.finditer(r'<part id="(\d+)" subtype="([^"]*)"', model_settings)
        if m.group(2) != "normal_part"
    )


def _object_names(model_settings: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for m in re.finditer(
        r'<object id="(\d+)">\s*<metadata key="name" value="([^"]*)"', model_settings
    ):
        names[int(m.group(1))] = m.group(2)
    return names


def _object_extruders(model_settings: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for m in re.finditer(
        r'<object id="(\d+)">\s*(?:<metadata key="name"[^>]*/>\s*)?<metadata key="extruder" value="(\d+)"',
        model_settings,
    ):
        out[int(m.group(1))] = int(m.group(2))
    return out


# --------------------------------------------------------------------------- bed
def bed_from_settings(settings: Mapping[str, Any]) -> Bed:
    """The rectangular bed in `printable_area`; anything else is refused."""
    raw = settings.get("printable_area")
    try:
        pts = [tuple(float(v) for v in str(p).split("x")) for p in raw]  # type: ignore[union-attr]
        ok = len(pts) == 4 and all(len(p) == 2 for p in pts)
        xs, ys = {p[0] for p in pts}, {p[1] for p in pts}
        ok = ok and len(xs) == 2 and len(ys) == 2
    except (TypeError, ValueError, IndexError):
        ok = False
    if not ok:
        raise InvalidArgument(
            "printable_area",
            json.dumps(raw),
            [
                'four corners of an axis-aligned rectangle, e.g. ["0x0","220x0","220x220","0x220"]'
            ],
            command="retarget",
            extra="Round / polygonal beds are not supported by the Z-rotation fit.",
        )
    return Bed(min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- settings rewrite
def coerce_override(existing: Any, raw: str) -> Any:
    """A `--set` value in the type of the key it replaces: JSON list syntax is honoured
    (`["a","b"]`), a plain value fills every slot of a list key, and scalars stay strings."""
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise InvalidArgument(
                "--set value",
                raw,
                ['a JSON list of strings, e.g. ["0.2"]'],
                command="retarget",
            ) from exc
        if not isinstance(value, list):
            raise InvalidArgument(
                "--set value", raw, ["a JSON list of strings"], command="retarget"
            )
        return [str(v) for v in value]
    if isinstance(existing, list):
        return [raw] * len(existing)
    return raw


def parse_assignment(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise InvalidArgument(
            "--set",
            text,
            ["KEY=VALUE, e.g. support_top_z_distance=0.2"],
            command="retarget",
        )
    return key.strip(), value


def _rewrite_settings(
    src: Mapping[str, Any],
    *,
    machine: PresetSource,
    process: PresetSource | None,
    filament: PresetSource | None,
    keep_slot: int,
    setting_sets: Sequence[str],
    overrides: Sequence[tuple[str, str]],
    report: Report,
) -> dict[str, Any]:
    scopes = setting_scopes()
    new: dict[str, Any] = dict(src)
    origin: dict[str, str] = {}

    def put(key: str, value: Any, why: str) -> None:
        new[key] = value
        origin[key] = why

    old_ext = len(src.get("nozzle_diameter", [])) or 1
    old_fil = len(src.get("filament_settings_id", [])) or 1

    nozzles = machine.settings.get("nozzle_diameter")
    if not isinstance(nozzles, list) or not nozzles:
        raise InvalidArgument(
            "machine preset",
            machine.name,
            ["a machine preset with nozzle_diameter"],
            command="retarget",
        )
    new_ext = len(nozzles)
    if old_fil > new_ext and new_ext > 1:
        raise InvalidArgument(
            "--to",
            machine.name,
            [f"a printer with >= {old_fil} extruders, or a single-extruder printer"],
            command="retarget",
            extra=f"The project uses {old_fil} filaments; {machine.name} has {new_ext} extruders.",
        )
    new_fil = 1 if new_ext == 1 else old_fil
    collapse_filaments = new_fil == 1 and old_fil > 1

    # 1. printer
    for key, value in machine.settings.items():
        if key in src:
            put(key, value, "machine")
    put("printer_settings_id", machine.name, "machine")
    cleared: list[str] = []
    for key in sorted(scopes["machine"]):
        if key in machine.settings or key not in src:
            continue
        if key in GCODE_KEYS:
            put(key, "", "machine")
            if src[key] != "":
                cleared.append(key)
            continue
        value = src[key]
        per_extruder = key not in _POINT_LIST_KEYS and not key.startswith(
            _FIXED_LENGTH_PREFIXES
        )
        if (
            isinstance(value, list)
            and per_extruder
            and old_ext != new_ext
            and len(value) == old_ext
        ):
            if new_ext == 1:  # the kept object's extruder, as for its filament below
                put(key, [value[min(keep_slot, old_ext) - 1]], "collapse")
            else:
                put(
                    key,
                    value[:new_ext] if new_ext < old_ext else value[:1] * new_ext,
                    "collapse",
                )

    if cleared:
        report.notes.append(
            f"{machine.name} defines no {', '.join(cleared)}: cleared, so macros of the source "
            "printer cannot reach the target"
        )

    # 2. filament + process presets
    for source, kind in ((filament, "filament"), (process, "process")):
        if source is None:
            continue
        for key, value in source.settings.items():
            if key not in src:
                continue
            # One filament preset serves every filament slot of a multi-extruder target.
            wide = (
                kind == "filament"
                and new_fil > 1
                and isinstance(value, list)
                and len(value) == 1
            )
            put(key, value * new_fil if wide else value, kind)
    if filament is not None:
        put("filament_settings_id", [filament.name] * new_fil, "filament")
    if process is not None:
        put("print_settings_id", process.name, "process")
    put("print_compatible_printers", [], "machine")

    # 3. one filament
    if new_fil == 1:
        for key in scopes["filament"]:
            value = new.get(key)
            if (
                key not in new
                or origin.get(key) == "filament"
                or not isinstance(value, list)
                or len(value) <= 1
            ):
                continue
            idx = keep_slot - 1 if len(value) == old_fil and old_fil > 1 else 0
            put(key, [value[min(idx, len(value) - 1)]], "collapse")
    if collapse_filaments:
        for key, value in list(new.items()):
            if (
                key.endswith(_SLOT_KEY_SUFFIX)
                and isinstance(value, str)
                and value.isdigit()
                and int(value) > 1
            ):
                put(key, "1", "collapse")
        if (
            "filament_map" in new
        ):  # filament -> extruder assignment: the one filament on extruder 1
            put("filament_map", ["1"], "collapse")
        put("flush_volumes_matrix", list(_SINGLE_FILAMENT_FLUSH_MATRIX), "collapse")
        put("flush_volumes_vector", list(_SINGLE_FILAMENT_FLUSH_VECTOR), "collapse")
        if new.get("enable_prime_tower") != "0":
            put("enable_prime_tower", "0", "collapse")

    # 4. named override sets, then explicit --set
    for name in setting_sets:
        for key, value in PRESETS[name].items():
            put(key, coerce_override(new.get(key), value), f"preset {name}")
    for key, raw in overrides:
        if key not in new:
            close = difflib.get_close_matches(key, list(new), n=5, cutoff=0.6)
            raise InvalidArgument(
                "--set",
                key,
                close or ["a key of Metadata/project_settings.config"],
                command="retarget",
                extra="Unknown setting key; the slicer would silently ignore it. Closest keys listed above.",
            )
        put(key, coerce_override(new[key], raw), "--set")

    for key in _REQUIRED_KEYS:
        if key not in new:
            raise InvalidArgument(
                "project settings", key, [f"a project with {key}"], command="retarget"
            )

    report.changes = [
        Change(k, src.get(k), v, origin[k]) for k, v in new.items() if src.get(k) != v
    ]
    return new


# --------------------------------------------------------------------------- package rewrite
def _drop_dead_parts(
    parts: dict[str, bytes], main_model: str, kept_objects: set[int]
) -> str:
    """Remove the objects/items that are not kept from the main model; delete the sub-model
    parts nothing references any more; return the new main model text."""
    leaves = _leaves(main_model)
    live_paths = {
        leaf.path for oid in kept_objects for leaf in leaves.get(oid, []) if leaf.path
    }
    # Meshes stored in the main model itself (no sub-model part) belong to their wrapper object.
    kept_objects = kept_objects | {
        leaf.object_id
        for oid in kept_objects
        for leaf in leaves.get(oid, [])
        if leaf.path is None
    }
    dead_paths = {
        leaf.path for ls in leaves.values() for leaf in ls if leaf.path
    } - live_paths

    def keep_object(m: re.Match[str]) -> str:
        return m.group(0) if int(m.group(1)) in kept_objects else ""

    text = _OBJECT_RE.sub(keep_object, main_model)
    for path in dead_paths:
        parts.pop(path, None)
    rels = parts.get(MODEL_RELS)
    if rels is not None:

        def keep_rel(m: re.Match[str]) -> str:
            target = re.search(r'Target="/?([^"]*)"', m.group(0))
            return "" if target and target.group(1) in dead_paths else m.group(0)

        parts[MODEL_RELS] = re.sub(
            r"[ \t]*<Relationship\b[^>]*/>[ \t]*\r?\n?", keep_rel, rels.decode("utf-8")
        ).encode("utf-8")
    return text


_PLATE_RE = re.compile(r"[ \t]*<plate>.*?</plate>[ \t]*\r?\n?", re.DOTALL)
_INSTANCE_RE = re.compile(
    r"[ \t]*<model_instance>.*?</model_instance>[ \t]*\r?\n?", re.DOTALL
)


def _keep_one_plate(text: str, instance_map: Mapping[tuple[int, int], int]) -> str:
    """The kept instances all live on one plate (else refused); drop the other plates and make
    that one plate 1, because the kept objects were just centred on the first plate's bed."""
    plates = list(_PLATE_RE.finditer(text))
    holders: list[re.Match[str]] = []
    for plate in plates:
        for inst in _INSTANCE_RE.finditer(plate.group(0)):
            oid = re.search(r'key="object_id" value="(\d+)"', inst.group(0))
            iid = re.search(r'key="instance_id" value="(\d+)"', inst.group(0))
            if oid and iid and (int(oid.group(1)), int(iid.group(1))) in instance_map:
                holders.append(plate)
                break
    if len(holders) > 1:
        ids = [re.search(r'key="plater_id" value="(\d+)"', h.group(0)) for h in holders]
        numbers = [m.group(1) if m else "?" for m in ids]
        raise InvalidArgument(
            "--keep",
            "objects on plates " + ", ".join(numbers),
            ["objects that all sit on one plate"],
            command="retarget",
            extra="Plates are separate beds in the project; retarget one plate's objects per run.",
        )
    keep_plate = holders[0] if holders else (plates[0] if plates else None)

    def sub(m: re.Match[str]) -> str:
        if keep_plate is None or m.start() != keep_plate.start():
            return ""
        return re.sub(r'(key="plater_id" value=")\d+(")', r"\g<1>1\g<2>", m.group(0))

    return _PLATE_RE.sub(sub, text)


def _filter_model_settings(
    text: str,
    kept_objects: set[int],
    instance_map: Mapping[tuple[int, int], int],
    *,
    one_extruder: bool,
) -> str:
    """Drop what is not kept from `model_settings.config` and renumber the kept instances.

    `instance_map` maps `(object id, old instance id)` of each kept build item to its new
    instance id: dropping a sibling instance shifts the ones after it down.
    """

    def keep_object(m: re.Match[str]) -> str:
        return m.group(0) if int(m.group(1)) in kept_objects else ""

    text = re.sub(
        r"[ \t]*<object id=\"(\d+)\">.*?</object>[ \t]*\r?\n?",
        keep_object,
        text,
        flags=re.DOTALL,
    )

    def keep_instance(m: re.Match[str]) -> str:
        block = m.group(0)
        oid = re.search(r'key="object_id" value="(\d+)"', block)
        iid = re.search(r'key="instance_id" value="(\d+)"', block)
        if not (oid and iid):
            return block
        new_id = instance_map.get((int(oid.group(1)), int(iid.group(1))))
        if new_id is None:
            return ""
        return re.sub(
            r'(key="instance_id" value=")\d+(")', rf"\g<1>{new_id}\g<2>", block
        )

    text = _INSTANCE_RE.sub(keep_instance, text)

    def keep_assemble(m: re.Match[str]) -> str:
        tag = m.group(0)
        oid = re.search(r'object_id="(\d+)"', tag)
        iid = re.search(r'instance_id="(\d+)"', tag)
        if not (oid and iid):
            return tag
        new_id = instance_map.get((int(oid.group(1)), int(iid.group(1))))
        if new_id is None or int(oid.group(1)) not in kept_objects:
            return ""
        return re.sub(r'instance_id="\d+"', f'instance_id="{new_id}"', tag, count=1)

    text = re.sub(r"[ \t]*<assemble_item\b[^>]*/>[ \t]*\r?\n?", keep_assemble, text)
    # The preview images are of the old layout; the slicer redraws them on open.
    text = re.sub(
        r'[ \t]*<metadata key="(?:thumbnail_file|thumbnail_no_light_file|top_file|pick_file|pattern_bbox_file|gcode_file)" value="[^"]*"/>[ \t]*\r?\n?',
        "",
        text,
    )
    if one_extruder:
        text = re.sub(r'(<metadata key="extruder" value=")\d+(")', r"\g<1>1\g<2>", text)
        text = re.sub(
            r'(<metadata key="filament_maps" value=")[^"]*(")', r"\g<1>1\g<2>", text
        )
    return text


def _filter_layer_profile(
    text: str, order: Sequence[int], kept_objects: set[int]
) -> tuple[str, list[str]]:
    """Orca keys this file by the 1-based position of the object in the model: keep the lines of
    kept objects and renumber them to their new position."""
    new_pos = {
        oid: i for i, oid in enumerate([o for o in order if o in kept_objects], start=1)
    }
    owner_of = dict(enumerate(order, start=1))
    kept_lines: list[str] = []
    notes: list[str] = []
    for line in text.splitlines():
        m = re.match(r"object_id=(\d+)\|(.*)$", line)
        if m is None:
            continue
        owner = owner_of.get(int(m.group(1)))
        if owner is None or owner not in kept_objects:
            continue
        kept_lines.append(f"object_id={new_pos[owner]}|{m.group(2)}")
        heights = [float(v) for v in m.group(2).split(";")[1::2] if v]
        if heights:
            notes.append(
                f"object {new_pos[owner]} keeps its variable layer-height profile "
                f"({min(heights):g}-{max(heights):g} mm); it overrides layer_height"
            )
    return "\n".join(kept_lines) + ("\n" if kept_lines else ""), notes


def _set_item_transforms(
    main_model: str, kept: Sequence[_Item], placement: Placement
) -> str:
    world = _rotation_about_z(
        placement.angle_deg, placement.pivot, placement.dx, placement.dy
    )
    new_by_index = {item.index: _compose(item.transform, world) for item in kept}
    kept_idx = set(new_by_index)
    counter = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        if counter not in kept_idx:
            return ""
        tag = m.group(0)
        formatted = _format_matrix(new_by_index[counter])
        if 'transform="' in tag:
            return re.sub(
                r'transform="[^"]*"', f'transform="{formatted}"', tag, count=1
            )
        return tag.replace("<item", f'<item transform="{formatted}"', 1)

    return _ITEM_RE.sub(sub, main_model)


def parse_keep(spec: str, count: int) -> list[int]:
    """`"1"`, `"1,3"` -> sorted unique 1-based build item numbers, each within 1..count."""
    try:
        picked = sorted({int(p) for p in spec.split(",") if p.strip()})
    except ValueError:
        picked = []
    if not picked or picked[0] < 1 or picked[-1] > count:
        raise InvalidArgument(
            "--keep",
            spec,
            [f"comma-separated numbers within 1..{count}"],
            command="retarget",
            extra=f"The project has {count} build object(s), numbered 1..{count} in the order listed.",
        )
    return picked


def retarget(
    src: Path,
    *,
    machine: PresetSource,
    process: PresetSource | None = None,
    filament: PresetSource | None = None,
    keep: str = "",
    margin: float = 5.0,
    setting_sets: Sequence[str] = (),
    overrides: Sequence[tuple[str, str]] = (),
) -> tuple[dict[str, bytes], Report]:
    """Build the retargeted parts for `src`; `keep` is a `--keep` spec ("1,3", empty = all).
    Raises GateFailure when the footprint cannot fit."""
    parts = read_package(src)
    settings = _load_settings(parts)
    main_model = parts[MAIN_MODEL].decode("utf-8")
    model_settings = parts[MODEL_SETTINGS].decode("utf-8")
    items = _build_items(main_model)
    if not items:
        raise InvalidArgument(
            "input",
            str(src),
            ["a project with at least one build object"],
            command="retarget",
        )

    names = _object_names(model_settings)
    report = Report(source=src.name, target=machine.name, margin=margin)
    report.objects = [
        f"{it.index}: {names.get(it.object_id, '?')} (object {it.object_id})"
        for it in items
    ]

    wanted = parse_keep(keep, len(items)) if keep else [it.index for it in items]
    kept = [items[i - 1] for i in wanted]
    report.kept = [it.index for it in kept]
    instance_map: dict[tuple[int, int], int] = {}
    per_object: dict[int, int] = {}
    for it in kept:  # `kept` is in build order, as is the slicer's instance numbering
        instance_map[(it.object_id, it.instance_id)] = per_object.get(it.object_id, 0)
        per_object[it.object_id] = per_object.get(it.object_id, 0) + 1
    # Before any fitting: objects of different plates are not one layout (they would be hulled
    # together across the plate gap and "not fit" for the wrong reason).
    model_settings = _keep_one_plate(model_settings, instance_map)
    if process is None:
        report.notes.append(
            f"process settings kept from the source ({settings.get('print_settings_id', '?')}); "
            "apply a target preset with --process NAME"
        )
    if filament is None:
        fil = settings.get("filament_settings_id", ["?"])
        report.notes.append(
            f"filament settings kept from the source ({fil[0] if fil else '?'}); "
            "apply a target preset with --filament NAME"
        )

    extruders = _object_extruders(model_settings)
    slot = extruders.get(kept[0].object_id, 1) or 1
    new_settings = _rewrite_settings(
        settings,
        machine=machine,
        process=process,
        filament=filament,
        keep_slot=slot,
        setting_sets=setting_sets,
        overrides=overrides,
        report=report,
    )
    used_slots = sorted({extruders.get(it.object_id, 1) or 1 for it in kept})
    if len(used_slots) > 1 and len(new_settings.get("filament_settings_id", [])) == 1:
        report.notes.append(
            f"kept objects use filament slots {used_slots}; all print with slot {slot} "
            "(the first kept object's) on this single-filament target"
        )

    bed = bed_from_settings(new_settings)
    report.bed = bed
    foot = _footprint(parts, main_model, kept, _non_printing_volumes(model_settings))
    height = float(str(new_settings["printable_height"]))
    report.zmax = foot.zmax
    span = foot.zmax - min(foot.zmin, 0.0)
    if span > height + _TOL:
        raise GateFailure(
            f"too tall for {machine.name}: part is {span:.1f} mm high, "
            f"printable_height is {height:g} mm",
            command="retarget",
            remediation=[
                "Keep fewer objects (--keep) or pick a printer with more Z travel."
            ],
        )
    if abs(foot.zmin) > 0.05:
        report.notes.append(
            f"lowest point is z={foot.zmin:.3f} mm, not on the bed; left as in the source"
        )

    placement = fit_to_bed(foot.hull, bed, margin)
    if placement is None:
        angle, (w, d), slack = closest_fit(foot.hull, bed, margin)
        usable = (bed.width - 2 * margin, bed.depth - 2 * margin)
        widest = margin + slack / 2
        hint = (
            f"it would fit with --margin {max(widest - 0.05, 0):.1f} or less"
            if widest > 0
            else "it does not fit even with no margin"
        )
        raise GateFailure(
            f"does not fit {machine.name}: footprint {w:.1f} x {d:.1f} mm at the closest Z rotation "
            f"({angle:.0f} deg) vs usable {usable[0]:g} x {usable[1]:g} mm "
            f"(bed {bed.width:g} x {bed.depth:g} mm minus {margin:g} mm margin each side); {hint}",
            command="retarget",
            remediation=[
                "Keep fewer objects: --keep N (objects: "
                + "; ".join(report.objects)
                + ")",
                "Re-orient (tilt) or split the part in the slicer; Z rotation alone cannot make it fit.",
            ],
        )
    report.placement = placement
    report.footprint = placement.size

    kept_objects = {it.object_id for it in kept}
    order = list(
        dict.fromkeys(it.object_id for it in items)
    )  # the slicer's object numbering
    out = dict(parts)
    new_main = _set_item_transforms(main_model, kept, placement)
    out[MAIN_MODEL] = _drop_dead_parts(out, new_main, kept_objects).encode("utf-8")

    one_extruder = len(new_settings.get("filament_settings_id", [])) == 1
    new_ms = _filter_model_settings(
        model_settings, kept_objects, instance_map, one_extruder=one_extruder
    )
    out[MODEL_SETTINGS] = new_ms.encode("utf-8")
    out[SETTINGS] = _dump_settings(new_settings)

    if LAYER_PROFILE in out:
        profile, notes = _filter_layer_profile(
            out[LAYER_PROFILE].decode("utf-8"), order, kept_objects
        )
        report.notes.extend(notes)
        if profile:
            out[LAYER_PROFILE] = profile.encode("utf-8")
        else:
            del out[LAYER_PROFILE]
    stale = [name for name in out if _PREVIEW_PART.match(name)]
    for name in stale:
        del out[name]
    if stale:
        _drop_preview_rels(out)
        report.notes.append(
            f"dropped {len(stale)} stale preview file(s); the slicer redraws them on open"
        )
    old_gcode = [name for name in out if _STALE_GCODE.match(name)]
    for name in old_gcode:
        del out[name]
    if old_gcode:
        for rels_name in (ROOT_RELS, "Metadata/_rels/model_settings.config.rels"):
            if rels_name in out:
                out[rels_name] = _GCODE_REL.sub(
                    "", out[rels_name].decode("utf-8")
                ).encode("utf-8")
        report.notes.append(
            f"dropped {len(old_gcode)} G-code part(s) sliced for the source printer; re-slice for the target"
        )

    if one_extruder:
        stripped = 0
        for name in [n for n in out if n.startswith("3D/") and n.endswith(".model")]:
            text = out[name].decode("utf-8")
            if _PAINT_ATTR.search(text):
                new_text, n = _PAINT_ATTR.subn("", text)
                out[name] = new_text.encode("utf-8")
                stripped += n
        if stripped:
            report.notes.append(
                f"removed {stripped} per-triangle colour paint marks (single filament target)"
            )
    return out, report


def _drop_preview_rels(parts: dict[str, bytes]) -> None:
    rels = parts.get(ROOT_RELS)
    if rels is not None:
        parts[ROOT_RELS] = _PREVIEW_REL.sub("", rels.decode("utf-8")).encode("utf-8")


# --------------------------------------------------------------------------- validation
def validate_parts(
    parts: Mapping[str, bytes], *, bed: Bed, margin: float, nozzles: int
) -> list[str]:
    """Independent re-check of a retargeted package; an empty list means it is sound."""
    problems: list[str] = []
    for name, data in parts.items():
        if name.startswith("3D/Objects/"):
            continue  # meshes are parsed (and so checked) by the footprint pass below
        if name.endswith((".xml", ".model", ".rels")) or name == MODEL_SETTINGS:
            try:
                ET.fromstring(data)
            except ET.ParseError as exc:
                problems.append(f"{name}: not well-formed XML ({exc})")
    try:
        settings = json.loads(parts[SETTINGS].decode("utf-8"))
    except (KeyError, ValueError) as exc:
        return [*problems, f"{SETTINGS}: not valid JSON ({exc})"]
    problems.extend(
        f"{SETTINGS}: missing key {k}" for k in _REQUIRED_KEYS if k not in settings
    )
    if problems:
        return problems

    try:
        if bed_from_settings(settings) != bed:
            problems.append("printable_area does not match the target bed")
    except InvalidArgument as exc:
        problems.append(str(exc))
    if len(settings.get("nozzle_diameter", [])) != nozzles:
        problems.append(
            f"nozzle_diameter has {len(settings.get('nozzle_diameter', []))} entries, expected {nozzles}"
        )
    if nozzles == 1:
        if len(settings.get("filament_settings_id", [])) != 1:
            problems.append(
                "more than one filament remains for a single-extruder target"
            )
        ms = parts[MODEL_SETTINGS].decode("utf-8")
        if any(int(v) > 1 for v in re.findall(r'key="extruder" value="(\d+)"', ms)):
            problems.append(
                f"{MODEL_SETTINGS}: an object/part is still assigned to extruder > 1"
            )

    try:
        main = parts[MAIN_MODEL].decode("utf-8")
        items = _build_items(main)
        leaves = _leaves(main)
    except (KeyError, ET.ParseError, InvalidArgument) as exc:
        return [*problems, f"{MAIN_MODEL}: unreadable ({exc})"]
    if not items:
        return [*problems, f"{MAIN_MODEL}: no build items"]
    for item in items:
        if not leaves.get(item.object_id):
            problems.append(
                f"build item {item.index} references object {item.object_id} that has no mesh"
            )
        problems.extend(
            f"object {item.object_id}: part {leaf.path} is missing from the archive"
            for leaf in leaves.get(item.object_id, [])
            if leaf.path and leaf.path not in parts
        )
    if problems:
        return problems

    try:
        foot = _footprint(
            parts,
            main,
            items,
            _non_printing_volumes(parts[MODEL_SETTINGS].decode("utf-8")),
        )
    except (ET.ParseError, InvalidArgument) as exc:
        return [f"a mesh part cannot be read: {exc}"]
    min_x, min_y, max_x, max_y = bounds(foot.hull)
    lo_x, lo_y = bed.min_x + margin - _TOL, bed.min_y + margin - _TOL
    hi_x, hi_y = bed.max_x - margin + _TOL, bed.max_y - margin + _TOL
    if min_x < lo_x or min_y < lo_y or max_x > hi_x or max_y > hi_y:
        problems.append(
            f"footprint x[{min_x:.2f},{max_x:.2f}] y[{min_y:.2f},{max_y:.2f}] leaves the bed "
            f"x[{bed.min_x + margin:g},{bed.max_x - margin:g}] y[{bed.min_y + margin:g},{bed.max_y - margin:g}]"
        )
    height = float(str(settings["printable_height"]))
    if foot.zmax > height + _TOL:
        problems.append(
            f"part top z={foot.zmax:.2f} exceeds printable_height {height:g}"
        )
    return problems


# --------------------------------------------------------------------------- slicing inputs
def _read_mesh(
    source: bytes, object_id: int
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Vertices and triangles of one `<object>`, streamed: elements are cleared as they close, so
    a large model file is never held as a tree, and parsing stops once the object ends."""
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    inside = False
    for event, elem in ET.iterparse(io.BytesIO(source), events=("start", "end")):
        if elem.tag == _TAG_OBJECT:
            if event == "start":
                inside = int(elem.attrib["id"]) == object_id
            else:
                if inside:
                    break
                elem.clear()
        elif event == "end":
            if inside and elem.tag == _TAG_VERTEX:
                verts.append(
                    (
                        float(elem.attrib["x"]),
                        float(elem.attrib["y"]),
                        float(elem.attrib["z"]),
                    )
                )
            elif inside and elem.tag == _TAG_TRIANGLE:
                faces.append(
                    (
                        int(elem.attrib["v1"]),
                        int(elem.attrib["v2"]),
                        int(elem.attrib["v3"]),
                    )
                )
            if not inside or elem.tag in (_TAG_VERTEX, _TAG_TRIANGLE):
                elem.clear()
    return verts, faces


def export_stl(parts: Mapping[str, bytes]) -> bytes:
    """Binary STL of every printing volume of the package's build items, in world (bed)
    coordinates. The Snapmaker Orca CLI crashes on project 3MFs but slices an STL with
    `--load-settings`, so `3d retarget --gcode` slices this plus the retargeted settings."""
    main = parts[MAIN_MODEL].decode("utf-8")
    skip = _non_printing_volumes(parts[MODEL_SETTINGS].decode("utf-8"))
    leaves = _leaves(main)
    meshes: dict[
        tuple[str | None, int],
        tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]],
    ] = {}
    packer = struct.Struct("<12fH")
    body = bytearray()
    count = 0
    for item in _build_items(main):
        for leaf in leaves.get(item.object_id, []):
            if leaf.object_id in skip:
                continue
            key = (leaf.path, leaf.object_id)
            if key not in meshes:
                meshes[key] = _read_mesh(
                    parts[leaf.path] if leaf.path else parts[MAIN_MODEL], leaf.object_id
                )
            local, faces = meshes[key]
            world = _compose(leaf.transform, item.transform)
            verts = [_apply(world, x, y, z) for x, y, z in local]
            for a, b, c in faces:
                body += packer.pack(0, 0, 0, *verts[a], *verts[b], *verts[c], 0)
                count += 1
    return b"retarget".ljust(80, b"\0") + struct.pack("<I", count) + body


def split_project_settings(
    settings: Mapping[str, Any], machine_name: str
) -> dict[str, dict[str, Any]]:
    """The retargeted `project_settings.config` as three preset files for `--load-settings` /
    `--load-filaments`: machine keys, filament keys, everything else = process. Process and
    filament presets name the machine as their only compatible printer (the CLI matches it
    against the machine preset's own name)."""
    process_name = settings.get("print_settings_id")
    filament_names = settings.get("filament_settings_id")
    if not process_name or not filament_names:
        raise GateFailure(
            "the project settings have no print_settings_id / filament_settings_id",
            command="retarget",
            remediation=[
                "re-save the project from the slicer so it carries its preset names"
            ],
        )
    scopes = setting_scopes()
    ident = {"from": "system", "instantiation": "true"}
    out: dict[str, dict[str, Any]] = {"machine": {}, "process": {}, "filament": {}}
    for key, value in settings.items():
        if key in _PRESET_IDENTITY_KEYS:
            continue
        if key in scopes["machine"]:
            out["machine"][key] = value
        elif key in scopes["filament"]:
            out["filament"][key] = value
        else:
            out["process"][key] = value
    # A third-party project must not make the slicer run a host command (`post_process`) or write
    # outside the work dir (`output_filename_format`) just because `--gcode` loads its settings.
    out["process"].update(_HOST_REACHING_KEYS)
    out["machine"].update(ident, type="machine", name=machine_name)
    out["process"].update(
        ident,
        type="process",
        name=str(process_name),
        compatible_printers=[machine_name],
        compatible_printers_condition="",
    )
    out["filament"].update(
        ident,
        type="filament",
        name=str(filament_names[0]),
        compatible_printers=[machine_name],
        compatible_printers_condition="",
    )
    return out
