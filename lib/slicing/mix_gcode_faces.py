"""mix_gcode_faces.py — the `mix face` rule judged on a sliced G-code alone: no project 3MF needed.

WHAT: streams a G-code once and, per labelled object (EXCLUDE_OBJECT / Bambu blocks — gcode_labels.py),
  collects which heads extruded in it and two visible areas taken from the slicer's own feature tags:
    top    `;TYPE:Top surface`  extruded XY length × `;WIDTH:`        — horizontal area looking up
    wall   `;TYPE:Outer wall`   extruded XY length × the layer height — vertical area
  An object that more than one head printed is MIXED (a manual layer pattern alternates heads layer by
  layer; Local-Z prints both heads on every layer, which slicing/head_sequence.py does not check but
  this does). The rule is the one of slicing/mix_faces.py (#411): FAIL when top > wall / 2 — from one
  direction a viewer sees about half of a part's walls but all of its top, so a part whose top outweighs
  that half shows its colour mainly on a flat top, which is one plain filament. An object on one head is
  not checked.
WHY: issue #419 / plan §20.6 — the Orca post-processing hook and a G-code started outside us have no
  project 3MF, and the common Orca-to-printer path was unprotected (the 3MF check said SKIP).
CAVEATS: a physical multi-colour part (different parts on different heads) is also "printed by more than
  one head", so without a project the print-preflight line is a WARN, never a FAIL (plan §20.6). Sloped
  faces are not told apart from tops: `Top surface` is every upward face the slicer closes with solid skin.
  Arcs (G2/G3) are measured along the arc; G91 and G92 on X/Y are not supported (Orca writes G90 + M83).
INVARIANTS: stdlib only; one pass; memory = the per-object totals.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from gcode_labels import LAYER_MARKERS, ObjectLabels, feature, line_width, undecorated
from slicing.mix_faces import FAIL, PASS, SKIP, WARN, classify

TOP_FEATURES = frozenset(
    {"Top surface", "Top solid infill"}
)  # Orca / Bambu, PrusaSlicer
WALL_FEATURES = frozenset({"Outer wall", "External perimeter"})
DEFAULT_WIDTH_MM = 0.42
_MAX_HEAD_INDEX = 255  # Bambu writes T255 / T1000 as pseudo tool changes
_TOOL = re.compile(r"^T(\d+)\b")
_WORD = re.compile(r"([GMXYZEIJ])(-?\d*\.?\d+)")
_MOVE = {"G0", "G1", "G2", "G3"}


@dataclass
class ObjectFaces:
    name: str
    heads: set[int] = field(default_factory=set)  # 1-based
    top_mm2: float = 0.0
    wall_mm2: float = 0.0
    layers: set[int] = field(default_factory=set)

    @property
    def mixed(self) -> bool:
        return len(self.heads) > 1

    def verdict(self) -> tuple[str, str]:
        """(PASS | FAIL, why) by the 3MF rule (no slope class: the G-code does not tell it)."""
        return classify(self.top_mm2, self.wall_mm2, 0.0)


@dataclass
class GcodeFaces:
    objects: list[ObjectFaces]  # every labelled object that extruded, in file order
    labelled: bool  # the file carries object labels at all
    layers: int

    @property
    def mixed(self) -> list[ObjectFaces]:
        return [o for o in self.objects if o.mixed]

    @property
    def measured(self) -> list[ObjectFaces]:
        """Mixed objects with a top or an outer wall to measure (a G-code without `;TYPE:` tags has none)."""
        return [o for o in self.mixed if o.top_mm2 > 0 or o.wall_mm2 > 0]

    @property
    def status(self) -> str:
        """SKIP without labels or a measurable mixed object; FAIL when one breaks the rule."""
        if not self.labelled or not self.measured:
            return SKIP
        return FAIL if any(o.verdict()[0] == FAIL for o in self.measured) else PASS


def _arc_length(
    x0: float, y0: float, x1: float, y1: float, i: float, j: float, cw: bool
) -> float:
    r = math.hypot(i, j)
    if r == 0:
        return math.hypot(x1 - x0, y1 - y0)
    cx, cy = x0 + i, y0 + j
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    sweep = (a0 - a1) % (2 * math.pi) if cw else (a1 - a0) % (2 * math.pi)
    if sweep == 0 and (x0, y0) == (x1, y1):
        sweep = 2 * math.pi  # a full circle: start == end
    return r * sweep


def _first_number(text: str) -> float | None:
    """The first whitespace-separated token of `text` as a positive finite number, else None."""
    try:
        value = float(text.split()[0])
    except (ValueError, IndexError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _tagged(line: str, keys: tuple[str, ...]) -> float | None:
    """`;Z:0.2` / `; Z_HEIGHT: 0.2` style comment: the number after the first matching key."""
    if not line.startswith(";"):
        return None
    body = line[1:].lstrip()
    for key in keys:
        if body.startswith(key):
            return _first_number(body[len(key) :])
    return None


def _layer_height(line: str) -> float | None:
    """`;HEIGHT:0.12` (Orca) / `; LAYER_HEIGHT: 0.12` (Bambu) -> 0.12."""
    return _tagged(line, ("HEIGHT:", "LAYER_HEIGHT:"))


def _layer_z(line: str) -> float | None:
    """`;Z:0.2` (Orca) / `; Z_HEIGHT: 0.2` (Bambu) -> 0.2."""
    return _tagged(line, ("Z:", "Z_HEIGHT:"))


def scan(lines: Iterable[str]) -> GcodeFaces:
    """One pass over the G-code lines."""
    labels = ObjectLabels()
    objs: dict[str, ObjectFaces] = {}
    tool, layer = 1, -1  # a single-head printer never writes T
    z_prev, z_now, height = 0.0, 0.0, 0.0
    height_announced = False
    kind: str | None = None
    width = DEFAULT_WIDTH_MM
    x = y = math.nan
    e_abs, relative_e = 0.0, True
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line[0] == ";":
            if line.startswith(LAYER_MARKERS):
                layer += 1
                height_announced = False
                z_prev = z_now
            elif (z := _layer_z(line)) is not None:
                z_now = z
                if not height_announced:
                    height = z_now - z_prev
            elif (h := _layer_height(line)) is not None:
                height, height_announced = h, True
            elif (f := feature(line)) is not None:
                kind = f
            elif (w := line_width(line)) is not None:
                width = _first_number(w) or width
            else:
                labels.feed(line)
            continue
        if labels.feed(line):
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        word = code.split(None, 1)[0].upper()
        if (m := _TOOL.match(code)) and int(m.group(1)) < _MAX_HEAD_INDEX:
            tool = int(m.group(1)) + 1
            continue
        if word == "M82":
            relative_e = False
            continue
        if word == "M83":
            relative_e = True
            continue
        if word == "G92":
            for key, val in _WORD.findall(code.upper()):
                if key == "E":
                    e_abs = float(val)
            continue
        if word not in _MOVE:
            continue
        args = {k: float(v) for k, v in _WORD.findall(code.upper()[len(word) :])}
        nx, ny = args.get("X", x), args.get("Y", y)
        e = args.get("E")
        pushed = 0.0
        if e is not None:
            pushed, e_abs = (e, e_abs) if relative_e else (e - e_abs, e)
        moved_xy = ("X" in args or "Y" in args) and not (math.isnan(x) or math.isnan(y))
        if (
            pushed > 0
            and moved_xy
            and labels.current is not None
            and not (math.isnan(nx) or math.isnan(ny))
        ):
            obj = objs.setdefault(labels.current, ObjectFaces(labels.current))
            obj.heads.add(tool)
            obj.layers.add(layer)
            if word in ("G2", "G3"):
                length = _arc_length(
                    x, y, nx, ny, args.get("I", 0.0), args.get("J", 0.0), word == "G2"
                )
            else:
                length = math.hypot(nx - x, ny - y)
            if kind in TOP_FEATURES:
                obj.top_mm2 += length * width
            elif kind in WALL_FEATURES:
                obj.wall_mm2 += length * height
        x, y = nx, ny
    return GcodeFaces(list(objs.values()), labels.labeled, layer + 1)


def check(gcode_path: str) -> GcodeFaces:
    with open(gcode_path, encoding="utf-8", errors="replace") as fh:
        return scan(fh)


def format_lines(objects: list[ObjectFaces]) -> list[str]:
    out = []
    for o in objects:
        status, why = o.verdict()
        heads = ", ".join(f"T{h}" for h in sorted(o.heads))
        name = undecorated(o.name)
        out.append(
            f"{status:<4} {name}: heads {heads} — top {o.top_mm2:.0f} mm², outer walls {o.wall_mm2:.0f} mm²: {why}"
        )
    return out


def preflight_check(gcode_path: str) -> dict[str, object]:
    """print-preflight entry when there is no project 3MF: {status PASS/WARN/SKIP, detail, lines}.
    A rule break is a WARN here, not a FAIL: a multi-colour physical part prints on several heads
    too (plan §20.6)."""
    report = check(gcode_path)
    if not report.labelled:
        return {
            "status": SKIP,
            "detail": "the G-code has no object labels (EXCLUDE_OBJECT / Bambu blocks): heads cannot be attributed to objects",
            "lines": [],
        }
    mixed = report.mixed
    if not mixed:
        return {
            "status": SKIP,
            "detail": f"no object printed on more than one head ({len(report.objects)} object(s))",
            "lines": [],
        }
    measured = report.measured
    if not measured:
        return {
            "status": SKIP,
            "detail": f"{len(mixed)} object(s) on several heads, but the G-code has no `;TYPE:Top surface` / "
            "`;TYPE:Outer wall` extrusion to measure",
            "lines": [],
        }
    bad = [o for o in measured if o.verdict()[0] == FAIL]
    n = len(measured)
    if bad:
        return {
            "status": WARN,
            "detail": f"{len(bad)} of {n} object(s) on several heads show their colour on a horizontal face "
            "(no project 3MF: a multi-colour physical part matches too; stand a layer mix up)",
            "lines": [
                line
                for o, line in zip(measured, format_lines(measured), strict=True)
                if o in bad
            ],
        }
    return {
        "status": PASS,
        "detail": f"{n} object(s) on several heads show their colour on walls",
        "lines": [],
    }
