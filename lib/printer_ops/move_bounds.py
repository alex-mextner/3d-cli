"""move_bounds.py — does every move of a sliced G-code stay on the machine?

WHY (issue https://github.com/alex-mextner/3d-cli/issues/101): a K1 SE plate faulted with
  "Y out of range" — the bed dropped mid-job. The cause was a spiral z-hop arc near the bed
  edge: `G3 Z1.36 I-.359 J-1.79` from (62.83, 2.774) is a full circle about (62.471, 0.984)
  that reaches Y -0.84, below the K1 SE's Y minimum of -0.5. Endpoint-only checks and slicer
  previews do not show such arc bulges.

WHAT, on every G0/G1/G2/G3 (absolute or G91-relative XY, G92 offsets; arcs by their real
  axis-aligned extremes from I/J, a full circle when start == end):
  - axis travel: the printer's real X/Y limits, when known, EVERYWHERE — the vendor start
    and end G-code included, since Klipper rejects any out-of-range move;
  - print area: the whole path inside the G-code's own `; printable_area` polygon, and no
    EXTRUDING path (lines and arcs, not just their points) through `; bed_exclude_area` —
    only on the plate section: from the first layer change to the last object end or last
    slicer-feature move (a final prime tower often follows the last object), skipping
    `;TYPE:Custom` blocks (Bambu Studio tags only its end G-code so: there everything
    outside its object blocks, wipe tower and feature runs, see _BambuMoves), because vendor
    start/end/toolchange G-code parks and purges where it likes. The axis-travel check still
    covers those.
  Each known coordinate is checked on its own axis even while the other axis is unknown.
  Arcs given by R, arcs in the XZ/YZ planes (G18/G19) and relative moves from an unknown
  position are counted as UNVERIFIED. Line numbers (`N12 G1 ...*45`), Klipper
  SET_GCODE_OFFSET and SAVE/RESTORE_GCODE_STATE are followed.
  MACROS: any command that is neither modeled here nor a stock command that cannot move X/Y
  (`_INERT`) is a macro whose body is invisible — PAUSE, TIMELAPSE_TAKE_FRAME, `Tn` (on
  Klipper every tool change is a macro), `G1_WIPE`-style names. Each is recorded (rep.macros)
  and afterwards the position and E are unknown. print-preflight FAILs on macros the
  printer's `trusted_macros` list does not name. Trusting a macro means: it keeps the head
  on the bed AND leaves G90/G91, G92 and SET_GCODE_OFFSET as it found them (the Klipper
  SAVE/RESTORE_GCODE_STATE convention) — the checker keeps those modes across it.

LIMITATIONS: positions are G-code coordinates; firmware-side tool offsets (toolchanger
  heads) and macro bodies are not visible here. Before the first X/Y move and after G28 of
  an axis that axis is unknown until set again. Area tests are exact: for an axis-aligned
  rectangle the end points and arc extremes decide; for any other polygon (and always for
  bed_exclude_area) each line or arc is split where it meets the polygon boundary and every
  piece is tested; arcs are also tested as the 1 mm chords Klipper prints. After an E offset
  (SET_GCODE_OFFSET E/E_ADJUST) or M221 the logical E is unknown until `G92 E`, and every
  absolute E word then counts as extrusion (conservative for bed_exclude_area).

Reference: garage-band babushkin-dom gen/move_bounds.py (same rules, project-local).
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from gcode_labels import LAYER_MARKERS as _LAYER_MARKERS
from gcode_labels import ObjectLabels, feature

Range = tuple[float, float]
Limits = tuple[Range, Range]  # ((xmin, xmax), (ymin, ymax))
Polygon = list[tuple[float, float]]

EPS = 1e-6
_MOVE = re.compile(r"G0*([0-3])(?![\d.])")
_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_LINE_NO = re.compile(r"^[Nn]\d+\s*")
MAX_CHORDS = 100_000  # chord simulation budget per arc (r * sweep / 1 mm)
# Commands this checker models itself (they move or change coordinates)
_MODELED = frozenset(
    {
        "G0",
        "G1",
        "G2",
        "G3",
        "G17",
        "G18",
        "G19",
        "G28",
        "G90",
        "G91",
        "G92",
        "M82",
        "M83",
        "M221",
        "ACTIVATE_EXTRUDER",
        "SET_GCODE_OFFSET",
        "SAVE_GCODE_STATE",
        "RESTORE_GCODE_STATE",
    }
)
# Stock Klipper/Marlin commands that neither move X/Y nor change coordinates. Anything else is a
# macro (or a command this checker does not model): its moves are invisible here.
_INERT = frozenset(
    {
        "G4",
        "G10",
        "G11",
        "G21",
        "M17",
        "M18",
        "M73",
        "M84",
        "M104",
        "M105",
        "M106",
        "M107",
        "M109",
        "M110",
        "M112",
        "M114",
        "M115",
        "M117",
        "M118",
        "M140",
        "M141",
        "M190",
        "M191",
        "M204",
        "M205",
        "M220",
        "M400",
        "M486",
        "M900",
        "EXCLUDE_OBJECT",
        "EXCLUDE_OBJECT_DEFINE",
        "EXCLUDE_OBJECT_START",
        "EXCLUDE_OBJECT_END",
        "M624",  # Bambu's EXCLUDE_OBJECT_START / _END (gcode_labels)
        "M625",
        "SET_VELOCITY_LIMIT",
        "SET_PRESSURE_ADVANCE",
        "SET_PRINT_STATS_INFO",
        "SET_FAN_SPEED",
        "SET_HEATER_TEMPERATURE",
        "SET_TEMPERATURE_FAN_TARGET",
        "TEMPERATURE_WAIT",
        "SET_RETRACTION",
        "SET_IDLE_TIMEOUT",
    }
)
_GM_WORD = re.compile(r"([GM])0*(\d+)(?![\d.])")


def command_word(line: str) -> str | None:
    """`G01 X1` / `G1X1` -> "G1", `T2` -> "T2", `MY_MACRO A=1` / `G1_WIPE` -> the macro name,
    comment/blank -> None. A name with "_" is always an extended command (a macro), even when
    it starts like a G/M code. Tool changes keep their number: on Klipper every `Tn` is a macro."""
    code = line.split(";", 1)[0].strip()
    if not code:
        return None
    token = code.split(None, 1)[0].upper()
    if "_" not in token:
        m = _GM_WORD.match(token)
        if m:
            return m.group(1) + m.group(2)
    return token


_KEEP = 5  # violations kept per kind (the count is always exact)


@dataclass
class Violation:
    line: int
    x: float | None  # None: that axis was unknown at this move
    y: float | None

    def __str__(self) -> str:
        fx = "?" if self.x is None else f"{self.x:.2f}"
        fy = "?" if self.y is None else f"{self.y:.2f}"
        return f"line {self.line} ({fx}, {fy})"


@dataclass
class BoundsReport:
    printable_area: Polygon | None = None
    exclude_area: Polygon | None = None
    plate_found: bool = False
    axis_out: list[Violation] = field(default_factory=list)
    axis_out_count: int = 0
    area_out: list[Violation] = field(default_factory=list)
    area_out_count: int = 0
    excluded_in: list[Violation] = field(default_factory=list)
    excluded_in_count: int = 0
    unverified: list[str] = field(default_factory=list)  # "line N why" (first few)
    unverified_count: int = 0
    macros: dict[str, list[int]] = field(
        default_factory=dict
    )  # name -> [count, first line]
    plate_extent: tuple[float, float, float, float] | None = (
        None  # xmin, xmax, ymin, ymax
    )
    all_extent: tuple[float, float, float, float] | None = None


def parse_polygon(value: str) -> Polygon | None:
    """`0x0,220x0,220x220,0x220` -> [(0,0), ...]; None for fewer than 3 points or junk."""
    pts: Polygon = []
    for item in value.split(","):
        a, sep, b = item.strip().partition("x")
        if not sep:
            return None
        try:
            pts.append((float(a), float(b)))
        except ValueError:
            return None
    return pts if len(pts) >= 3 else None


def parse_limits(text: str) -> Limits:
    """`-5:229,-0.5:227` -> ((-5, 229), (-0.5, 227)); raises ValueError on anything else."""
    parts = text.split(",")
    if len(parts) != 2:
        raise ValueError(text)
    out = []
    for p in parts:
        lo, sep, hi = p.partition(":")
        if not sep:
            raise ValueError(text)
        a, b = float(lo), float(hi)
        if not a < b:
            raise ValueError(text)
        out.append((a, b))
    return (out[0], out[1])


def _on_segment(
    px: float, py: float, a: tuple[float, float], b: tuple[float, float]
) -> bool:
    (ax, ay), (bx, by) = a, b
    if abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) > EPS * max(
        1.0, math.hypot(bx - ax, by - ay)
    ):
        return False
    return (
        min(ax, bx) - EPS <= px <= max(ax, bx) + EPS
        and min(ay, by) - EPS <= py <= max(ay, by) + EPS
    )


def inside(px: float, py: float, poly: Polygon, *, boundary: bool) -> bool:
    """Point in polygon (ray casting). `boundary` says what a point ON an edge counts as."""
    n = len(poly)
    for k in range(n):
        if _on_segment(px, py, poly[k], poly[(k + 1) % n]):
            return boundary
    hit = False
    for k in range(n):
        (ax, ay), (bx, by) = poly[k], poly[(k + 1) % n]
        if (ay > py) != (by > py) and px < ax + (py - ay) * (bx - ax) / (by - ay):
            hit = not hit
    return hit


def arc_sweep(
    x0: float, y0: float, x1: float, y1: float, i: float, j: float, cw: bool
) -> tuple[float, float]:
    """(start angle, signed sweep) the way Klipper's gcode_arcs.py plans G2/G3: the
    counter-clockwise angle between the radius vectors is in [0, 2π); clockwise subtracts 2π,
    so a clockwise arc whose end lies on the start's angle is a full turn; a counter-clockwise
    one is a full turn only when start and end coincide."""
    cx, cy = x0 + i, y0 + j
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    ccw = (a1 - a0) % (2 * math.pi)
    if ccw > 2 * math.pi - 1e-12:
        ccw = 0.0
    if cw:
        return a0, ccw - 2 * math.pi
    if ccw < 1e-12 and abs(x1 - x0) < EPS and abs(y1 - y0) < EPS:
        return a0, 2 * math.pi
    return a0, ccw


def arc_points(
    x0: float, y0: float, x1: float, y1: float, i: float, j: float, cw: bool
) -> list[tuple[float, float]]:
    """End points plus every axis-aligned extreme the arc passes, about centre (x0+i, y0+j)."""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0, sweep = arc_sweep(x0, y0, x1, y1, i, j, cw)
    pts = [(x0, y0), (x1, y1)]
    for k in range(4):
        a = k * math.pi / 2
        # angle travelled from a0 to reach `a` in the arc's direction
        along = (a - a0) % (2 * math.pi) if sweep >= 0 else (a0 - a) % (2 * math.pi)
        if along <= abs(sweep) + EPS:
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


@dataclass
class _Plate:
    first_layer: int | None  # first layer-change line (1-based)
    end: float  # last line of the plate section (inf: to the end of the file)
    typed: bool  # the slicer tags features with `;TYPE:` comments
    config: dict[str, str]
    bambu: bool = False  # Bambu Studio labels: slicer moves are told by _BambuMoves


class _BambuMoves:
    """Bambu Studio tags only its end G-code `; FEATURE: Custom`, so which moves are the
    slicer's comes from its own labels: an object block (gcode_labels), the wipe tower, and a
    `; FEATURE:` run, which a layer change, an object end or the tower end closes (the
    layer-change timelapse / clog-detect G-code that follows parks at the waste chute). Vendor
    G-code inside those is not: a tool change's filament change (`; CP TOOLCHANGE START` ..
    `; CP TOOLCHANGE WIPE`, the trip to the chute) and the firmware-conditional `M622 J<flag>`
    .. `M623` blocks (timelapse, clog detection), which Bambu also inserts mid-object.

    Neither kind of vendor block ever holds an object label or a layer change (all
    babushkin-dom A1 mini plates), so one still open there is unbalanced (truncated or edited
    G-code): it is closed, and `feed` returns True so the caller reports it, instead of
    exempting every later move from the print area."""

    __slots__ = ("change", "conditional", "feature", "labels", "tower")

    def __init__(self) -> None:
        self.labels = ObjectLabels()
        self.tower = self.change = self.feature = False
        self.conditional = 0  # open M622 .. M623 blocks

    def _boundary(self) -> bool:
        """A label or layer change: close any vendor block left open; True when one was."""
        unbalanced = self.change or self.conditional > 0
        self.change = False
        self.conditional = 0
        return unbalanced

    def feed(self, line: str) -> bool:
        """Track one line; True when it closed an unbalanced vendor block."""
        if self.labels.feed(line):
            if self.labels.current is None:
                self.feature = False
            return self._boundary()
        c0 = line[:1]
        if c0 in ("M", "m"):
            word = line.split(";", 1)[0].split(None, 1)[0].upper()
            if word == "M622":
                self.conditional += 1
            elif word == "M623":
                self.conditional = max(0, self.conditional - 1)
        elif c0 == ";":
            if line.startswith(_LAYER_MARKERS):
                self.feature = False
                return self._boundary()
            if (tag := feature(line)) is not None:
                self.feature = tag != "Custom"
            elif line.startswith("; WIPE_TOWER_START"):
                self.tower = True
            elif line.startswith("; WIPE_TOWER_END"):
                self.tower = self.feature = False
            elif line.startswith("; CP TOOLCHANGE START"):
                self.change = True
            elif line.startswith(("; CP TOOLCHANGE WIPE", "; CP TOOLCHANGE END")):
                self.change = False
        return False

    @property
    def slicer(self) -> bool:
        return (
            not self.change
            and not self.conditional
            and (self.labels.current is not None or self.tower or self.feature)
        )


def _scan_plate(path: Path) -> _Plate:
    """Pass 1: where the plate section starts and ends, plus the area config.

    The section ends at the later of the last object end and the last move of a slicer
    feature: a prime tower is often printed AFTER the last object on the final layer, and it
    must stay on the bed too. Vendor G-code is tagged `;TYPE:Custom` (Bambu: _BambuMoves)."""
    first_layer: int | None = None
    last_obj: int | None = None
    last_feature: int | None = None
    ftype: str | None = None
    typed = False
    config: dict[str, str] = {}
    moves = _BambuMoves()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if line[:1] in (" ", "\t"):
                line = line.lstrip()
            c0 = line[:1]
            in_object = moves.labels.current is not None
            moves.feed(line)
            if in_object and moves.labels.current is None:
                last_obj = n
            if c0 == ";":
                if line.startswith(";TYPE:"):
                    ftype, typed = line[6:].strip(), True
                elif first_layer is None and line.startswith(_LAYER_MARKERS):
                    first_layer = n
                elif line.startswith(("; printable_area =", "; bed_exclude_area =")):
                    key, _, value = line[1:].partition("=")
                    config.setdefault(key.strip(), value.strip())
            elif (
                c0 in ("G", "g")
                or (c0 in ("N", "n") and _LINE_NO.sub("", line)[:1] in ("G", "g"))
            ) and (
                first_layer is not None
                and (
                    moves.slicer
                    if moves.labels.bambu
                    else ftype not in (None, "Custom")
                )
            ):
                last_feature = n
    ends = [v for v in (last_obj, last_feature) if v is not None]
    return _Plate(
        first_layer,
        float(max(ends)) if ends else math.inf,
        typed,
        config,
        moves.labels.bambu,
    )


Point = tuple[float, float]


@dataclass(frozen=True)
class Move:
    """One XY path: a straight segment, or an arc about (cx, cy) from angle a0 through `sweep`
    radians (negative = clockwise). `at(t)` walks it for t in 0..1."""

    x0: float
    y0: float
    x1: float
    y1: float
    arc: tuple[float, float, float, float, float] | None = None  # cx, cy, r, a0, sweep

    @classmethod
    def arc_move(
        cls, x0: float, y0: float, x1: float, y1: float, i: float, j: float, cw: bool
    ) -> Move:
        a0, sweep = arc_sweep(x0, y0, x1, y1, i, j, cw)
        return cls(x0, y0, x1, y1, (x0 + i, y0 + j, math.hypot(i, j), a0, sweep))

    def chords(self, mm_per_segment: float = 1.0) -> list[Move] | None:
        """The straight segments Klipper prints for this planar arc (gcode_arcs.py, default
        resolution 1 mm): floor(r * |sweep| / resolution) segments, the last one ending at the
        commanded end point. [] for a straight move; None when the arc would need more than
        MAX_CHORDS segments (absurd radius) — the caller reports it unverified."""
        if self.arc is None:
            return []
        _, _, r, _, sweep = self.arc
        segments = max(1, math.floor(r * abs(sweep) / mm_per_segment))
        if segments > MAX_CHORDS:
            return None
        pts = [self.at(k / segments) for k in range(segments)] + [(self.x1, self.y1)]
        return [Move(a[0], a[1], b[0], b[1]) for a, b in itertools.pairwise(pts)]

    def at(self, t: float) -> Point:
        if self.arc is None:
            return (
                self.x0 + (self.x1 - self.x0) * t,
                self.y0 + (self.y1 - self.y0) * t,
            )
        cx, cy, r, a0, sweep = self.arc
        return (cx + r * math.cos(a0 + sweep * t), cy + r * math.sin(a0 + sweep * t))

    def bbox(self) -> tuple[float, float, float, float]:
        if self.arc is None:
            pts = [(self.x0, self.y0), (self.x1, self.y1)]
        else:
            cx, cy, _, _, sweep = self.arc
            pts = arc_points(
                self.x0,
                self.y0,
                self.x1,
                self.y1,
                cx - self.x0,
                cy - self.y0,
                sweep < 0,
            )
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        return (min(xs), max(xs), min(ys), max(ys))

    def cuts(self, poly: Polygon) -> list[float]:
        """Path parameters (0..1) where the path meets the polygon boundary, ends included."""
        ts = {0.0, 1.0}
        n = len(poly)
        for k in range(n):
            (cx_, cy_), (dx_, dy_) = poly[k], poly[(k + 1) % n]
            sx, sy = dx_ - cx_, dy_ - cy_
            if self.arc is None:
                rx, ry = self.x1 - self.x0, self.y1 - self.y0
                qx, qy = cx_ - self.x0, cy_ - self.y0
                den = rx * sy - ry * sx
                if abs(den) > EPS:
                    t, u = (qx * sy - qy * sx) / den, (qx * ry - qy * rx) / den
                    if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
                        ts.add(min(max(t, 0.0), 1.0))
                elif (
                    abs(qx * ry - qy * rx) <= EPS * max(1.0, math.hypot(rx, ry))
                    and rx * rx + ry * ry > 0
                ):
                    rr = rx * rx + ry * ry  # collinear: the overlap's ends
                    for px, py in ((cx_, cy_), (dx_, dy_)):
                        t = ((px - self.x0) * rx + (py - self.y0) * ry) / rr
                        if 0.0 <= t <= 1.0:
                            ts.add(t)
            else:
                acx, acy, r, a0, sweep = self.arc
                fx, fy = cx_ - acx, cy_ - acy
                qa, qb, qc = (
                    sx * sx + sy * sy,
                    2 * (fx * sx + fy * sy),
                    fx * fx + fy * fy - r * r,
                )
                if qa <= 0:
                    continue
                disc = qb * qb - 4 * qa * qc
                if disc < -EPS:
                    continue
                root = math.sqrt(max(disc, 0.0))
                for u in ((-qb - root) / (2 * qa), (-qb + root) / (2 * qa)):
                    if -EPS <= u <= 1 + EPS:
                        ang = math.atan2(fy + u * sy, fx + u * sx)
                        t = (
                            ((ang - a0) * (1 if sweep >= 0 else -1))
                            % (2 * math.pi)
                            / abs(sweep)
                            if sweep
                            else 0.0
                        )
                        if t <= 1 + EPS:
                            ts.add(min(t, 1.0))
        return sorted(ts)

    def _pieces(self, poly: Polygon) -> list[Point]:
        """A point inside every stretch between two boundary contacts: each stretch is wholly
        inside, wholly outside, or on the boundary, so testing these points is exact."""
        ts = self.cuts(poly)
        return [
            self.at((t0 + t1) / 2)
            for t0, t1 in itertools.pairwise(ts)
            if t1 - t0 > 1e-12
        ]

    def first_outside(self, poly: Polygon) -> Point | None:
        for q in [(self.x0, self.y0), *self._pieces(poly), (self.x1, self.y1)]:
            if not inside(q[0], q[1], poly, boundary=True):
                return q
        return None

    def first_inside(self, poly: Polygon) -> Point | None:
        for q in [(self.x0, self.y0), *self._pieces(poly), (self.x1, self.y1)]:
            if inside(q[0], q[1], poly, boundary=False):
                return q
        return None


def is_axis_rectangle(poly: Polygon) -> bool:
    """4 corners joined by horizontal/vertical edges: points + arc extremes decide containment."""
    if len(poly) != 4:
        return False
    return all(
        abs(poly[k][0] - poly[(k + 1) % 4][0]) < EPS
        or abs(poly[k][1] - poly[(k + 1) % 4][1]) < EPS
        for k in range(4)
    )


def _bbox_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return (
        a[0] <= b[1] + EPS
        and b[0] <= a[1] + EPS
        and a[2] <= b[3] + EPS
        and b[2] <= a[3] + EPS
    )


def _poly_bbox(poly: Polygon) -> tuple[float, float, float, float]:
    xs, ys = [q[0] for q in poly], [q[1] for q in poly]
    return (min(xs), max(xs), min(ys), max(ys))


def _axes(text: str) -> set[str]:
    """Axis letters named in a G28 argument list, with or without values (`G28 Z`, `G28 X0`)."""
    return set(re.findall(r"[XYZ]", text))


def check_bounds(path: Path, axis_limits: Limits | None = None) -> BoundsReport:
    plate = _scan_plate(path)
    rep = BoundsReport(
        printable_area=parse_polygon(plate.config.get("printable_area", "")),
        exclude_area=parse_polygon(plate.config.get("bed_exclude_area", "")),
        plate_found=plate.first_layer is not None,
    )
    ftype: str | None = None
    x: float | None = None
    y: float | None = None
    # G92 offsets (machine = gcode + G92 offset + SET_GCODE_OFFSET); None = set at an unknown position
    ox: float | None = 0.0
    oy: float | None = 0.0
    sx = sy = (
        0.0  # Klipper SET_GCODE_OFFSET X/Y (survives G28, like Klipper's homing_position)
    )
    e: float | None = (
        0.0  # logical E for M82 extrusion detection; None = unknown (E offset, M221)
    )
    rel = False  # G91
    abs_e = True  # M82 (absolute E) vs M83; E is relative when G91 or M83
    plane = 17
    # SAVE_GCODE_STATE name -> (G91, M82, G92 x/y, SET_GCODE_OFFSET x/y, position x/y, E)
    saved: dict[
        str,
        tuple[
            bool,
            bool,
            float | None,
            float | None,
            float,
            float,
            float | None,
            float | None,
            float | None,
        ],
    ] = {}
    plate_ext = [math.inf, -math.inf, math.inf, -math.inf]
    all_ext = [math.inf, -math.inf, math.inf, -math.inf]

    def unverified(n: int, why: str) -> None:
        rep.unverified_count += 1
        if len(rep.unverified) < _KEEP:
            rep.unverified.append(f"line {n} {why}")

    def axis(px: float | None, py: float | None, n: int) -> None:
        """Each KNOWN coordinate against its own axis: an unknown Y never hides a bad X."""
        if px is not None:
            all_ext[0], all_ext[1] = min(all_ext[0], px), max(all_ext[1], px)
        if py is not None:
            all_ext[2], all_ext[3] = min(all_ext[2], py), max(all_ext[3], py)
        if axis_limits is None:
            return
        (x0, x1), (y0, y1) = axis_limits
        bad_x = px is not None and not x0 - EPS <= px <= x1 + EPS
        bad_y = py is not None and not y0 - EPS <= py <= y1 + EPS
        if bad_x or bad_y:
            rep.axis_out_count += 1
            if len(rep.axis_out) < _KEEP:
                rep.axis_out.append(Violation(n, px, py))

    rect_area = rep.printable_area is not None and is_axis_rectangle(rep.printable_area)
    area_box = _poly_bbox(rep.printable_area) if rep.printable_area else None
    exclude_box = _poly_bbox(rep.exclude_area) if rep.exclude_area else None

    def area_violation(n: int, qx: float | None, qy: float | None) -> None:
        rep.area_out_count += 1
        if len(rep.area_out) < _KEEP:
            rep.area_out.append(Violation(n, qx, qy))

    def area(pts: list[Point], move: Move | None, extruding: bool, n: int) -> None:
        """End points and arc extremes always. An EXTRUDING move also its start (plastic laid
        from outside the area). A rectangle is decided by those points exactly (convex). Any
        other polygon follows the path piece by piece (a line can cut an L-shape's notch) —
        for travels too, unless the travel starts outside (entering from a vendor park spot)."""
        for px, py in pts:
            plate_ext[:] = [
                min(plate_ext[0], px),
                max(plate_ext[1], px),
                min(plate_ext[2], py),
                max(plate_ext[3], py),
            ]
        if rep.printable_area is None:
            return
        if (
            move is not None
            and not rect_area
            and (
                extruding or inside(move.x0, move.y0, rep.printable_area, boundary=True)
            )
        ):
            out = move.first_outside(rep.printable_area)
            if out is not None:
                area_violation(n, out[0], out[1])
                return
        check = [*pts, (move.x0, move.y0)] if move is not None and extruding else pts
        for qx, qy in check:
            if not inside(qx, qy, rep.printable_area, boundary=True):
                area_violation(n, qx, qy)
                return

    def area_partial(px: float | None, py: float | None, n: int) -> None:
        """One axis unknown on the plate: FAIL only what the known axis proves (outside the
        area's bounding range); otherwise containment cannot be established -> UNVERIFIED."""
        if area_box is None:
            return
        if (px is not None and not area_box[0] - EPS <= px <= area_box[1] + EPS) or (
            py is not None and not area_box[2] - EPS <= py <= area_box[3] + EPS
        ):
            area_violation(n, px, py)
        elif px is not None or py is not None:
            unverified(
                n, "plate move with one axis unknown; printable area not checked"
            )

    def excluded_hit(n: int, q: Point) -> None:
        rep.excluded_in_count += 1
        if len(rep.excluded_in) < _KEEP:
            rep.excluded_in.append(Violation(n, q[0], q[1]))

    def excluded(move: Move, n: int, helical: bool) -> None:
        """The ideal path AND, for arcs, the chords the firmware actually prints (Klipper
        splits an arc into straight segments; a chord can cut a corner the arc misses)."""
        assert rep.exclude_area is not None and exclude_box is not None
        if not _bbox_overlap(move.bbox(), exclude_box):
            return
        q = move.first_inside(rep.exclude_area)
        if q is not None:
            excluded_hit(n, q)
            return
        chords = move.chords()
        if chords is None or (helical and chords):
            # Klipper counts Z travel into the segment count of a helical arc, and a huge arc
            # would need millions of chords: the printed chords are not reproduced here
            unverified(
                n, "arc next to bed_exclude_area: firmware chords not reproduced"
            )
            return
        for chord in chords:
            q = chord.first_inside(rep.exclude_area)
            if q is not None:
                excluded_hit(n, q)
                return

    def physical_move(
        nx_: float | None, ny_: float | None, n: int, in_plate: bool
    ) -> None:
        """A toolhead move made by a macro command (SET_GCODE_OFFSET / RESTORE_GCODE_STATE
        MOVE=1): a travel from the current position, checked like any travel."""
        if nx_ is None and ny_ is None:
            return
        if nx_ is None or ny_ is None or x is None or y is None:
            axis(nx_, ny_, n)
            if in_plate:
                area_partial(nx_, ny_, n)
            return
        axis(nx_, ny_, n)
        if in_plate:
            area([(nx_, ny_)], Move(x, y, nx_, ny_), False, n)

    moves = _BambuMoves()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if line[:1] in (" ", "\t"):
                line = line.lstrip()
            if plate.bambu and moves.feed(line):
                unverified(
                    n,
                    "Bambu M622 / CP TOOLCHANGE block left open before this line;"
                    " print area not checked inside it",
                )
            c0 = line[:1]
            if c0 == ";":
                if line.startswith(";TYPE:"):
                    ftype = line[6:].strip()
                continue
            if c0 in (
                "N",
                "n",
            ):  # line-numbered G-code: `N12 G1 X10*45` (Klipper accepts it)
                line = _LINE_NO.sub("", line).split("*", 1)[0]
                c0 = line[:1]
            # plate = slicer-generated moves; vendor `;TYPE:Custom` blocks inside it are exempt
            in_plate = (
                plate.first_layer is not None
                and plate.first_layer <= n <= plate.end
                and (
                    moves.slicer
                    if plate.bambu
                    else not (plate.typed and ftype in (None, "Custom"))
                )
            )
            word = command_word(line)
            if word is None:
                continue
            if word not in _MODELED:
                if word not in _INERT:
                    # a macro: its body may move the head anywhere, extrude, or change E
                    seen_macro = rep.macros.setdefault(word, [0, n])
                    seen_macro[0] += 1
                    x = y = e = None
                continue
            if c0 in ("M", "m"):
                code = line.split(";", 1)[0].strip().upper()
                head_m = code.split(" ", 1)[0]
                if head_m in ("M82", "M83"):
                    abs_e = head_m == "M82"
                elif (
                    head_m == "M221"
                ):  # extrusion factor scales logical E offsets: E unknown until G92 E
                    e = None
                continue
            if c0 in ("A", "a") and line[:17].upper() == "ACTIVATE_EXTRUDER":
                e = None  # Klipper resets the logical E coordinate for the newly active extruder
                continue
            if c0 in ("S", "s", "R", "r"):
                raw = line.split(";", 1)[0].strip()
                cmd = raw.upper()
                # Klipper upper-cases parameter NAMES only; values (state names) keep their case
                params = {
                    k.upper(): v.strip("\"'")
                    for k, v in re.findall(
                        r"([A-Za-z_]+)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", raw
                    )
                }
                move_flag = params.get("MOVE", "0") not in ("0", "0.0")
                if cmd.startswith("SET_GCODE_OFFSET"):
                    try:
                        nsx = (
                            float(params["X"])
                            if "X" in params
                            else sx + float(params.get("X_ADJUST", 0))
                        )
                        nsy = (
                            float(params["Y"])
                            if "Y" in params
                            else sy + float(params.get("Y_ADJUST", 0))
                        )
                    except (
                        ValueError
                    ):  # malformed: the firmware rejects it; our offsets become unknown
                        unverified(n, "SET_GCODE_OFFSET with a non-numeric value")
                        ox = oy = None
                        continue
                    if move_flag:  # MOVE=1: the toolhead follows the offset
                        tx = x + nsx - sx if x is not None else None
                        ty = y + nsy - sy if y is not None else None
                        physical_move(tx, ty, n, in_plate)
                        x, y = tx, ty
                    sx, sy = nsx, nsy
                    if (
                        "E" in params or "E_ADJUST" in params
                    ):  # logical E shifts (MOVE, M221 dependent)
                        e = None
                elif cmd.startswith("SAVE_GCODE_STATE"):
                    saved[params.get("NAME", "default")] = (
                        rel,
                        abs_e,
                        ox,
                        oy,
                        sx,
                        sy,
                        x,
                        y,
                        e,
                    )
                elif cmd.startswith("RESTORE_GCODE_STATE"):
                    state = saved.get(params.get("NAME", "default"))
                    if (
                        state is None
                    ):  # saved in a macro we cannot see: modes and offsets unknown
                        unverified(
                            n, "RESTORE_GCODE_STATE of a state saved outside this file"
                        )
                        x = y = ox = oy = e = None
                    else:
                        rel, abs_e, ox, oy, sx, sy, kx, ky, e = state
                        if move_flag:  # MOVE=1: back to the saved spot
                            physical_move(kx, ky, n, in_plate)
                            x, y = kx, ky
                continue
            if c0 not in ("G", "g"):
                continue
            code = line.split(";", 1)[0].strip().upper()
            if (
                code.startswith(("G90", "G91", "G92", "G17", "G18", "G19", "G28"))
                and not code[3:4].isdigit()
            ):
                head = code[:3]
                if head == "G90":
                    rel = False
                elif head == "G91":
                    rel = True
                elif head in ("G17", "G18", "G19"):
                    plane = int(head[1:])
                elif (
                    head == "G28"
                ):  # homing resets the G92 offset; the homed position is unknown here
                    named = _axes(code[3:])
                    if not named or "X" in named:
                        x, ox = None, 0.0
                    if not named or "Y" in named:
                        y, oy = None, 0.0
                else:  # G92: the current machine position gets new G-code coordinates (bare G92: all 0)
                    words = dict(_WORD.findall(code[3:]))
                    if (
                        "X" in words or not words
                    ):  # at an unknown position the offset becomes unknown
                        ox = (
                            x - float(words.get("X", 0)) - sx if x is not None else None
                        )
                    if "Y" in words or not words:
                        oy = (
                            y - float(words.get("Y", 0)) - sy if y is not None else None
                        )
                    if "E" in words or not words:
                        e = float(words.get("E", 0))
                continue
            m = _MOVE.match(code)
            if not m:
                continue
            words = dict(_WORD.findall(code[m.end() :]))
            g = int(m.group(1))
            extruding = False
            if "E" in words:
                ev = float(words["E"])
                if (
                    rel or not abs_e
                ):  # relative E still advances the absolute position (Klipper)
                    extruding, e = ev > EPS, (e + ev if e is not None else None)
                else:  # absolute E: with the logical E unknown, any E word counts as extruding
                    extruding, e = (ev > e + EPS) if e is not None else True, ev
            nx, ny = x, y
            if "X" in words:
                v = float(words["X"])
                nx = (
                    (x + v if x is not None else None)
                    if rel
                    else (v + ox + sx if ox is not None else None)
                )
            if "Y" in words:
                v = float(words["Y"])
                ny = (
                    (y + v if y is not None else None)
                    if rel
                    else (v + oy + sy if oy is not None else None)
                )
            moved = "X" in words or "Y" in words or g in (2, 3)
            if not moved:
                # extrusion in place (a prime blob, an un-retract): plastic at the current spot
                if (
                    extruding
                    and in_plate
                    and rep.exclude_area
                    and x is not None
                    and y is not None
                    and inside(x, y, rep.exclude_area, boundary=False)
                ):
                    excluded_hit(n, (x, y))
                continue
            if ("X" in words and nx is None) or ("Y" in words and ny is None):
                unverified(n, "move from an unknown position or G92 offset")
            if nx is None or ny is None or (g in (2, 3) and (x is None or y is None)):
                # an axis is still unknown (start, after G28): check what IS known
                if g in (2, 3):
                    unverified(n, "arc from an unknown position")
                px_, py_ = (
                    (nx if "X" in words else None),
                    (ny if "Y" in words else None),
                )
                axis(px_, py_, n)
                if in_plate:
                    area_partial(px_, py_, n)
                x, y = nx, ny
                continue
            pts: list[Point]
            move: Move | None
            if (
                x is None or y is None
            ):  # from an unknown position to a known point: the point only
                pts, move = [(nx, ny)], None
                if in_plate and extruding:
                    unverified(
                        n,
                        "extrusion from an unknown position; only its end point checked",
                    )
                    if rep.exclude_area and inside(
                        nx, ny, rep.exclude_area, boundary=False
                    ):
                        excluded_hit(n, (nx, ny))
            elif g in (2, 3) and (plane != 17 or "R" in words):
                unverified(n, "arc by R or outside the XY plane (end point only)")
                pts, move = [(nx, ny)], Move(x, y, nx, ny)
            elif g in (2, 3):
                ci, cj = float(words.get("I", 0)), float(words.get("J", 0))
                pts = arc_points(x, y, nx, ny, ci, cj, g == 2)
                move = Move.arc_move(x, y, nx, ny, ci, cj, g == 2)
            else:
                pts, move = [(nx, ny)], Move(x, y, nx, ny)
            for px, py in pts:
                axis(px, py, n)
            if in_plate:
                area(pts, move, extruding, n)
                if extruding and move is not None and rep.exclude_area:
                    excluded(move, n, g in (2, 3) and "Z" in words)
            x, y = nx, ny

    if plate_ext[0] <= plate_ext[1]:
        rep.plate_extent = (plate_ext[0], plate_ext[1], plate_ext[2], plate_ext[3])
    if all_ext[0] <= all_ext[1] and all_ext[2] <= all_ext[3]:
        rep.all_extent = (all_ext[0], all_ext[1], all_ext[2], all_ext[3])
    return rep


def fmt_extent(e: tuple[float, float, float, float] | None) -> str:
    return (
        "no XY moves"
        if e is None
        else f"x {e[0]:.2f}..{e[1]:.2f}, y {e[2]:.2f}..{e[3]:.2f}"
    )


def fmt_area(poly: Polygon) -> str:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return f"{min(xs):g}..{max(xs):g} x {min(ys):g}..{max(ys):g}"


def fmt_limits(lim: Limits) -> str:
    (x0, x1), (y0, y1) = lim
    return f"X {x0:g}..{x1:g}, Y {y0:g}..{y1:g}"
