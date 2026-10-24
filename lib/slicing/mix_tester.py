"""mix_tester.py — a small numbered plate of colour-mix swatches for one target colour (U1).

WHAT: `build()` takes the ranked mix candidates (slicing.mix_model) for one target and the loaded
  filaments, and writes four files next to each other:
  - NAME.3mf — a Snapmaker Orca project: the loaded spools as the physical filaments, one
    `mixed_filament_definitions` row per candidate (a manual layer pattern), one standing swatch
    per candidate on its virtual filament and one per pure loaded spool on its physical slot
    (the references for white balance and spool correction), and a flat seven-segment number in
    front of every swatch in the darkest loaded spool;
  - NAME.mix.json — the manifest (schema printops.mix_tester/1): target, slots, candidates with
    their pattern, weights, prediction, ΔE, bed boxes of swatch and label, and the keys of the
    colour calibration kinds (`mix_color` mix key, `filament_color` filament id) when the
    Spoolman filament ids are known;
  - NAME.png — the swatch sheet: per number, the target next to the predicted colour.
  - NAME.card.png — the white card to print at 100 % and lay the printed swatches on, face up, for
    the readout photo (#416): fiducials, a QR naming the manifest (`id`), white paper regions and a
    numbered frame per swatch; its layout is the manifest's `readout` (slicing/mix_card.py).
  Project settings come from the installed Snapmaker Orca's U1 system presets (the CLI exports a
  probe project, so every key is as the slicer writes it) or from a given project 3MF.
WHY: epic #410 / issue #413: a mix chosen from predictions alone came out ΔE 16.6 from the
  mockup (tg#22816); a tester of a few grams printed first shows the real colours, and the
  manifest lets the readout (#416) find each swatch on a photo.
INVARIANTS: stdlib only; deterministic output for the same inputs (no UUIDs, no timestamps in
  the 3MF); a swatch stands with its wide face vertical, so `3d mix check` passes the plate;
  mixed rows use component a = 1, b = 2, so every pattern digit is the physical filament itself
  (Snapmaker Orca MixedFilament.cpp physical_filament_from_token, cycle mode).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printops.color import parse_hex, srgb_to_lab
from slicing import mix_card
from slicing.mix_model import Candidate
from slicing.mix_target import validate_source

SCHEMA = "printops.mix_tester/1"
VENDOR = "Snapmaker"
MACHINE = "Snapmaker U1 (0.4 nozzle)"
FILAMENT = "Snapmaker PLA Basic @U1"
PROCESS = {
    0.08: "0.08mm Standard @Snapmaker U1 (0.4 nozzle)",
    0.12: "0.12mm Standard @Snapmaker U1 (0.4 nozzle)",
    0.16: "0.16mm Standard @Snapmaker U1 (0.4 nozzle)",
    0.2: "0.20mm Standard @Snapmaker U1 (0.4 nozzle)",
}
BED = (0.5, 0.5, 270.5, 270.5)  # U1 printable_area
BED_TYPE = "Textured PEI Plate"  # the U1's own sheet (fleet plate textured_pei)
U1_HEADS = 4  # the U1 is a four-head toolchanger: the probe project loads one filament per head
PROBE_TIMEOUT_S = 600

SWATCH_W = 20.0  # the face the camera sees (X) …
SWATCH_D = 3.0  # … its thickness (Y)
DIGIT_W, DIGIT_H, STROKE, DIGIT_GAP = 5.0, 8.0, 1.0, 1.5
LABEL_T = 0.6  # label thickness (mm, flat on the bed)
LABEL_GAP = 2.0  # swatch front face → label
PITCH_X, PITCH_Y = 26.0, 18.0
COLUMNS = 6
TOWER_CLEAR = 6.0  # prime tower brim + ribs keep this far from every object

# seven segments, cell (0, 0)–(w, h): a top, b upper right, c lower right, d bottom, e lower left,
# f upper left, g middle
_DIGITS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


class MixTesterError(ValueError):
    """The tester cannot be built (no slicer, bad base project, a slot the printer lacks)."""


@dataclass(frozen=True)
class LoadedSlot:
    slot: int  # 1-based physical filament / U1 head number
    hex: str
    name: str = ""
    spool_id: int | None = None
    filament_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "hex": self.hex,
            "name": self.name,
            "spool_id": self.spool_id,
            "filament_id": self.filament_id,
        }


@dataclass
class Swatch:
    number: int
    extruder: (
        int  # project filament (physical slot, or the candidate's virtual filament)
    )
    hex: str  # predicted (candidate) or spool (reference) colour
    candidate: Candidate | None = None
    slot: LoadedSlot | None = None
    swatch_box: Rect = (0.0, 0.0, 0.0, 0.0)
    label_box: Rect = (0.0, 0.0, 0.0, 0.0)
    label_rects: list[Rect] = field(default_factory=list)  # label-local


# -- geometry ---------------------------------------------------------------------------------


def digit_rects(text: str) -> list[Rect]:
    """Seven-segment strokes of `text`, origin at its lower-left corner (mm)."""
    t, w, h = STROKE, DIGIT_W, DIGIT_H
    seg = {
        "a": (t, h - t, w - t, h),
        "b": (w - t, h / 2 + t / 2, w, h),
        "c": (w - t, 0.0, w, h / 2 - t / 2),
        "d": (t, 0.0, w - t, t),
        "e": (0.0, 0.0, t, h / 2 - t / 2),
        "f": (0.0, h / 2 + t / 2, t, h),
        "g": (t, h / 2 - t / 2, w - t, h / 2 + t / 2),
    }
    out = []
    for i, ch in enumerate(text):
        x0 = i * (w + DIGIT_GAP)
        for s in _DIGITS[ch]:
            a, b, c, d = seg[s]
            out.append((x0 + a, b, x0 + c, d))
    return out


def text_width(text: str) -> float:
    return len(text) * DIGIT_W + (len(text) - 1) * DIGIT_GAP


Mesh = tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]


def boxes_mesh(
    boxes: Sequence[tuple[float, float, float, float, float, float]],
) -> Mesh:
    """Closed boxes (x0, y0, z0, x1, y1, z1) as one triangle mesh, outward winding."""
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    for x0, y0, z0, x1, y1, z1 in boxes:
        k = len(verts)
        verts += [
            (x0, y0, z0),
            (x1, y0, z0),
            (x1, y1, z0),
            (x0, y1, z0),
            (x0, y0, z1),
            (x1, y0, z1),
            (x1, y1, z1),
            (x0, y1, z1),
        ]
        for a, b, c in (
            (0, 2, 1),
            (0, 3, 2),
            (4, 5, 6),
            (4, 6, 7),
            (0, 1, 5),
            (0, 5, 4),
            (1, 2, 6),
            (1, 6, 5),
            (2, 3, 7),
            (2, 7, 6),
            (3, 0, 4),
            (3, 4, 7),
        ):
            tris.append((k + a, k + b, k + c))
    return verts, tris


def layout(swatches: list[Swatch]) -> None:
    """Place swatches in rows of COLUMNS, labels in front (−Y), centred on the bed."""
    rows = (len(swatches) + COLUMNS - 1) // COLUMNS
    cols = min(COLUMNS, len(swatches))
    cx0 = (BED[0] + BED[2]) / 2 - (cols - 1) * PITCH_X / 2
    cy0 = (BED[1] + BED[3]) / 2 + (rows - 1) * PITCH_Y / 2
    for i, s in enumerate(swatches):
        cx = cx0 + (i % COLUMNS) * PITCH_X
        cy = cy0 - (i // COLUMNS) * PITCH_Y
        s.swatch_box = (
            cx - SWATCH_W / 2,
            cy - SWATCH_D / 2,
            cx + SWATCH_W / 2,
            cy + SWATCH_D / 2,
        )
        text = str(s.number)
        w = text_width(text)
        y1 = cy - SWATCH_D / 2 - LABEL_GAP
        s.label_box = (cx - w / 2, y1 - DIGIT_H, cx + w / 2, y1)
        s.label_rects = digit_rects(text)


def tower_spot(objects: list[Rect], size: float) -> tuple[float, float]:
    """Lower-left corner of a free size×size square on the bed (rows from the back, left first)."""
    y = BED[3] - size - 5
    while y >= BED[1] + 5:
        x = BED[0] + 5
        while x <= BED[2] - size - 5:
            if all(
                x + size + TOWER_CLEAR <= a
                or x >= c + TOWER_CLEAR
                or y + size + TOWER_CLEAR <= b
                or y >= d + TOWER_CLEAR
                for a, b, c, d in objects
            ):
                return x, y
            x += 2.0
        y -= 2.0
    raise MixTesterError("no room on the bed for the prime tower: lower --count")


# -- project settings -------------------------------------------------------------------------


def find_slicer() -> str:
    """The Snapmaker Orca binary: $SNAPMAKER_ORCA, the extracted Linux AppImage, the macOS app."""
    env = os.environ.get("SNAPMAKER_ORCA")
    cands = [
        env or "",
        str(Path.home() / "Applications" / "snapmaker_orca" / "AppRun"),
        "/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker Orca",
        shutil.which("snapmaker-orca") or "",
    ]
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    raise MixTesterError(
        "Snapmaker Orca not found: set SNAPMAKER_ORCA to its binary (extracted AppImage AppRun), "
        "or pass --base-3mf with a U1 project saved by it"
    )


def slicer_version(slicer: str) -> str:
    """The build number the CLI accepts in a project's Application tag (it refuses newer ones)."""
    try:
        # the CLI writes result.json into its working directory, even for --help
        with tempfile.TemporaryDirectory(prefix="3dmixtester.") as cwd:
            r = subprocess.run(
                [slicer, "--help"], capture_output=True, text=True, timeout=120, cwd=cwd
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MixTesterError(f"Snapmaker Orca {slicer} did not run: {e}") from e
    m = re.search(r"Snapmaker_Orca-([0-9.]+)", r.stdout + r.stderr)
    if m is None:
        raise MixTesterError(f"{slicer} --help names no Snapmaker_Orca version")
    return m.group(1)


def probe_settings(slicer: str, layer: float, n_filaments: int) -> dict[str, Any]:
    """project_settings.config exactly as Snapmaker Orca writes it for the U1 system presets."""
    from slicing.calibration_gen import GenError, ProfileIndex, profile_roots

    process = next((name for h, name in PROCESS.items() if abs(h - layer) < 1e-9), None)
    if process is None:
        raise MixTesterError(
            f"no U1 process preset for layer {layer:g} mm (known: {', '.join(f'{h:g}' for h in PROCESS)}); "
            "pass --base-3mf"
        )
    idx = ProfileIndex(profile_roots(slicer))
    tmp = Path(tempfile.mkdtemp(prefix="3dmixtester."))
    try:
        files = []
        for kind, name in (
            ("machine", MACHINE),
            ("process", process),
            ("filament", FILAMENT),
        ):
            try:
                data = idx.flat(VENDOR, kind, name)
            except GenError as e:
                raise MixTesterError(str(e)) from e
            data = dict(data, name=name, **{"from": "system", "instantiation": "true"})
            path = tmp / f"{kind}.json"
            path.write_text(json.dumps(data, indent=1))
            files.append(path)
        cx, cy = (BED[0] + BED[2]) / 2, (BED[1] + BED[3]) / 2
        stl = tmp / "probe.stl"
        verts, tris = boxes_mesh([(cx - 5, cy - 5, 0, cx + 5, cy + 5, 2)])
        stl.write_text(
            "solid probe\n"
            + "".join(
                "facet normal 0 0 0\nouter loop\n"
                + "".join(
                    f"vertex {verts[i][0]} {verts[i][1]} {verts[i][2]}\n" for i in t
                )
                + "endloop\nendfacet\n"
                for t in tris
            )
            + "endsolid probe\n"
        )
        cmd = [
            slicer,
            "--datadir",
            str(tmp / "datadir"),
            "--load-settings",
            f"{files[0]};{files[1]}",
            "--load-filaments",
            ";".join([str(files[2])] * n_filaments),
            "--outputdir",
            str(tmp),
            "--export-3mf",
            "probe.3mf",
            str(stl),
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise MixTesterError(f"Snapmaker Orca {slicer} did not run: {e}") from e
        out = tmp / "probe.3mf"
        if not out.is_file():
            raise MixTesterError(
                f"Snapmaker Orca wrote no probe project (exit {r.returncode}):\n"
                + (r.stdout + r.stderr)[-2000:]
            )
        with zipfile.ZipFile(out) as zf:
            cfg: dict[str, Any] = json.loads(
                zf.read("Metadata/project_settings.config")
            )
        return cfg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_base(path: Path) -> tuple[dict[str, Any], str]:
    """(project settings, Application tag) of a project 3MF saved by Snapmaker Orca."""
    try:
        with zipfile.ZipFile(path) as zf:
            cfg = json.loads(zf.read("Metadata/project_settings.config"))
            with zf.open(
                "3D/3dmodel.model"
            ) as fh:  # the tag is in the header; meshes follow
                head = fh.read(8192).decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        raise MixTesterError(f"{path}: not a Snapmaker Orca project 3MF ({e})") from e
    if not isinstance(cfg, dict) or not physical_count(cfg):
        raise MixTesterError(f"{path}: project settings name no filaments")
    m = re.search(r'name="Application">([^<]*)<', head)
    return cfg, (m.group(1) if m else "")


def layer_height(cfg: dict[str, Any]) -> float:
    """The project's layer height (the base project's process decides what prints)."""
    raw = cfg.get("layer_height")
    try:
        value = float(_first(raw, math.nan))
    except (TypeError, ValueError) as e:
        raise MixTesterError(f"project layer_height {raw!r} is not a number") from e
    if not math.isfinite(value) or value <= 0:
        raise MixTesterError(f"project layer_height {raw!r} must be a positive number")
    return value


def physical_count(cfg: dict[str, Any]) -> int:
    """Physical filaments of a project: one filament preset each (`filament_settings_id`)."""
    ids = cfg.get("filament_settings_id")
    return max(
        len(ids) if isinstance(ids, list) else 0,
        len(cfg.get("filament_colour") or []),
    )


def _add_keys(ds: list[str], i: int, keys: set[str]) -> None:
    ds[i] = ";".join(sorted(set(filter(None, ds[i].split(";"))) | keys))


def project_settings(
    base: dict[str, Any],
    slots: Sequence[LoadedSlot],
    rows: Sequence[str],
    objects: list[Rect],
) -> dict[str, Any]:
    """The base settings with the loaded colours, the mixed rows and the prime tower placed."""
    cfg = dict(base)
    n = physical_count(cfg)
    colours = list(cfg.get("filament_colour") or [])
    # the CLI's export keeps one colour for filaments it loaded from one preset
    colours = (colours + [colours[-1] if colours else "#FFFFFF"] * n)[:n]
    for s in slots:
        if s.slot > n:
            raise MixTesterError(
                f"slot {s.slot}: the project has {n} physical filaments"
            )
        colours[s.slot - 1] = s.hex
    cfg["filament_colour"] = colours
    cfg["extruder_colour"] = list(colours)
    ds = list(cfg.get("different_settings_to_system") or [])
    # one entry for the process, one per physical filament, one for the machine
    ds += [""] * (n + 2 - len(ds))
    for s in slots:
        _add_keys(ds, s.slot, {"filament_colour"})
    cfg["mixed_filament_definitions"] = ";".join(rows)
    cfg["enable_support"] = "0"
    cfg["brim_type"] = "no_brim"
    cfg["curr_bed_type"] = BED_TYPE
    over = {
        "mixed_filament_definitions",
        "enable_support",
        "brim_type",
        "curr_bed_type",
    }
    tower = cfg.get("enable_prime_tower") in ("1", "true", True)
    if tower:
        ring = (
            float(_first(cfg.get("prime_tower_brim_width"), 5.0))
            + float(_first(cfg.get("wipe_tower_extra_rib_length"), 0.0))
            + 2.0
        )
        size = float(_first(cfg.get("prime_tower_width"), 30.0)) + 2 * ring
        x, y = tower_spot(objects, size)
        cfg["wipe_tower_x"] = [f"{x + ring:.1f}"]
        cfg["wipe_tower_y"] = [f"{y + ring:.1f}"]
    _add_keys(ds, 0, over)
    cfg["different_settings_to_system"] = ds
    return cfg


def _first(value: Any, default: float) -> Any:
    if isinstance(value, list):
        value = value[0] if value else None
    return default if value in (None, "") else value


def mixed_row(index: int, pattern: str) -> str:
    """One Snapmaker Orca mixed filament row (MixedFilamentManager::serialize_custom_entries):
    component_a, component_b = 1, 2 (cycle mode maps pattern digits 1/2 to a/b and the rest to
    slots, so every digit is the slot itself); enabled 1; custom 1; mix_b_percent 50 (unused
    by a manual pattern); pointillism 0; g/w = no gradient component ids/weights; m2 =
    distribution mode; z0 = no Local-Z sublayers; xa0/xb0 = no surface offsets; d0 = not
    deleted; o0 = not auto-created; u7xx = stable id clear of the GUI's own rows; cm1 = cycle
    UI mode; then the manual pattern."""
    return f"1,2,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u{701 + index},cm1,{pattern}"


# -- 3MF --------------------------------------------------------------------------------------

NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"'
)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _mesh_xml(oid: int, mesh: Mesh) -> str:
    vs = "".join(
        f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in mesh[0]
    )
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh[1])
    return (
        f'  <object id="{oid}" type="model"><mesh><vertices>{vs}</vertices>'
        f"<triangles>{ts}</triangles></mesh></object>"
    )


@dataclass
class _Obj:
    name: str
    extruder: int
    mesh: Mesh  # object-local, footprint centred on the origin, z from 0
    at: tuple[float, float]  # footprint centre on the bed
    footprint: Rect


def _objects(swatches: list[Swatch], height: float, label_slot: int) -> list[_Obj]:
    out = []
    for s in swatches:
        x0, y0, x1, y1 = s.swatch_box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        w, d = x1 - x0, y1 - y0
        out.append(
            _Obj(
                f"{s.number} swatch",
                s.extruder,
                boxes_mesh([(-w / 2, -d / 2, 0.0, w / 2, d / 2, height)]),
                (cx, cy),
                s.swatch_box,
            )
        )
        lx0, ly0, lx1, ly1 = s.label_box
        lcx, lcy = (lx0 + lx1) / 2, (ly0 + ly1) / 2
        ox, oy = lx0 - lcx, ly0 - lcy
        out.append(
            _Obj(
                f"{s.number} label",
                label_slot,
                boxes_mesh(
                    [
                        (a + ox, b + oy, 0.0, c + ox, e + oy, LABEL_T)
                        for a, b, c, e in s.label_rects
                    ]
                ),
                (lcx, lcy),
                s.label_box,
            )
        )
    return out


def write_3mf(
    path: Path, objs: list[_Obj], cfg: dict[str, Any], app: str, title: str
) -> None:
    sub = "3D/Objects/object_1.model"
    meshes, resources, build, settings, insts = [], [], [], [], []
    for i, o in enumerate(objs):
        pid, oid = i + 1, 1000 + i
        meshes.append(_mesh_xml(pid, o.mesh))
        resources.append(
            f'  <object id="{oid}" type="model"><components>'
            f'<component p:path="/{sub}" objectid="{pid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            "</components></object>"
        )
        build.append(
            f'  <item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {o.at[0]:.4f} {o.at[1]:.4f} 0" printable="1"/>'
        )
        settings.append(
            f'  <object id="{oid}">\n    <metadata key="name" value="{_esc(o.name)}"/>\n'
            f'    <metadata key="extruder" value="{o.extruder}"/>\n'
            f'    <part id="{pid}" subtype="normal_part">\n'
            f'      <metadata key="name" value="{_esc(o.name)}"/>\n'
            '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            f'      <metadata key="extruder" value="{o.extruder}"/>\n'
            "    </part>\n  </object>"
        )
        insts.append(
            f'    <model_instance>\n      <metadata key="object_id" value="{oid}"/>\n'
            '      <metadata key="instance_id" value="0"/>\n'
            f'      <metadata key="identify_id" value="{2000 + i}"/>\n    </model_instance>'
        )
    head = f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n'
    app_meta = f' <metadata name="Application">{_esc(app)}</metadata>\n' if app else ""
    main = (
        head
        + app_meta
        + ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        + f' <metadata name="Title">{_esc(title)}</metadata>\n <resources>\n'
        + "\n".join(resources)
        + "\n </resources>\n <build>\n"
        + "\n".join(build)
        + "\n </build>\n</model>\n"
    )
    sub_xml = (
        head
        + ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
        + "\n".join(meshes)
        + "\n </resources>\n <build/>\n</model>\n"
    )
    n_fil = len(cfg["filament_colour"])
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "\n".join(settings)
        + '\n  <plate>\n    <metadata key="plater_id" value="1"/>\n'
        + f'    <metadata key="plater_name" value="{_esc(title)}"/>\n'
        + '    <metadata key="locked" value="false"/>\n'
        + '    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
        + f'    <metadata key="filament_maps" value="{" ".join("1" for _ in range(n_fil))}"/>\n'
        + "\n".join(insts)
        + "\n  </plate>\n  <assemble>\n  </assemble>\n</config>\n"
    )
    rels_t = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
    rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n'
        ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n'
    )
    xs = [x for o in objs for x in (o.footprint[0], o.footprint[2])]
    ys = [y for o in objs for y in (o.footprint[1], o.footprint[3])]
    plate_json = {
        "bbox_all": [min(xs), min(ys), max(xs), max(ys)],
        "bbox_objects": [
            {
                "area": (o.footprint[2] - o.footprint[0])
                * (o.footprint[3] - o.footprint[1]),
                "bbox": list(o.footprint),
                "id": 1000 + i,
                "layer_height": float(_first(cfg.get("layer_height"), 0.12)),
                "name": o.name,
            }
            for i, o in enumerate(objs)
        ],
        "bed_type": cfg.get("curr_bed_type", "Textured PEI Plate"),
        "filament_colors": list(cfg["filament_colour"]),
        "filament_ids": list(range(n_fil)),
        "first_extruder": 1,
        "is_seq_print": False,
        "nozzle_diameter": 0.4,
        "version": 2,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr(
            "_rels/.rels",
            f'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="{rels_ns}">\n'
            f' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{rels_t}"/>\n</Relationships>\n',
        )
        zf.writestr("3D/3dmodel.model", main)
        zf.writestr(
            "3D/_rels/3dmodel.model.rels",
            f'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="{rels_ns}">\n'
            f' <Relationship Target="/{sub}" Id="rel-1" Type="{rels_t}"/>\n</Relationships>\n',
        )
        zf.writestr(sub, sub_xml)
        zf.writestr("Metadata/model_settings.config", model_settings)
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps(cfg, indent=4, ensure_ascii=False),
        )
        zf.writestr("Metadata/plate_1.json", json.dumps(plate_json))


# -- swatch sheet PNG -------------------------------------------------------------------------


def _png(width: int, height: int, rgb: bytearray) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    stride = width * 3
    raw = b"".join(
        b"\x00" + bytes(rgb[y * stride : (y + 1) * stride]) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def sheet_png(target: str, swatches: list[Swatch]) -> bytes:
    """Per row: the number, the target, the predicted (or spool) colour."""
    scale, row, pad = 4, 64, 8
    width, height = 3 * row + 2 * pad + 60, pad + len(swatches) * (row + pad)
    img = bytearray(b"\xff" * (width * height * 3))

    def fill(x0: int, y0: int, x1: int, y1: int, hex_: str) -> None:
        c = bytes(int(hex_[i : i + 2], 16) for i in (1, 3, 5))
        for y in range(max(0, y0), min(height, y1)):
            start = (y * width + max(0, x0)) * 3
            n = max(0, min(width, x1) - max(0, x0))
            img[start : start + n * 3] = c * n

    for k, s in enumerate(swatches):
        top = pad + k * (row + pad)
        for a, b, c, d in digit_rects(str(s.number)):
            # bed digits: y up; image rows: y down
            fill(
                pad + int(a * scale),
                top + int((DIGIT_H - d) * scale),
                pad + int(c * scale),
                top + int((DIGIT_H - b) * scale),
                "#000000",
            )
        x = pad + 60
        fill(x, top, x + row, top + row, target)
        fill(x + row + pad, top, x + 2 * row + pad, top + row, s.hex)
    return _png(width, height, img)


# -- build ------------------------------------------------------------------------------------


def darkest(slots: Sequence[LoadedSlot]) -> LoadedSlot:
    return min(slots, key=lambda s: (srgb_to_lab(s.hex)[0], s.slot))


def mix_key_of(c: Candidate, slots: Sequence[LoadedSlot], layer: float) -> str | None:
    from printops.mix_key import MixKeyError, cycle_key

    fid = {s.slot: s.filament_id for s in slots}
    ids = [fid.get(int(d)) for d in c.pattern]
    if any(i is None for i in ids):
        return None
    try:
        return cycle_key(layer, [i for i in ids if i is not None])
    except MixKeyError as e:
        raise MixTesterError(str(e)) from e


def build(
    *,
    target: str,
    slots: Sequence[LoadedSlot],
    candidates: Sequence[Candidate],
    out: Path,
    layer: float,
    height: float,
    base_settings: dict[str, Any],
    app: str,
    source: str,
    target_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write OUT.3mf, OUT.mix.json, OUT.png; return the manifest."""
    target_hex = parse_hex(target)
    if target_hex is None:
        raise MixTesterError(f"{target!r} is not a colour")
    if not slots:
        raise MixTesterError("no loaded slots")
    if not 0 < layer <= 1 or abs(round(layer, 4) - layer) > 1e-9:
        raise MixTesterError(
            f"layer {layer!r} must be in (0, 1] mm with at most 4 decimals"
        )
    n_phys = physical_count(base_settings)
    mixes = list(candidates)
    if any(c.cycle < 2 for c in mixes):
        raise MixTesterError(
            "candidates must be layer mixes (the pure spools are references)"
        )
    swatches = [
        Swatch(i + 1, n_phys + 1 + i, c.hex, candidate=c) for i, c in enumerate(mixes)
    ]
    for ref in sorted(slots, key=lambda r: r.slot):
        swatches.append(Swatch(len(swatches) + 1, ref.slot, ref.hex, slot=ref))
    layout(swatches)
    try:
        mix_card.layout(len(swatches), height, "0" * 12)  # before any file is written
    except mix_card.MixCardError as e:
        raise MixTesterError(str(e)) from e
    label = darkest(slots)
    objs = _objects(swatches, height, label.slot)
    rows = [mixed_row(i, c.pattern) for i, c in enumerate(mixes)]
    cfg = project_settings(base_settings, slots, rows, [o.footprint for o in objs])
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        "3mf": out.with_name(out.name + ".3mf"),
        "manifest": out.with_name(out.name + ".mix.json"),
        "png": out.with_name(out.name + ".png"),
        "card": out.with_name(out.name + ".card.png"),
    }
    write_3mf(paths["3mf"], objs, cfg, app, f"mix tester {target_hex}")
    paths["png"].write_bytes(sheet_png(target_hex, swatches))

    def box(r: Rect) -> list[float]:
        return [round(v, 2) for v in r]

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "target": target_hex,
        "layer_height": layer,
        "swatch_height": height,
        "settings_source": source,
        "label_slot": label.slot,
        "slots": [r.to_dict() for r in sorted(slots, key=lambda r: r.slot)],
        "candidates": [],
        "references": [],
        "files": {k: p.name for k, p in paths.items()},
    }
    if target_source is not None:
        manifest["target_source"] = target_source
    for sw in swatches:
        if sw.candidate is not None:
            c = sw.candidate
            manifest["candidates"].append(
                {
                    "number": sw.number,
                    "filament": sw.extruder,
                    **c.to_dict(),
                    "swatch_box": box(sw.swatch_box),
                    "label_box": box(sw.label_box),
                    "calibration": {
                        "kind": "mix_color",
                        "mix": mix_key_of(c, slots, layer),
                    },
                }
            )
        else:
            assert sw.slot is not None
            manifest["references"].append(
                {
                    "number": sw.number,
                    "slot": sw.slot.slot,
                    "hex": sw.hex,
                    "swatch_box": box(sw.swatch_box),
                    "label_box": box(sw.label_box),
                    "calibration": {
                        "kind": "filament_color",
                        "filament_id": sw.slot.filament_id,
                    },
                }
            )
    manifest["id"] = mix_card.manifest_id(manifest)
    manifest["readout"] = mix_card.layout(len(swatches), height, manifest["id"])
    paths["card"].write_bytes(mix_card.render(manifest["readout"]))
    paths["manifest"].write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    return manifest


def validate_manifest(m: Any) -> list[str]:
    """Problems with a printops.mix_tester/1 manifest ([] = valid)."""
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    if m.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    if parse_hex(m.get("target")) is None:
        errs.append("target is not a colour")
    if "target_source" in m:
        errs.extend(validate_source(m["target_source"]))
    errs.extend(
        f"{key} must be a list"
        for key in ("slots", "candidates", "references")
        if not isinstance(m.get(key), list)
    )
    if errs:
        return errs
    entries = m["candidates"] + m["references"]
    if not all(isinstance(e, dict) for e in entries):
        return [*errs, "every candidate and reference must be an object"]
    numbers = [e.get("number") for e in entries]
    if not all(
        isinstance(n, int) and not isinstance(n, bool) for n in numbers
    ) or sorted(numbers) != list(range(1, len(numbers) + 1)):
        errs.append(f"swatch numbers must be 1..{len(numbers)}, got {numbers}")
    for c in m["candidates"]:
        if not re.fullmatch(r"[1-9]+", str(c.get("pattern", ""))):
            errs.append(f"candidate {c.get('number')}: pattern must be slot digits")
        if parse_hex(c.get("hex")) is None:
            errs.append(f"candidate {c.get('number')}: hex is not a colour")
    for e in entries:
        for k in ("swatch_box", "label_box"):
            b = e.get(k)
            if not (
                isinstance(b, list)
                and len(b) == 4
                and all(isinstance(v, (int, float)) for v in b)
                and b[0] < b[2]
                and b[1] < b[3]
            ):
                errs.append(f"swatch {e.get('number')}: {k} must be [x0, y0, x1, y1]")
    if "readout" in m:
        mid = m.get("id")
        if not isinstance(mid, str) or mid != mix_card.manifest_id(m):
            errs.append("id is not the id of this manifest's content")
        else:
            errs.extend(mix_card.validate_readout(m["readout"], len(entries), mid))
    return errs
