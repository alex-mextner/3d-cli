"""calibration_plate.py — calibration plates: many tests on one bed, each object with its own parameters.

WHAT: a Plate declares PlateObjects (meshes sliced by Orca) and CustomBlocks (hand-written G-code:
  PA lines, a max-volumetric-speed strip, bed labels). build_plate() slices the plate in up to two
  Orca runs and splices one G-code:
    START_PRINT → custom blocks → "layer" objects (print_sequence by layer: short objects at the
    front) → lift → "sequential" objects (by object: towers, low → high) → END_PRINT
  then applies the object hooks and writes NAME.json next to the G-code: object → parameters,
  bbox, Z bands, labelled items, how to read it — for reading the result from a photo later.
OBJECT HOOKS (the per-object parameter contract): every value a test changes is global in the
  firmware (M221, M220, PA, ACCEL, temperature, retraction), so it is set when the nozzle enters
  the object and undone when it leaves:
    before  once, before the object's first EXCLUDE_OBJECT_START
    enter   after every EXCLUDE_OBJECT_START (every layer in by-layer mode)
    band    the Band whose [z0, z1) holds the current layer: its gcode after START and again when
            a ;Z: marker inside the object moves into the next band; its transform rewrites the
            object's own lines (between START and END only, never travels between objects)
    leave   before every EXCLUDE_OBJECT_END (restore: M221 S100, M220 S100, stock PA, ACCEL …)
    after   once, after the object's last EXCLUDE_OBJECT_END
  Height-parameterised objects (temperature, retraction, speed by Z) go to the sequential phase:
  there one object prints all its layers alone, so a temperature change never makes other
  objects wait, and `before`/`after` can use M109.
K1 GANTRY: sequential objects must stay under extruder_clearance_height_to_rod and apart by
  extruder_clearance_radius; Orca's sequential slice checks both and fails otherwise. The layer
  objects are kept that radius away (in Y) from the sequential row, so the carriage never sweeps
  over a finished tower while printing low objects, and the splice lifts above everything before
  travelling to the first tower.
WHY: one 30–40 min plate replaces several separate calibration prints (express calibration of a
  new spool, docs/printops/express-calibration.md; issue #254 and its follow-up).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from slicing.calibration_gen import (
    Box,
    Extrusion,
    Generated,
    GenError,
    Presets,
    ProfileIndex,
    _apply_overrides,
    _filament_overrides,
    _first,
    _path_gcode,
    boxes_stl,
    digit_paths,
    filament_area,
    finish,
    first_layer_extrusion,
    insert_before_end,
    profile_roots,
    publish,
    require_relative_e,
    slice_models,
    COMBO_LABEL,
    COMBO_PA_VALUES,
    text_width,
    Z_MOVE_F,
    TRAVEL_F,
    presets_for,
)

Phase = Literal["layer", "sequential"]
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_START = re.compile(r"^EXCLUDE_OBJECT_START NAME=([A-Za-z0-9_]+)\.stl\S*")
_END = re.compile(r"^EXCLUDE_OBJECT_END NAME=([A-Za-z0-9_]+)\.stl\S*")
_ZMARK = re.compile(r"^;Z:([\d.]+)\s*$")
_EST = re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M)
SPLICE_LIFT = (
    5.0  # mm above the tallest layer object before travelling to the first tower
)
SPLICE_MARK = "; 3d calibrate plate: layer objects done; lift and go to the first tower"


@dataclass(frozen=True)
class Band:
    """A Z range of one object (mm above the bed) with its own setting."""

    z0: float
    z1: float
    gcode: tuple[str, ...] = ()
    params: Mapping[str, float | str] = field(default_factory=dict)
    transform: Callable[[str], str] | None = None


@dataclass(frozen=True)
class Item:
    """A separately read, labelled piece: a tile, a PA line, a strip segment."""

    label: str
    bbox: Box
    params: Mapping[str, float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlateObject:
    name: str  # [a-z][a-z0-9_]*: the STL name and the EXCLUDE_OBJECT name prefix
    kind: str  # calibration kind (fleet/calibrations.yaml) or a test name (bridge, overhang)
    boxes: tuple[Box, ...] = ()
    stl: str = ""  # a mesh instead of boxes (prism_stl …)
    stl_bbox: Box | None = None  # required with stl
    phase: Phase = "layer"
    before: tuple[str, ...] = ()
    enter: tuple[str, ...] = ()
    leave: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    bands: tuple[Band, ...] = ()
    params: Mapping[str, float | str] = field(default_factory=dict)
    items: tuple[Item, ...] = ()
    read: str = ""
    record: str = ""
    bed_label: tuple[str, float, float] | None = (
        None  # text, centre x, bottom y: printed on the bed
    )

    def mesh(self) -> str:
        return self.stl or boxes_stl(list(self.boxes))

    def bbox(self) -> Box:
        if self.stl:
            if self.stl_bbox is None:
                raise GenError(f"{self.name}: an stl object needs stl_bbox")
            return self.stl_bbox
        return _union(self.boxes)

    def band_at(self, z: float) -> Band | None:
        return next((b for b in self.bands if b.z0 <= z + 1e-6 < b.z1), None)


@dataclass(frozen=True)
class CustomBlock:
    """Hand-written G-code printed right after START_PRINT, before the sliced objects. The
    lines start and end with the filament primed and the nozzle lifted (custom_lines())."""

    name: str
    kind: str
    lines: tuple[str, ...]
    items: tuple[Item, ...] = ()
    params: Mapping[str, float | str] = field(default_factory=dict)
    read: str = ""
    record: str = ""

    def bbox(self) -> Box:
        return _gcode_bbox(self.lines)


@dataclass(frozen=True)
class Plate:
    plate_id: str
    title: str
    objects: tuple[PlateObject, ...]
    custom: tuple[CustomBlock, ...] = ()
    layer_process: Mapping[str, Any] = field(default_factory=dict)
    layer_filament: Mapping[str, Any] = field(default_factory=dict)
    seq_process: Mapping[str, Any] = field(default_factory=dict)
    seq_filament: Mapping[str, Any] = field(default_factory=dict)
    restore: tuple[
        str, ...
    ] = ()  # before END_PRINT: undo anything a hook may have left set
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlateContext:
    """What a plate declaration needs to know about the printer and the filament."""

    p: Presets
    idx: ProfileIndex
    material: str
    color: str
    temp: int | None
    proc: dict[str, Any]  # flattened process preset
    fil: dict[str, Any]  # flattened filament preset with color/temp applied
    mach: dict[str, Any]
    ex: Extrusion  # first-layer line for hand-written G-code
    tests: frozenset[str] | None = None  # the plate's test ids to print (None: all)
    pa: tuple[float, ...] = (
        COMBO_PA_VALUES  # PA line values (material default or --pa-range)
    )

    def wants(self, test: str) -> bool:
        return self.tests is None or test in self.tests

    @property
    def base_temp(self) -> int:
        return int(_first(self.fil, "nozzle_temperature"))

    @property
    def accel(self) -> float:
        return _first(self.proc, "default_acceleration")

    @property
    def retract(self) -> float:
        return _first(self.mach, "retraction_length")

    def e_per_mm(self, width: float, height: float) -> float:
        return (
            width
            * height
            * _first(self.fil, "filament_flow_ratio")
            / filament_area(self.fil)
        )

    def clearance(self) -> tuple[float, float]:
        """(height to the gantry rod, radius) for sequential printing."""
        return (
            _first(self.mach, "extruder_clearance_height_to_rod"),
            _first(self.mach, "extruder_clearance_radius"),
        )


def make_context(
    model: str,
    slicer: str,
    material: str,
    color: str,
    temp: int | None,
    tests: frozenset[str] | None = None,
    pa: tuple[float, ...] = COMBO_PA_VALUES,
) -> PlateContext:
    p = presets_for(model)
    idx = ProfileIndex(profile_roots(slicer))
    if material not in p.filament:
        raise GenError(
            f"no filament preset for {material!r} on {p.machine} (known: {', '.join(p.filament)})"
        )
    proc = idx.flat(p.vendor, "process", p.process)
    fil = _apply_overrides(
        idx.flat(p.vendor, "filament", p.filament[material]),
        _filament_overrides(color, temp),
    )
    mach = idx.flat(p.vendor, "machine", p.machine)
    return PlateContext(
        p,
        idx,
        material,
        color,
        temp,
        proc,
        fil,
        mach,
        first_layer_extrusion(proc, fil, mach),
        tests,
        pa,
    )


def _union(boxes: tuple[Box, ...] | list[Box]) -> Box:
    if not boxes:
        raise GenError("empty geometry")
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        min(b[2] for b in boxes),
        max(b[3] for b in boxes),
        max(b[4] for b in boxes),
        max(b[5] for b in boxes),
    )


def _gcode_bbox(lines: tuple[str, ...] | list[str]) -> Box:
    """Extent of the extruding moves of hand-written G-code (absolute XYZ, relative E)."""
    x = y = z = 0.0
    pts: list[tuple[float, float, float]] = []
    for line in lines:
        if not line.startswith(("G1 ", "G0 ")):
            continue
        words = dict(re.findall(r"([XYZE])(-?[\d.]+)", line.split(";", 1)[0]))
        x, y, z = (
            float(words.get("X", x)),
            float(words.get("Y", y)),
            float(words.get("Z", z)),
        )
        if float(words.get("E", 0)) > 0 and ("X" in words or "Y" in words):
            pts.append((x, y, z))
    if not pts:
        raise GenError("custom block has no extruding moves")
    return (
        min(p[0] for p in pts),
        min(p[1] for p in pts),
        0.0,
        max(p[0] for p in pts),
        max(p[1] for p in pts),
        max(p[2] for p in pts),
    )


def custom_lines(ctx: PlateContext, body: list[str], title: str) -> tuple[str, ...]:
    """Wrap hand-written moves: relative E, retract before, prime after (the next block or the
    slicer's body starts with the filament where it found it)."""
    ex = ctx.ex
    return (
        f"; 3d calibrate plate: {title}",
        # Before the slicer's first layer these moves would be skipped by the bounds check
        # (plate = from the first layer change) and by the toolpath preview (;TYPE:Custom).
        ";LAYER_CHANGE",
        ";TYPE:Outer wall",
        "G90",
        "M83",
        f"G1 E-{ex.retract:.3f} F{ex.retract_f:.0f}",
        *body,
        f"G1 E{ex.retract:.3f} F{ex.retract_f:.0f}",
        f"; 3d calibrate plate: end of {title}",
    )


def label_lines(
    ctx: PlateContext,
    text: str,
    x0: float,
    y0: float,
    size: tuple[float, float, float] = COMBO_LABEL,
) -> list[str]:
    """One-layer seven-segment digits at (x0, y0) (lower-left), printed slowly on the bed.
    Starts and ends retracted (wrap with custom_lines)."""
    w, h, gap = size
    out: list[str] = []
    for path in digit_paths(text, x0, y0, w, h, gap):
        out += _path_gcode(path, ctx.ex, [10.0] * (len(path) - 1))
    return out


def label_width(text: str, size: tuple[float, float, float] = COMBO_LABEL) -> float:
    w, _, gap = size
    return text_width(text, w, gap)


_E_WORD = re.compile(r" E(-?[\d.]+)")
_XY_WORD = re.compile(r" [XY]-?[\d.]")
_OWED_EPS = 1e-9


class _RetractionLedger:
    """Firmware retraction state across object and band boundaries. A retraction made by one
    object is undone by the next object's de-retraction, so a band that rewrites retractions must
    not rewrite only the lines inside it: every de-retraction returns exactly the share of what
    was really retracted (`owed_out` of `owed_orig`), whoever made the retraction."""

    def __init__(self) -> None:
        self.owed_orig = 0.0  # as sliced
        self.owed_out = 0.0  # as emitted

    def settle(self, raw: str, new: str) -> str:
        """`raw` is the sliced line, `new` the line after the band transform."""
        if not raw.startswith("G1 ") or " E" not in raw:
            return new
        code = raw.partition(";")[0]
        m = _E_WORD.search(code)
        if m is None:
            return new
        e = float(m.group(1))
        if e < 0:
            nm = _E_WORD.search(new.partition(";")[0])
            self.owed_orig -= e
            self.owed_out -= float(nm.group(1)) if nm else e
            return new
        if e == 0 or _XY_WORD.search(code) or self.owed_orig <= _OWED_EPS:
            return new  # extrusion, or nothing owed to undo
        undo = min(e, self.owed_orig)
        share = self.owed_out / self.owed_orig
        emit = undo * share + (e - undo)
        self.owed_orig -= undo
        self.owed_out -= undo * share
        nm = _E_WORD.search(new)
        if nm is None or abs(float(nm.group(1)) - emit) < _OWED_EPS:
            return new
        return new[: nm.start()] + f" E{emit:.5f}" + new[nm.end() :]


def apply_hooks(text: str, objects: tuple[PlateObject, ...]) -> str:
    """Insert every object's before/enter/band/leave/after G-code and run band transforms on its
    own lines (see the module docstring)."""
    by = {o.name: o for o in objects}
    lines = text.split("\n")
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    for i, line in enumerate(lines):
        if m := _START.match(line):
            first.setdefault(m.group(1), i)
        elif m := _END.match(line):
            last[m.group(1)] = i
    missing = sorted(set(by) - set(first))
    if missing:
        raise GenError(f"objects not found in the slice: {', '.join(missing)}")
    out: list[str] = []
    z = 0.0
    cur: PlateObject | None = None
    band: Band | None = None
    ledger = _RetractionLedger()
    for i, line in enumerate(lines):
        if m := _ZMARK.match(line):
            z = float(m.group(1))
            out.append(line)
            if cur is not None:
                nb = cur.band_at(z)
                if nb is not band and nb is not None:
                    out += nb.gcode
                band = nb
            continue
        if (m := _START.match(line)) and m.group(1) in by:
            cur = by[m.group(1)]
            if first[cur.name] == i:
                out += cur.before
            out.append(line)
            out += cur.enter
            band = cur.band_at(z)
            if band is not None:
                out += band.gcode
            continue
        if (m := _END.match(line)) and cur is not None and m.group(1) == cur.name:
            out += cur.leave
            out.append(line)
            if last[cur.name] == i:
                out += cur.after
            cur, band = None, None
            continue
        raw = line
        if band is not None and band.transform is not None:
            line = band.transform(line)
        out.append(ledger.settle(raw, line))
    return "\n".join(out)


def scale_retractions(factor: float) -> Callable[[str], str]:
    """A band transform: retractions (E < 0, incl. those during a wipe) and E-only
    de-retractions (E > 0 without X/Y) scaled by `factor` — a retraction length test with the
    slicer's own travel and z-hop logic."""

    def tf(line: str) -> str:
        if not line.startswith("G1 ") or " E" not in line:
            return line
        code, sep, comment = line.partition(";")
        m = re.search(r" E(-?[\d.]+)", code)
        if m is None:
            return line
        e = float(m.group(1))
        deretract = e > 0 and not re.search(r" [XY]-?[\d.]", code)
        if e < 0 or deretract:
            code = code[: m.start()] + f" E{e * factor:.5f}" + code[m.end() :]
        return code + sep + comment

    return tf


def _seconds(text: str) -> int:
    m = _EST.search(text)
    if m is None:
        return 0
    total = 0
    for n, unit in re.findall(r"(\d+)([dhms])", m.group(1)):
        total += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


def custom_seconds(lines: tuple[str, ...] | list[str]) -> float:
    """Rough time of hand-written moves: length / feedrate (no acceleration)."""
    x = y = z = 0.0
    f = 3000.0
    t = 0.0
    for line in lines:
        if not line.startswith("G1 "):
            continue
        w = dict(re.findall(r"([XYZEF])(-?[\d.]+)", line.split(";", 1)[0]))
        f = float(w.get("F", f))
        nx, ny, nz = float(w.get("X", x)), float(w.get("Y", y)), float(w.get("Z", z))
        d = ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5 or abs(
            float(w.get("E", 0))
        )
        t += d / (f / 60)
        x, y, z = nx, ny, nz
    return t


def _split(text: str) -> tuple[list[str], list[str], list[str]]:
    """(header through START_PRINT, body, END_PRINT and the rest)."""
    lines = text.split("\n")
    try:
        s = next(i for i, ln in enumerate(lines) if ln.startswith("START_PRINT"))
        e = next(i for i, ln in enumerate(lines) if ln.startswith("END_PRINT"))
    except StopIteration as exc:
        raise GenError("slice has no START_PRINT/END_PRINT") from exc
    return lines[: s + 1], lines[s + 1 : e], lines[e:]


def _strip_thumbnails(lines: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for ln in lines:
        if ln.startswith("; THUMBNAIL_BLOCK_START"):
            skip = True
        if not skip:
            out.append(ln)
        if ln.startswith("; THUMBNAIL_BLOCK_END"):
            skip = False
    return out


def _max_z(lines: list[str]) -> float:
    zs = [float(m.group(1)) for ln in lines if (m := _ZMARK.match(ln))]
    return max(zs, default=0.0)


def _first_xy(lines: list[str]) -> tuple[float, float]:
    for ln in lines:
        if ln.startswith(("G1 ", "G0 ")):
            w = dict(re.findall(r"([XY])(-?[\d.]+)", ln.split(";", 1)[0]))
            if "X" in w and "Y" in w:
                return float(w["X"]), float(w["Y"])
    raise GenError("sequential slice has no XY move")


def splice(
    ctx: PlateContext,
    a_text: str | None,
    b_text: str | None,
    custom: tuple[CustomBlock, ...],
) -> str:
    """Layer slice `a_text` + sequential slice `b_text` + hand-written `custom` blocks in one
    program. Without layer objects (`a_text` None) the sequential slice supplies the header."""
    first = a_text if a_text is not None else b_text
    if first is None:
        raise GenError("splice: neither a layer nor a sequential slice")
    head, body_a, tail = _split(first)
    head = _strip_thumbnails(head)
    custom_g = [ln for block in custom for ln in block.lines]
    parts = head[:-1]
    start = head[-1]
    body_b: list[str] = []
    if a_text is not None and b_text is not None:
        head_b, body_b, _ = _split(b_text)
        defines = [ln for ln in head_b if ln.startswith("EXCLUDE_OBJECT_DEFINE")]
        last_def = max(
            (i for i, ln in enumerate(parts) if ln.startswith("EXCLUDE_OBJECT_DEFINE")),
            default=None,
        )
        if last_def is None:
            raise GenError(
                "layer slice has no EXCLUDE_OBJECT_DEFINE (label objects is off?)"
            )
        parts = parts[: last_def + 1] + defines + parts[last_def + 1 :]
        bx, by = _first_xy(body_b)
        lift = max(_max_z(body_a), ctx.ex.layer_h) + SPLICE_LIFT
        transition = [
            SPLICE_MARK,
            f"G1 Z{lift:.3f} F{Z_MOVE_F}",
            f"G1 X{bx:.3f} Y{by:.3f} F{TRAVEL_F}",
        ]
        body_b = transition + body_b
    lines = parts + [start] + custom_g + body_a + body_b + tail
    return "\n".join(ln for ln in lines if not ln.startswith("M73 "))


def restore_prime(text: str, retract_f: float) -> str:
    """The layer phase ends retracted (its last retraction, as rewritten by any band), and the
    sequential slice starts as if primed: its first de-retraction only undoes its own first
    retraction. Add the layer phase's leftover right after that first de-retraction, at the
    nozzle on the tower's first layer (no ooze in the air during the lift)."""
    lines = text.split("\n")
    mark = lines.index(SPLICE_MARK)
    owed = 0.0
    for line in reversed(lines[:mark]):
        if not line.startswith("G1 "):
            continue
        code = line.split(";", 1)[0]
        m = re.search(r" E(-?[\d.]+)", code)
        if m is None:
            continue
        e = float(m.group(1))
        if e > 0 and re.search(r" [XY]-?[\d.]", code):
            break  # the last extruding move: everything after it is the leftover retraction
        owed -= e
    if owed <= 1e-6:
        return text
    for i in range(mark + 1, len(lines)):
        code = lines[i].split(";", 1)[0]
        if re.match(r"^G1 E[\d.]+ F", code) and not re.search(r" [XY]-?[\d.]", code):
            lines.insert(
                i + 1,
                f"G1 E{owed:.5f} F{retract_f:.0f} ; 3d calibrate plate: undo the layer phase's last retraction",
            )
            return "\n".join(lines)
    raise GenError("sequential phase has no de-retraction to prime after")


def bed_labels(
    ctx: PlateContext, objects: tuple[PlateObject, ...]
) -> CustomBlock | None:
    """One hand-written block with every object's bed label (printed before the objects)."""
    body: list[str] = []
    for o in objects:
        if o.bed_label is not None:
            text, cx, y = o.bed_label
            body += label_lines(ctx, text, cx - label_width(text) / 2, y)
    if not body:
        return None
    return CustomBlock(
        "labels",
        "labels",
        custom_lines(ctx, body, "object labels"),
        read="Bed labels in front of objects (flow %, bridge mm, overhang °).",
    )


def build_plate(ctx: PlateContext, plate: Plate, slicer: str, out: Path) -> Generated:
    labels = bed_labels(ctx, plate.objects)
    if labels is not None:
        plate = replace(plate, custom=(*plate.custom, labels))
    names = [o.name for o in plate.objects] + [c.name for c in plate.custom]
    bad = [n for n in names if not _NAME.match(n)]
    if bad or len(set(names)) != len(names):
        raise GenError(
            f"plate {plate.plate_id}: object names must be unique [a-z][a-z0-9_]*: {names}"
        )
    layer = tuple(o for o in plate.objects if o.phase == "layer")
    seq = tuple(o for o in plate.objects if o.phase == "sequential")
    if not layer and not seq:
        raise GenError(
            f"plate {plate.plate_id}: needs at least one object with a mesh (a layer object or "
            "a tower): hand-written lines alone leave no slice to take the header from"
        )
    rod, radius = ctx.clearance()
    for o in seq:
        if o.bbox()[5] > rod:
            raise GenError(
                f"{o.name}: {o.bbox()[5]:g} mm is taller than the gantry rod clearance {rod:g} mm"
            )
    if seq and layer:
        low_y0 = min(o.bbox()[1] for o in seq)
        front = max(o.bbox()[4] for o in layer)
        if low_y0 - front < radius:
            raise GenError(
                f"layer objects end at y={front:g}, towers start at y={low_y0:g}: "
                f"keep {radius:g} mm (extruder_clearance_radius) between them"
            )
    base_fil = _filament_overrides(ctx.color, ctx.temp)
    a_text: str | None = None
    a_log = ""
    if layer:
        a_text, a_log = slice_models(
            slicer,
            ctx.idx,
            ctx.p,
            ctx.material,
            {f"{o.name}.stl": o.mesh() for o in layer},
            dict(plate.layer_process),
            {**base_fil, **plate.layer_filament},
        )
        require_relative_e(a_text, ctx.p)
    b_text: str | None = None
    b_log = ""
    if seq:
        b_text, b_log = slice_models(
            slicer,
            ctx.idx,
            ctx.p,
            ctx.material,
            {f"{o.name}.stl": o.mesh() for o in seq},
            {**plate.seq_process, "print_sequence": "by object"},
            {**base_fil, **plate.seq_filament},
        )
        require_relative_e(b_text, ctx.p)
    text = splice(ctx, a_text, b_text, plate.custom)
    text = apply_hooks(text, plate.objects)
    if layer and seq:
        text = restore_prime(text, ctx.ex.retract_f)
    text = insert_before_end(text, list(plate.restore))
    publish(text, out)
    finish(out, a_log + "\n" + b_log, ctx.mach)
    estimate = (
        (_seconds(a_text) if a_text else 0)
        + (_seconds(b_text) if b_text else 0)
        + sum(custom_seconds(c.lines) for c in plate.custom)
    )
    sidecar = write_map(ctx, plate, out, estimate, _sequence(text, seq))
    minutes = estimate / 60
    return Generated(
        plate.plate_id,
        [out, sidecar],
        [
            f"{plate.title}: {len(plate.custom)} hand-written blocks, {len(layer)} objects by layer, "
            f"{len(seq)} towers in sequence; estimate ~{minutes:.0f} min (Orca estimates + hand-written moves, "
            "plus heating and START_PRINT).",
            f"Map of objects, parameters and how to read them: {sidecar}",
            *plate.notes,
        ],
    )


def _sequence(text: str, seq: tuple[PlateObject, ...]) -> list[str]:
    order: list[str] = []
    names = {o.name for o in seq}
    for ln in text.split("\n"):
        if (m := _START.match(ln)) and m.group(1) in names and m.group(1) not in order:
            order.append(m.group(1))
    return order


def _r(v: float) -> float:
    return round(v, 3)


def _box(b: Box) -> list[float]:
    return [_r(v) for v in b]


def write_map(
    ctx: PlateContext, plate: Plate, gcode: Path, estimate: float, seq_order: list[str]
) -> Path:
    def items(its: tuple[Item, ...]) -> list[dict[str, Any]]:
        return [
            {"label": it.label, "bbox": _box(it.bbox), "params": dict(it.params)}
            for it in its
        ]

    objects: list[dict[str, Any]] = [
        {
            "name": c.name,
            "kind": c.kind,
            "phase": "custom",
            "bbox": _box(c.bbox()),
            "params": dict(c.params),
            "bands": [],
            "items": items(c.items),
            "read": c.read,
            "record": c.record,
        }
        for c in plate.custom
    ]
    objects += [
        {
            "name": o.name,
            "kind": o.kind,
            "phase": o.phase,
            "bbox": _box(o.bbox()),
            "params": dict(o.params),
            "bands": [
                {"z0": _r(b.z0), "z1": _r(b.z1), "params": dict(b.params)}
                for b in o.bands
            ],
            "items": items(o.items),
            "read": o.read,
            "record": o.record,
        }
        for o in plate.objects
    ]
    data = {
        "schema": 1,
        "plate": plate.plate_id,
        "title": plate.title,
        "gcode": gcode.name,
        "gcode_sha256": hashlib.sha256(gcode.read_bytes()).hexdigest(),
        "printer_model": ctx.p.machine,
        "material": ctx.material,
        "filament_preset": ctx.p.filament[ctx.material],
        "color": ctx.color,
        "nozzle_temp": ctx.base_temp,
        "estimate_min": round(estimate / 60, 1),
        "print_order": ["custom: " + c.name for c in plate.custom]
        + ["layer: " + o.name for o in plate.objects if o.phase == "layer"]
        + ["sequential: " + n for n in seq_order],
        "objects": objects,
        "generated_by": f"3d calibrate gen {plate.plate_id}",
    }
    path = gcode.with_suffix(".json")
    path.write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
