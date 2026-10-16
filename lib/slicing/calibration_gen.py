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
  the slicer presets are the ones the printer and spool print with (#293): user presets from the
  repo's profiles/ (D10, synced by `3d profiles sync`) or the slicer's own user folder, resolved
  against the installed Orca's system profiles — never a silent fallback to stock presets.
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

KINDS = ("pa", "flow", "combo", "temp", "shaper")
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
    # where each slicer preset name came from (kind, source), stamped into the G-code
    chosen_by: tuple[tuple[str, str], ...] = ()


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


@dataclass(frozen=True)
class PresetChoice:
    """The slicer presets a calibration print uses — the ones the printer and the spool really
    print with (user presets), never a silent fallback to the stock system presets."""

    machine: str
    process: str
    filament: str
    sources: tuple[tuple[str, str], ...] = ()  # (kind, where the name came from)


def apply_choice(p: Presets, material: str, choice: PresetChoice) -> Presets:
    return replace(
        p,
        machine=choice.machine,
        process=choice.process,
        filament={**p.filament, material: choice.filament},
        chosen_by=choice.sources,
    )


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def preset_name_problem(name: str) -> str | None:
    """Why `name` cannot be a preset name: it is written into a G-code comment, so a line break or
    another control character would turn the rest into commands the printer runs."""
    if not name.strip():
        return "empty preset name"
    if _CONTROL.search(name):
        return f"preset name {name!r} contains a control character (line break, tab, …)"
    return None


def _read_preset(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise GenError(f"slicer preset {path} is not readable JSON: {e}") from e
    if not isinstance(data, dict):
        raise GenError(f"slicer preset {path} is not a JSON object")
    return data


def check_choice(idx: ProfileIndex, p: Presets, material: str) -> None:
    """Every chosen preset exists, and the filament preset is for `material`."""
    for name in (p.machine, p.process, p.filament[material]):
        if (why := preset_name_problem(name)) is not None:
            raise GenError(why)
    for kind, name in (
        ("machine", p.machine),
        ("process", p.process),
        ("filament", p.filament[material]),
    ):
        idx.source(p.vendor, kind, name)
    ftype = idx.flat(p.vendor, "filament", p.filament[material]).get("filament_type")
    ftype = ftype[0] if isinstance(ftype, list) and ftype else ftype
    if ftype and str(ftype).upper() != material.upper():
        raise GenError(
            f"filament preset {p.filament[material]!r} is {ftype}, not {material} (--material)"
        )


NO_PRESETS_HINT = (
    "name the slicer presets the printer and spool print with: --machine-preset/--process-preset "
    "(or `slicer: {machine, process}` in fleet/printers.yaml) and --filament-preset (the spool's "
    "user preset, e.g. 'Orange PLA @K1SE'); calibrating with Orca's stock presets measures settings "
    "you do not print with"
)


def chosen_presets(
    model: str, slicer: str, material: str, choice: PresetChoice | None
) -> tuple[Presets, ProfileIndex]:
    """The printer model's facts with the chosen slicer presets, and an index that resolves
    them (system + user presets). No choice is an error, not a fallback to the stock presets."""
    if choice is None:
        raise GenError("no slicer presets chosen: " + NO_PRESETS_HINT)
    p = apply_choice(presets_for(model), material, choice)
    idx = ProfileIndex(profile_roots(slicer), user_preset_roots())
    check_choice(idx, p, material)
    return p, idx


def presets_stamp(p: Presets, material: str) -> str:
    """A G-code comment naming the presets and where each came from (print-preflight and the
    calibration history read the slicer's own *_settings_id keys; this keeps the reason)."""
    by = dict(p.chosen_by)
    names = {
        "machine": p.machine,
        "process": p.process,
        "filament": p.filament[material],
    }
    return "; 3d calibrate presets: " + "; ".join(
        f"{k}={v} ({by.get(k, 'default')})" for k, v in names.items()
    )


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


def user_preset_roots() -> list[Path]:
    """Where user presets live, first wins: the repo's profiles (decision D10, synced to the
    slicers by `3d profiles sync`), then the Orca-family slicers' own user preset folders, found
    the way `3d profiles` finds them (XDG/Flatpak/macOS data dir, the logged-in `preset_folder`).
    Each holds filament/, process/, machine/ directly (no vendor level)."""
    env = os.environ.get("ORCA_USER_PRESETS")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if Path(p).is_dir()]
    from profiles.slicers import SLICERS, find_data_dir, profiles_root, user_presets_dir

    roots = [profiles_root() / sid for sid in ("snapmaker-orca", "orca")]
    for sid in ("snapmaker-orca", "orca"):
        data = find_data_dir(SLICERS[sid])
        if data is not None:
            roots.append(user_presets_dir(data, SLICERS[sid]))
    return [r for r in roots if r.is_dir()]


class ProfileIndex:
    """Orca presets by (vendor, kind, name) with `inherits` resolved (child keys win): the
    vendor's system presets, plus user presets (any vendor) that inherit from them."""

    def __init__(self, roots: list[Path], user_roots: list[Path] | None = None) -> None:
        self.roots = roots
        self.user_roots = user_roots or []
        self._cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._user: dict[str, dict[str, tuple[dict[str, Any], Path]]] = {}

    def _index(self, vendor: str, kind: str) -> dict[str, dict[str, Any]]:
        key = (vendor, kind)
        if key not in self._cache:
            found: dict[str, dict[str, Any]] = {}
            for root in reversed(self.roots):  # first root wins
                for path in sorted((root / vendor / kind).glob("*.json")):
                    data = _read_preset(path)
                    found[str(data.get("name", path.stem))] = data
            self._cache[key] = found
        return self._cache[key]

    def _user_index(self, kind: str) -> dict[str, tuple[dict[str, Any], Path]]:
        if kind not in self._user:
            found: dict[str, tuple[dict[str, Any], Path]] = {}
            for root in reversed(self.user_roots):  # first root wins
                for path in sorted((root / kind).glob("*.json")):
                    data = _read_preset(path)
                    found[str(data.get("name", path.stem))] = (data, path)
            self._user[kind] = found
        return self._user[kind]

    def source(self, vendor: str, kind: str, name: str) -> str:
        """'system' or the user preset's file."""
        if name in self._index(vendor, kind):
            return "system"
        if name in self._user_index(kind):
            return str(self._user_index(kind)[name][1])
        raise GenError(f"Orca {kind} preset {name!r} not found ({self._where(vendor)})")

    def _where(self, vendor: str) -> str:
        roots = [str(r / vendor) for r in self.roots] + [
            str(r) for r in self.user_roots
        ]
        return "searched " + (", ".join(roots) or "no profile folder")

    def flat(
        self, vendor: str, kind: str, name: str, _seen: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        index = self._index(vendor, kind)
        user = self._user_index(kind)
        if name not in index and name not in user:
            raise GenError(
                f"Orca {kind} preset {name!r} not found ({self._where(vendor)})"
            )
        if name in _seen:
            raise GenError(
                f"Orca {kind} preset {name!r}: `inherits` loop {' -> '.join((*_seen, name))}"
            )
        data = dict(index[name]) if name in index else dict(user[name][0])
        parent = data.pop("inherits", None)
        if parent:
            base = self.flat(vendor, kind, str(parent), (*_seen, name))
            base.update(data)
            data = base
        return data


Box = tuple[float, float, float, float, float, float]  # x0, y0, z0, x1, y1, z1


def box_stl(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> str:
    """An ASCII STL of one axis-aligned box (outward winding)."""
    return boxes_stl([(x0, y0, z0, x1, y1, z1)])


def boxes_stl(boxes: list[Box]) -> str:
    """One ASCII STL solid made of axis-aligned boxes (outward winding). Boxes may touch;
    the slicer unions the shells of one object layer by layer."""
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
    for x0, y0, z0, x1, y1, z1 in boxes:
        v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        for a, b, c in tris:
            out.append("facet normal 0 0 0\nouter loop")
            out += [f"vertex {v[i][0]:g} {v[i][1]:g} {v[i][2]:g}" for i in (a, b, c)]
            out.append("endloop\nendfacet")
    out.append("endsolid box\n")
    return "\n".join(out)


def prism_stl(profile: list[tuple[float, float]], y0: float, y1: float) -> str:
    """An ASCII STL of a prism: a convex polygon in the x-z plane (`profile`, any winding)
    extruded from y0 to y1. Every facet is turned to face away from the centroid."""
    n = len(profile)
    if n < 3:
        raise GenError("prism profile needs at least 3 points")
    front = [(x, y0, z) for x, z in profile]
    back = [(x, y1, z) for x, z in profile]
    tris = [(front[0], front[i], front[i + 1]) for i in range(1, n - 1)]
    tris += [(back[0], back[i], back[i + 1]) for i in range(1, n - 1)]
    for i in range(n):
        j = (i + 1) % n
        tris += [(front[i], front[j], back[j]), (front[i], back[j], back[i])]
    cx = sum(p[0] for p in front) / n
    cz = sum(p[2] for p in front) / n
    centroid = (cx, (y0 + y1) / 2, cz)
    out = ["solid prism"]
    for a, b, c in tris:
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        nrm = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        )
        mid = [(a[k] + b[k] + c[k]) / 3 - centroid[k] for k in range(3)]
        if sum(nrm[k] * mid[k] for k in range(3)) < 0:
            b, c = c, b
        out.append("facet normal 0 0 0\nouter loop")
        out += [f"vertex {q[0]:g} {q[1]:g} {q[2]:g}" for q in (a, b, c)]
        out.append("endloop\nendfacet")
    out.append("endsolid prism\n")
    return "\n".join(out)


def _orca_value(template: Any, value: Any) -> Any:
    """Orca stores filament options as one-element string lists, process options as strings."""
    return [str(value)] if isinstance(template, list) else str(value)


def _apply_overrides(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _orca_value(base.get(k), v)
    return out


def _write_preset(
    tmp: Path, kind: str, name: str, data: dict[str, Any], machine: str = ""
) -> Path:
    data = dict(data, name=name, **{"from": "system", "instantiation": "true"})
    if machine:
        # The presets were chosen explicitly. Orca's CLI matches compatible_printers against the
        # machine preset's own name, so a user machine ("… - Copy") would reject the system
        # process/filament it inherits from; the user's slicer already accepts the combination.
        data["compatible_printers"] = [machine]
        data["compatible_printers_condition"] = ""
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
            p.machine,
        )
        fil_name = p.filament[material]
        filament = _write_preset(
            tmp,
            "filament",
            fil_name,
            _apply_overrides(idx.flat(p.vendor, "filament", fil_name), filament_over),
            p.machine,
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
        return produced[0].read_text().rstrip("\n") + "\n" + presets_stamp(
            p, material
        ) + "\n", log
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
# SET_PRESSURE_ADVANCE semantics; labels are seven-segment digits = PA x 1000 ("42.5" in a fine pass).
# Default range per material: wide enough for an unknown spool; a fine pass narrows it with
# --pa-range/--pa-step.
PA_RANGES = {
    "PLA": (0.0, 0.10, 0.01),
    "PLA+": (0.0, 0.10, 0.01),
    "PETG": (0.0, 0.15, 0.01),
}
PA_MAX_LINES = 31
COMBO_PITCH = (
    5.0  # mm between PA lines: a 3 mm label plus a clear gap at 0.5 mm line width
)
# Short lines: the width change shows within millimetres of each speed change; the fast part
# only needs room to reach COMBO_SPEEDS[1] at COMBO_ACCEL (~1.4 mm each way).
COMBO_SEGMENTS = (6.0, 14.0, 6.0)  # slow, fast, slow (mm)
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
    # letters that seven segments draw unambiguously (codes of plate variants)
    "A": "abcefg",
    "C": "adef",
    "E": "adefg",
    "F": "aefg",
    "H": "bcefg",
    "L": "def",
    "P": "abefg",
    "U": "bcdef",
}
Point = tuple[float, float]
DOT_W = 0.4  # length of the "." stroke (a short line on the baseline)


def pa_values(
    material: str,
    pa_range: tuple[float, float] | None = None,
    step: float | None = None,
) -> tuple[float, ...]:
    """PA values of the line test: the material's default range, or --pa-range/--pa-step."""
    if material not in PA_RANGES and pa_range is None:
        raise GenError(
            f"no default PA range for {material!r} (known: {', '.join(PA_RANGES)}); pass --pa-range"
        )
    lo, hi, st = PA_RANGES.get(material, (0.0, 0.1, 0.01))
    if pa_range is not None:
        lo, hi = pa_range
    if step is not None:
        st = step
    if (
        not all(math.isfinite(v) for v in (lo, hi, st))
        or not (0 <= lo < hi <= 2.0)
        or st <= 0
    ):
        raise GenError(
            f"PA range {lo:g}:{hi:g} step {st:g}: need 0 <= low < high <= 2 and step > 0"
        )
    # whole steps that fit: never a line above HIGH (0.03:0.05 step 0.003 ends at 0.048)
    n = math.floor((hi - lo) / st + 1e-9) + 1
    if n > PA_MAX_LINES:
        raise GenError(
            f"PA range {lo:g}:{hi:g} step {st:g} is {n} lines; at most {PA_MAX_LINES}"
        )
    return tuple(round(lo + i * st, 5) for i in range(n))


COMBO_PA_VALUES = pa_values("PLA")


def pa_label(pa: float) -> str:
    """PA x 1000, with a decimal point only when needed: 0.04 -> "40", 0.0425 -> "42.5"."""
    return f"{round(pa * 1000, 2):g}"


def text_width(text: str, w: float, gap: float) -> float:
    """Width of `text` drawn by digit_paths (digits w, "." DOT_W, gap between glyphs)."""
    return sum(DOT_W if ch == "." else w for ch in text) + gap * (len(text) - 1)


def digit_paths(
    digits: str, x0: float, y0: float, w: float, h: float, gap: float
) -> list[list[Point]]:
    """Polylines drawing `digits` (and ".") with their lower-left corner at (x0, y0); strokes
    that share an end are chained so each digit needs few retractions."""
    paths: list[list[Point]] = []
    ox = x0
    for ch in digits:
        if ch == ".":
            paths.append([(ox, y0), (ox + DOT_W, y0)])
            ox += DOT_W + gap
            continue
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
        ox += w + gap
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
    x0: float,
    y0: float,
    ex: Extrusion,
    stock_pa: float,
    accel: float,
    *,
    values: tuple[float, ...] = COMBO_PA_VALUES,
    pitch: float = COMBO_PITCH,
    segments: tuple[float, float, float] = COMBO_SEGMENTS,
) -> list[str]:
    """The PA line block: per value a label (PA x 1000) at x0, then a slow-fast-slow line
    after SET_PRESSURE_ADVANCE. Ends with the stock PA, M221 S100 and the process's own
    acceleration (`accel`): all three are global and the slicer's body must not inherit them.
    Extrusion is relative (M83), as the slicer's body (gen_combo checks the preset)."""
    lw, lh, lgap = COMBO_LABEL
    slow, fast = COMBO_SPEEDS
    xs = x0 + pa_label_width(values) + 2.0
    out = [
        "; 3d calibrate gen combo: PA lines, label = PA x 1000; slow-fast-slow per line",
        "M221 S100",
        "G90",
        "M83",
        f"SET_VELOCITY_LIMIT ACCEL={COMBO_ACCEL} ACCEL_TO_DECEL={COMBO_ACCEL // 2}",
        "M106 S0",
        f"G1 E-{ex.retract:.3f} F{ex.retract_f:.0f}",
    ]
    for i, pa in enumerate(values):
        y = y0 + i * pitch
        out.append(f"SET_PRESSURE_ADVANCE ADVANCE={stock_pa:g}")
        for path in digit_paths(pa_label(pa), x0, y - lh / 2, lw, lh, lgap):
            out += _path_gcode(path, ex, [10.0] * (len(path) - 1))
        out.append(f"SET_PRESSURE_ADVANCE ADVANCE={pa:g}")
        a, b, c = segments
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


def pa_label_width(values: tuple[float, ...]) -> float:
    lw, _, lgap = COMBO_LABEL
    return max(text_width(pa_label(v), lw, lgap) for v in values)


def _first(preset: dict[str, Any], key: str) -> float:
    """A numeric preset option (Orca keeps some as one-element lists)."""
    try:
        v = preset[key]
        return float(v[0] if isinstance(v, list) else v)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise GenError(
            f"Orca preset option {key!r} missing or not a number: {e}"
        ) from e


def require_relative_e(text: str, p: Presets) -> None:
    """Hand-written blocks use relative E (M83); the sliced footer is authoritative (the K1
    preset leaves the option at Orca's default)."""
    rel_e = re.search(r"^; use_relative_e_distances = (\d)", text, re.M)
    if rel_e is None or rel_e.group(1) != "1":
        raise GenError(
            f"{p.machine}: hand-written lines are relative-E (M83); the slice is not relative-E"
        )


def filament_area(fpre: dict[str, Any]) -> float:
    diameter = _first(fpre, "filament_diameter")
    if diameter <= 0:
        raise GenError(
            f"preset values must be positive: filament_diameter={diameter:g}"
        )
    return math.pi * diameter**2 / 4


def first_layer_extrusion(
    proc: dict[str, Any], fpre: dict[str, Any], mach: dict[str, Any]
) -> Extrusion:
    """Line parameters of hand-written first-layer G-code (PA lines, labels) from the presets."""
    layer_h = _first(proc, "initial_layer_print_height")
    ratio = _first(fpre, "filament_flow_ratio")
    if layer_h <= 0 or ratio <= 0:
        raise GenError(
            f"preset values must be positive: initial_layer_print_height={layer_h:g}, "
            f"filament_flow_ratio={ratio:g}"
        )
    return Extrusion(
        layer_h=layer_h,
        line_w=COMBO_LINE_W,
        e_per_mm=COMBO_LINE_W * layer_h * ratio / filament_area(fpre),
        retract=_first(mach, "retraction_length"),
        retract_f=_first(mach, "retraction_speed") * 60,
        lift=COMBO_LIFT,
    )


def gen_combo(
    slicer: str,
    idx: ProfileIndex,
    p: Presets,
    material: str,
    color: str,
    out: Path,
    temp: int | None,
    pa: tuple[float, ...] = COMBO_PA_VALUES,
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
    require_relative_e(text, p)
    ex = first_layer_extrusion(proc, fpre, mach)
    ratio = _first(fpre, "filament_flow_ratio")
    lw, lh, lgap = COMBO_LABEL
    block_w = pa_label_width(pa) + 2.0 + sum(COMBO_SEGMENTS)
    pa_x0 = (p.bed[0] - block_w) / 2
    pa_y0 = p.bed[1] * COMBO_PA_Y_FRAC
    if pa_y0 + (len(pa) - 1) * COMBO_PITCH + lh >= tiles_y - COMBO_CLEARANCE:
        raise GenError("combo layout: the PA lines run into the flow tiles on this bed")
    # The PA block runs right after START_PRINT, before the slicer's first layer: the bed is hot,
    # the nozzle primed, and every line lies on the bare plate.
    block = pa_lines(
        pa_x0, pa_y0, ex, p.stock_pa, _first(proc, "default_acceleration"), values=pa
    )
    text = insert_after(text, r"^START_PRINT\b.*$", block)
    text = flow_factors(text)
    text = insert_before_end(
        text, ["M221 S100", f"SET_PRESSURE_ADVANCE ADVANCE={p.stock_pa:g}"]
    )
    publish(text, out)
    finish(out, log, mach)
    labels = ", ".join(f"{pa_label(v)}={v:g}" for v in pa)
    return Generated(
        "combo",
        [out],
        [
            f"Front: {len(pa)} PA lines, bottom to top (front to back), label = PA x 1000: {labels}.",
            f"Each line: {COMBO_SEGMENTS[0]:g} mm at {COMBO_SPEEDS[0]:g} mm/s, {COMBO_SEGMENTS[1]:g} mm at "
            f"{COMBO_SPEEDS[1]:g} mm/s, {COMBO_SEGMENTS[2]:g} mm slow. Pick the line whose width stays even "
            "at both speed changes; Record: 3d calibrate record pressure_advance --printer PRINTER "
            "--values pressure_advance=VALUE --method pa_lines",
            f"Back: flow tiles printed at PA {p.stock_pa:g} (stock).",
            *flow_notes(ratio),
        ],
    )


# Temperature tower: one block per temperature, hottest at the bottom (the first layers need the
# best adhesion). A block is two pillars joined by a bridge slab at its top: the gap shows
# stringing, the slab bridging, the outer walls surface and layer bonding. The block's
# temperature stands up from its floor in the gap (the base plate, then the slab below), so a
# label never overhangs and is printed at its own block's temperature; read it from the front.
TEMP_RANGES = {
    "PLA": (230, 200),
    "PLA+": (230, 200),
    "PETG": (250, 220),
}  # bottom, top (°C)
TEMP_STEP = 5
TEMP_BLOCK_H = 8.0
TEMP_PILLAR = 9.0  # pillar width and depth (mm)
TEMP_GAP = (
    14.0  # gap between the pillars = bridge length; under print-preflight's 15 mm limit
)
TEMP_SLAB_H = 1.6
TEMP_BASE = (44.0, 20.0, 1.0)  # base plate under the tower for adhesion (x, y, z)
TEMP_DIGIT = (
    2.0,
    4.0,
    0.8,
    0.8,
)  # digit width, depth (along y), gap; stroke width (mm)
TEMP_EMBOSS = 0.6  # label height above the floor (mm)


def temp_steps(material: str) -> list[int]:
    if material not in TEMP_RANGES:
        raise GenError(
            f"no temperature range for {material!r} (known: {', '.join(TEMP_RANGES)})"
        )
    hot, cold = TEMP_RANGES[material]
    return list(range(hot, cold - 1, -TEMP_STEP))


def digit_boxes(
    digits: str,
    x0: float,
    y0: float,
    z_floor: float,
    digit: tuple[float, float, float, float] = TEMP_DIGIT,
    emboss: float = TEMP_EMBOSS,
) -> list[Box]:
    """Seven-segment `digits` (and ".") lying on a floor at z_floor, raised `emboss`, read from
    the front (digit 'up' = +y), lower-left corner at (x0, y0). `digit` = width, depth, gap,
    stroke width; a "." is one stroke square on the baseline."""
    w, h, gap, s = digit
    out: list[Box] = []
    ox = x0
    for ch in digits:
        if ch == ".":
            out.append(
                (
                    ox,
                    y0 - s / 2,
                    z_floor,
                    ox + s,
                    y0 + s / 2,
                    z_floor + emboss,
                )
            )
            ox += s + gap
            continue
        for seg in _DIGITS[ch]:
            (ax, ay), (bx, by) = _SEG[seg]
            xa, xb = sorted((ox + ax * w, ox + bx * w))
            ya, yb = sorted((y0 + ay * h / 2, y0 + by * h / 2))
            # a stroke is the segment grown by half the stroke width on every side
            out.append(
                (
                    xa - s / 2,
                    ya - s / 2,
                    z_floor,
                    xb + s / 2,
                    yb + s / 2,
                    z_floor + emboss,
                )
            )
        ox += w + gap
    return out


@dataclass(frozen=True)
class TowerShape:
    """Temperature tower dimensions (mm); the default is `gen temp`'s full-size tower."""

    pillar: float = TEMP_PILLAR
    gap: float = TEMP_GAP
    block_h: float = TEMP_BLOCK_H
    slab_h: float = TEMP_SLAB_H
    base: tuple[float, float, float] = TEMP_BASE
    digit: tuple[float, float, float, float] = TEMP_DIGIT

    def block_floor(self, i: int) -> float:
        return self.base[2] + i * self.block_h


FULL_TOWER = TowerShape()


def temp_tower_boxes(
    steps: list[int], cx: float, cy: float, shape: TowerShape = FULL_TOWER
) -> list[Box]:
    bx, by, bz = shape.base
    width = 2 * shape.pillar + shape.gap
    x0, y0 = cx - width / 2, cy - shape.pillar / 2
    boxes: list[Box] = [(cx - bx / 2, cy - by / 2, 0.0, cx + bx / 2, cy + by / 2, bz)]
    w, h, gap, _ = shape.digit
    label_w = 3 * w + 2 * gap
    if label_w > shape.gap or h > shape.pillar:
        raise GenError(
            f"temperature labels ({label_w:g} x {h:g} mm) do not fit the {shape.gap:g} mm gap"
        )
    lx = x0 + shape.pillar + (shape.gap - label_w) / 2
    ly = y0 + (shape.pillar - h) / 2
    for i, t in enumerate(steps):
        z0 = shape.block_floor(i)
        top = z0 + shape.block_h
        boxes.extend(
            (px, y0, z0, px + shape.pillar, y0 + shape.pillar, top - shape.slab_h)
            for px in (x0, x0 + shape.pillar + shape.gap)
        )
        boxes.append((x0, y0, top - shape.slab_h, x0 + width, y0 + shape.pillar, top))
        boxes += digit_boxes(str(t), lx, ly, z0, shape.digit)
    return boxes


def temp_changes(text: str, steps: list[int]) -> str:
    """`M104 S<t>` on the first layer of each block above the first (Orca marks every layer
    with `;Z:<height>` after `;LAYER_CHANGE`); the first block prints at the slice's own
    temperature, which gen_temp sets to steps[0]."""
    bz = TEMP_BASE[2]
    starts = [bz + i * TEMP_BLOCK_H for i in range(1, len(steps))]
    lines = text.split("\n")
    out: list[str] = []
    nxt = 0
    for line in lines:
        out.append(line)
        m = re.match(r";Z:([\d.]+)\s*$", line)
        if m and nxt < len(starts) and float(m.group(1)) > starts[nxt] + 1e-6:
            out.append(
                f"M104 S{steps[nxt + 1]} ; 3d calibrate gen temp: block {nxt + 2}"
            )
            nxt += 1
    if nxt != len(starts):
        raise GenError(
            f"post-process: {nxt} of {len(starts)} temperature changes placed (no ;Z: markers?)"
        )
    return "\n".join(out)


def gen_temp(
    slicer: str,
    idx: ProfileIndex,
    p: Presets,
    material: str,
    color: str,
    out: Path,
) -> Generated:
    """The tower prints its own temperature range (generate() refuses --nozzle-temp)."""
    steps = temp_steps(material)
    boxes = temp_tower_boxes(steps, p.bed[0] / 2, p.bed[1] / 2)
    fil = _filament_overrides(color, steps[0])
    proc = {"brim_type": "no_brim", "sparse_infill_density": "15%", "wall_loops": 2}
    text, log = slice_models(
        slicer, idx, p, material, {"temp_tower.stl": boxes_stl(boxes)}, proc, fil
    )
    text = temp_changes(text, steps)
    publish(text, out)
    finish(out, log, idx.flat(p.vendor, "machine", p.machine))
    blocks = ", ".join(f"{i + 1}: {t} °C" for i, t in enumerate(steps))
    return Generated(
        "temp",
        [out],
        [
            f"{len(steps)} blocks of {TEMP_BLOCK_H:g} mm, bottom to top: {blocks}; each block is labelled.",
            "Per block look at stringing in the gap, the bridge underside, wall surface and layer bonding "
            "(try to break the top blocks). min = coldest block that still bonds and bridges, max = hottest "
            "without stringing or droop, best = the cleanest.",
            "Record: 3d calibrate record temp_tower --filament ID --values nozzle_temp_min=MIN,"
            "nozzle_temp_best=BEST,nozzle_temp_max=MAX --method temp_tower",
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
        "# run on an idle printer; the sweeps shake the head for ~5 min).",
        "G28",
        # A hot nozzle keeps the hotend fan running, and its vibration swamps the resonance
        # (K1 SE: G28 heats the nozzle to 140 °C for its probe; it cools in ~6 min).
        "M104 S0",
        "M107",
        "TEMPERATURE_WAIT SENSOR=extruder MAXIMUM=38",
        "ACCELEROMETER_QUERY",
        "MEASURE_AXES_NOISE",
        f"# resonance_tester probe point from printer.cfg: {px:g},{py:g},{pz:g}",
        # One run for both axes: Creality's Klipper writes an AXIS=Y result into X as well.
        "SHAPER_CALIBRATE",
        "# Read the recommended shaper_type/shaper_freq per axis from the console, then:",
        "#   3d calibrate record input_shaper --printer PRINTER --method klipper_shaper_calibrate \\",
        "#     --values shaper_type_x=T,shaper_freq_x=F,shaper_type_y=T,shaper_freq_y=F",
        "# Then SAVE_CONFIG while idle (it restarts Klipper). Creality's SHAPER_CALIBRATE already",
        "# writes printer.cfg itself (CXSAVE_CONFIG) without applying the values until a restart.",
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


def all_kinds() -> tuple[str, ...]:
    """Single tests (KINDS) and the plates of the series (calibration_plates.PLATES)."""
    from slicing.calibration_plates import (
        PLATES,
    )  # lazy: the plate engine imports this module

    return KINDS + tuple(PLATES)


def generate(
    kind: str,
    model: str,
    slicer: str,
    out: Path,
    *,
    material: str = "PLA",
    color: str = "",
    temp: int | None = None,
    only: frozenset[str] = frozenset(),
    skip: frozenset[str] = frozenset(),
    pa_range: tuple[float, float] | None = None,
    pa_step: float | None = None,
    presets: PresetChoice | None = None,
) -> Generated:
    """One calibration print. `only`/`skip` pick the tests of a plate (plateN kinds only);
    `pa_range`/`pa_step` set the PA line values (combo, plates) instead of the material's;
    `presets` are the slicer presets to slice with (required for everything but shaper)."""
    if kind not in all_kinds():
        raise GenError(f"unknown kind {kind!r} (accepted: {', '.join(all_kinds())})")
    if kind not in KINDS:  # a plate of the series (lib/slicing/calibration_plates)
        from slicing.calibration_plate import build_plate, make_context
        from slicing.calibration_plates import PLATES, resolve_tests

        ctx = make_context(
            model,
            slicer,
            material,
            color,
            temp,
            resolve_tests(kind, only, skip),
            pa_values(material, pa_range, pa_step),
            presets,
        )
        return build_plate(ctx, PLATES[kind].build(ctx), slicer, out)
    if (pa_range is not None or pa_step is not None) and kind != "combo":
        raise GenError(
            f"--pa-range/--pa-step set the PA lines of combo and the plates, not {kind!r}"
        )
    if only or skip:
        raise GenError(
            f"--only/--skip pick the tests of a plate; {kind!r} is a single test"
        )
    if kind == "shaper":
        return gen_shaper(presets_for(model), out)
    if kind == "temp" and temp is not None:
        ranges = ", ".join(f"{m} {a}->{b}" for m, (a, b) in TEMP_RANGES.items())
        raise GenError(f"gen temp prints its own range ({ranges}); drop --nozzle-temp")
    p, idx = chosen_presets(model, slicer, material, presets)
    if kind == "pa":
        return gen_pa(slicer, idx, p, material, color, out, temp)
    if kind == "combo":
        return gen_combo(
            slicer,
            idx,
            p,
            material,
            color,
            out,
            temp,
            pa_values(material, pa_range, pa_step),
        )
    if kind == "temp":
        return gen_temp(slicer, idx, p, material, color, out)
    return gen_flow(slicer, idx, p, material, color, out, temp)
