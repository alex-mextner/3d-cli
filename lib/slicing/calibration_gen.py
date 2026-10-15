"""calibration_gen.py — calibration prints for `3d calibrate gen`: PA tower, flow tiles, combo plate, shaper commands.

WHAT: builds the test geometry (plain boxes, written as STL), slices it with the Orca CLI from
  the printer's flattened system presets plus per-test overrides, then post-processes the G-code:
  - pa:     hollow single-wall tower; `TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE …` after
            START_PRINT changes PA with Z (Klipper's own tuning-tower method, written here from
            the Klipper docs, not a port of any pattern generator).
  - flow:   a row of tiles, one object each; `M221 S<pct>` at every EXCLUDE_OBJECT_START sets
            the tile's extrusion factor around the preset's filament_flow_ratio.
  - combo:  one plate for a new spool — PA lines written here as G-code (slow-fast-slow, one
            SET_PRESSURE_ADVANCE per line, seven-segment labels) on the bare plate right after
            START_PRINT, then the flow tiles sliced by Orca. PA and M221 are global, so each is
            reset before the part that must not inherit it.
  - shaper: no G-code; the console commands for the printer's accelerometer.
  Every G-code gets thumbnails and a slice-warnings sidecar, so `3d print-preflight` can PASS it.
WHY: Orca's calibration wizards are GUI-only; #168 (plan §8.2 step 2) wants generators that run
  from the CLI and go through the same preflight as any print.
INVARIANTS: the printer must be a Klipper machine listed in PRESETS (the commands are Klipper's);
  the slicer presets come from the installed Orca's system profiles, never from a hand-kept copy.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("pa", "flow", "combo", "shaper")
SLICE_TIMEOUT_S = 600


class GenError(RuntimeError):
    """A calibration print could not be generated."""


@dataclass(frozen=True)
class Presets:
    """Orca system presets and machine facts for one printer model."""

    vendor: str
    machine: str
    process: str
    filament: dict[str, str]  # material -> filament preset
    bed: tuple[float, float]  # printable X, Y (mm) of the machine preset
    # Klipper's stock pressure_advance in printer.cfg: restored after the PA tower.
    stock_pa: float
    has_accelerometer: bool
    probe_point: tuple[float, float, float]
    sources: tuple[str, ...] = field(default=())


# Creality K1 SE: presets as named in Orca's Creality profile folder; stock_pa, the ADXL345 on the
# nozzle MCU and the resonance_tester probe point from Creality's printer.cfg for the K1 SE board.
PRESETS: dict[str, Presets] = {
    "Creality K1 SE": Presets(
        vendor="Creality",
        machine="Creality K1 SE 0.4 nozzle",
        process="0.20mm Standard @Creality K1 SE",
        filament={
            "PLA": "Creality Generic PLA @K1-all",
            "PETG": "Creality Generic PETG @K1-all",
        },
        bed=(220.0, 220.0),
        stock_pa=0.04,
        has_accelerometer=True,
        probe_point=(110.0, 110.0, 10.0),
        sources=(
            "https://github.com/CrealityOfficial/K1_Series_Klipper/blob/master/config/"
            "K1_SE_CR4CU220812S12/printer.cfg",
        ),
    ),
}

# Klipper "Pressure Advance" tuning: 1 wall, no infill/top, ~100 mm/s, ACCEL=500,
# SQUARE_CORNER_VELOCITY=1, FACTOR=.005 for direct drive. https://www.klipper3d.org/Pressure_Advance.html
PA_FACTOR = 0.005
PA_ACCEL = 500
PA_SCV = 1
PA_SPEED = 100
PA_TOWER = (40.0, 40.0, 30.0)  # x, y, z mm: z * FACTOR spans PA 0 .. 0.15
# Flow tiles: extrusion factor offsets (%) around the preset's flow ratio, left to right.
FLOW_STEPS = (-10, -5, 0, 5, 10)
FLOW_TILE = (25.0, 25.0, 3.0)
FLOW_GAP = 8.0


@dataclass
class Generated:
    kind: str
    files: list[Path]
    notes: list[str]


def presets_for(model: str) -> Presets:
    p = PRESETS.get(model)
    if p is None:
        raise GenError(
            f"no calibration presets for printer model {model!r} (known: {', '.join(PRESETS)})"
        )
    return p


def profile_roots(slicer: str) -> list[Path]:
    """Where the Orca build keeps its system profiles: next to the binary (extracted AppImage,
    macOS bundle), then the per-user copies Orca writes on first start."""
    env = os.environ.get("ORCA_PROFILES")
    roots = [Path(env)] if env else []
    exe = Path(slicer).resolve()
    roots += [
        exe.parent / "resources" / "profiles",
        exe.parent.parent / "Resources" / "profiles",
    ]
    home = Path.home()
    roots += [
        home / ".config" / app / "system" for app in ("Snapmaker_Orca", "OrcaSlicer")
    ]
    return [r for r in roots if r.is_dir()]


class ProfileIndex:
    """System presets by (vendor, kind, name) with `inherits` resolved (child keys win)."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self._cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    def _index(self, vendor: str, kind: str) -> dict[str, dict[str, Any]]:
        key = (vendor, kind)
        if key not in self._cache:
            found: dict[str, dict[str, Any]] = {}
            for root in reversed(self.roots):  # first root wins
                for path in sorted((root / vendor / kind).glob("*.json")):
                    data = json.loads(path.read_text())
                    found[str(data.get("name", path.stem))] = data
            self._cache[key] = found
        return self._cache[key]

    def flat(
        self, vendor: str, kind: str, name: str, _seen: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        index = self._index(vendor, kind)
        if name not in index:
            raise GenError(
                f"Orca {kind} preset {name!r} not found under {vendor} in "
                f"{', '.join(map(str, self.roots)) or 'no profile folder'}"
            )
        if name in _seen:
            raise GenError(
                f"Orca {kind} preset {name!r}: `inherits` loop {' -> '.join((*_seen, name))}"
            )
        data = dict(index[name])
        parent = data.pop("inherits", None)
        if parent:
            base = self.flat(vendor, kind, str(parent), (*_seen, name))
            base.update(data)
            data = base
        return data


def box_stl(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> str:
    """An ASCII STL of one axis-aligned box (outward winding)."""
    v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    tris = [
        (0, 2, 1),
        (1, 2, 3),
        (4, 5, 6),
        (5, 7, 6),
        (0, 1, 4),
        (1, 5, 4),
        (2, 6, 3),
        (3, 6, 7),
        (0, 4, 2),
        (2, 4, 6),
        (1, 3, 5),
        (3, 7, 5),
    ]
    out = ["solid box"]
    for a, b, c in tris:
        out.append("facet normal 0 0 0\nouter loop")
        out += [f"vertex {v[i][0]:g} {v[i][1]:g} {v[i][2]:g}" for i in (a, b, c)]
        out.append("endloop\nendfacet")
    out.append("endsolid box\n")
    return "\n".join(out)


def _orca_value(template: Any, value: Any) -> Any:
    """Orca stores filament options as one-element string lists, process options as strings."""
    return [str(value)] if isinstance(template, list) else str(value)


def _apply_overrides(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _orca_value(base.get(k), v)
    return out


def _write_preset(tmp: Path, kind: str, name: str, data: dict[str, Any]) -> Path:
    data = dict(data, name=name, **{"from": "system", "instantiation": "true"})
    path = tmp / f"{kind}.json"
    path.write_text(json.dumps(data, indent=1))
    return path


def slice_models(
    slicer: str,
    idx: ProfileIndex,
    p: Presets,
    material: str,
    models: dict[str, str],
    process_over: dict[str, Any],
    filament_over: dict[str, Any],
) -> tuple[str, str]:
    """Slice `models` (file name -> STL text, one object each, placed by their coordinates).
    Returns (G-code text, slicer log incl. result.json) — the log feeds the warnings sidecar."""
    if material not in p.filament:
        raise GenError(
            f"no filament preset for {material!r} on {p.machine} (known: {', '.join(p.filament)})"
        )
    tmp = Path(tempfile.mkdtemp(prefix="3dcalgen."))
    try:
        machine = _write_preset(
            tmp, "machine", p.machine, idx.flat(p.vendor, "machine", p.machine)
        )
        process = _write_preset(
            tmp,
            "process",
            p.process,
            _apply_overrides(idx.flat(p.vendor, "process", p.process), process_over),
        )
        fil_name = p.filament[material]
        filament = _write_preset(
            tmp,
            "filament",
            fil_name,
            _apply_overrides(idx.flat(p.vendor, "filament", fil_name), filament_over),
        )
        stls = []
        for name, text in models.items():
            path = tmp / name
            path.write_text(text)
            stls.append(str(path))
        cmd = [
            slicer,
            "--datadir",
            str(tmp / "datadir"),
            "--arrange",
            "0",
            "--load-settings",
            f"{machine};{process}",
            "--load-filaments",
            str(filament),
            "--slice",
            "0",
            "--outputdir",
            str(tmp),
            *stls,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=SLICE_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GenError(f"Orca CLI {slicer} did not run: {e}") from e
        log = r.stdout + r.stderr
        produced = sorted(tmp.glob("*.gcode"))
        if r.returncode != 0 or not produced:
            raise GenError(f"Orca CLI failed (exit {r.returncode}):\n{log[-2000:]}")
        result = tmp / "result.json"
        if result.is_file():
            log += "\n" + result.read_text()
        return produced[0].read_text(), log
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def insert_after(
    text: str,
    pattern: str,
    lines: list[str] | None = None,
    *,
    every: bool = False,
    render: Callable[[re.Match[str]], list[str]] | None = None,
) -> str:
    """Insert `lines` (or `render(match)`) after the line matching `pattern` (first, or every)."""
    rx = re.compile(pattern, re.M)
    hits = list(rx.finditer(text)) if every else ([m] if (m := rx.search(text)) else [])
    if not hits:
        raise GenError(f"post-process: no line matches {pattern!r}")
    out, last = [], 0
    for m in hits:
        end = text.index("\n", m.end()) + 1
        out.append(text[last:end])
        out.append(
            "".join(f"{line}\n" for line in (render(m) if render else lines or []))
        )
        last = end
    out.append(text[last:])
    return "".join(out)


# Orca's GUI plate code logs this as an error for a machine whose bed_exclude_area is empty
# ("0x0", the K1 SE's): an empty polygon has no triangles. Nothing on the plate is affected.
EMPTY_EXCLUDE_ERROR = "calc_exclude_triangles:Unable to create exclude triangles"


def insert_before_end(text: str, lines: list[str]) -> str:
    """Put `lines` before the END_PRINT macro: the restore of state the test changed. A
    profile without END_PRINT would leave that state on the printer, so it is an error."""
    rx = re.compile(r"^END_PRINT\b", re.M)
    m = rx.search(text)
    if m is None:
        raise GenError(
            "post-process: no END_PRINT line; the test's PA/flow change could not be undone"
        )
    return (
        text[: m.start()] + "".join(f"{line}\n" for line in lines) + text[m.start() :]
    )


def publish(text: str, out: Path) -> None:
    """Write the final G-code atomically (temp file in the same dir, then rename)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def finish(gcode: Path, log: str, machine: dict[str, Any]) -> None:
    try:
        _finish(gcode, log, machine)
    except BaseException:
        gcode.unlink(
            missing_ok=True
        )  # a G-code without thumbnails/sidecar must not stay behind
        raise


def _finish(gcode: Path, log: str, machine: dict[str, Any]) -> None:
    """Thumbnails, then the slice-warnings sidecar for the final bytes (print-preflight checks both).
    The empty-exclude-area error is accepted with its reason only when the machine has none."""
    from gcode_thumbnails import ensure
    from slicing.slice_warnings import record, warning_id

    ensure(str(gcode))
    accept = {}
    if [a for a in machine.get("bed_exclude_area") or [] if a not in ("0x0", "")] == []:
        accept[warning_id("error", EMPTY_EXCLUDE_ERROR)] = (
            "the printer profile defines no bed exclude area"
        )
    record(gcode, logs=[("slicer log", log)], accept=accept)


def _filament_overrides(color: str, temp: int | None) -> dict[str, Any]:
    over: dict[str, Any] = {"filament_colour": color} if color else {}
    if temp is not None:
        over |= {"nozzle_temperature": temp, "nozzle_temperature_initial_layer": temp}
    return over


def gen_pa(
    slicer: str,
    idx: ProfileIndex,
    p: Presets,
    material: str,
    color: str,
    out: Path,
    temp: int | None,
) -> Generated:
    x, y, z = PA_TOWER
    cx, cy = p.bed[0] / 2, p.bed[1] / 2
    model = box_stl(cx - x / 2, cy - y / 2, 0, cx + x / 2, cy + y / 2, z)
    # Every *_acceleration / *_jerk the slicer would emit is pinned, so its per-feature
    # SET_VELOCITY_LIMIT lines keep the test's ACCEL and SQUARE_CORNER_VELOCITY.
    proc = idx.flat(p.vendor, "process", p.process)
    over: dict[str, Any] = {k: PA_ACCEL for k in proc if k.endswith("_acceleration")}
    over |= {k: PA_SCV for k in proc if k.endswith("_jerk")}
    over |= {
        "wall_loops": 1,
        "sparse_infill_density": "0%",
        "top_shell_layers": 0,
        "bottom_shell_layers": 3,
        "outer_wall_speed": PA_SPEED,
        "inner_wall_speed": PA_SPEED,
        "brim_type": "no_brim",
        "skirt_loops": 0,
    }
    fil: dict[str, Any] = {"enable_pressure_advance": 0} | _filament_overrides(
        color, temp
    )
    text, log = slice_models(
        slicer, idx, p, material, {"pa_tower.stl": model}, over, fil
    )
    text = insert_after(
        text,
        r"^START_PRINT\b.*$",
        [
            "; 3d calibrate gen pa: Klipper tuning tower, PA = Z(mm) * "
            + f"{PA_FACTOR:g}",
            f"SET_VELOCITY_LIMIT ACCEL={PA_ACCEL} ACCEL_TO_DECEL={PA_ACCEL // 2} SQUARE_CORNER_VELOCITY={PA_SCV}",
            f"TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE PARAMETER=ADVANCE START=0 FACTOR={PA_FACTOR:g}",
        ],
    )
    # After the last layer the tower leaves PA at its top value until a restart: put back the stock one.
    text = insert_before_end(text, [f"SET_PRESSURE_ADVANCE ADVANCE={p.stock_pa:g}"])
    publish(text, out)
    finish(out, log, idx.flat(p.vendor, "machine", p.machine))
    return Generated(
        "pa",
        [out],
        [
            f"PA tower {x:g}x{y:g}x{z:g} mm: PA = height(mm) x {PA_FACTOR:g} (0 at the bed, "
            f"{z * PA_FACTOR:g} at the top).",
            "Measure the height (mm) where corners look sharpest without bulge or gap; "
            f"pressure_advance = height x {PA_FACTOR:g}.",
            "Record: 3d calibrate record pressure_advance --printer PRINTER --values pressure_advance=VALUE "
            "--method pa_tower",
        ],
    )


def gen_flow(
    slicer: str,
    idx: ProfileIndex,
    p: Presets,
    material: str,
    color: str,
    out: Path,
    temp: int | None,
) -> Generated:
    tx, _, _ = FLOW_TILE
    width = len(FLOW_STEPS) * tx + (len(FLOW_STEPS) - 1) * FLOW_GAP
    models = flow_models(
        (p.bed[0] - width) / 2, (p.bed[1] - FLOW_TILE[1]) / 2, FLOW_TILE, FLOW_GAP
    )
    fil = _filament_overrides(color, temp)
    text, log = slice_models(slicer, idx, p, material, models, FLOW_PROCESS, fil)
    text = flow_factors(text)
    text = insert_before_end(text, ["M221 S100"])
    publish(text, out)
    finish(out, log, idx.flat(p.vendor, "machine", p.machine))
    return Generated("flow", [out], flow_notes(_flow_ratio(idx, p, material)))


FLOW_PROCESS = {
    "top_shell_layers": 4,
    "sparse_infill_density": "15%",
    "brim_type": "no_brim",
}


def flow_models(
    x0: float, y0: float, tile: tuple[float, float, float], gap: float
) -> dict[str, str]:
    """One box per FLOW_STEPS entry, left to right; the file name carries the step
    (flow_m10 = -10 %), which flow_factors() reads back from EXCLUDE_OBJECT_START."""
    tx, ty, tz = tile
    models: dict[str, str] = {}
    for i, step in enumerate(FLOW_STEPS):
        name = f"flow_{'m' if step < 0 else 'p'}{abs(step):02d}.stl"
        xa = x0 + i * (tx + gap)
        models[name] = box_stl(xa, y0, 0, xa + tx, y0 + ty, tz)
    return models


def flow_factors(text: str) -> str:
    """`M221 S<100+step>` at every EXCLUDE_OBJECT_START of a tile. M221 is global, so the factor
    is set again each time the slicer enters a tile (every tile on every layer)."""

    def factor(m: re.Match[str]) -> list[str]:
        step = int(m.group(2)) * (-1 if m.group(1) == "m" else 1)
        return [f"M221 S{100 + step}"]

    return insert_after(
        text,
        r"^EXCLUDE_OBJECT_START NAME=flow_([mp])(\d+)\S*$",
        every=True,
        render=factor,
    )


def _flow_ratio(idx: ProfileIndex, p: Presets, material: str) -> float:
    return _first(
        idx.flat(p.vendor, "filament", p.filament[material]), "filament_flow_ratio"
    )


def flow_notes(ratio: float) -> list[str]:
    tiles = ", ".join(
        f"{i + 1}: {ratio * (100 + s) / 100:.3f}" for i, s in enumerate(FLOW_STEPS)
    )
    return [
        f"{len(FLOW_STEPS)} tiles left to right (front view), extrusion "
        f"{', '.join(f'{100 + s}%' for s in FLOW_STEPS)} of the preset flow ratio {ratio:g}.",
        f"flow_ratio per tile: {tiles}.",
        "Pick the tile with the smoothest top (no gaps, no ridges); "
        "Record: 3d calibrate record flow_ratio --printer PRINTER --values flow_ratio=VALUE --method flow_tiles",
    ]


# Combo plate: PA lines on the first layer (front), flow tiles (back). Each line is slow-fast-slow;
# at the right PA the width stays even across both speed changes (too low: blob/bulge after the
# fast part, too high: thin gap at its start). Our own pattern, written from Klipper's
# SET_PRESSURE_ADVANCE semantics; labels are seven-segment digits = PA x 1000.
COMBO_PA_VALUES = tuple(round(i * 0.005, 3) for i in range(17))  # 0 .. 0.080
COMBO_PITCH = (
    5.0  # mm between PA lines: a 3 mm label plus a clear gap at 0.5 mm line width
)
COMBO_SEGMENTS = (20.0, 50.0, 20.0)  # slow, fast, slow (mm)
COMBO_SPEEDS = (20.0, 120.0)  # slow, fast (mm/s)
COMBO_LINE_W = 0.5
COMBO_ACCEL = 5000
COMBO_LABEL = (2.0, 3.0, 1.0)  # digit width, height, gap (mm): legible on a phone photo
COMBO_TILE = (20.0, 20.0, 3.0)
COMBO_TILE_GAP = 6.0
# Layout as fractions of the bed depth: PA lines start near the front, tiles sit at the back,
# with at least COMBO_CLEARANCE mm between the top label and the tiles.
COMBO_PA_Y_FRAC = 0.12
COMBO_TILES_Y_FRAC = 0.68
COMBO_CLEARANCE = 5.0
COMBO_LIFT = 0.4  # mm above the first layer for travels between lines and label strokes
Z_MOVE_F = 1200  # mm/min
TRAVEL_F = 12000  # mm/min

# Seven-segment strokes in a unit cell (0..1 wide, 0..2 high).
_SEG = {
    "a": ((0, 2), (1, 2)),
    "b": ((1, 2), (1, 1)),
    "c": ((1, 1), (1, 0)),
    "d": ((0, 0), (1, 0)),
    "e": ((0, 0), (0, 1)),
    "f": ((0, 1), (0, 2)),
    "g": ((0, 1), (1, 1)),
}
_DIGITS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}
Point = tuple[float, float]


def digit_paths(
    digits: str, x0: float, y0: float, w: float, h: float, gap: float
) -> list[list[Point]]:
    """Polylines drawing `digits` with their lower-left corner at (x0, y0); strokes
    that share an end are chained so each digit needs few retractions."""
    paths: list[list[Point]] = []
    for k, ch in enumerate(digits):
        ox = x0 + k * (w + gap)
        segs = [_SEG[s] for s in _DIGITS[ch]]
        while segs:
            a, b = segs.pop(0)
            path = [a, b]
            grew = True
            while grew:
                grew = False
                for s in segs:
                    if s[0] == path[-1] or s[1] == path[-1]:
                        path.append(s[1] if s[0] == path[-1] else s[0])
                        segs.remove(s)
                        grew = True
                        break
            paths.append([(ox + px * w, y0 + py * h / 2) for px, py in path])
    return paths


@dataclass(frozen=True)
class Extrusion:
    """First-layer extrusion parameters taken from the sliced presets."""

    layer_h: float
    line_w: float
    e_per_mm: float  # filament mm per mm of line (flow ratio included)
    retract: float
    retract_f: float  # mm/min
    lift: float


def _path_gcode(path: list[Point], ex: Extrusion, speeds: list[float]) -> list[str]:
    """Travel lifted to the path start, extrude through it (speed per segment, mm/s), retract, lift."""
    (sx, sy), rest = path[0], path[1:]
    out = [
        f"G1 Z{ex.layer_h + ex.lift:.3f} F{Z_MOVE_F}",
        f"G1 X{sx:.3f} Y{sy:.3f} F{TRAVEL_F}",
        f"G1 Z{ex.layer_h:.3f} F{Z_MOVE_F}",
        f"G1 E{ex.retract:.3f} F{ex.retract_f:.0f}",
    ]
    px, py = sx, sy
    for (x, y), v in zip(rest, speeds, strict=True):
        dist = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
        out.append(f"G1 X{x:.3f} Y{y:.3f} E{dist * ex.e_per_mm:.5f} F{v * 60:.0f}")
        px, py = x, y
    out += [
        f"G1 E-{ex.retract:.3f} F{ex.retract_f:.0f}",
        f"G1 Z{ex.layer_h + ex.lift:.3f} F{Z_MOVE_F}",
    ]
    return out


def pa_lines(
    x0: float, y0: float, ex: Extrusion, stock_pa: float, accel: float
) -> list[str]:
    """The PA line block: per value a label (PA x 1000) at x0, then a slow-fast-slow line
    after SET_PRESSURE_ADVANCE. Ends with the stock PA, M221 S100 and the process's own
    acceleration (`accel`): all three are global and the slicer's body must not inherit them.
    Extrusion is relative (M83), as the slicer's body (gen_combo checks the preset)."""
    lw, lh, lgap = COMBO_LABEL
    slow, fast = COMBO_SPEEDS
    xs = x0 + 3 * (lw + lgap) + 2.0
    out = [
        "; 3d calibrate gen combo: PA lines, label = PA x 1000; slow-fast-slow per line",
        "M221 S100",
        "G90",
        "M83",
        f"SET_VELOCITY_LIMIT ACCEL={COMBO_ACCEL} ACCEL_TO_DECEL={COMBO_ACCEL // 2}",
        "M106 S0",
        f"G1 E-{ex.retract:.3f} F{ex.retract_f:.0f}",
    ]
    for i, pa in enumerate(COMBO_PA_VALUES):
        y = y0 + i * COMBO_PITCH
        out.append(f"SET_PRESSURE_ADVANCE ADVANCE={stock_pa:g}")
        for path in digit_paths(str(round(pa * 1000)), x0, y - lh / 2, lw, lh, lgap):
            out += _path_gcode(path, ex, [10.0] * (len(path) - 1))
        out.append(f"SET_PRESSURE_ADVANCE ADVANCE={pa:g}")
        a, b, c = COMBO_SEGMENTS
        line = [(xs, y), (xs + a, y), (xs + a + b, y), (xs + a + b + c, y)]
        out += _path_gcode(line, ex, [slow, fast, slow])
    out += [
        f"G1 E{ex.retract:.3f} F{ex.retract_f:.0f}",
        f"SET_PRESSURE_ADVANCE ADVANCE={stock_pa:g}",
        "M221 S100",
        f"SET_VELOCITY_LIMIT ACCEL={accel:g} ACCEL_TO_DECEL={accel / 2:g}",
        "; 3d calibrate gen combo: end of PA lines",
    ]
    return out


def _first(preset: dict[str, Any], key: str) -> float:
    """A numeric preset option (Orca keeps some as one-element lists)."""
    try:
        v = preset[key]
        return float(v[0] if isinstance(v, list) else v)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise GenError(
            f"Orca preset option {key!r} missing or not a number: {e}"
        ) from e


def gen_combo(
    slicer: str,
    idx: ProfileIndex,
    p: Presets,
    material: str,
    color: str,
    out: Path,
    temp: int | None,
) -> Generated:
    tx, ty, _ = COMBO_TILE
    n = len(FLOW_STEPS)
    tiles_w = n * tx + (n - 1) * COMBO_TILE_GAP
    tiles_y = p.bed[1] * COMBO_TILES_Y_FRAC
    models = flow_models((p.bed[0] - tiles_w) / 2, tiles_y, COMBO_TILE, COMBO_TILE_GAP)
    fil = _filament_overrides(color, temp)
    text, log = slice_models(slicer, idx, p, material, models, FLOW_PROCESS, fil)

    proc = idx.flat(p.vendor, "process", p.process)
    fpre = _apply_overrides(idx.flat(p.vendor, "filament", p.filament[material]), fil)
    mach = idx.flat(p.vendor, "machine", p.machine)
    # The sliced footer is authoritative (the K1 preset leaves the option at Orca's default).
    rel_e = re.search(r"^; use_relative_e_distances = (\d)", text, re.M)
    if rel_e is None or rel_e.group(1) != "1":
        raise GenError(f"{p.machine}: combo writes relative-E (M83) lines; the slice is not relative-E")
    diameter = _first(fpre, "filament_diameter")
    layer_h = _first(proc, "initial_layer_print_height")
    ratio = _first(fpre, "filament_flow_ratio")
    if diameter <= 0 or layer_h <= 0 or ratio <= 0:
        raise GenError(
            f"preset values must be positive: filament_diameter={diameter:g}, "
            f"initial_layer_print_height={layer_h:g}, filament_flow_ratio={ratio:g}"
        )
    ex = Extrusion(
        layer_h=layer_h,
        line_w=COMBO_LINE_W,
        e_per_mm=COMBO_LINE_W * layer_h * ratio / (math.pi * diameter**2 / 4),
        retract=_first(mach, "retraction_length"),
        retract_f=_first(mach, "retraction_speed") * 60,
        lift=COMBO_LIFT,
    )
    lw, lh, lgap = COMBO_LABEL
    block_w = 3 * (lw + lgap) + 2.0 + sum(COMBO_SEGMENTS)
    pa_x0 = (p.bed[0] - block_w) / 2
    pa_y0 = p.bed[1] * COMBO_PA_Y_FRAC
    if (
        pa_y0 + (len(COMBO_PA_VALUES) - 1) * COMBO_PITCH + lh
        >= tiles_y - COMBO_CLEARANCE
    ):
        raise GenError("combo layout: the PA lines run into the flow tiles on this bed")
    # The PA block runs right after START_PRINT, before the slicer's first layer: the bed is hot,
    # the nozzle primed, and every line lies on the bare plate.
    block = pa_lines(pa_x0, pa_y0, ex, p.stock_pa, _first(proc, "default_acceleration"))
    text = insert_after(text, r"^START_PRINT\b.*$", block)
    text = flow_factors(text)
    text = insert_before_end(
        text, ["M221 S100", f"SET_PRESSURE_ADVANCE ADVANCE={p.stock_pa:g}"]
    )
    publish(text, out)
    finish(out, log, mach)
    labels = ", ".join(f"{round(v * 1000)}={v:g}" for v in COMBO_PA_VALUES)
    return Generated(
        "combo",
        [out],
        [
            f"Front: {len(COMBO_PA_VALUES)} PA lines, bottom to top (front to back), label = PA x 1000: {labels}.",
            f"Each line: {COMBO_SEGMENTS[0]:g} mm at {COMBO_SPEEDS[0]:g} mm/s, {COMBO_SEGMENTS[1]:g} mm at "
            f"{COMBO_SPEEDS[1]:g} mm/s, {COMBO_SEGMENTS[2]:g} mm slow. Pick the line whose width stays even "
            "at both speed changes; Record: 3d calibrate record pressure_advance --printer PRINTER "
            "--values pressure_advance=VALUE --method pa_lines",
            f"Back: flow tiles printed at PA {p.stock_pa:g} (stock).",
            *flow_notes(ratio),
        ],
    )


def gen_shaper(p: Presets, out: Path) -> Generated:
    if not p.has_accelerometer:
        raise GenError(
            f"{p.machine}: no accelerometer; input shaping needs the ringing-tower method"
        )
    px, py, pz = p.probe_point
    lines = [
        f"# {p.machine}: input shaper calibration with the built-in ADXL345 (console commands,",
        "# run on an idle, homed printer; each SHAPER_CALIBRATE shakes the head for ~1 min).",
        "G28",
        "ACCELEROMETER_QUERY",
        "MEASURE_AXES_NOISE",
        f"# resonance_tester probe point from printer.cfg: {px:g},{py:g},{pz:g}",
        "SHAPER_CALIBRATE AXIS=X",
        "SHAPER_CALIBRATE AXIS=Y",
        "# Read the recommended shaper_type/shaper_freq per axis from the console, then:",
        "#   3d calibrate record input_shaper --printer PRINTER --method klipper_shaper_calibrate \\",
        "#     --values shaper_type_x=T,shaper_freq_x=F,shaper_type_y=T,shaper_freq_y=F",
        "# SAVE_CONFIG writes them to printer.cfg and restarts Klipper (only when idle).",
        "SAVE_CONFIG",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return Generated(
        "shaper",
        [out],
        [
            f"console commands in {out.name}; ADXL345 per {p.sources[0]}"
            if p.sources
            else f"console commands in {out.name}"
        ],
    )


def generate(
    kind: str,
    model: str,
    slicer: str,
    out: Path,
    *,
    material: str = "PLA",
    color: str = "",
    temp: int | None = None,
) -> Generated:
    if kind not in KINDS:
        raise GenError(f"unknown kind {kind!r} (accepted: {', '.join(KINDS)})")
    p = presets_for(model)
    if kind == "shaper":
        return gen_shaper(p, out)
    idx = ProfileIndex(profile_roots(slicer))
    if kind == "pa":
        return gen_pa(slicer, idx, p, material, color, out, temp)
    if kind == "combo":
        return gen_combo(slicer, idx, p, material, color, out, temp)
    return gen_flow(slicer, idx, p, material, color, out, temp)
