"""gcode_scan.py — one streaming pass over a sliced G-code, layer by layer.

WHAT: reads an Orca / Snapmaker-Orca G-code line by line (never the whole file as one string) and hands
  every finished layer to the consumers it was given: the layer's Z and height, its extrusion runs
  (feature type, tool, XY polyline) and the tools it selected. While streaming it also collects what
  the gates need across layers in compact form: per-layer Z / height, M600 pauses, the tool-change
  count, the material the extrusion moves add up to per (feature type, tool), the `;TYPE:` blocks and
  the footer's `; key = value` settings. Memory is one layer plus those small tables.
WHY: the multi-colour gates (gcode_mix_gate.py) judge a 75 MB G-code with 650 layers; holding the
  parsed file (or its text) made a 10 GB run of what is a per-layer measurement.
INVARIANTS: relative extrusion (M83) — an extruding move is a G0-G3 with X or Y and a positive E (an arc G2/G3 is
  followed as a chord to its end point: its I/J are ignored);
  `;LAYER_CHANGE`, `;Z:`, `;HEIGHT:`, `;TYPE:` are Orca's comments; an unknown start position is NaN;
  tools above T199 are macros, not extruders.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

FILAMENT_AREA_MM2 = math.pi * (1.75 / 2) ** 2
DEFAULT_DENSITY = 1.24

# footer settings the gates compare against; first occurrence wins (the footer block is written once)
HEADER_KEYS = frozenset(
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
        "filament_density",
    }
)

_RE_TOOL = re.compile(r"^T(\d+)\s*(;.*)?$")
_RE_AXES = re.compile(r"([XYE])(-?\d*\.?\d+)")
_RE_KV = re.compile(r"^; ([A-Za-z_]+) = (.*)$")

Points = npt.NDArray[np.float64]


@dataclass
class Run:
    """Consecutive extruding moves of one feature type with one tool; `xy[0]` is where the run starts."""

    type: str | None
    tool: int | None
    xy: Points  # (N, 2), NaN where the position was not known yet

    def valid_xy(self) -> Points:
        return self.xy[~np.isnan(self.xy[:, 0])]


@dataclass
class Layer:
    index: int
    z: float | None
    h: float | None
    runs: list[Run]
    tools: list[int]

    def mid_z(self) -> float | None:
        return None if self.z is None or self.h is None else self.z - self.h / 2


@dataclass
class Pause:
    """An M600: `layer` is the index of the layer being printed (-1 before the first layer)."""

    layer: int
    line: int
    runs_before: int  # extrusion runs the layer already had when the pause came


@dataclass
class Scan:
    """Everything a pass collected besides the per-layer data that went to the consumers."""

    layer_z: list[float | None] = field(default_factory=list)
    layer_h: list[float | None] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    tool_changes: int = 0
    header: dict[str, str] = field(default_factory=dict)
    filament_g: list[float] = field(default_factory=list)
    time: str | None = None
    types: Counter[str] = field(default_factory=Counter)
    extruded_mm: dict[tuple[str | None, int], float] = field(default_factory=dict)

    @property
    def layers(self) -> int:
        return len(self.layer_z)

    def stats(self) -> dict[str, Any]:
        """Grams per tool and feature type from the extrusion moves themselves, next to the slicer's footer."""
        try:
            density = float(self.header.get("filament_density", "").split(",")[0])
        except ValueError:
            density = DEFAULT_DENSITY
        by_tool: dict[int, float] = {}
        by_type: dict[str, float] = {}
        tower: dict[int, float] = {}
        for (typ, tool), mm in self.extruded_mm.items():
            grams = mm * FILAMENT_AREA_MM2 * density / 1000.0
            by_tool[tool] = by_tool.get(tool, 0.0) + grams
            by_type[typ or "?"] = by_type.get(typ or "?", 0.0) + grams
            if typ == "Prime tower":
                tower[tool] = tower.get(tool, 0.0) + grams
        return {
            "layers": self.layers,
            "tool_changes": self.tool_changes,
            "changes_per_layer": round(self.tool_changes / max(1, self.layers), 2),
            "time": self.time,
            "density": density,
            "grams_by_tool": {f"T{k}": round(v, 1) for k, v in sorted(by_tool.items())},
            "grams_by_type": {
                k: round(v, 1)
                for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
            },
            "prime_tower_g": round(sum(tower.values()), 1),
            "prime_tower_by_tool": {
                f"T{k}": round(v, 1) for k, v in sorted(tower.items())
            },
            "slicer_footer_g": self.filament_g,
            "total_g": round(sum(by_tool.values()), 1),
        }


LayerConsumer = Callable[[Layer], None]


def _finish(
    layer: Layer,
    pts: list[list[tuple[float, float]]],
    raw: list[tuple[str | None, int | None]],
) -> None:
    layer.runs = [
        Run(t, tool, np.array(p, dtype=float))
        for (t, tool), p in zip(raw, pts, strict=True)
    ]


def scan_gcode(path: Path | str, consumers: Iterable[LayerConsumer] = ()) -> Scan:
    """Stream `path`; call every consumer with each finished layer in file order; return the cross-layer tables."""
    consumers = list(consumers)
    scan = Scan()
    tool: int | None = None
    typ: str | None = None
    x = y = math.nan
    layer: Layer | None = None
    raw: list[tuple[str | None, int | None]] = []
    pts: list[list[tuple[float, float]]] = []
    cur: tuple[str | None, int | None] | None = None

    def close() -> None:
        nonlocal layer
        if layer is None:
            return
        _finish(layer, pts, raw)
        scan.layer_z.append(layer.z)
        scan.layer_h.append(layer.h)
        for consume in consumers:
            consume(layer)
        layer = None

    with open(path, errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            c = line[0]
            if line.startswith("M600"):
                scan.pauses.append(
                    Pause(-1 if layer is None else layer.index, lineno, 0 if layer is None else len(raw))
                )
            if c == ";":
                if line.startswith(";LAYER_CHANGE"):
                    close()
                    layer = Layer(len(scan.layer_z), None, None, [], [])
                    raw, pts, cur = [], [], None
                elif line.startswith(";Z:") and layer is not None:
                    layer.z = float(line[3:])
                elif (
                    line.startswith(";HEIGHT:")
                    and layer is not None
                    and layer.h is None
                ):
                    layer.h = float(line[8:])
                elif line.startswith(";TYPE:"):
                    typ = line[6:].strip()
                    scan.types[typ] += 1
                    cur = None
                elif line.startswith("; filament used [g] ="):
                    scan.filament_g = [float(v) for v in line.split("=", 1)[1].split(",") if v.strip()]
                elif line.startswith("; estimated printing time (normal mode) ="):
                    scan.time = line.split("=", 1)[1].strip()
                else:
                    m = _RE_KV.match(line)
                    if m and m.group(1) in HEADER_KEYS and m.group(1) not in scan.header:
                        scan.header[m.group(1)] = m.group(2).strip()
                continue
            if c == "T":
                m = _RE_TOOL.match(line)
                if m and int(m.group(1)) < 200:
                    t = int(m.group(1))
                    if tool is not None and t != tool:
                        scan.tool_changes += 1
                    tool = t
                    if layer is not None:
                        layer.tools.append(t)
                    cur = None
                continue
            if c == "G" and line.startswith(("G0 ", "G1 ", "G2 ", "G3 ")):
                body = line.split(";", 1)[0]
                ax = {k: float(v) for k, v in _RE_AXES.findall(body)}
                nx, ny = ax.get("X", x), ax.get("Y", y)
                e = ax.get("E", 0.0)
                if e > 0 and ("X" in ax or "Y" in ax):
                    if tool is not None:
                        key = (typ, tool)
                        scan.extruded_mm[key] = scan.extruded_mm.get(key, 0.0) + e
                    if layer is not None:
                        if cur is None or cur != (typ, tool):
                            cur = (typ, tool)
                            raw.append(cur)
                            pts.append([(x, y)])
                        pts[-1].append((nx, ny))
                else:
                    cur = None
                x, y = nx, ny
    close()
    return scan
