"""gcode_resume.py — resume a stopped multi-head print from a layer: a new G-code that continues the original above
the last intact layer of a part still on the bed (no gluing, no Z homing). Issue
https://github.com/alex-mextner/3d-cli/issues/138, #238.

Ported from the babushkin-dom project's gen/resume_gcode.py (Snapmaker U1). Printer-specific values (the off-bed purge
macro, purge lengths, travel clearance, start commands) come from the printer's `resume:` block in
printer-devices.yaml (printer_ops/devices.py ResumeProfile); this module never contacts a printer.

Assumptions:
- Orca / Snapmaker Orca G-code: `;LAYER_CHANGE` `;Z:` ... `;AFTER_LAYER_CHANGE` per layer, EXCLUDE_OBJECT blocks,
  M83 relative E, bare `T<n>` tool changes, `; EXECUTABLE_BLOCK_START/END`, the stats lines before
  `; CONFIG_BLOCK_START`.
- The printer still holds the X/Y/Z homing, the bed mesh and the per-head G-code offsets of the original job. The U1
  `G28 Z` probes the bed with the nozzle, which the part on the bed forbids: no Z homing here.
- U1 firmware Snapmaker/u1-klipper@10f2f69: SDCARD_PRINT_FILE runs TURN_OFF_HEATERS before the file (the header
  reheats the bed on its first line); `T<n>` moves X/Y only, at the current Z (so Z goes above everything first);
  the purge macro with `INDEX=n FORCE=1` purges off the bed (LENGTH mm, then purge_retract_mm back: its own
  default; RETRACT_LENGTH is never passed) and sets the once-per-job flag, so the original per-change purge lines
  stay no-ops.
- Logical tools map 1:1 to the heads, as in the original job.
- Filament state per head is tracked as "mm retracted from primed"; the header leaves every head exactly as the
  original left it at the cut, so the original un-retracts prime it back.

- Z headroom: travels go over the old tower top + 2 mm and tool changes add the original 1.5 mm hop; with the
  tower near the Z ceiling (resume.z_max_mm - 1 mm) there is no headroom left and a final pass over the finished
  file refuses it (fail-closed: no Z above the limit or below the bed reaches the printer).

Past bugs (issue #238, ported from babushkin-dom gen/resume_gcode.py): a resume below the old prime tower top
continued the tower into it (41 layers of tower moves, spaghetti); now the layers below the old tower top print
without the tower (off-bed purges, travels and tool changes lifted over it) and the tower resumes above it.
RETRACT_LENGTH in the purge macro crashed the U1 macro (`| abs` on a string) and cancelled the job. Arcs (G2/G3
spiral hops) are tracked; a travel arc whose start the resume moved becomes a straight G1. Orca's one-axis tower
moves are followed with modal X/Y.

Traps it guards: --no-tower purges after EVERY `T<n>` (the slicer writes no purge line when it switches back to the
job's starting head, which then reached the model unprimed); the layer preamble ends the previous layer's object
(`EXCLUDE_OBJECT_END` with no object open is a Klipper error -> CANCEL_PRINT -> M84); the wipe moves of that preamble
belong to the layer below and are dropped.
"""

from __future__ import annotations

import contextlib
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from errors import GateFailure, UsageError

if TYPE_CHECKING:
    from printer_ops.devices import ResumeProfile

CMD = "gcode-resume"
HEADER_START = "; ===== RESUME of"
HEADER_END = "; ===== end of resume header: original G-code follows ====="

NUM = re.compile(rb"([XYZEFIJR])(-?\d*\.?\d+)")
TOOL = re.compile(rb"^T(\d+)\s*$")
Z_MARGIN = 1.0  # mm kept below stepper_z.position_max (head G-code offsets, bed mesh)
TOWER_CLEARANCE = 2.0  # mm above the old prime tower top for every X/Y move over it


def _bad(msg: str) -> UsageError:
    return UsageError(msg, command=CMD)


def words(line: bytes) -> dict[str, float]:
    """b'M104 S215 T3 ; c' -> {'M': 104, 'S': 215.0, 'T': 3.0} (first letter of each word of the code part)"""
    out: dict[str, float] = {}
    for w in line.split(b";", 1)[0].split():
        with contextlib.suppress(ValueError):
            out[chr(w[0])] = float(w[1:])
    return out


def move(line: bytes) -> dict[str, float] | None:
    """G0/G1/G2/G3 line -> dict of X/Y/Z/E/F (and I/J) floats, arcs with "arc": 2 (cw) or 3 (ccw); else None.
    Snapmaker Orca writes spiral z-hop travels as G2/G3 (`G3 Z.. I.. J.. P1`)."""
    if not (
        line[:1] == b"G"
        and line[1:2]
        and line[1:2] in b"0123"
        and (len(line) == 2 or line[2:3] in b" \t;")
    ):
        return None
    m = {chr(k[0]): float(v) for k, v in NUM.findall(line.split(b";", 1)[0])}
    if line[1:2] in b"23":
        m["arc"] = int(line[1:2])
    return m


def arc_points(x0: float, y0: float, m: dict[str, float]) -> list[tuple[float, float]]:
    """start, end and the axis-aligned extreme points of the arc move m starting at (x0, y0) (start == end: full
    circle, as Klipper draws it)"""
    x1, y1 = m.get("X", x0), m.get("Y", y0)
    cx, cy = x0 + m.get("I", 0.0), y0 + m.get("J", 0.0)
    r = math.hypot(x0 - cx, y0 - cy)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    if m["arc"] == 2:
        a0, a1 = a1, a0  # sweep counter-clockwise from a0 to a1
    sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    pts = [(x0, y0), (x1, y1)]
    for k in range(4):
        a = k * math.pi / 2
        if (a - a0) % (2 * math.pi) <= sweep:
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def obj_name(line: bytes) -> str | None:
    return line.split(b"NAME=", 1)[1].split()[0].decode() if b"NAME=" in line else None


def fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _copy_state(st: dict[str, Any]) -> dict[str, Any]:
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in st.items()}


class Walk:
    """One forward pass over the G-code: layers, and the machine state (tool, temperatures, fans, retraction per
    head, open object) at any line."""

    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.layers: list[dict[str, Any]] = []
        self.exec_start: int | None = None
        self.exec_end: int | None = None
        self.config_start: int | None = None
        off = 0
        self.offsets: list[int] = []  # byte offset of every line start
        for i, line in enumerate(lines):
            self.offsets.append(off)
            off += len(line) + 1
            if line.startswith(b"; EXECUTABLE_BLOCK_START"):
                self.exec_start = i
            elif line.startswith(b"; EXECUTABLE_BLOCK_END"):
                self.exec_end = i
            elif line.startswith(b"; CONFIG_BLOCK_START"):
                self.config_start = i
            elif line.startswith(b";LAYER_CHANGE"):
                self.layers.append(
                    {
                        "n": len(self.layers) + 1,
                        "start": i,
                        "z": None,
                        "h": None,
                        "after": None,
                    }
                )
            elif (
                self.layers and line.startswith(b";Z:") and self.layers[-1]["z"] is None
            ):
                self.layers[-1]["z"] = float(line[3:])
            elif (
                self.layers
                and line.startswith(b";HEIGHT:")
                and self.layers[-1]["h"] is None
            ):
                self.layers[-1]["h"] = float(line[8:])
            elif (
                self.layers
                and line.startswith(b";AFTER_LAYER_CHANGE")
                and self.layers[-1]["after"] is None
            ):
                self.layers[-1]["after"] = i
        if self.exec_start is None or self.exec_end is None or not self.layers:
            raise _bad(
                "not an Orca G-code: no EXECUTABLE_BLOCK markers or no ;LAYER_CHANGE"
            )
        if (
            bad := next((L["n"] for L in self.layers if L["z"] is None), None)
        ) is not None:
            raise _bad(f"layer {bad} has no ;Z: line (truncated or edited G-code?)")
        self.exec_lo: int = self.exec_start
        self.exec_hi: int = self.exec_end

    @classmethod
    def read(cls, path: Path) -> Walk:
        return cls(path.read_bytes().split(b"\n"))

    def line_at_byte(self, pos: int) -> int:
        lo, hi = 0, len(self.offsets) - 1
        while lo < hi:  # last line whose start <= pos
            mid = (lo + hi + 1) // 2
            if self.offsets[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def layer_of_line(self, i: int) -> dict[str, Any] | None:
        found = None
        for L in self.layers:
            if L["start"] > i:
                break
            found = L
        return found

    def state(
        self, stop: int, snap_from: int | None = None
    ) -> tuple[dict[str, Any], dict[int, Any]]:
        """machine state just before line `stop`; with snap_from, also the retraction of the head a line acts on
        for every E move / tool change at or after snap_from (a forward pass to the end)"""
        st = initial_state()
        snaps: dict[int, Any] = {}
        end = len(self.lines) if snap_from is not None else stop
        cut = None
        for i in range(end):
            if i == stop:
                cut = _copy_state(st)
                if snap_from is None:
                    break
            snap = advance(st, self.lines[i])
            if st["e_abs"]:
                raise _bad(f"line {i + 1}: M82 absolute E is not supported")
            if snap is not None and snap_from is not None and i >= snap_from:
                snaps[i] = snap
        if cut is None:
            cut = _copy_state(st)
        return cut, snaps


def initial_state() -> dict[str, Any]:
    """The machine state before the first G-code line, as `advance` keeps it."""
    return {
        "tool": None,
        "temp": {},  # target °C per tool (M104/M109)
        "bed": None,
        "chamber": None,  # M141/M191 target
        "fan": {},  # M106 P index -> 0..255
        "vel": None,
        "accel": None,
        "pa": {},
        "purifier": None,
        "r": {},  # mm retracted from primed, per tool (relative E)
        "rel": False,
        "obj": None,
        "feature": None,  # last ;TYPE:
        "width": None,  # last ;WIDTH: (mm)
        "height": None,  # last ;HEIGHT: (mm)
        "m73": None,
        "zmax": 0.0,
        "z": None,
        "f": None,  # modal feed rate, mm/min
        "fe": None,  # feed rate of the last extruding X/Y move, mm/min
        "extrusions": 0,  # extruding X/Y moves so far
        "e_abs": False,  # M82 in force
        "e_pos": 0.0,  # E axis position (absolute bookkeeping)
    }


def advance(st: dict[str, Any], line: bytes) -> Any:
    """Apply one G-code line (no trailing newline) to `st`. Returns the retraction snapshot the
    line acts on — the active head's mm retracted before an E move, every head's before a tool
    change — else None. The one per-line parser behind Walk.state and state_at."""
    c = line[:1]
    if c == b"G":
        m = move(line)
        if m is None:
            if line.startswith(b"G91"):
                st["rel"] = True
            elif line.startswith(b"G90"):
                st["rel"] = False
            elif line.startswith(b"G92") and "E" in (w := words(line)):
                st["e_pos"] = w["E"]
            return None
        if "F" in m:
            st["f"] = m["F"]
        if "Z" in m:
            st["z"] = (st["z"] or 0.0) + m["Z"] if st["rel"] else m["Z"]
            st["zmax"] = max(st["zmax"], st["z"])
        if "E" not in m:
            return None
        e = m["E"]
        delta = e - st["e_pos"] if st["e_abs"] else e
        st["e_pos"] = e if st["e_abs"] else st["e_pos"] + e
        if delta > 0 and ("X" in m or "Y" in m):
            st["fe"] = st["f"]
            st["extrusions"] += 1
        if st["tool"] is None:
            return None
        t = st["tool"]
        snap = st["r"].get(t, 0.0)
        st["r"][t] = round(
            st["r"].get(t, 0.0) - e if e < 0 else max(0.0, st["r"].get(t, 0.0) - e),
            5,
        )
        return snap
    if c == b"T":
        mt = TOOL.match(line)
        if mt:
            snapshot = dict(st["r"])
            st["tool"] = int(mt.group(1))
            return snapshot
    elif c == b"M":
        w = words(line)
        code = int(w.get("M", -1))
        if code in (104, 109) and "S" in w:
            tt = int(w["T"]) if "T" in w else st["tool"]
            st["temp"][tt] = w["S"]
        elif code in (140, 190) and "S" in w:
            st["bed"] = w["S"]
        elif code in (141, 191) and "S" in w:
            st["chamber"] = w["S"]
        elif code == 106:
            st["fan"][int(w.get("P", 0))] = w.get("S", 255.0)
        elif code == 107:
            st["fan"][int(w.get("P", 0))] = 0.0
        elif code == 204:
            st["accel"] = line.split(b";")[0].strip()
        elif code == 73 and "P" in w:
            st["m73"] = (int(w["P"]), int(w.get("R", 0)))
        elif code in (82, 83):
            st["e_abs"] = code == 82
    elif c == b"E":
        if line.startswith(b"EXCLUDE_OBJECT_START"):
            st["obj"] = obj_name(line)
        elif line.startswith(b"EXCLUDE_OBJECT_END"):
            st["obj"] = None
    elif c == b"S":
        if line.startswith(b"SET_VELOCITY_LIMIT"):
            st["vel"] = line.split(b";")[0].strip()
        elif line.startswith(b"SET_PRESSURE_ADVANCE") and b"EXTRUDER=" not in line:
            st["pa"][st["tool"]] = words(line.replace(b"ADVANCE=", b"A"))["A"]
        elif line.startswith(b"SET_PURIFIER_MODE"):
            st["purifier"] = line.split(b";")[0].strip()
    elif c == b";":
        if line.startswith(b";TYPE:"):
            st["feature"] = line[6:].strip().decode(errors="replace")
        elif line.startswith(b";WIDTH:"):
            with contextlib.suppress(ValueError):
                st["width"] = float(line[7:])
        elif line.startswith(b";HEIGHT:"):
            with contextlib.suppress(ValueError):
                st["height"] = float(line[8:])
    return None


@dataclass
class Cursor:
    """Where a `state_at` scan stopped and the machine state in force there."""

    layer: int  # ;LAYER_CHANGE markers seen (1-based layer; 0 = start G-code)
    z: float | None  # that layer's ;Z:
    line: int  # 0-based line number
    byte: int  # byte offset of that line
    state: dict[str, Any]


# Per-file checkpoints of `state_at`: the state just before every ;LAYER_CHANGE a scan passed, so
# the next question about the same file (another incident during the same print) resumes at the
# nearest layer instead of re-reading from byte 0. Keyed by (path, size, mtime); the few most
# recently used files, each within _CHECKPOINT_ENTRIES_PER_FILE state entries so no file (many
# layer markers, ever more tools) grows the cache without bound; later layers then scan from the
# last checkpoint. A state only grows along the file and checkpoints fill a prefix of its layers,
# so (checkpoints + 1) × the current state's size bounds what one more checkpoint brings the total to.
class _Checkpoint(NamedTuple):
    line: int  # 0-based line of the ;LAYER_CHANGE
    layers: int  # ;LAYER_CHANGE markers before it
    z: float | None  # the previous layer's ;Z:
    state: dict[str, Any]


_CHECKPOINTS: OrderedDict[tuple[str, int, int], dict[int, _Checkpoint]] = OrderedDict()
_CHECKPOINT_FILES = 4
_CHECKPOINT_ENTRIES_PER_FILE = 1 << 17  # ~4000 layers of a usual ~30-entry state
_CHECKPOINT_LOCK = threading.Lock()


def _checkpoints(path: Path) -> dict[int, _Checkpoint]:
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    with _CHECKPOINT_LOCK:
        if key in _CHECKPOINTS:
            _CHECKPOINTS.move_to_end(key)
        else:
            while len(_CHECKPOINTS) >= _CHECKPOINT_FILES:
                _CHECKPOINTS.popitem(last=False)  # the least recently used file
            _CHECKPOINTS[key] = {}
        return _CHECKPOINTS[key]


def _state_entries(st: dict[str, Any]) -> int:
    return len(st) + sum(len(v) for v in st.values() if isinstance(v, dict))


def _nearest_checkpoint(
    marks: dict[int, _Checkpoint], *, byte: int | None, layer: int | None
) -> tuple[int, _Checkpoint] | None:
    """(byte offset, checkpoint) of the latest checkpoint at or before `byte`, or before layer
    `layer` begins; None when the scan has to start at byte 0."""
    with _CHECKPOINT_LOCK:
        if byte is not None:
            usable = [offset for offset in marks if offset <= byte]
        else:
            target = layer if layer is not None else 0
            usable = [offset for offset, mark in marks.items() if mark.layers < target]
        if not usable:
            return None
        offset = max(usable)
        return offset, marks[offset]


def state_at(
    path: Path,
    *,
    byte: int | None = None,
    layer: int | None = None,
    deadline: float | None = None,
) -> Cursor:
    """Stream `path` (constant memory: a 130 MB plate never sits in RAM) and return the state
    at the line holding `byte` (a printer's file position), or — with `layer` — right after
    the first extruding move of that layer (its ;LAYER_CHANGE when it extrudes nothing). A
    scan resumes from the nearest layer checkpoint an earlier scan of the same file left.
    Absolute E (M82) is tracked, not refused: only resuming needs relative E. ValueError when
    the file ends first or `deadline` (time.monotonic()) passes."""
    if (byte is None) == (layer is None):
        raise ValueError("state_at needs exactly one of byte= or layer=")
    marks = _checkpoints(path)
    if (nearest := _nearest_checkpoint(marks, byte=byte, layer=layer)) is not None:
        offset, mark = nearest
        first, n_layer, z, st = mark.line, mark.layers, mark.z, _copy_state(mark.state)
    else:
        offset, first, n_layer, z, st = 0, 0, 0, None, initial_state()
    found: tuple[int, int, int] | None = (
        None  # (line, byte, extrusions) at the layer marker
    )
    with path.open("rb") as fh:
        fh.seek(offset)
        for i, raw in enumerate(fh, start=first):
            # every 4096 lines (slicer lines are short, so this stays well inside the budget); a
            # single pathological multi-MB line is not bounded — the file matched the printer's size
            if (
                deadline is not None
                and not (i - first) % 4096
                and time.monotonic() > deadline
            ):
                raise ValueError(
                    f"G-code scan of {path.name} exceeded its time budget at line {i + 1}"
                )
            line = raw[:-1] if raw.endswith(b"\n") else raw
            if byte is not None and offset + len(raw) > byte:
                return Cursor(n_layer, z, i, offset, _copy_state(st))
            if line.startswith(b";LAYER_CHANGE"):
                with _CHECKPOINT_LOCK:
                    if (
                        offset not in marks
                        and (len(marks) + 1) * _state_entries(st)
                        <= _CHECKPOINT_ENTRIES_PER_FILE
                    ):
                        marks[offset] = _Checkpoint(i, n_layer, z, _copy_state(st))
                if (
                    found is not None
                ):  # layer `layer` printed nothing: its start is the answer
                    return Cursor(n_layer, z, found[0], found[1], _copy_state(st))
                n_layer, z = n_layer + 1, None
                if n_layer == layer:
                    found = (i, offset, st["extrusions"])
            elif line.startswith(b";Z:") and z is None and n_layer:
                with contextlib.suppress(ValueError):
                    z = float(line[3:])
            advance(st, line)
            if found is not None and st["extrusions"] > found[2]:
                return Cursor(n_layer, z, i, offset, _copy_state(st))
            offset += len(raw)
    if found is not None:
        return Cursor(n_layer, z, found[0], found[1], _copy_state(st))
    if byte is not None:
        raise ValueError(f"byte {byte} is past the end of {path.name} ({offset} bytes)")
    raise ValueError(f"{path.name} has {n_layer} layer(s); no layer {layer}")


def extruder_name(t: int) -> str:
    return "extruder" if t == 0 else f"extruder{t}"


Box = tuple[float, float, float, float]


def _axis(cur: float | None, m: dict[str, float], k: str, rel: bool) -> float | None:
    """modal X or Y after the move m (None: unknown, a relative move from an unknown point)"""
    if k not in m:
        return cur
    if rel:
        return None if cur is None else cur + m[k]
    return m[k]


def tower_bbox(w: Walk, margin: float, purge_macro: str) -> Box | None:
    """bounding box (+ margin) of every prime-tower extrusion segment, both ends, with modal X/Y (Orca writes
    one-axis moves) and relative X/Y after G91"""
    xs: list[float] = []
    ys: list[float] = []
    feature = None
    x: float | None = None
    y: float | None = None
    rel = False
    resetters = (
        b"G28",
        purge_macro.encode(),
        b"MOVE_TO",
    )  # the firmware moves the head on these
    for line in w.lines[w.exec_lo : w.exec_hi]:
        if line.startswith(b";TYPE:"):
            feature = line[6:].strip()
            continue
        if line.startswith(b"G91"):
            rel = True
        elif line.startswith(b"G90"):
            rel = False
        elif TOOL.match(line) or line.startswith(resetters):
            x = y = None  # the firmware moved the head: position unknown
        m = move(line)
        if m is None:
            continue
        nx = _axis(x, m, "X", rel)
        ny = _axis(y, m, "Y", rel)
        if (
            feature == b"Prime tower"
            and m.get("E", 0) > 0
            and ("X" in m or "Y" in m or "arc" in m)
        ):
            pts: list[tuple[float | None, float | None]] = [(x, y), (nx, ny)]
            if "arc" in m and x is not None and y is not None and not rel:
                pts += arc_points(x, y, m)
            for px, py in pts:
                if px is not None and py is not None:
                    xs.append(px)
                    ys.append(py)
        x, y = nx, ny
    if not xs:
        return None
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def path_hits_box(p: tuple[float, float], m: dict[str, float], box: Box) -> bool:
    """does the move m from point p cross the rectangle box? arcs by their bounding box (conservative)"""
    q = (m.get("X", p[0]), m.get("Y", p[1]))
    if "arc" not in m:
        return seg_hits_box(p, q, box)
    pts = arc_points(p[0], p[1], m)
    xs, ys = [a for a, _ in pts], [b for _, b in pts]
    return (
        min(xs) <= box[2]
        and max(xs) >= box[0]
        and min(ys) <= box[3]
        and max(ys) >= box[1]
    )


def seg_hits_box(p: tuple[float, float], q: tuple[float, float], box: Box) -> bool:
    """does the segment p-q (x, y) cross the rectangle box (x0, y0, x1, y1)? (Liang-Barsky)"""
    x0, y0, x1, y1 = box
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in (
        (-dx, p[0] - x0),
        (dx, x1 - p[0]),
        (-dy, p[1] - y0),
        (dy, y1 - p[1]),
    ):
        if pk == 0:
            if qk < 0:
                return False
        else:
            t = qk / pk
            if pk < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def where(w: Walk, pos: int) -> str:
    i = w.line_at_byte(pos)
    st, _ = w.state(i)
    L = w.layer_of_line(i)
    line = w.lines[i].decode(errors="replace")
    prev = next(
        (
            w.lines[j].decode(errors="replace")
            for j in range(i - 1, max(0, i - 400), -1)
            if w.lines[j].startswith((b"; toolchange #", b"; Change Tool"))
        ),
        None,
    )
    return "\n".join(
        [
            f"byte {pos} -> line {i + 1} (starts at byte {w.offsets[i]}): {line!r}",
            f"layer {L['n']} of {len(w.layers)}  Z {L['z']}  (layer starts line {L['start'] + 1})"
            if L
            else "before layer 1",
            f"active tool T{st['tool']}  object {st['obj']}  feature {st['feature']}  last tool-change marker: {prev}",
            f"targets {st['temp']}  retracted mm per head {st['r']}  M73 (P%, R min) {st['m73']}  "
            f"max Z so far {st['zmax']}",
        ]
    )


def table(w: Walk, spec: str) -> str:
    try:
        a, b = (int(x) for x in spec.split(":"))
    except ValueError as exc:
        raise _bad(f"--table wants FIRST:LAST layer numbers, got {spec!r}") from exc
    if not 1 <= a <= b <= len(w.layers):
        raise _bad(
            f"--table {spec}: layers must satisfy 1 <= FIRST <= LAST <= {len(w.layers)}"
        )
    cur = w.state(w.layers[a - 1]["start"])[0]["tool"]
    rows = []
    for L in w.layers[a - 1 : b]:
        end = w.layers[L["n"]]["start"] if L["n"] < len(w.layers) else w.exec_hi
        tin, tools, m73, obj = cur, [], None, None
        objs: dict[str, set[int]] = {}
        for line in w.lines[L["start"] : end]:
            mt = TOOL.match(line)
            if mt:
                cur = int(mt.group(1))
                tools.append(cur)
            elif line.startswith(b"EXCLUDE_OBJECT_START"):
                obj = (obj_name(line) or "?").split("_\u2014_")[0]
            elif line.startswith(b"EXCLUDE_OBJECT_END"):
                obj = None
            elif line.startswith(b"M73 P") and m73 is None:
                m73 = int(words(line).get("R", -1))
            elif obj and line.startswith(b"G1"):
                m = move(line)
                if m and m.get("E", 0) > 0 and ("X" in m or "Y" in m):
                    objs.setdefault(obj, set()).add(cur)
        o = " ".join(
            f"{k}:{'/'.join(f'T{t}' for t in sorted(v))}"
            for k, v in sorted(objs.items())
        )
        rows.append(
            f"L{L['n']:5d} Z{L['z']:8.3f} line {L['start'] + 1:8d} byte {w.offsets[L['start']]:9d} in T{tin}"
            f" changes {'>'.join(f'T{t}' for t in tools) or '-':8s} min-left {m73} {o}"
        )
    return "\n".join(rows)


@dataclass
class Resume:
    """The resumed G-code (lines without newlines) and what the build decided."""

    lines: list[bytes]
    layer: int
    z: float
    kept_layer: int
    kept_z: float
    gap: float | None
    active: int
    tools: list[int]
    z_travel: float
    first: dict[str, float]
    dropped: int
    changes: int
    minutes_left: int
    grams: list[float]
    pause_layer: int | None
    notes: list[str]
    line_from: int

    def summary(self, dest: Path) -> str:
        out = [
            f"wrote {dest}",
            f"resume at layer {self.layer} (Z {fmt(self.z)}), last kept layer {self.kept_layer} "
            f"(top Z {fmt(self.kept_z)})"
            + (
                f", first new layer {fmt(self.gap)} mm above the measured top"
                if self.gap is not None
                else ""
            ),
            f"original line {self.line_from} on; paused layer {self.pause_layer}; heads {self.tools}, "
            f"T{self.active} picked; travel Z {fmt(self.z_travel)}; start X/Y/Z "
            f"{fmt(self.first['X'])}/{fmt(self.first['Y'])}/{fmt(self.first.get('Z', self.z))}",
            f"{self.changes} tool changes, ~{self.minutes_left} min left (slicer), "
            f"filament g per tool {[round(x, 1) for x in self.grams]}; {self.dropped} original move lines dropped",
        ]
        out += [f"NOTE: {n}" for n in self.notes]
        return "\n".join(out)


def _refuse_untracked_moves(w: Walk) -> None:
    """fail-closed on what the passes do not track: relative X/Y (G91) and radius-form arcs would make the tower
    area, the arc paths and the tower-crossing checks unknown, and G92 X/Y/Z would shift the coordinates the safety
    checks compare"""
    rel = False
    for i in range(w.exec_lo, w.exec_hi):
        line = w.lines[i]
        if (
            i < w.layers[0]["start"]
        ):  # start G-code: only its offsets matter (it is not copied)
            if line.startswith(b"G92") and {"X", "Y", "Z"} & set(words(line)):
                raise _bad(
                    f"line {i + 1}: G92 X/Y/Z (coordinate offset) is not supported"
                )
            continue
        if line.startswith(b"G91"):
            rel = True
        elif line.startswith(b"G90"):
            rel = False
        elif line.startswith(b"G92") and {"X", "Y", "Z"} & set(words(line)):
            raise _bad(f"line {i + 1}: G92 X/Y/Z (coordinate offset) is not supported")
        elif (m := move(line)) is not None:
            if rel and ("X" in m or "Y" in m):
                raise _bad(f"line {i + 1}: relative X/Y move (G91) is not supported")
            if "arc" in m and ("R" in m or not ("I" in m or "J" in m)):
                raise _bad(
                    f"line {i + 1}: radius-form arc (G2/G3 R) is not supported: its centre is not tracked"
                )


def build(
    w: Walk,
    profile: ResumeProfile,
    *,
    name: str,
    top_z: float | None = None,
    layer: int | None = None,
    paused_at: int | None = None,
    tower_top: float | None = None,
    no_tower: bool = False,
    home_xy: bool = False,
) -> Resume:
    """The resume G-code: original header + a resume header + the original from the resume layer on."""
    if (top_z is None) == (layer is None):
        raise _bad("give exactly one of --top-z, --layer")
    lines = w.lines
    exec_end = w.exec_hi
    name = re.sub(
        r"[\x00-\x1f\x7f]", "?", name
    )  # goes into a G-code comment: no line breaks
    purge = profile.purge_macro
    if layer is not None:
        rl = layer
    else:
        assert top_z is not None
        good = [L for L in w.layers if L["z"] <= top_z + 0.005]
        if not good:
            raise _bad(f"--top-z {top_z} is below the first layer")
        rl = good[-1]["n"] + 1
    if not 2 <= rl <= len(w.layers):
        raise _bad(f"resume layer {rl} out of range 2..{len(w.layers)}")
    LR, LG = w.layers[rl - 1], w.layers[rl - 2]
    i0, i1 = LR["start"], LR["after"]
    if i1 is None:
        raise _bad(f"layer {rl} has no ;AFTER_LAYER_CHANGE")
    notes: list[str] = []
    pause_layer = z_seen = None
    if paused_at is not None:
        ip = w.line_at_byte(paused_at)
        pl = w.layer_of_line(ip)
        if pl is None:
            raise _bad(f"--paused-at {paused_at} is before layer 1")
        pause_layer = pl["n"]
        z_seen = w.state(ip)[0]["zmax"]
        if rl > pause_layer + 1:
            raise _bad(
                f"resume layer {rl} is above the paused layer {pause_layer}: nothing was printed there"
            )
    _refuse_untracked_moves(w)
    st, snaps = w.state(i1, snap_from=i1)
    A = st["tool"]
    body_tools = {
        int(tm.group(1)) for line in lines[i1:exec_end] if (tm := TOOL.match(line))
    } | {A}
    for t in body_tools:
        if st["temp"].get(t) is None:
            raise _bad(f"no temperature known for T{t} at the cut")
    # print temperature of each head: its last M109 (>= min_print_temp) before the cut, else its first one after
    hot = profile.min_print_temp
    print_temp: dict[int, float] = {}
    for line in lines[w.exec_lo : i1]:
        if line.startswith(b"M109"):
            ww = words(line)
            if "T" in ww and ww.get("S", 0) >= hot:
                print_temp[int(ww["T"])] = ww["S"]
    for line in lines[i1:exec_end]:
        if line.startswith(b"M109"):
            ww = words(line)
            if "T" in ww and ww.get("S", 0) >= hot:
                print_temp.setdefault(int(ww["T"]), ww["S"])
    for t in body_tools:
        print_temp.setdefault(t, st["temp"][t])
        if print_temp[t] < hot:
            raise _bad(f"no print temperature for T{t}")
    tower = tower_bbox(w, profile.tower_margin_mm, purge)
    if no_tower and tower is None:
        raise _bad("--no-tower: the G-code has no prime tower")
    # the old prime tower still stands up to the paused layer: below its top the tower moves would run into it,
    # so those layers print as --no-tower (purge off the bed, travel over it) and the tower resumes above it
    for opt, v in (("--top-z", top_z), ("--tower-top", tower_top)):
        if v is not None and not (math.isfinite(v) and v >= 0):
            raise _bad(f"{opt} wants a finite Z >= 0, got {v}")
    if tower_top is None and pause_layer is not None:
        tower_top = w.layers[pause_layer - 1]["z"]
    if tower and tower_top is None:
        raise _bad(
            "the old prime tower height is unknown: give --paused-at BYTE or --tower-top Z"
        )
    first_xy, obj = None, None  # first X/Y move the body keeps: the header goes there
    for line in lines[i1:exec_end]:
        if line.startswith(b"EXCLUDE_OBJECT_START"):
            obj = obj_name(line)
        elif line.startswith(b"EXCLUDE_OBJECT_END"):
            obj = None
        m = move(line)
        if m and ("X" in m or "Y" in m):
            if tower and (
                obj is None
                or tower[0] <= m.get("X", -1e9) <= tower[2]
                and tower[1] <= m.get("Y", -1e9) <= tower[3]
            ):
                continue
            first_xy = m
            break
    if first_xy is None or "X" not in first_xy or "Y" not in first_xy:
        raise _bad("no X/Y start move found after the cut")
    z_lim = (
        profile.z_max_mm - Z_MARGIN
    )  # highest G-code Z the header and the tower lifts may use
    tower_top_eff = (
        tower_top if tower_top is not None else max(LR["z"], z_seen or 0.0)
    )  # no tower: unused
    # everything already standing on the bed: the part, whatever the paused job reached, the old tower
    z_need = max(
        LR["z"] + 0.6, z_seen or 0.0, tower_top_eff + TOWER_CLEARANCE if tower else 0.0
    )
    z_travel = min(z_need + profile.clearance_mm, z_lim)
    if z_travel < z_need:
        raise _bad(
            f"no room above the part: needs Z {z_need:.2f}, axis limit {fmt(profile.z_max_mm)} - {fmt(Z_MARGIN)}"
        )
    tower_safe_z = tower_top_eff + TOWER_CLEARANCE
    out_r: dict[int, float] = {}
    purged: dict[
        int, float
    ] = {}  # filament the off-bed purges consume: counted in the statistics
    out: list[bytes] = []
    emit = out.append

    def put(s: str) -> None:
        emit(s.encode())

    # ---- original header, thumbnails, object definitions ----
    out.extend(lines[: w.exec_lo + 1])
    body_objs = {
        obj_name(line)
        for line in lines[i1:exec_end]
        if line.startswith(b"EXCLUDE_OBJECT_START")
    }
    out.extend(
        line
        for line in lines[w.exec_lo : w.layers[0]["start"]]
        if line.startswith(b"EXCLUDE_OBJECT_DEFINE") and obj_name(line) in body_objs
    )
    # ---- resume header ----
    gap = None if top_z is None else LR["z"] - top_z
    put(";TYPE:Custom")
    put(f"{HEADER_START} {name} at layer {rl}/{len(w.layers)} (Z {fmt(LR['z'])}) =====")
    put(
        f"; last kept layer {rl - 1} top Z {fmt(LG['z'])}"
        + (
            f"; measured top Z {fmt(top_z)}, first new layer {fmt(gap)} mm above it"
            if top_z is not None and gap is not None
            else ""
        )
    )
    tower_note = (
        "replaced by off-bed purges (--no-tower)"
        if no_tower
        else "none"
        if not tower
        else f"off-bed purges up to the old tower top Z {fmt(tower_top_eff)}, tower continued above it"
    )
    put(
        f"; heads T{A} first, then {'/'.join(f'T{t}' for t in sorted(body_tools - {A})) or 'none'}; "
        f"prime tower: {tower_note}"
    )
    put(
        "; start only with: CLEAR_PAUSE, then SDCARD_PRINT_FILE (printer homed xyz; never after CANCEL_PRINT)"
    )
    put(
        f"M140 S{fmt(st['bed'] or 0)} ; SDCARD_PRINT_FILE just ran TURN_OFF_HEATERS: bed back on at once"
    )
    for t in sorted(body_tools):
        put(f"M104 S{fmt(print_temp[t])} T{t}")
    put("G21")
    put("G90")
    put("M83")
    put(
        f"G1 Z{fmt(z_travel)} F600 ; above the part, the tower and everything printed before the pause"
    )
    if home_xy:
        put("G28 X Y ; X/Y only: Z keeps its homing, the bed mesh stays")
    if st["purifier"]:
        emit(st["purifier"])
    put(f"M190 S{fmt(st['bed'] or 0)}")
    put(f"SET_PRINT_STATS_INFO TOTAL_LAYER={len(w.layers)} CURRENT_LAYER={rl - 1}")
    for c in profile.start_commands:
        put(c)
    order = sorted(body_tools - {A}) + [A]  # active head last: it stays picked
    for t in order:
        put(f"M109 S{fmt(print_temp[t])} T{t}")
        put("M400")
        put(f"T{t}")
        put(f"; off-bed purge of T{t} ({purge}): plastic must come out here")
        # never RETRACT_LENGTH=: the U1 macro does `| abs` on the raw string -> TypeError -> CANCEL_PRINT -> M84
        # (seen 2026-10-14, issue #238); its default leaves the head purge_retract_mm back
        put(
            f"{purge} INDEX={t} FORCE=1 TEMP={fmt(print_temp[t])} LENGTH={fmt(profile.purge_mm)}"
        )
        put("G90")
        put("M83")
        want = st["r"].get(t, 0.0)
        if abs(want - profile.purge_retract_mm) > 1e-4:
            put(
                f"G1 E{fmt(profile.purge_retract_mm - want)} F1800 ; leave T{t} {fmt(want)} mm retracted, "
                "as the original did"
            )
        out_r[t] = want
        purged[t] = purged.get(t, 0.0) + profile.purge_mm
        if t != A and abs(st["temp"][t] - print_temp[t]) > 0.5:
            put(f"M104 S{fmt(st['temp'][t])} T{t} ; standby temperature at the cut")
    for t in sorted(body_tools):
        if t in st["pa"]:
            put(
                f"SET_PRESSURE_ADVANCE EXTRUDER={extruder_name(t)} ADVANCE={fmt(st['pa'][t])}"
            )
    for p, s in sorted(st["fan"].items()):
        put(f"M106 S{fmt(s)}" if p == 0 else f"M106 P{p} S{fmt(s)}")
    if st["accel"]:
        emit(st["accel"])
    else:
        put(
            f"M204 S{fmt(profile.default_accel)} ; none set before the cut: the printer's default"
        )
    if st["vel"]:
        emit(st["vel"])
    put("M220 S100")
    put("M221 S100")
    # progress of the resumed job: P restarts at 0 against r0, the slicer's minutes left at the cut (R stays as is)
    left = st["m73"][1] if st["m73"] else 0
    r0 = next(
        (
            int(words(x).get("R", 0))
            for x in islice(lines, i0, exec_end)
            if x.startswith(b"M73 ") and b"R" in x
        ),
        left,
    )

    def m73(r: int) -> str:
        return f"M73 P{max(0, min(100, round(100 * (1 - r / r0)))) if r0 else 100} R{r}"

    if st["m73"] or r0:  # a plate without progress lines gets none
        put(m73(r0))
    put(f"G1 X{fmt(first_xy['X'])} Y{fmt(first_xy['Y'])} F12000")
    put(f"G1 Z{fmt(first_xy.get('Z', LR['z']))} F600")
    if st["f"]:
        put(f"G1 F{fmt(st['f'])} ; feed rate in force at the cut")
    put(HEADER_END)
    # ---- body ----
    tool, obj, rel = A, None, False
    orig_f = cur_f = st["f"]  # modal F of the original stream / of the output
    dropped = 0
    zmin_body = cur_z = first_xy.get(
        "Z", LR["z"]
    )  # modal Z of the output, relative moves included
    used: dict[int, float] = dict(
        purged
    )  # preflight reads the heads in use from these statistics
    changes = 0
    last_xy: tuple[float, float] | None = (first_xy["X"], first_xy["Y"])
    oxy: tuple[float, float] | None = (
        None  # X/Y of the original stream (None: unknown, e.g. after T<n>)
    )
    tower_hits: list[int] = []
    tower_lifts = 0
    pending_z = None

    def correct(d: float, why: str) -> None:
        nonlocal cur_f
        put(f"G1 E{fmt(d)} F1800 ; {why}")
        cur_f = 1800.0

    layer_z = LR["z"]
    purge_b = purge.encode()
    resetters = (
        purge_b,
        b"MOVE_TO",
        b"G28",
    )  # the firmware moves the head: X/Y unknown after these
    for i in range(i0, exec_end + 1):
        line = lines[i]
        m = move(line)
        if line.startswith(b";Z:"):
            layer_z = float(line[3:])
        # below the old tower top (or everywhere with --no-tower) the tower is not printed
        skip = tower is not None and (no_tower or layer_z <= tower_top_eff + 1e-6)
        if line.startswith(b"M73 ") and b"R" in line:
            put(m73(int(words(line).get("R", 0))))
            continue
        if i0 < i < i1:  # preamble of the cut layer: belongs to the layer below
            if TOOL.match(line) or line.startswith(b"EXCLUDE_OBJECT_START"):
                raise _bad(
                    f"line {i + 1}: tool change or object start inside the layer preamble is not supported"
                )
            if m is not None or line.startswith(b"EXCLUDE_OBJECT_END"):
                dropped += 1
                continue
            emit(line)
            continue
        if line.startswith(b"G91"):
            rel = True
        elif line.startswith(b"G90"):
            rel = False
        elif line.startswith(b"EXCLUDE_OBJECT_START"):
            obj = obj_name(line)
        elif line.startswith(b"EXCLUDE_OBJECT_END"):
            if obj is None:
                raise _bad(f"line {i + 1}: EXCLUDE_OBJECT_END with no object open")
            obj = None
        mt = TOOL.match(line)
        if mt:
            if skip and cur_z < tower_safe_z - 1e-6:
                # the firmware parks the old head by X first, then Y to the dock, at the current Z: lift above the
                # old tower before it moves (the original 1.5 mm hop does not clear it)
                put("G90")
                put(f"G1 Z{fmt(tower_safe_z)} F600 ; tool change over the old tower")
                if rel:
                    put("G91")
                cur_z = tower_safe_z
                cur_f = 600.0
            want = snaps[i].get(tool, 0.0)
            if abs(out_r.get(tool, 0.0) - want) > 1e-4:
                correct(
                    out_r.get(tool, 0.0) - want,
                    f"T{tool} parks {fmt(want)} mm retracted",
                )
                out_r[tool] = want
            tool = int(mt.group(1))
            changes += 1
            last_xy = oxy = None  # the firmware parks/picks/purges: X/Y unknown here
            emit(line)
            if skip:
                # the tower prime goes, so every pick purges off the bed; the slicer writes no
                # purge line when it switches back to the job's starting head
                put(
                    "; below the old tower top: prime off the bed instead of on the tower"
                )
                put(
                    f"{purge} INDEX={tool} FORCE=1 LENGTH={fmt(out_r.get(tool, 0.0) + profile.no_tower_prime_mm)}"
                )
                used[tool] = used.get(tool, 0.0) + profile.no_tower_prime_mm
                out_r[tool] = profile.purge_retract_mm
            continue
        if line.startswith(resetters):
            oxy = last_xy = None
        if skip and line.startswith(purge_b):
            continue  # replaced by the purge after the T<n> above
        if m is not None:
            if "F" in m:
                orig_f = m["F"]
            arc_start = (
                oxy  # where the original arc starts (its I/J are relative to it)
            )
            if "X" in m or "Y" in m:
                if rel:
                    oxy = None
                else:
                    nx = m.get("X", oxy[0] if oxy else math.nan)
                    ny = m.get("Y", oxy[1] if oxy else math.nan)
                    oxy = None if math.isnan(nx + ny) else (nx, ny)
            if (
                "arc" in m
                and not rel
                and (
                    arc_start is None
                    or last_xy is None
                    or math.dist(arc_start, last_xy) > 1e-3
                )
            ):
                # the output head is not where the original arc starts (resume point, a dropped tower move): its
                # I/J centre would shift, so a travel arc goes straight to its end point instead
                if m.get("E", 0) > 0:
                    raise _bad(
                        f"line {i + 1}: extruding arc from a position the resume changed"
                    )
                m = {k: v for k, v in m.items() if k in {"X", "Y", "Z", "E", "F"}}
                line = (
                    "G1 "
                    + " ".join(f"{k}{fmt(v)}" for k, v in m.items())
                    + " ; was an arc"
                ).encode()
            if (
                skip
                and tower
                and not rel
                and (
                    obj is None
                    or (
                        "E" not in m
                        and "X" in m
                        and "Y" in m
                        and tower[0] <= m["X"] <= tower[2]
                        and tower[1] <= m["Y"] <= tower[3]
                    )
                )
            ):
                dropped += 1  # tower move, tower travel or tower wipe
                if "Z" in m and (obj is not None or not ("X" in m or "Y" in m)):
                    pending_z = m[
                        "Z"
                    ]  # a layer change or lift on a dropped line still sets the modal Z
                if (
                    m.get("E", 0) < 0
                    and out_r.get(tool, 0.0) < snaps[i] - m["E"] - 1e-4
                ):
                    # the park retraction happens here, while the head is still at print temperature
                    correct(
                        out_r.get(tool, 0.0) - (snaps[i] - m["E"]),
                        f"T{tool} retracts here as on the tower",
                    )
                    out_r[tool] = snaps[i] - m["E"]
                continue
            if pending_z is not None and ("Z" not in m or rel):
                if rel:  # pending_z is absolute
                    put("G90")
                put(f"G1 Z{fmt(pending_z)}")
                if rel:
                    put("G91")
                cur_z = pending_z
                zmin_body = min(zmin_body, cur_z)
            pending_z = None
            if "E" in m:
                want = snaps[i]
                have = out_r.get(tool, 0.0)
                if abs(have - want) > 1e-4:
                    correct(have - want, f"T{tool} back to the original retraction")
                    have = want
                e = m["E"]
                out_r[tool] = round(have - e if e < 0 else max(0.0, have - e), 5)
                if e > 0 and ("X" in m or "Y" in m):
                    used[tool] = used.get(tool, 0.0) + e
            if "F" not in m and orig_f is not None and cur_f != orig_f:
                put(
                    f"G1 F{fmt(orig_f)}"
                )  # a dropped or inserted line had changed the feed rate
            cur_f = orig_f
            z_after = (cur_z + m["Z"] if rel else m["Z"]) if "Z" in m else cur_z
            if "Z" in m:
                zmin_body = min(zmin_body, z_after)
            if ("X" in m or "Y" in m or "arc" in m) and not rel:
                nxt = (
                    m.get("X", last_xy[0] if last_xy else math.nan),
                    m.get("Y", last_xy[1] if last_xy else math.nan),
                )
                known = last_xy is not None and not math.isnan(nxt[0] + nxt[1])
                if (
                    skip
                    and tower
                    and (
                        last_xy is None or not known or path_hits_box(last_xy, m, tower)
                    )
                    and min(cur_z, z_after) < tower_safe_z - 1e-6
                ):
                    if m.get("E", 0) > 0:
                        tower_hits.append(
                            i + 1
                        )  # an extrusion over the old tower: should not exist
                    else:
                        # travel (straight or a G2/G3 spiral hop) from an unknown point (after a pick/purge) or
                        # across the tower that still stands: go over it in a straight line, then down to the
                        # travel's own Z
                        put(f"G1 Z{fmt(tower_safe_z)} F600 ; over the old tower")
                        xy = " ".join(
                            f"{k}{fmt(m[k])}" for k in ("X", "Y", "E") if k in m
                        )
                        if xy:
                            put(f"G1 {xy} F{fmt(orig_f or 12000)}")
                        put(f"G1 Z{fmt(z_after)} F600")
                        cur_f = 600.0
                        cur_z = z_after
                        tower_lifts += 1
                        last_xy = None if math.isnan(nxt[0] + nxt[1]) else nxt
                        continue
                last_xy = None if math.isnan(nxt[0] + nxt[1]) else nxt
            elif ("X" in m or "Y" in m) and rel:
                last_xy = None
            cur_z = z_after
        emit(line)
    # ---- stats + config block ----
    stats_end = w.config_start if w.config_start is not None else len(lines)
    orig: dict[str, list[float]] = {}
    for line in lines[exec_end + 1 : stats_end]:
        if line.startswith(b"; filament used [mm] ="):
            orig["mm"] = [float(x) for x in line.split(b"=")[1].split(b",")]
        elif line.startswith(b"; filament cost ="):
            orig["cost"] = [float(x) for x in line.split(b"=")[1].split(b",")]
    density = [1.24] * 4  # PLA, when the header names no filament_density
    for line in lines[: w.exec_lo]:
        if line.startswith(b"; filament_density:"):
            density = [float(x) for x in line.split(b":")[1].split(b",")]
    fil_area = math.pi * (profile.filament_diameter_mm / 2) ** 2
    n = len(orig.get("mm", density))
    mm = [used.get(t, 0.0) for t in range(n)]
    cm3 = [v * fil_area / 1000 for v in mm]
    g = [
        c * (density[t] if t < len(density) else density[-1]) for t, c in enumerate(cm3)
    ]
    oc, om = orig.get("cost", []), orig.get("mm", [])
    cost = [
        oc[t] * mm[t] / om[t] if t < len(oc) and t < len(om) and om[t] else 0.0
        for t in range(n)
    ]
    for line in lines[exec_end + 1 : stats_end]:
        key = line.split(b"=")[0].strip()
        if key == b"; filament used [mm]":
            put("; filament used [mm] = " + ", ".join(f"{v:.2f}" for v in mm))
        elif key == b"; filament used [cm3]":
            put("; filament used [cm3] = " + ", ".join(f"{v:.2f}" for v in cm3))
        elif key == b"; filament used [g]":
            put("; filament used [g] = " + ", ".join(f"{v:.2f}" for v in g))
        elif key == b"; filament cost":
            put("; filament cost = " + ", ".join(f"{v:.2f}" for v in cost))
        elif key == b"; total filament used [g]":
            put(f"; total filament used [g] = {sum(g):.2f}")
        elif key == b"; total filament cost":
            put(f"; total filament cost = {sum(cost):.2f}")
        elif key == b"; total filament change":
            put(f"; total filament change = {changes}")
        elif key == b"; estimated printing time (normal mode)":
            put(
                f"; estimated printing time (normal mode) = {left // 60}h {left % 60}m 0s"
            )
        else:
            emit(line)
    out.extend(lines[stats_end:])
    if zmin_body < LR["z"] - 1e-6:
        raise GateFailure(
            f"internal: a body move goes to Z {zmin_body} below the resume layer Z {LR['z']}",
            command=CMD,
        )
    if tower_hits:
        raise GateFailure(
            f"{len(tower_hits)} extrusions cross the old tower area below its top (lines {tower_hits[:5]})",
            command=CMD,
        )
    if tower_lifts:
        notes.append(
            f"{tower_lifts} travels lifted to Z {fmt(tower_safe_z)} over the old tower (after a pick/purge or across it)"
        )
    if pause_layer is not None and rl < pause_layer - 40:
        notes.append(
            f"resume layer {rl} is {pause_layer - rl} layers below the paused layer {pause_layer}: "
            "check the measurement"
        )
    if pause_layer is not None and rl > pause_layer:
        notes.append(
            f"layer {pause_layer} was paused part-way: its rest is skipped; if nothing below it failed, "
            "the printer's own RESUME continues exactly from the pause"
        )
    z_range_check(out, z_lim)
    return Resume(
        lines=out,
        layer=rl,
        z=LR["z"],
        kept_layer=rl - 1,
        kept_z=LG["z"],
        gap=gap,
        active=A,
        tools=sorted(body_tools),
        z_travel=z_travel,
        first=first_xy,
        dropped=dropped,
        changes=changes,
        minutes_left=left,
        grams=g,
        pause_layer=pause_layer,
        notes=notes,
        line_from=i0 + 1,
    )


def z_range_check(out: list[bytes], z_hi: float) -> None:
    """fail-closed last pass over the finished resume file: absolute Z of every move after the RESUME marker
    (G90/G91, G92 Z) must stay within 0..z_hi; otherwise refuse the whole file"""
    z: float | None = None
    rel = seen = False
    for n, line in enumerate(out, 1):
        if not seen:
            seen = line.startswith(HEADER_START.encode())
            continue
        code = line.split(b";", 1)[0].strip()
        if code.startswith(b"G91"):
            rel = True
        elif code.startswith(b"G90"):
            rel = False
        elif code.startswith(b"G92"):
            if {"X", "Y", "Z"} & set(
                words(code)
            ):  # a coordinate offset would hide the real Z
                raise GateFailure(
                    f"output line {n}: coordinate offset {code.decode()} after the RESUME marker",
                    command=CMD,
                )
        elif code.startswith(b"G28"):
            z = None
        else:
            m = move(code)
            if m is not None and "Z" in m:
                if rel:
                    if z is None:
                        raise GateFailure(
                            f"output line {n}: relative Z move from an unknown Z: {code.decode()}",
                            command=CMD,
                        )
                    z += m["Z"]
                else:
                    z = m["Z"]
                if not 0.0 <= z <= z_hi + 1e-6:
                    raise GateFailure(
                        f"output line {n}: Z {z:.3f} outside 0..{z_hi:g} (Z axis limit - {fmt(Z_MARGIN)}): "
                        f"{code.decode()}; nothing written",
                        command=CMD,
                    )
    if not seen:
        raise GateFailure("internal: no RESUME marker in the output", command=CMD)
