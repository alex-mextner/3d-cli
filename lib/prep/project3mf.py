"""project3mf.py — write the multi-colour project 3MF the Orca-family slicers slice, plus its manifest.

WHAT: `build_project(spec, out)` turns colour parts (`contracts.Part`), a filament list, a process preset
  and its overrides into ONE object 3MF — parts as volumes, per-part extruder, Full Spectrum mixed rows,
  a variable layer-height profile, brim, prime tower — and writes `NAME.manifest.json` next to it with
  what the G-code verifier must expect (parts and their z-ranges, filaments, shift, process, gates).
WHY: the Chekhov cat took hours partly because the 3MF builder was a spec-file script bound to one
  project (u1/build_3mf.py, babushkin-dom gen/build_orca_3mf.py). Ported here as a library the `3d prep`
  pipeline calls with objects instead of JSON files (ticket #431).
INVARIANTS:
  * ONE object: the prime tower needs identical layer heights for all objects and a variable layer-height
    profile is per object (quirk `prime-tower-single-object`); colours are volumes of that object.
  * Mixed rows are written component_a=1, component_b=2 so a pattern token is the slot number (quirk
    `mixed-row-convention`); G-code tool Tn = slot n+1 (`tool-index-offset`), the manifest lists both.
  * The layer profile must end at the object's height (quirk `layer-profile-top-mismatch`): refused here,
    because the slicer would drop it silently.
  * The 3MF keeps its <plate> block for the GUI; the slicing copy (`orca_cli.slicing_copy`) drops it.
  * UUIDs are derived from the name, so a rebuild with unchanged input gives byte-identical files.
ASSUMPTIONS:
  * Parts are disjoint volumes in the print frame (`frame.py`); the lowest point is put on the bed and the
    footprint centred on `position` (the applied `shift` is in the manifest).
  * `project_settings.config` comes from the vendor presets through the CLI (`orca_cli.reference_project_settings`);
    without the Orca install `build_project` raises `MissingDependency` unless `base_settings` is passed.
  * Mix rules: `slicing.mix_pattern.analyse_pattern` when it exists (#410), else the local fallback below
    (no filament on two adjacent layers of the cyclic pattern, mix layer <= 0.12 mm). The fallback is
    replaced when #410 merges.
"""

from __future__ import annotations

import json
import re
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import quoteattr

import numpy as np

from errors import InvalidArgument, MissingDependency
from orca_project_3mf import _content_types_xml, _root_rels_xml
from prep.contracts import Filament, Part, Verdict
from slicing import orca_cli
from standing_props import CORE, PROD, REL_3DMODEL

MIX_LAYER_CAP_MM = 0.12
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_PRESET_META = frozenset(
    {
        "from",
        "inherits",
        "name",
        "print_settings_id",
        "filament_settings_id",
        "version",
        "is_custom_defined",
        "setting_id",
        "instantiation",
        "compatible_printers",
        "compatible_printers_condition",
        "base_id",
    }
)
_NS = (
    f'xmlns="{CORE}" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    f'xmlns:p="{PROD}" requiredextensions="p"'
)


@dataclass(frozen=True)
class PrinterModel:
    vendor: str
    machine: str
    extruders: int
    bed: float
    bed_height: float
    bed_type: str
    default_filament: str
    #: machine-level keys proven against tool-change ooze (babushkin-dom docs/u1_toolchange_ooze.md)
    machine_overlay: Mapping[str, str]


PRINTERS: dict[str, PrinterModel] = {
    "u1": PrinterModel(
        vendor="Snapmaker",
        machine="Snapmaker U1 (0.4 nozzle)",
        extruders=4,
        bed=270.0,
        bed_height=270.0,
        bed_type="Textured PEI Plate",
        default_filament="Snapmaker PLA Basic @U1",
        machine_overlay={
            "retraction_length": "1",
            "retraction_speed": "40",
            "deretraction_speed": "30",
            "z_hop": "0.6",
            "z_hop_types": "Slope Lift",
        },
    )
}


@dataclass
class ProcessOverrides:
    """Process settings written over the vendor preset. None leaves the preset's value."""

    wall_loops: int | None = 4
    sparse_infill_density: str | None = "15%"
    sparse_infill_pattern: str | None = "gyroid"
    top_shell_layers: int | None = None
    bottom_shell_layers: int | None = None
    layer_height: float | None = 0.2
    initial_layer_print_height: float | None = 0.2
    adaptive_layer_height: bool | None = None
    extra: dict[str, str | list[str]] = field(default_factory=dict)

    def to_config(self) -> dict[str, str | list[str]]:
        """Settings as `project_settings.config` stores them (strings); supports, raft and Local-Z dithering
        are always off (dithering would replace the layer profile, quirk `local-z-dithering`)."""
        cfg: dict[str, str | list[str]] = {}
        scalars: dict[str, object | None] = {
            "wall_loops": self.wall_loops,
            "sparse_infill_density": self.sparse_infill_density,
            "sparse_infill_pattern": self.sparse_infill_pattern,
            "top_shell_layers": self.top_shell_layers,
            "bottom_shell_layers": self.bottom_shell_layers,
            "layer_height": self.layer_height,
            "initial_layer_print_height": self.initial_layer_print_height,
        }
        for key, value in scalars.items():
            if value is not None:
                cfg[key] = f"{value:g}" if isinstance(value, float) else str(value)
        if self.adaptive_layer_height is not None:
            cfg["adaptive_layer_height"] = "1" if self.adaptive_layer_height else "0"
        cfg.update(self.extra)
        cfg.update(
            {"enable_support": "0", "raft_layers": "0", "dithering_local_z_mode": "0"}
        )
        return cfg

    @classmethod
    def from_user_preset(cls, path: str | Path) -> ProcessOverrides:
        """Every setting of a user process preset file (its identity keys dropped) as overrides."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        extra = {k: v for k, v in data.items() if k not in _PRESET_META}
        return cls(
            wall_loops=None,
            sparse_infill_density=None,
            sparse_infill_pattern=None,
            layer_height=None,
            initial_layer_print_height=None,
            extra=extra,
        )


class LayerPlanLike(Protocol):
    """Anything that carries the text of Metadata/layer_heights_profile.txt for object 1."""

    @property
    def profile_text(self) -> str: ...


@dataclass
class ProjectSpec:
    name: str
    parts: list[Part]
    filaments: list[Filament]
    process_preset: str
    printer: str = "u1"
    filament_preset: str | None = None
    overrides: ProcessOverrides = field(default_factory=ProcessOverrides)
    layer_plan: LayerPlanLike | None = None
    brim_mm: float = 0.0
    position: tuple[float, float] | None = None
    prime_tower: bool | None = None  # None: on when more than one head prints
    tower_xy: tuple[float, float] | None = None
    #: layer height assumed for the mix-pattern thickness rule when no layer plan says otherwise
    mix_layer_mm: float = 0.10
    app_version: str = orca_cli.CLI_FALLBACK_VERSION


@dataclass
class ProjectManifest:
    """What was built; satisfies `prep.contracts.StepResult`."""

    verdict: Verdict
    path: str
    manifest_path: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.data

    def summary(self) -> str:
        d = self.data
        tower = (
            f"prime tower @ {d['tower_xy']}" if d["prime_tower"] else "no prime tower"
        )
        return (
            f"{d['name']}: {len(d['parts'])} parts, mixes {[r['pattern'] for r in d['mix_rows']]}, "
            f"heads {d['heads_slots']}, {tower}"
        )


# --------------------------------------------------------------------------- mix


def check_mix_pattern(pattern: str, layer_mm: float) -> list[str]:
    """Reasons a slot-digit layer pattern is illegal (empty when fine)."""
    try:
        from slicing.mix_pattern import analyse_pattern  # type: ignore[import-not-found]  # #410 not on main yet
    except ImportError:
        pass
    else:
        report = analyse_pattern(pattern, layer_mm)
        return [f"{v.rule}: {v.detail}" for v in report.violations]
    reasons: list[str] = []
    n = len(pattern)
    adjacent = [i for i in range(n) if pattern[i] == pattern[(i + 1) % n]]
    if adjacent:
        reasons.append(
            f"same filament on neighbouring layers (cyclic, positions {adjacent}): a thick band"
        )
    if layer_mm > MIX_LAYER_CAP_MM:
        reasons.append(
            f"layer {layer_mm} mm exceeds the {MIX_LAYER_CAP_MM} mm mix layer cap"
        )
    return reasons


def _key_map(filaments: Sequence[Filament]) -> dict[str, int]:
    keys = [f.key for f in filaments]
    if not all(len(k) == 1 and not k.isdigit() for k in keys if k):
        raise InvalidArgument(
            "filament key", str(keys), ["single non-digit characters"], command="prep"
        )
    named = [k for k in keys if k]
    if len(named) != len(set(named)):
        raise InvalidArgument(
            "filament key", str(keys), ["unique keys"], command="prep"
        )
    return {f.key: f.slot for f in filaments if f.key}


@dataclass
class _Resolved:
    slot: int | None
    mix: str | None  # slot digits
    mix_src: str | None


def _resolve(part: Part, keymap: Mapping[str, int], n: int) -> _Resolved:
    def slot_of(ch: str) -> int:
        if ch in keymap:
            return keymap[ch]
        if ch.isdigit() and 1 <= int(ch) <= n:
            return int(ch)
        raise InvalidArgument(
            f"part {part.name!r} filament",
            ch,
            [*sorted(keymap), *map(str, range(1, n + 1))],
            command="prep",
        )

    a = part.assign
    if a.slot is not None:
        return _Resolved(slot_of(str(a.slot)), None, None)
    if a.key is not None:
        return _Resolved(slot_of(a.key), None, None)
    assert a.mix is not None
    return _Resolved(None, "".join(str(slot_of(c)) for c in a.mix), a.mix)


class _Mixes:
    def __init__(self, n_phys: int) -> None:
        self.n = n_phys
        self.patterns: list[str] = []

    def extruder(self, r: _Resolved) -> int:
        if r.slot is not None:
            return r.slot
        assert r.mix is not None
        if r.mix not in self.patterns:
            self.patterns.append(r.mix)
        return self.n + 1 + self.patterns.index(r.mix)

    def definitions(self) -> str:
        return ";".join(
            f"1,2,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u{701 + i},cm1,{p}"
            for i, p in enumerate(self.patterns)
        )

    def heads(self, extruder: int) -> set[int]:
        if extruder <= self.n:
            return {extruder}
        return {int(c) for c in self.patterns[extruder - self.n - 1]}


# --------------------------------------------------------------------------- layer profile


def _parse_profile(text: str) -> tuple[int, list[float]]:
    head, sep, body = text.strip().partition("|")
    m = re.fullmatch(r"object_id=(\d+)", head)
    try:
        values = [float(v) for v in body.split(";") if v]
    except ValueError:
        values = []
    if not sep or not m or len(values) < 4 or len(values) % 2:
        raise InvalidArgument(
            "layer_plan.profile_text",
            text[:60],
            ["object_id=N|z;h;z;h;... with at least two (z, h) pairs"],
            command="prep",
        )
    return int(m.group(1)), values


# --------------------------------------------------------------------------- placement / xml


def free_spot(
    rects: Sequence[tuple[float, float, float, float]],
    w: float,
    d: float,
    bed: float,
    margin: float = 6.0,
) -> tuple[float, float] | None:
    """Lower-left corner of a w x d square on the bed clearing every footprint by `margin` (None: no room)."""
    for y in np.arange(bed - d - 5, 4, -2.0):
        for x in np.arange(5, bed - w - 5, 2.0):
            if all(
                x + w + margin <= a
                or x >= c + margin
                or y + d + margin <= b
                or y >= e + margin
                for a, b, c, e in rects
            ):
                return float(x), float(y)
    return None


class _Uid:
    def __init__(self, name: str) -> None:
        self.name, self.n = name, 0

    def __call__(self) -> uuid.UUID:
        self.n += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f"3d-cli/prep/{self.name}/{self.n}")


def _mesh_xml(oid: int, v: np.ndarray, f: np.ndarray, uid: _Uid) -> str:
    vs = "\n".join(
        f'     <vertex x="{a:.6f}" y="{b:.6f}" z="{c:.6f}"/>' for a, b, c in v
    )
    ts = "\n".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
    return (
        f'  <object id="{oid}" p:UUID="{uid()}" type="model">\n   <mesh>\n    <vertices>\n{vs}\n'
        f"    </vertices>\n    <triangles>\n{ts}\n    </triangles>\n   </mesh>\n  </object>"
    )


def _tr(t: np.ndarray) -> str:
    return f"1 0 0 0 1 0 0 0 1 {t[0]:.6f} {t[1]:.6f} {t[2]:.6f}"


def _mat4(t: np.ndarray) -> str:
    return f"1 0 0 {t[0]:.6f} 0 1 0 {t[1]:.6f} 0 0 1 {t[2]:.6f} 0 0 0 1"


def _mark(ds: list[str], idx: int, keys: Sequence[str]) -> None:
    ds[idx] = ";".join(sorted(set(filter(None, ds[idx].split(";"))) | set(keys)))


_RELS_OBJECTS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    f' <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="{REL_3DMODEL}"/>\n'
    "</Relationships>\n"
)


def _reference_settings(
    spec: ProjectSpec, printer: PrinterModel, presets: list[str]
) -> dict[str, Any]:
    cli = orca_cli.find_orca()
    if cli is None:
        raise MissingDependency(
            "Snapmaker Orca CLI",
            install=orca_cli.INSTALL_HINT,
            degrades="project_settings.config",
            command="prep",
        )
    res = orca_cli.find_resources(cli=cli)
    if res is None:
        raise MissingDependency(
            "Snapmaker Orca resources/profiles",
            install="extract the Snapmaker Orca AppImage to ~/Applications/snapmaker_orca/ or set ORCA_RES",
            degrades="project_settings.config cannot be generated from the vendor presets",
            command="prep",
        )
    bed = printer.bed / 2
    return orca_cli.reference_project_settings(
        cli,
        orca_cli.SystemPresets(res),
        vendor=printer.vendor,
        machine=printer.machine,
        process=spec.process_preset,
        filaments=presets,
        bed_centre=(bed, bed),
    )


# --------------------------------------------------------------------------- build


def build_project(
    spec: ProjectSpec,
    out_path: str | Path,
    *,
    base_settings: Mapping[str, Any] | None = None,
) -> ProjectManifest:
    """Write the project 3MF and `NAME.manifest.json` beside it.

    `base_settings` is a ready `project_settings.config` (as the CLI writes it for this printer, process and
    filaments); without it the CLI generates one from the vendor presets (seconds, needs the Orca install)."""
    if not _NAME_RE.fullmatch(spec.name):
        raise InvalidArgument(
            "name",
            spec.name,
            ["letters, digits, _ . - only (it is the file name)"],
            command="prep",
        )
    if spec.printer not in PRINTERS:
        raise InvalidArgument("printer", spec.printer, sorted(PRINTERS), command="prep")
    if not spec.parts:
        raise InvalidArgument(
            "parts", "[]", ["at least one colour part"], command="prep"
        )
    printer = PRINTERS[spec.printer]
    n = len(spec.filaments)
    if [f.slot for f in spec.filaments] != list(range(1, printer.extruders + 1)):
        raise InvalidArgument(
            "filaments",
            str([f.slot for f in spec.filaments]),
            [f"slots 1..{printer.extruders} in order (use a stub for an unused slot)"],
            command="prep",
        )
    keymap = _key_map(spec.filaments)
    out = Path(out_path)

    # 1. parts -> extruders, mixed rows
    mixes = _Mixes(n)
    resolved = [_resolve(p, keymap, n) for p in spec.parts]
    for p, r in zip(spec.parts, resolved, strict=True):
        if r.mix is not None:
            slots = set(r.mix)
            reasons = (
                []
                if len(slots) > 1
                else [f"uses one slot: assign slot {r.mix[0]} directly"]
            )
            reasons += check_mix_pattern(r.mix, spec.mix_layer_mm)
            if reasons:
                raise InvalidArgument(
                    f"part {p.name!r} mix",
                    str(p.assign.mix),
                    ["a legal Full Spectrum layer pattern"],
                    command="prep",
                    extra="; ".join(reasons),
                )
    extruders = [mixes.extruder(r) for r in resolved]
    arrays = [
        (np.asarray(p.mesh.vertices, float), np.asarray(p.mesh.faces, int))
        for p in spec.parts
    ]

    # 2. placement: lowest point on the bed, footprint centre on `position`
    all_v = np.vstack([v for v, _ in arrays])
    lo, hi = all_v.min(0), all_v.max(0)
    pos = np.array(spec.position or (printer.bed / 2, printer.bed / 2), float)
    shift = np.array(
        [pos[0] - (lo[0] + hi[0]) / 2, pos[1] - (lo[1] + hi[1]) / 2, -lo[2]]
    )
    arrays = [(v + shift, f) for v, f in arrays]
    lo, hi = lo + shift, hi + shift
    if (
        lo[0] < 1
        or lo[1] < 1
        or hi[0] > printer.bed - 1
        or hi[1] > printer.bed - 1
        or hi[2] > printer.bed_height
    ):
        raise InvalidArgument(
            "model size/position",
            f"{lo.round(1).tolist()}..{hi.round(1).tolist()}",
            [
                f"inside the {printer.bed:g} x {printer.bed:g} x {printer.bed_height:g} mm bed"
            ],
            command="prep",
        )

    # 3. layer profile (object 1)
    profile: list[float] | None = None
    if spec.layer_plan is not None:
        object_id, profile = _parse_profile(spec.layer_plan.profile_text)
        if object_id != 1 or abs(profile[-2] - float(hi[2])) > 1e-3:
            raise InvalidArgument(
                "layer_plan.profile_text",
                f"object_id={object_id}, top z={profile[-2]:.4f}",
                [
                    f"object_id=1 and a last z equal to the object height {hi[2]:.4f} mm (+-0.001)"
                ],
                command="prep",
                extra="the slicer would drop the profile silently (quirk layer-profile-top-mismatch)",
            )

    # 4. heads, tower
    heads: set[int] = set()
    for e in extruders:
        heads |= mixes.heads(e)
    use_tower = len(heads) > 1 if spec.prime_tower is None else spec.prime_tower
    presets = [spec.filament_preset or printer.default_filament] * n
    cfg: dict[str, Any] = (
        dict(base_settings)
        if base_settings is not None
        else _reference_settings(spec, printer, presets)
    )
    tower_xy = (0.0, 0.0)
    if use_tower:
        if spec.tower_xy is not None:
            tower_xy = spec.tower_xy
        else:
            ring = (
                float(cfg.get("prime_tower_brim_width", "5"))
                + float(cfg.get("wipe_tower_extra_rib_length", "8"))
                + 2.0
            )
            side = float(cfg.get("prime_tower_width", "30")) + 2 * ring
            spot = free_spot(
                [(lo[0], lo[1], hi[0], hi[1])], side, side + 10, printer.bed
            )
            if spot is None:
                raise InvalidArgument(
                    "prime tower",
                    f"{side:.0f} mm square",
                    ["free bed area beside the model"],
                    command="prep",
                )
            tower_xy = (spot[0] + ring, spot[1] + ring)

    # 5. project_settings
    ig = list(cfg.get("inherits_group") or [""] * (n + 2))
    ds = list(cfg.get("different_settings_to_system") or [""] * (n + 2))
    colours = [f.hex for f in spec.filaments]
    for i in range(n):
        ig[1 + i] = presets[i]
        ds[1 + i] = "filament_colour"
    cfg["filament_colour"] = colours
    cfg["extruder_colour"] = list(colours)
    process_cfg = spec.overrides.to_config()
    cfg.update(process_cfg)
    for key, setting in printer.machine_overlay.items():
        cur = cfg.get(key)
        cfg[key] = [setting] * len(cur) if isinstance(cur, list) else setting
    cfg["curr_bed_type"] = printer.bed_type
    if spec.brim_mm:
        cfg["brim_type"], cfg["brim_width"] = "outer_only", f"{spec.brim_mm:g}"
    else:
        cfg["brim_type"] = "no_brim"
    if mixes.patterns:
        cfg["mixed_filament_definitions"] = mixes.definitions()
    cfg["enable_prime_tower"] = "1" if use_tower else "0"
    if use_tower:
        cfg["wipe_tower_x"], cfg["wipe_tower_y"] = (
            [f"{tower_xy[0]:.1f}"],
            [f"{tower_xy[1]:.1f}"],
        )
    _mark(
        ds,
        0,
        [
            *process_cfg,
            "curr_bed_type",
            "brim_type",
            "brim_width",
            "enable_prime_tower",
        ],
    )
    _mark(ds, -1, list(printer.machine_overlay))
    cfg["inherits_group"], cfg["different_settings_to_system"] = ig, ds

    # 6. XML
    uid = _Uid(spec.name)
    oc = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
    sub: list[str] = []
    comps: list[str] = []
    part_cfg: list[str] = []
    volumes = [float(p.mesh.volume) for p in spec.parts]
    biggest = max(range(len(spec.parts)), key=lambda i: volumes[i])
    for i, (part, (v, f), r, ext) in enumerate(
        zip(spec.parts, arrays, resolved, extruders, strict=True)
    ):
        pc = (v.min(0) + v.max(0)) / 2
        pid = i + 1
        sub.append(_mesh_xml(pid, v - pc, f, uid))
        off = pc - oc
        comps.append(
            f'    <component p:path="/3D/Objects/object_1.model" objectid="{pid}" p:UUID="{uid()}" transform="{_tr(off)}"/>'
        )
        label = f"mix {r.mix_src}" if r.mix is not None else f"slot {r.slot}"
        part_cfg.append(
            f'    <part id="{pid}" subtype="normal_part">\n'
            f'      <metadata key="name" value={quoteattr(part.name + " · " + label)}/>\n'
            f'      <metadata key="matrix" value="{_mat4(off)}"/>\n'
            f'      <metadata key="source_file" value={quoteattr(spec.name)}/>\n'
            '      <metadata key="source_object_id" value="0"/>\n'
            f'      <metadata key="source_volume_id" value="{i}"/>\n'
            f'      <metadata key="source_offset_x" value="{off[0]:.6f}"/>\n'
            f'      <metadata key="source_offset_y" value="{off[1]:.6f}"/>\n'
            f'      <metadata key="source_offset_z" value="{off[2]:.6f}"/>\n'
            f'      <metadata key="extruder" value="{ext}"/>\n'
            '      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
            "    </part>"
        )
    obj_id = 1000
    resources = (
        f'  <object id="{obj_id}" p:UUID="{uid()}" type="model">\n   <components>\n'
        + "\n".join(comps)
        + "\n   </components>\n  </object>"
    )
    main = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {_NS}>\n'
        f' <metadata name="Application">Snapmaker_Orca-{spec.app_version}</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        f' <metadata name="Title">{spec.name}</metadata>\n <resources>\n{resources}\n </resources>\n'
        f' <build p:UUID="{uid()}">\n  <item objectid="{obj_id}" p:UUID="{uid()}" transform="{_tr(oc)}" printable="1"/>\n </build>\n</model>\n'
    )
    sub_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {_NS}>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
        + "\n".join(sub)
        + "\n </resources>\n <build/>\n</model>\n"
    )
    name_attr = quoteattr(spec.name)
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        f'  <object id="{obj_id}">\n    <metadata key="name" value={name_attr}/>\n'
        f'    <metadata key="extruder" value="{extruders[biggest]}"/>\n'
        + "\n".join(part_cfg)
        + "\n  </object>\n"
        f'  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value={name_attr}/>\n'
        '    <metadata key="locked" value="false"/>\n    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
        f'    <metadata key="filament_maps" value="{" ".join("1" for _ in range(n))}"/>\n'
        f'    <model_instance>\n      <metadata key="object_id" value="{obj_id}"/>\n      <metadata key="instance_id" value="0"/>\n'
        '      <metadata key="identify_id" value="2000"/>\n    </model_instance>\n  </plate>\n  <assemble>\n  </assemble>\n</config>\n'
    )
    bbox2 = [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])]
    plate_json = {
        "bbox_all": bbox2,
        "bbox_objects": [
            {
                "area": float((hi[0] - lo[0]) * (hi[1] - lo[1])),
                "bbox": bbox2,
                "id": obj_id,
                "layer_height": float(spec.overrides.layer_height or 0.2),
                "name": spec.name,
            }
        ],
        "bed_type": cfg["curr_bed_type"],
        "filament_colors": colours,
        "filament_ids": list(range(n)),
        "first_extruder": 1,
        "is_seq_print": False,
        "nozzle_diameter": 0.4,
        "version": 2,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types_xml())
        z.writestr("_rels/.rels", _root_rels_xml())
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/_rels/3dmodel.model.rels", _RELS_OBJECTS)
        z.writestr("3D/Objects/object_1.model", sub_xml)
        z.writestr("Metadata/model_settings.config", model_settings)
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps(cfg, indent=4, ensure_ascii=False),
        )
        z.writestr("Metadata/plate_1.json", json.dumps(plate_json, ensure_ascii=False))
        if spec.layer_plan is not None:
            z.writestr(
                "Metadata/layer_heights_profile.txt", spec.layer_plan.profile_text
            )

    # 7. manifest
    mins, maxs = cfg.get("min_layer_height"), cfg.get("max_layer_height")
    band = (
        [max(float(x) for x in mins), min(float(x) for x in maxs)]
        if isinstance(mins, list) and isinstance(maxs, list) and mins and maxs
        else None
    )
    manifest_path = out.with_name(out.stem + ".manifest.json")
    data: dict[str, Any] = {
        "file": str(out),
        "name": spec.name,
        "printer": spec.printer,
        "process_preset": spec.process_preset,
        "filament_presets": presets,
        "filaments": [
            {
                "slot": f.slot,
                "key": f.key,
                "name": f.name,
                "hex": f.hex,
                "tool": f.slot - 1,
            }
            for f in spec.filaments
        ],
        "object_bbox": [lo.round(3).tolist(), hi.round(3).tolist()],
        "shift": shift.round(4).tolist(),
        "brim": float(spec.brim_mm),
        "prime_tower": bool(use_tower),
        "tower_xy": list(tower_xy),
        "heads_slots": sorted(heads),
        "mix_rows": [
            {
                "pattern": p,
                "extruder": n + 1 + i,
                "source": next(r.mix_src for r in resolved if r.mix == p),
            }
            for i, p in enumerate(mixes.patterns)
        ],
        "mixed_filament_definitions": mixes.definitions(),
        "parts": [
            {
                "name": part.name,
                "assign": {"slot": r.slot}
                if r.mix is None
                else {"mix": r.mix, "mix_src": r.mix_src},
                "extruder": ext,
                "tools": sorted(h - 1 for h in mixes.heads(ext)),
                "volume_mm3": round(vol, 2),
                "bbox": [v.min(0).round(3).tolist(), v.max(0).round(3).tolist()],
                "z_range": [
                    round(float(v[:, 2].min()), 3),
                    round(float(v[:, 2].max()), 3),
                ],
            }
            for part, (v, _), r, ext, vol in zip(
                spec.parts, arrays, resolved, extruders, volumes, strict=True
            )
        ],
        "process": process_cfg,
        "layer_profile": None
        if profile is None
        else {
            "points": len(profile) // 2,
            "top_mm": profile[-2],
            "min_h": min(profile[1::2]),
            "max_h": max(profile[1::2]),
        },
        "gates": {
            "wall_loops": spec.overrides.wall_loops,
            "sparse_infill_density": spec.overrides.sparse_infill_density,
            "supports": False,
            "layer_height_band": band,
            "bed": [printer.bed, printer.bed, printer.bed_height],
            "prime_tower": bool(use_tower),
            "mix_layer_cap_mm": MIX_LAYER_CAP_MM,
            "tool_of_slot": "slot - 1",
        },
    }
    manifest_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return ProjectManifest("PASS", str(out), str(manifest_path), data)
