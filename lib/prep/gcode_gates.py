"""gcode_gates.py — parse a sliced G-code once and run gates on it that can FAIL (`3d prep` proof of the slice).

WHAT: `parse(path)` reads an Orca/Snapmaker-dialect G-code in one pass into `Parsed` (layers with z/height,
  extrusion runs by `;TYPE:` with tool and XY points, tool sequence, header comments incl. print time and
  filament grams). `check_gcode(path, spec, expectations)` runs the gates on it and returns a `GcodeReport`
  (a `contracts.StepResult`): `walls`, `infill`, `supports`, `layer-heights`, `mix-layer-cap`, `tools-allowed`,
  `region-tools`, `mix-minor-adjacent`, `prime-tower`, `bed-bounds`, `feedrates`, `overrides`.
  `GateExpectations` carries what only the project builder knows (which tool/mix pattern prints in which Z slab);
  `GateExpectations.from_manifest(path)` reads the `NAME.manifest.json` the builder writes next to the 3MF.
WHY: the Chekhov U1 print needed hours of slice-inspect-reslice because wrong wall counts, 30 % infill, a stray
  support, thick mixed layers or a minor colour printed on adjacent layers were only found by eye on the preview
  (`verify_gcode.py` in the chekhov-u1 worktree). Every gate here has a deliberately broken input in the tests that
  makes it FAIL, so a PASS means something.
INVARIANTS:
  * A gate with nothing to check is a SKIP whose detail names what was missing; it is never a silent PASS. The report
    verdict is the worst gate (`contracts.worst`): FAIL > WARN > PASS, SKIP only when every gate skipped.
  * A bad G-code is a FAIL verdict with reasons, never an exception; a missing file raises `InputNotFound`.
  * G-code dialect: `;LAYER_CHANGE`, `;Z:` (top of the layer), `;HEIGHT:` (first one after the change is the layer
    height), `;TYPE:<feature>`, `Tn` tool changes (tool numbers are 0-based G-code tools: slot n = `T(n-1)`),
    `G0/G1` moves with X/Y/E, relative (M83) or absolute (M82) extrusion. Extrusion before the first
    `;LAYER_CHANGE` (start G-code purge lines) is not part of any layer.
  * Manifest `parts[]` (the project builder's real schema: name, assign{slot|mix}, tools, bbox, z_range) become
    non-exclusive regions: parts overlap in Z, so a mix is judged only on its EXCLUSIVE tools (those no part
    overlapping it in Z and XY also prints with) and only on extrusion inside the part's bbox; see `exclusive_tools`.
  * Regions are Z slabs (a layer belongs to a region when its mid-plane is inside [z_min, z_max]) in which the WHOLE
    layer is expected to print with the region's tools; prime tower, wipe tower, skirt, brim and `Custom` runs are
    ignored when attributing a layer to tools.
  * A mix pattern's index is the layer number counted from the first print layer (Orca's `MixedFilament::resolve`),
    one entry per layer, repeating.
  * Parse speed: ~1 M lines in a few seconds (one regex per move line, points kept as flat float lists until a layer
    is closed, then one numpy array per run).
ASSUMPTIONS: sparse-infill density is only estimated on layers with exactly one outer-wall loop (one island, no
  holes), from the area inside the innermost inner-wall loop; on a hollow or multi-island model pass a `zone` of
  solid islands (`GateSpec.zone` or the manifest's `structural` slab) or switch the gate off (`infill=None`).
  The mixed-colour rule "a minor colour never prints on adjacent layers" is evaluated by
  `slicing.mix_pattern.analyse` (ticket #410) when that module exists; until #410 merges the local fallback
  `_minor_adjacent_fallback` applies the same rule (minor = a tool with <= 50 % of the region's height; two adjacent
  layers of the same minor tool violate it). The fallback is deleted when #410 merges.
Past bugs: none yet.
"""

from __future__ import annotations

import importlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import InputNotFound, InvalidArgument
from prep.contracts import Verdict, worst

#: runs that are not the model itself: they never decide which tool a layer "is"
NON_MODEL_KINDS = frozenset({"Prime tower", "Wipe tower", "Custom", "Skirt", "Brim"})
WALL_KINDS = frozenset({"Outer wall", "Inner wall"})
_EPS_MM = 1e-3
_BBOX_MARGIN_MM = (
    0.2  # extrusion centre lines sit inside the mesh bbox; absorb rounding
)
_MIN_LOOP_POINTS = 8  # a wall run with fewer points is a stub, not a loop
_DEFAULT_LINE_MM = 0.45

_HEADER_KEYS = frozenset(
    {
        "wall_loops",
        "sparse_infill_density",
        "sparse_infill_pattern",
        "enable_support",
        "sparse_infill_line_width",
        "layer_height",
        "initial_layer_print_height",
        "min_layer_height",
        "max_layer_height",
        "filament_colour",
        "outer_wall_line_width",
        "inner_wall_line_width",
        "enable_prime_tower",
        "printable_area",
        "filament used [g]",
        "estimated printing time (normal mode)",
    }
)
_WORD = re.compile(r"([XYEF])(-?\d*\.?\d+)")
_TOOL = re.compile(r"T(\d+)\s*(?:;.*)?$")
_E_WORD = re.compile(r"E(-?\d*\.?\d+)")
_DURATION = re.compile(r"(\d+)\s*([dhms])")
_DURATION_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}

Pts = NDArray[np.float64]


# ---------------------------------------------------------------- parsed G-code


@dataclass(slots=True)
class Run:
    """One uninterrupted extrusion of one feature with one tool; `pts[0]` is where the nozzle stood when it started."""

    kind: str
    tool: int | None
    pts: Pts  # (n, 2) mm


@dataclass
class Layer:
    z: float  # top of the layer
    h: float  # thickness
    runs: list[Run]
    tool_changes: list[int]  # Tn commands inside the layer, in order

    @cached_property
    def model_tools(self) -> frozenset[int]:
        """Tools that printed the model in this layer (tower, skirt, brim and start purge excluded)."""
        return frozenset(
            r.tool
            for r in self.runs
            if r.kind not in NON_MODEL_KINDS and r.tool is not None
        )

    @property
    def mid(self) -> float:
        return self.z - self.h / 2


@dataclass
class Parsed:
    path: str
    layers: list[Layer]
    header: dict[str, str]
    types: dict[str, int]  # `;TYPE:` block count over the whole file
    filament_g: list[float]
    time: str | None
    time_s: float | None
    tool_sequence: list[int]  # every Tn line in file order
    n_tool_changes: int  # Tn lines that switched to a different tool
    n_lines: int
    extents: (
        tuple[float, float, float, float] | None
    )  # x0, x1, y0, y1 of model extrusion (no `Custom`)
    parse_s: float
    #: fastest feedrate (mm/s) the G-code commands for each feature (`;TYPE:` name), plus `travel` (moves without
    #: extrusion) and `wipe` (retracting XY moves); only moves inside a layer count, the start G-code does not
    feed_max: dict[str, float] = field(default_factory=dict)
    #: every `; key = value` comment of the file (the slicer's config block): the settings the slice really used
    config: dict[str, str] = field(default_factory=dict)

    def tools_used(self) -> set[int]:
        """Tools that extruded anything inside a layer (the start-G-code purge does not count)."""
        return {
            r.tool for layer in self.layers for r in layer.runs if r.tool is not None
        }


def _duration_s(text: str) -> float | None:
    parts = _DURATION.findall(text)
    return float(sum(int(n) * _DURATION_S[u] for n, u in parts)) if parts else None


def _make_layer(
    z: float | None,
    h: float | None,
    raw: list[tuple[str, int | None, list[float]]],
    changes: list[int],
    prev_z: float,
) -> Layer | None:
    """Close a layer; a layer with no `;Z:` cannot be placed and is dropped, a missing `;HEIGHT:` is z - previous z."""
    if z is None:
        return None
    runs = [
        Run(k, t, np.asarray(flat, dtype=np.float64).reshape(-1, 2))
        for k, t, flat in raw
    ]
    return Layer(z, z - prev_z if h is None else h, runs, changes)


def parse(path: str | Path) -> Parsed:
    """One pass over the G-code: layers, extrusion runs by feature and tool, tool sequence, header comments."""
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(str(p), command="prep")
    started = time.perf_counter()
    layers: list[Layer] = []
    header: dict[str, str] = {}
    types: Counter[str] = Counter()
    tool_seq: list[int] = []
    n_changes = 0
    n_lines = 0
    tool: int | None = None
    kind = "Unknown"
    x: float | None = None
    y: float | None = None
    rel_e = False
    e_abs = 0.0
    feed = 0.0  # modal F, mm/min
    feed_max: dict[str, float] = {}
    config: dict[str, str] = {}
    in_layer = False
    z: float | None = None
    h: float | None = None
    raw: list[tuple[str, int | None, list[float]]] = []
    changes: list[int] = []
    run: list[float] | None = None
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    find = _WORD.findall

    with open(p, errors="replace") as fh:
        for line in fh:
            n_lines += 1
            c = line[:1]
            if c == "G":
                if line.startswith(("G1 ", "G0 ")):
                    if ";" in line:
                        line = line.partition(";")[0]
                    nx, ny, ev = x, y, None
                    moved = False
                    for k, v in find(line):
                        if k == "E":
                            ev = float(v)
                        elif k == "F":
                            feed = float(v)
                        elif k == "X":
                            nx = float(v)
                            moved = True
                        else:
                            ny = float(v)
                            moved = True
                    extruding = False
                    if ev is not None and line[1] == "1":
                        if rel_e:
                            extruding = ev > 0
                        else:
                            extruding = ev > e_abs
                            e_abs = ev
                    if (
                        extruding
                        and moved
                        and in_layer
                        and nx is not None
                        and ny is not None
                    ):
                        if run is None:
                            run = [nx if x is None else x, ny if y is None else y]
                            raw.append((kind, tool, run))
                        run.append(nx)
                        run.append(ny)
                        if kind != "Custom":
                            if nx < x0:
                                x0 = nx
                            if nx > x1:
                                x1 = nx
                            if ny < y0:
                                y0 = ny
                            if ny > y1:
                                y1 = ny
                    else:
                        run = None
                    if moved and in_layer and feed > 0:
                        name = (
                            kind
                            if extruding
                            else ("wipe" if ev is not None and ev < 0 else "travel")
                        )
                        if feed > feed_max.get(name, 0.0):
                            feed_max[name] = feed
                    x, y = nx, ny
                elif line.startswith("G92"):
                    m = _E_WORD.search(line)
                    if m:
                        e_abs = float(m.group(1))
            elif c == ";":
                if line.startswith(";TYPE:"):
                    kind = line[6:].strip()
                    types[kind] += 1
                    run = None
                elif line.startswith("; FEATURE:"):
                    kind = line[10:].strip()
                    types[kind] += 1
                    run = None
                elif line.startswith(";LAYER_CHANGE"):
                    if in_layer:
                        done = _make_layer(
                            z, h, raw, changes, layers[-1].z if layers else 0.0
                        )
                        if done is not None:
                            layers.append(done)
                    in_layer, z, h, raw, changes, run = True, None, None, [], [], None
                elif line.startswith(";Z:") and in_layer:
                    z = float(line[3:])
                elif line.startswith(";HEIGHT:") and in_layer and h is None:
                    h = float(line[8:])
                elif line.startswith("; ") and " = " in line:
                    key, _, val = line[2:].partition(" = ")
                    config[key] = val.strip()
                    if key in _HEADER_KEYS:
                        header.setdefault(key, val.strip())
            elif c == "T":
                m = _TOOL.match(line)
                if (
                    m and int(m.group(1)) < 200
                ):  # Tn >= 200 would be a macro, not a tool
                    t = int(m.group(1))
                    if tool is not None and t != tool:
                        n_changes += 1
                    tool = t
                    tool_seq.append(t)
                    if in_layer:
                        changes.append(t)
                    run = None
            elif c == "M":
                if line.startswith("M83"):
                    rel_e = True
                elif line.startswith("M82"):
                    rel_e = False
    if in_layer:
        done = _make_layer(z, h, raw, changes, layers[-1].z if layers else 0.0)
        if done is not None:
            layers.append(done)

    grams = header.get("filament used [g]", "")
    try:
        filament_g = [float(v) for v in grams.split(",")] if grams else []
    except ValueError:
        filament_g = []
    time_text = header.get("estimated printing time (normal mode)")
    return Parsed(
        path=str(p),
        layers=layers,
        header=header,
        types=dict(types),
        filament_g=filament_g,
        time=time_text,
        time_s=_duration_s(time_text) if time_text else None,
        tool_sequence=tool_seq,
        n_tool_changes=n_changes,
        n_lines=n_lines,
        extents=(x0, x1, y0, y1) if x0 <= x1 else None,
        parse_s=time.perf_counter() - started,
        feed_max={k: v / 60.0 for k, v in feed_max.items()},
        config=config,
    )


# ---------------------------------------------------------------- spec / expectations


@dataclass(frozen=True)
class GateSpec:
    """What the slice must satisfy. `None` in an optional field switches that gate off (it reports SKIP)."""

    walls: int | None = (
        4  # at least this many outer+inner wall loops per layer of the zone
    )
    wall_deficit_fraction: float = (
        0.02  # share of zone layers allowed below `walls` (tips, tiny top layers)
    )
    infill: float | None = 0.15  # sparse infill density, 0..1
    infill_tol: float = 0.25  # relative tolerance of the density estimate
    infill_min_layers: int = 3  # layers needed for a usable estimate
    supports: bool = False  # False: no `;TYPE:Support*` allowed
    hmin: float = 0.08
    hmax: float = 0.32
    rare_fraction: float = (
        0.25  # at most this share of layers (first layer excluded) may sit at hmin
    )
    mix_cap: float = 0.12  # thickest layer allowed inside a mix region
    bed_mm: tuple[float, float, float] = (270.0, 270.0, 270.0)
    tools: frozenset[int] | None = (
        None  # allowed G-code tools; None -> `GateExpectations.allowed_tools`
    )
    zone: tuple[float, float] | None = (
        None  # Z slab for walls/infill; None -> manifest `structural`, else all layers
    )
    line_width_mm: float = _DEFAULT_LINE_MM  # used when the G-code header does not say
    #: feature (`;TYPE:` name or `travel`) -> the speed in mm/s the slicer was asked for; the `feedrates` gate FAILs
    #: when the G-code commands more than that. `None`: the gate only reports what the G-code does.
    speed_limits: dict[str, float] | None = None
    #: setting -> the value the G-code's config block must show (what the slicer was asked for); the `overrides`
    #: gate FAILs on a setting the slicer did not take. `None`: not requested.
    expect_settings: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "walls": self.walls,
            "wall_deficit_fraction": self.wall_deficit_fraction,
            "infill": self.infill,
            "infill_tol": self.infill_tol,
            "infill_min_layers": self.infill_min_layers,
            "supports": self.supports,
            "hmin": self.hmin,
            "hmax": self.hmax,
            "rare_fraction": self.rare_fraction,
            "mix_cap": self.mix_cap,
            "bed_mm": list(self.bed_mm),
            "tools": sorted(self.tools) if self.tools is not None else None,
            "zone": list(self.zone) if self.zone is not None else None,
            "line_width_mm": self.line_width_mm,
            "speed_limits": self.speed_limits,
            "expect_settings": self.expect_settings,
        }


@dataclass(frozen=True)
class Region:
    """A Z slab with the tools it must print with. `tools` = only these tools; `mix` = the per-layer cycle.

    `exclusive=True` (manifest `regions`): the whole layer inside the slab belongs to this region.
    `exclusive=False` (manifest `parts`): the part overlaps others in z, `bbox` = (x0, y0, x1, y1) mm in G-code
    coordinates is where it sits, and only tools no overlapping part also uses can be attributed to it.
    """

    name: str
    z_min: float
    z_max: float
    tools: tuple[int, ...] | None = None
    mix: tuple[int, ...] | None = None
    bbox: tuple[float, float, float, float] | None = None
    exclusive: bool = True


@dataclass(frozen=True)
class GateExpectations:
    """What only the project builder knows. All fields optional; every gate that needs a missing one SKIPs.

    Manifest (`NAME.manifest.json`) keys, all optional:
      `regions`: [{`name`, `z_min`, `z_max`, `tools`|`slots`, `mix`|`mix_slots`}]. `tools`/`mix` are 0-based G-code
        tool numbers (list of ints, or a digit string for `mix`); `slots`/`mix_slots` are the same in 1-based filament
        slots (slot n = `T(n-1)`).
      `allowed_tools` (Tn list) or `heads_slots` (1-based slots): tools the print may use.
      `prime_tower`: bool — true/false demands exactly that; absent demands a tower only when >1 tool prints.
      `structural`: {`z_min`, `z_max`} — Z slab of the solid, simple part used by the walls/infill gates.
    """

    regions: tuple[Region, ...] = ()
    allowed_tools: frozenset[int] | None = None
    prime_tower: bool | None = None
    structural_zone: tuple[float, float] | None = None

    @classmethod
    def from_manifest(cls, path: str | Path) -> GateExpectations:
        """Load `NAME.manifest.json`; missing keys mean "no expectation". Malformed values raise `InvalidArgument`."""
        p = Path(path)
        if not p.is_file():
            raise InputNotFound(str(p), command="prep")
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise InvalidArgument(
                "manifest", str(p), ["a JSON object"], command="prep", extra=str(exc)
            ) from exc
        if not isinstance(data, dict):
            raise InvalidArgument("manifest", str(p), ["a JSON object"], command="prep")
        regions = tuple(_region(r, str(p)) for r in data.get("regions") or [])
        if not regions:
            parts = (_part_region(r, str(p)) for r in data.get("parts") or [])
            regions = tuple(r for r in parts if r is not None)
        allowed: frozenset[int] | None = None
        if data.get("allowed_tools") is not None:
            allowed = frozenset(
                _tool_list(data["allowed_tools"], 0, "allowed_tools", str(p))
            )
        elif data.get("heads_slots") is not None:
            allowed = frozenset(
                _tool_list(data["heads_slots"], -1, "heads_slots", str(p))
            )
        tower = data.get("prime_tower")
        zone: tuple[float, float] | None = None
        st = data.get("structural")
        if isinstance(st, dict) and "z_min" in st and "z_max" in st:
            zone = (float(st["z_min"]), float(st["z_max"]))
        return cls(
            regions=regions,
            allowed_tools=allowed,
            prime_tower=tower if isinstance(tower, bool) else None,
            structural_zone=zone,
        )


def _tool_list(value: object, shift: int, key: str, src: str) -> tuple[int, ...]:
    if isinstance(value, str) and value.isdigit():
        return tuple(int(ch) + shift for ch in value)
    if isinstance(value, list) and all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        return tuple(v + shift for v in value)
    raise InvalidArgument(
        f"manifest {key}",
        repr(value),
        ["list of ints", "digit string"],
        command="prep",
        extra=src,
    )


def _region(raw: object, src: str) -> Region:
    if not isinstance(raw, dict) or "z_min" not in raw or "z_max" not in raw:
        raise InvalidArgument(
            "manifest regions[]",
            repr(raw),
            ["object with z_min and z_max"],
            command="prep",
            extra=src,
        )

    def pick(plain: str, slots: str) -> tuple[int, ...] | None:
        if raw.get(plain) is not None:
            return _tool_list(raw[plain], 0, plain, src)
        if raw.get(slots) is not None:
            return _tool_list(raw[slots], -1, slots, src)
        return None

    return Region(
        name=str(raw.get("name", f"z{raw['z_min']}-{raw['z_max']}")),
        z_min=float(raw["z_min"]),
        z_max=float(raw["z_max"]),
        tools=pick("tools", "slots"),
        mix=pick("mix", "mix_slots"),
    )


def _part_region(raw: object, src: str) -> Region | None:
    """One manifest `parts[]` entry -> Region, None when the part names no tool (nothing to judge)."""
    if not isinstance(raw, dict):
        raise InvalidArgument(
            "manifest parts[]", repr(raw), ["object"], command="prep", extra=src
        )
    raw_assign = raw.get("assign")
    assign: dict[str, Any] = raw_assign if isinstance(raw_assign, dict) else {}
    mix: tuple[int, ...] | None = None
    tools: tuple[int, ...] | None = None
    if assign.get("mix") is not None:
        mix = _tool_list(str(assign["mix"]), -1, "parts[].assign.mix", src)
    elif assign.get("slot") is not None:
        tools = (int(assign["slot"]) - 1,)
    elif raw.get("tools") is not None:
        tools = _tool_list(raw["tools"], 0, "parts[].tools", src)
    if mix is None and tools is None:
        return None
    bbox_raw = raw.get("bbox")
    zr = raw.get("z_range")
    if zr is None and bbox_raw is not None:
        zr = [bbox_raw[0][2], bbox_raw[1][2]]
    if zr is None:
        return None
    bbox: tuple[float, float, float, float] | None = None
    if bbox_raw is not None:
        (ax, ay, _az), (bx, by, _bz) = bbox_raw
        bbox = (float(ax), float(ay), float(bx), float(by))
    return Region(
        name=str(raw.get("name", "part")),
        z_min=float(zr[0]),
        z_max=float(zr[1]),
        tools=tools,
        mix=mix,
        bbox=bbox,
        exclusive=False,
    )


# ---------------------------------------------------------------- report


@dataclass(frozen=True)
class Gate:
    name: str
    verdict: Verdict
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail}


@dataclass
class GcodeReport:
    """All gates of one G-code. Satisfies `contracts.StepResult`."""

    path: str
    spec: GateSpec
    gates: list[Gate]
    stats: dict[str, Any]
    verdict: Verdict = field(init=False)

    def __post_init__(self) -> None:
        self.verdict = worst([g.verdict for g in self.gates])

    def gate(self, name: str) -> Gate:
        for g in self.gates:
            if g.name == name:
                return g
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "verdict": self.verdict,
            "spec": self.spec.to_dict(),
            "gates": [g.to_dict() for g in self.gates],
            "stats": self.stats,
        }

    def summary(self) -> str:
        counts = Counter(g.verdict for g in self.gates)
        failed = [g.name for g in self.gates if g.verdict == "FAIL"]
        tail = f" — FAIL: {', '.join(failed)}" if failed else ""
        return (
            f"{self.stats.get('layers', 0)} layers, {self.stats.get('tool_changes', 0)} tool changes, "
            f"{counts['PASS']} PASS / {counts['WARN']} WARN / {counts['FAIL']} FAIL / {counts['SKIP']} SKIP{tail}"
            f"{self.print_facts()}"
        )

    def print_facts(self) -> str:
        """`; 32m 51s, 13.56 g, x 66.0..159.8 y 107.2..149.6` of what the G-code itself says (empty when it says nothing)."""
        facts: list[str] = []
        if self.stats.get("time"):
            facts.append(str(self.stats["time"]))
        grams = self.stats.get("filament_g") or []
        if grams:
            facts.append(f"{sum(grams):.2f} g")
        ext = self.stats.get("extents")
        if ext:
            facts.append(f"x {ext[0]:.1f}..{ext[1]:.1f} y {ext[2]:.1f}..{ext[3]:.1f}")
        return f"; {', '.join(facts)}" if facts else ""


# ---------------------------------------------------------------- gates


def _poly_area(a: Pts) -> float:
    return 0.5 * abs(
        float(
            np.dot(a[:, 0], np.roll(a[:, 1], -1))
            - np.dot(a[:, 1], np.roll(a[:, 0], -1))
        )
    )


def _path_len(a: Pts) -> float:
    return float(np.hypot(*np.diff(a, axis=0).T).sum())


def _in_slab(layer: Layer, z_min: float, z_max: float) -> bool:
    return z_min - _EPS_MM <= layer.mid <= z_max + _EPS_MM


def _zone_layers(parsed: Parsed, zone: tuple[float, float] | None) -> list[Layer]:
    if zone is None:
        return parsed.layers
    return [layer for layer in parsed.layers if _in_slab(layer, *zone)]


def _zone_text(zone: tuple[float, float] | None) -> str:
    return "all layers" if zone is None else f"z {zone[0]:g}..{zone[1]:g}"


def _gate_walls(
    parsed: Parsed, spec: GateSpec, zone: tuple[float, float] | None
) -> Gate:
    if spec.walls is None:
        return Gate("walls", "SKIP", "not requested (spec.walls is None)")
    counts = [
        (
            layer.z,
            sum(
                1
                for r in layer.runs
                if r.kind in WALL_KINDS and len(r.pts) > _MIN_LOOP_POINTS
            ),
        )
        for layer in _zone_layers(parsed, zone)
    ]
    counts = [(z, n) for z, n in counts if n]
    if not counts:
        return Gate("walls", "SKIP", f"no Outer/Inner wall loops in {_zone_text(zone)}")
    short = [(z, n) for z, n in counts if n < spec.walls]
    ok = len(short) <= spec.wall_deficit_fraction * len(counts)
    e_g = f", e.g. z={short[0][0]:g} has {short[0][1]}" if short else ""
    return Gate(
        "walls",
        "PASS" if ok else "FAIL",
        f"{len(counts)} layers with walls in {_zone_text(zone)}, {len(short)} have fewer than {spec.walls} "
        f"outer+inner loops (allowed {spec.wall_deficit_fraction:.0%}){e_g}",
    )


def _gate_infill(
    parsed: Parsed, spec: GateSpec, zone: tuple[float, float] | None
) -> Gate:
    if spec.infill is None:
        return Gate("infill", "SKIP", "not requested (spec.infill is None)")
    h = parsed.header
    wi = _float(h.get("sparse_infill_line_width"), spec.line_width_mm)
    ww = _float(h.get("inner_wall_line_width"), spec.line_width_mm)
    dens: list[float] = []
    for layer in _zone_layers(parsed, zone):
        outer = [
            r
            for r in layer.runs
            if r.kind == "Outer wall" and len(r.pts) > _MIN_LOOP_POINTS
        ]
        inner = [
            r
            for r in layer.runs
            if r.kind == "Inner wall" and len(r.pts) > _MIN_LOOP_POINTS
        ]
        sparse = [r for r in layer.runs if r.kind == "Sparse infill"]
        if len(outer) != 1 or not inner or not sparse:
            continue
        loop = min(inner, key=lambda r: _poly_area(r.pts))
        # area inside the innermost wall's outer edge: centre-line area less half a line width all round
        area = _poly_area(loop.pts) - _path_len(loop.pts) * ww / 2
        if area > 50:
            dens.append(sum(_path_len(r.pts) for r in sparse) * wi / area)
    if len(dens) < spec.infill_min_layers:
        return Gate(
            "infill",
            "SKIP",
            f"only {len(dens)} single-island layers with sparse infill in {_zone_text(zone)} "
            f"(need {spec.infill_min_layers}); no density estimate possible",
        )
    est = float(np.median(dens))
    ok = abs(est - spec.infill) <= spec.infill_tol * spec.infill
    return Gate(
        "infill",
        "PASS" if ok else "FAIL",
        f"estimate {est:.3f} vs wanted {spec.infill:.3f} (±{spec.infill_tol:.0%} rel) = sum(path)*{wi:g}/area inside "
        f"the innermost wall, median of {len(dens)} layers in {_zone_text(zone)}",
    )


def _float(text: str | None, default: float) -> float:
    try:
        return float(text) if text else default
    except ValueError:
        return default


def _gate_supports(parsed: Parsed, spec: GateSpec) -> Gate:
    sup = {k: v for k, v in parsed.types.items() if "upport" in k}
    if spec.supports:
        return Gate(
            "supports",
            "PASS",
            f"supports allowed by spec; TYPE blocks: {sup or 'none'}",
        )
    return Gate(
        "supports",
        "FAIL" if sup else "PASS",
        f"TYPE blocks with 'upport': {sup or 'none'}; enable_support = {parsed.header.get('enable_support', 'unknown')}",
    )


def _gate_layer_heights(parsed: Parsed, spec: GateSpec) -> Gate:
    if not parsed.layers:
        return Gate(
            "layer-heights", "FAIL", "no layers found (no `;LAYER_CHANGE` with `;Z:`)"
        )
    zs = np.array([layer.z for layer in parsed.layers])
    hs = np.array([layer.h for layer in parsed.layers])
    consistency = float(np.abs(np.diff(np.concatenate([[0.0], zs])) - hs).max())
    body = hs[1:] if len(hs) > 1 else hs  # the first layer has its own height
    lo, hi = float(body.min()), float(body.max())
    thin = int((body < spec.hmin + 0.005).sum())
    share = thin / len(body)
    problems: list[str] = []
    if lo < spec.hmin - _EPS_MM or hi > spec.hmax + _EPS_MM:
        problems.append(
            f"outside {spec.hmin:g}..{spec.hmax:g} mm (min {lo:.3f}, max {hi:.3f})"
        )
    if share > spec.rare_fraction:
        problems.append(
            f"{share:.0%} of layers at {spec.hmin:g} mm, allowed {spec.rare_fraction:.0%}"
        )
    if consistency > 2 * _EPS_MM:
        problems.append(f"Z steps differ from ;HEIGHT by up to {consistency:.4f} mm")
    return Gate(
        "layer-heights",
        "FAIL" if problems else "PASS",
        "; ".join(problems)
        if problems
        else f"{len(hs)} layers, min {lo:.3f} max {hi:.3f}, {share:.1%} at {spec.hmin:g} mm, Z steps match ;HEIGHT",
    )


def _mix_regions(exp: GateExpectations | None) -> list[Region]:
    return [r for r in exp.regions if r.mix] if exp else []


def _gate_mix_layer_cap(
    parsed: Parsed, spec: GateSpec, exp: GateExpectations | None
) -> Gate:
    mixes = _mix_regions(exp)
    if not mixes:
        return Gate(
            "mix-layer-cap",
            "SKIP",
            "no mix region in the manifest (GateExpectations.regions[].mix)",
        )
    bad: list[str] = []
    seen = 0
    for reg in mixes:
        layers = [
            layer for layer in parsed.layers if _in_slab(layer, reg.z_min, reg.z_max)
        ]
        seen += len(layers)
        thick = [layer for layer in layers if layer.h > spec.mix_cap + 5e-4]
        if thick:
            worst_l = max(thick, key=lambda layer: layer.h)
            bad.append(
                f"{reg.name}: {len(thick)} of {len(layers)} layers above {spec.mix_cap:g} mm "
                f"(thickest {worst_l.h:.3f} at z={worst_l.z:g})"
            )
    if not seen:
        return Gate(
            "mix-layer-cap", "SKIP", "no layers fall inside any mix region's z range"
        )
    return Gate(
        "mix-layer-cap",
        "FAIL" if bad else "PASS",
        "; ".join(bad)
        if bad
        else f"{seen} layers in {len(mixes)} mix region(s), all <= {spec.mix_cap:g} mm",
    )


def _gate_tools_allowed(
    parsed: Parsed, spec: GateSpec, exp: GateExpectations | None
) -> Gate:
    allowed = (
        spec.tools if spec.tools is not None else (exp.allowed_tools if exp else None)
    )
    if allowed is None:
        return Gate(
            "tools-allowed",
            "SKIP",
            "no allowed tools given (GateSpec.tools / manifest heads_slots)",
        )
    used = parsed.tools_used()
    extra = sorted(used - allowed)
    return Gate(
        "tools-allowed",
        "FAIL" if extra else "PASS",
        f"extruding tools {_t(used)}, allowed {_t(allowed)}"
        + (f"; not allowed: {_t(extra)}" if extra else ""),
    )


def _t(tools: Iterable[int]) -> str:
    return "[" + ", ".join(f"T{t}" for t in sorted(tools)) + "]"


@dataclass(frozen=True)
class _Partial:
    """Verdict inputs for one mix region that overlaps other parts (`Region.exclusive` False).

    `skip` names why nothing can be judged; otherwise `adjacent` / `follow` list the defects of the region's
    exclusive tools measured inside its bbox, one string per tool.
    """

    skip: str | None
    adjacent: list[str]
    follow: list[str]


def _tools_of(reg: Region) -> set[int]:
    return set(reg.mix or reg.tools or ())


def exclusive_tools(reg: Region, exp: GateExpectations) -> frozenset[int]:
    """Tools of `reg`'s mix that no other part overlapping it in z AND xy also prints with."""
    mine = set(reg.mix or ())
    for other in exp.regions:
        if other is reg or not _tools_of(other):
            continue
        if not (reg.z_min < other.z_max and other.z_min < reg.z_max):
            continue
        if reg.bbox is not None and other.bbox is not None:
            ax0, ay0, ax1, ay1 = reg.bbox
            bx0, by0, bx1, by1 = other.bbox
            if not (ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1):
                continue
        mine -= _tools_of(other)
    return frozenset(mine)


def _tools_inside(
    layer: Layer, bbox: tuple[float, float, float, float] | None, wanted: frozenset[int]
) -> set[int]:
    """Which of `wanted` extrude with a point inside `bbox` (+ margin) in this layer; the tower etc. never count."""
    found: set[int] = set()
    for r in layer.runs:
        if r.tool not in wanted or r.tool in found or r.kind in NON_MODEL_KINDS:
            continue
        pts = r.pts[1:]
        if bbox is not None:
            x0, y0, x1, y1 = bbox
            m = _BBOX_MARGIN_MM
            pts = pts[
                (pts[:, 0] >= x0 - m)
                & (pts[:, 0] <= x1 + m)
                & (pts[:, 1] >= y0 - m)
                & (pts[:, 1] <= y1 + m)
            ]
        if len(pts):
            found.add(r.tool)
    return found


def _partial_mix(parsed: Parsed, reg: Region, exp: GateExpectations) -> _Partial:
    assert reg.mix
    excl = exclusive_tools(reg, exp)
    if not excl:
        return _Partial(
            f"every tool of mix {_t(reg.mix)} is also used by an overlapping part",
            [],
            [],
        )
    slab = [
        (k, layer)
        for k, layer in enumerate(parsed.layers)
        if _in_slab(layer, reg.z_min, reg.z_max)
    ]
    if not slab:
        return _Partial(f"no layers in z {reg.z_min:g}..{reg.z_max:g}", [], [])
    present = {k: _tools_inside(layer, reg.bbox, excl) for k, layer in slab}
    pat = reg.mix
    adjacent: list[str] = []
    follow: list[str] = []
    ks = [k for k, _ in slab]
    for tool in sorted(excl):
        pairs = [k for k in ks if tool in present[k] and tool in present.get(k + 1, ())]
        if pairs:
            adjacent.append(
                f"{reg.name}: T{tool} prints inside the part's bbox on {len(pairs)} adjacent layer pair(s) "
                f"(first layers {pairs[0]}-{pairs[0] + 1}, z={parsed.layers[pairs[0]].z:g})"
            )
        missing = [
            k for k in ks if pat[k % len(pat)] == tool and tool not in present[k]
        ]
        extra = [k for k in ks if pat[k % len(pat)] != tool and tool in present[k]]
        if missing or extra:
            follow.append(
                f"{reg.name}: T{tool} does not follow mix {''.join(map(str, pat))} inside the bbox — "
                f"absent on {len(missing)} layer(s) where the pattern wants it"
                f"{' (first ' + str(missing[0]) + ')' if missing else ''}, present on {len(extra)} where it must not be"
                f"{' (first ' + str(extra[0]) + ')' if extra else ''}"
            )
    return _Partial(None, adjacent, follow)


def _partials(parsed: Parsed, exp: GateExpectations | None) -> dict[Region, _Partial]:
    if exp is None:
        return {}
    return {
        r: _partial_mix(parsed, r, exp)
        for r in exp.regions
        if r.mix and not r.exclusive
    }


def _gate_region_tools(
    parsed: Parsed, exp: GateExpectations | None, partials: dict[Region, _Partial]
) -> Gate:
    regs = [r for r in exp.regions if r.tools or r.mix] if exp else []
    if not regs:
        return Gate(
            "region-tools",
            "SKIP",
            "no manifest regions with tools or mix (GateExpectations.regions)",
        )
    bad: list[str] = []
    skipped: list[str] = []
    checked = 0
    for reg in regs:
        if not reg.exclusive:
            if not reg.mix:
                skipped.append(f"{reg.name}: part overlaps other parts' tools")
                continue
            part = partials[reg]
            if part.skip:
                skipped.append(f"{reg.name}: {part.skip}")
                continue
            checked += 1
            bad += part.follow
            continue
        checked += 1
        idx = [
            (k, layer)
            for k, layer in enumerate(parsed.layers)
            if _in_slab(layer, reg.z_min, reg.z_max)
        ]
        used = [(k, layer) for k, layer in idx if layer.model_tools]
        if not used:
            bad.append(f"{reg.name}: no extrusion in z {reg.z_min:g}..{reg.z_max:g}")
            continue
        if reg.mix:
            bad += _mix_follow(reg, used)
        else:
            assert reg.tools is not None
            off = [k for k, layer in used if not layer.model_tools <= set(reg.tools)]
            if off:
                bad.append(
                    f"{reg.name}: {len(off)} of {len(used)} layers print with tools outside {_t(reg.tools)} "
                    f"(first layer {off[0]}: {_t(parsed.layers[off[0]].model_tools)})"
                )
    note = f" (skipped: {'; '.join(skipped)})" if skipped else ""
    if not checked and not bad:
        return Gate("region-tools", "SKIP", "; ".join(skipped))
    return Gate(
        "region-tools",
        "FAIL" if bad else "PASS",
        "; ".join(bad)
        if bad
        else f"{checked} region(s) print with exactly the expected tools / mix cycle{note}",
    )


def _mix_follow(reg: Region, used: list[tuple[int, Layer]]) -> list[str]:
    assert reg.mix
    pat = reg.mix
    single = {
        k: next(iter(layer.model_tools))
        for k, layer in used
        if len(layer.model_tools) == 1
    }
    multi = [k for k, layer in used if len(layer.model_tools) > 1]
    best_off, best_hits = 0, -1
    for off in range(len(pat)):
        hits = sum(1 for k, t in single.items() if pat[(k + off) % len(pat)] == t)
        if hits > best_hits:
            best_off, best_hits = off, hits
    ok_here = sum(1 for k, t in single.items() if pat[k % len(pat)] == t)
    if ok_here == len(single) and not multi:
        return []
    ks = sorted(single)[:8]
    return [
        f"{reg.name}: mix {''.join(map(str, pat))} not followed — {ok_here} of {len(single)} single-tool layers match "
        f"(best phase shift {best_off}), {len(multi)} layers with >1 tool; first tools {[single[k] for k in ks]} "
        f"expected {[pat[k % len(pat)] for k in ks]}"
    ]


def _minor_adjacent_fallback(seq: list[str], heights: list[float]) -> int:
    """Number of adjacent layer pairs where the same MINOR colour prints (see header: replaced when #410 merges)."""
    total = sum(heights)
    share: defaultdict[str, float] = defaultdict(float)
    for c, h in zip(seq, heights, strict=True):
        share[c] += h / total
    if len(share) < 2:
        return 0
    minor = {c for c, s in share.items() if s <= 0.5}
    return sum(1 for a, b in zip(seq, seq[1:], strict=False) if a == b and a in minor)


def _minor_adjacent(seq: list[str], heights: list[float], cap: float) -> int:
    try:
        mp = importlib.import_module("slicing.mix_pattern")
        rules = mp.MixRules(layer_cap_mm=cap)
        report = mp.analyse(seq, heights, rules, cyclic=False)
    except (ImportError, AttributeError):
        return _minor_adjacent_fallback(seq, heights)
    return int(
        sum(v.layers - 1 for v in report.violations if v.rule == mp.MINOR_ADJACENT)
    )


def _gate_mix_minor(
    parsed: Parsed,
    spec: GateSpec,
    exp: GateExpectations | None,
    partials: dict[Region, _Partial],
) -> Gate:
    mixes = _mix_regions(exp)
    if not mixes:
        return Gate(
            "mix-minor-adjacent",
            "SKIP",
            "no mix region in the manifest (GateExpectations.regions[].mix)",
        )
    bad: list[str] = []
    skipped: list[str] = []
    seen = 0
    for reg in mixes:
        if not reg.exclusive:
            part = partials[reg]
            if part.skip:
                skipped.append(f"{reg.name}: {part.skip}")
                continue
            seen += 1
            bad += part.adjacent
            continue
        layers = [
            layer
            for layer in parsed.layers
            if _in_slab(layer, reg.z_min, reg.z_max) and layer.model_tools
        ]
        if len(layers) < 2:
            continue
        seen += 1
        seq = ["+".join(str(t) for t in sorted(layer.model_tools)) for layer in layers]
        n = _minor_adjacent(seq, [layer.h for layer in layers], spec.mix_cap)
        if n:
            bad.append(
                f"{reg.name}: {n} adjacent layer pair(s) print the same minor colour (sequence {','.join(seq[:12])}...)"
            )
    note = f" (skipped: {'; '.join(skipped)})" if skipped else ""
    if not seen and not bad:
        return Gate(
            "mix-minor-adjacent",
            "SKIP",
            "; ".join(skipped) or "no mix region has two printed layers to compare",
        )
    return Gate(
        "mix-minor-adjacent",
        "FAIL" if bad else "PASS",
        "; ".join(bad)
        if bad
        else f"{seen} mix region(s): the minor colour never prints on adjacent layers{note}",
    )


def _gate_prime_tower(parsed: Parsed, exp: GateExpectations | None) -> Gate:
    used = parsed.tools_used()
    tower = {k: v for k, v in parsed.types.items() if "ower" in k}
    if exp is not None and exp.prime_tower is not None:
        want = exp.prime_tower
        ok = bool(tower) == want
        return Gate(
            "prime-tower",
            "PASS" if ok else "FAIL",
            f"manifest says prime_tower={want}; TYPE blocks {tower or 'none'}",
        )
    if len(used) <= 1:
        return Gate(
            "prime-tower",
            "PASS",
            f"{len(used)} tool prints: no tower needed; TYPE blocks {tower or 'none'}",
        )
    return Gate(
        "prime-tower",
        "PASS" if tower else "FAIL",
        f"{len(used)} tools print ({_t(used)}); TYPE blocks {tower or 'none (a multi-tool print needs a prime tower)'}",
    )


def _gate_bed(parsed: Parsed, spec: GateSpec) -> Gate:
    bx, by, bz = spec.bed_mm
    if parsed.extents is None:
        return Gate(
            "bed-bounds",
            "SKIP",
            "no model extrusion in any layer; nothing to place on the bed",
        )
    x0, x1, y0, y1 = parsed.extents
    zmax = max((layer.z for layer in parsed.layers), default=0.0)
    out: list[str] = []
    if x0 < -_EPS_MM or x1 > bx + _EPS_MM or y0 < -_EPS_MM or y1 > by + _EPS_MM:
        out.append(
            f"extrusion x {x0:.1f}..{x1:.1f}, y {y0:.1f}..{y1:.1f} leaves the {bx:g}x{by:g} bed"
        )
    if zmax > bz + _EPS_MM:
        out.append(f"top layer z={zmax:.1f} above the {bz:g} mm build height")
    warn: list[str] = []
    if "printable_area" in parsed.header:
        from printer_ops.move_bounds import (
            check_bounds,
        )  # the repo's arc-aware checker, travel moves included

        rep = check_bounds(Path(parsed.path))
        if rep.area_out_count:
            out.append(
                f"{rep.area_out_count} move(s) outside the G-code's own printable_area, first {rep.area_out[0]}"
            )
        if rep.unverified_count:
            warn.append(
                f"{rep.unverified_count} move(s) could not be verified ({rep.unverified[0]})"
            )
    if out:
        return Gate("bed-bounds", "FAIL", "; ".join(out))
    detail = f"extrusion x {x0:.1f}..{x1:.1f}, y {y0:.1f}..{y1:.1f}, top z={zmax:.1f} inside {bx:g}x{by:g}x{bz:g}"
    return Gate(
        "bed-bounds",
        "WARN" if warn else "PASS",
        detail + ("; " + "; ".join(warn) if warn else ""),
    )


_SPEED_TOL = 1.005  # the G-code rounds F to whole mm/min
_CAP_NOTE_RATIO = 0.9  # a feature running below this share of the requested speed is reported as capped


def _gate_feedrates(parsed: Parsed, spec: GateSpec) -> Gate:
    """The real maximum feedrate per feature, read from the `F` words of the moves (not from the slicer's config
    comments). With `spec.speed_limits` a feature faster than requested FAILs; a feature slower than 90 % of its
    limit is listed (volumetric limit, cornering, layer-time slowdown; the limit is the highest setting that can
    apply, so a feature set by a lower sub-setting, e.g. an overhang step, is listed without being wrong)."""
    feed = parsed.feed_max
    if not feed:
        return Gate("feedrates", "SKIP", "no feedrate (F word) on any move inside a layer")
    shown = ", ".join(f"{k} {v:.0f}" for k, v in sorted(feed.items()))
    limits = spec.speed_limits
    if limits is None:
        return Gate("feedrates", "PASS", f"max mm/s per feature: {shown} (no limits requested)")
    over = [
        f"{k} {feed[k]:.0f} > {lim:g}"
        for k, lim in sorted(limits.items())
        if k in feed and feed[k] > lim * _SPEED_TOL
    ]
    capped = [
        f"{k} {feed[k]:.0f} of {lim:g}"
        for k, lim in sorted(limits.items())
        if k in feed and feed[k] < lim * _CAP_NOTE_RATIO
    ]
    note = f"; below 90 % of the highest setting that can apply (volumetric cap, cornering, layer time): {', '.join(capped)}" if capped else ""
    if over:
        return Gate("feedrates", "FAIL", f"faster than requested (mm/s): {', '.join(over)}; max per feature: {shown}")
    return Gate("feedrates", "PASS", f"max mm/s per feature: {shown}{note}")


def _same_setting(want: str, got: str) -> bool:
    """Equal as text (quotes around a name ignored) or as numbers (`0.40` = `0.4`) element by element."""
    want, got = want.strip('"'), got.strip('"')
    if want == got:
        return True
    a, b = want.split(","), got.split(",")
    if len(a) != len(b):
        return False
    try:
        return all(abs(float(x) - float(y)) < 1e-9 for x, y in zip(a, b, strict=True))
    except ValueError:
        return False


def _gate_overrides(parsed: Parsed, spec: GateSpec) -> Gate:
    """Every requested setting is echoed with that value in the G-code's config block: a misspelt key or a value
    the slicer replaced would otherwise print at the old setting without a word."""
    want = spec.expect_settings
    if not want:
        return Gate("overrides", "SKIP", "no settings requested (spec.expect_settings is None)")
    if not parsed.config:
        return Gate("overrides", "SKIP", "the G-code has no `; key = value` config block to compare with")
    missing = sorted(k for k in want if k not in parsed.config)
    wrong = [
        f"{k} = {parsed.config[k]} (asked {want[k]})"
        for k in sorted(want)
        if k in parsed.config and not _same_setting(want[k], parsed.config[k])
    ]
    if missing or wrong:
        parts = []
        if missing:
            parts.append(f"not in the G-code config block (a misspelt key? the slicer ignored it): {', '.join(missing)}")
        if wrong:
            parts.append(f"slicer used another value: {'; '.join(wrong)}")
        return Gate("overrides", "FAIL", "; ".join(parts))
    return Gate("overrides", "PASS", f"{len(want)} requested setting(s) confirmed in the G-code config block")


# ---------------------------------------------------------------- driver


def check_gcode(
    path: str | Path, spec: GateSpec, expectations: GateExpectations | None = None
) -> GcodeReport:
    """Parse `path` and run every gate. See the module header for gate names and SKIP semantics."""
    parsed = parse(path)
    zone = spec.zone or (expectations.structural_zone if expectations else None)
    partials = _partials(parsed, expectations)
    gates = [
        _gate_walls(parsed, spec, zone),
        _gate_infill(parsed, spec, zone),
        _gate_supports(parsed, spec),
        _gate_layer_heights(parsed, spec),
        _gate_mix_layer_cap(parsed, spec, expectations),
        _gate_tools_allowed(parsed, spec, expectations),
        _gate_region_tools(parsed, expectations, partials),
        _gate_mix_minor(parsed, spec, expectations, partials),
        _gate_prime_tower(parsed, expectations),
        _gate_bed(parsed, spec),
        _gate_feedrates(parsed, spec),
        _gate_overrides(parsed, spec),
    ]
    hs = [layer.h for layer in parsed.layers]
    stats: dict[str, Any] = {
        "layers": len(parsed.layers),
        "layer_h_min": min(hs) if hs else None,
        "layer_h_max": max(hs) if hs else None,
        "tool_changes": parsed.n_tool_changes,
        "tools_used": sorted(parsed.tools_used()),
        "time": parsed.time,
        "time_s": parsed.time_s,
        "filament_g": parsed.filament_g,
        "types": dict(parsed.types),
        "feed_max_mm_s": {k: round(v, 1) for k, v in parsed.feed_max.items()},
        "extents": list(parsed.extents) if parsed.extents else None,
        "n_lines": parsed.n_lines,
        "parse_s": round(parsed.parse_s, 3),
    }
    return GcodeReport(str(path), spec, gates, stats)
