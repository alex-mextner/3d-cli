"""gcode_resume.py — resume a stopped multi-head print from a layer: a new G-code that continues the original above
the last intact layer of a part still on the bed (no gluing, no Z homing). Issue
https://github.com/alex-mextner/3d-cli/issues/138.

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
  the purge macro with `INDEX=n FORCE=1` purges off the bed (LENGTH mm, RETRACT_LENGTH back) and sets the
  once-per-job flag, so the original per-change purge lines stay no-ops.
- Logical tools map 1:1 to the heads, as in the original job.
- Filament state per head is tracked as "mm retracted from primed"; the header leaves every head exactly as the
  original left it at the cut, so the original un-retracts prime it back.

Traps it guards: --no-tower purges after EVERY `T<n>` (the slicer writes no purge line when it switches back to the
job's starting head, which then reached the model unprimed); the layer preamble ends the previous layer's object
(`EXCLUDE_OBJECT_END` with no object open is a Klipper error -> CANCEL_PRINT -> M84); the wipe moves of that preamble
belong to the layer below and are dropped.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import GateFailure, UsageError

if TYPE_CHECKING:
    from printer_ops.devices import ResumeProfile

CMD = "gcode-resume"
HEADER_START = "; ===== RESUME of"
HEADER_END = "; ===== end of resume header: original G-code follows ====="

NUM = re.compile(rb"([XYZEF])(-?\d*\.?\d+)")
TOOL = re.compile(rb"^T(\d+)\s*$")
ARC = re.compile(rb"^G[23](?![0-9.])")


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
    """G0/G1 line -> dict of X/Y/Z/E/F floats, else None"""
    if not (
        line.startswith((b"G1", b"G0")) and (len(line) == 2 or line[2:3] in b" \t;")
    ):
        return None
    return {chr(k[0]): float(v) for k, v in NUM.findall(line.split(b";", 1)[0])}


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
        st: dict[str, Any] = {
            "tool": None,
            "temp": {},
            "bed": None,
            "fan": {},
            "vel": None,
            "accel": None,
            "pa": {},
            "purifier": None,
            "r": {},
            "rel": False,
            "obj": None,
            "feature": None,
            "m73": None,
            "zmax": 0.0,
            "z": None,
            "f": None,
        }
        snaps: dict[int, Any] = {}
        end = len(self.lines) if snap_from is not None else stop
        cut = None
        for i in range(end):
            if i == stop:
                cut = _copy_state(st)
                if snap_from is None:
                    break
            line = self.lines[i]
            c = line[:1]
            if c == b"G":
                m = move(line)
                if m is None:
                    if line.startswith(b"G91"):
                        st["rel"] = True
                    elif line.startswith(b"G90"):
                        st["rel"] = False
                    continue
                if "F" in m:
                    st["f"] = m["F"]
                if "Z" in m:
                    st["z"] = (st["z"] or 0.0) + m["Z"] if st["rel"] else m["Z"]
                    st["zmax"] = max(st["zmax"], st["z"])
                if "E" in m and st["tool"] is not None:
                    t = st["tool"]
                    if snap_from is not None and i >= snap_from:
                        snaps[i] = st["r"].get(t, 0.0)
                    e = m["E"]
                    st["r"][t] = round(
                        st["r"].get(t, 0.0) - e
                        if e < 0
                        else max(0.0, st["r"].get(t, 0.0) - e),
                        5,
                    )
            elif c == b"T":
                mt = TOOL.match(line)
                if mt:
                    if snap_from is not None and i >= snap_from:
                        snaps[i] = dict(st["r"])
                    st["tool"] = int(mt.group(1))
            elif c == b"M":
                w = words(line)
                code = int(w.get("M", -1))
                if code in (104, 109) and "S" in w:
                    tt = int(w["T"]) if "T" in w else st["tool"]
                    st["temp"][tt] = w["S"]
                elif code in (140, 190) and "S" in w:
                    st["bed"] = w["S"]
                elif code == 106:
                    st["fan"][int(w.get("P", 0))] = w.get("S", 255.0)
                elif code == 107:
                    st["fan"][int(w.get("P", 0))] = 0.0
                elif code == 204:
                    st["accel"] = line.split(b";")[0].strip()
                elif code == 73 and "P" in w:
                    st["m73"] = (int(w["P"]), int(w.get("R", 0)))
                elif code == 82:
                    raise _bad(f"line {i + 1}: M82 absolute E is not supported")
            elif c == b"E":
                if line.startswith(b"EXCLUDE_OBJECT_START"):
                    st["obj"] = obj_name(line)
                elif line.startswith(b"EXCLUDE_OBJECT_END"):
                    st["obj"] = None
            elif c == b"S":
                if line.startswith(b"SET_VELOCITY_LIMIT"):
                    st["vel"] = line.split(b";")[0].strip()
                elif (
                    line.startswith(b"SET_PRESSURE_ADVANCE")
                    and b"EXTRUDER=" not in line
                ):
                    st["pa"][st["tool"]] = words(line.replace(b"ADVANCE=", b"A"))["A"]
                elif line.startswith(b"SET_PURIFIER_MODE"):
                    st["purifier"] = line.split(b";")[0].strip()
            elif c == b";" and line.startswith(b";TYPE:"):
                st["feature"] = line[6:].strip().decode()
        if cut is None:
            cut = _copy_state(st)
        return cut, snaps


def extruder_name(t: int) -> str:
    return "extruder" if t == 0 else f"extruder{t}"


Box = tuple[float, float, float, float]


def tower_bbox(w: Walk, margin: float) -> Box | None:
    xs, ys = [], []
    feature = None
    for line in w.lines[w.layers[0]["start"] : w.exec_hi]:
        if line.startswith(b";TYPE:"):
            feature = line[6:].strip()
        elif feature == b"Prime tower":
            m = move(line)
            if m and m.get("E", 0) > 0 and "X" in m and "Y" in m:
                xs.append(m["X"])
                ys.append(m["Y"])
    if not xs:
        return None
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


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


def build(
    w: Walk,
    profile: ResumeProfile,
    *,
    name: str,
    top_z: float | None = None,
    layer: int | None = None,
    paused_at: int | None = None,
    no_tower: bool = False,
    home_xy: bool = False,
) -> Resume:
    """The resume G-code: original header + a resume header + the original from the resume layer on."""
    if (top_z is None) == (layer is None):
        raise _bad("give exactly one of --top-z, --layer")
    lines = w.lines
    exec_end = w.exec_hi
    name = re.sub(r"[\x00-\x1f\x7f]", "?", name)  # goes into a G-code comment: no line breaks
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
    tower = tower_bbox(w, profile.tower_margin_mm) if no_tower else None
    if no_tower and tower is None:
        raise _bad("--no-tower: the G-code has no prime tower")
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
    z_travel = max(LR["z"] + 0.6, z_seen or 0.0) + profile.clearance_mm
    out_r: dict[int, float] = {}
    purged: dict[int, float] = {}  # filament the off-bed purges consume: counted in the statistics
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
    put(
        f"; heads T{A} first, then {'/'.join(f'T{t}' for t in sorted(body_tools - {A})) or 'none'}; "
        f"prime tower {'replaced by off-bed purges (--no-tower)' if no_tower else 'continued'}"
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
        put(
            f"{purge} INDEX={t} FORCE=1 TEMP={fmt(print_temp[t])} LENGTH={fmt(profile.purge_mm)}"
            f" RETRACT_LENGTH={fmt(profile.purge_retract_mm)}"
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
    left = st["m73"][1] if st["m73"] else 0  # slicer minutes left at the cut: 0 % of the resume file
    if st["m73"]:
        put(f"M73 P0 R{left}")
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
    used: dict[int, float] = dict(purged)  # preflight reads the heads in use from these statistics
    changes = 0
    last_xy: tuple[float, float] | None = (first_xy["X"], first_xy["Y"])
    tower_hits: list[int] = []
    pending_z = None

    def correct(d: float, why: str) -> None:
        nonlocal cur_f
        put(f"G1 E{fmt(d)} F1800 ; {why}")
        cur_f = 1800.0

    for i in range(i0, exec_end + 1):
        line = lines[i]
        m = move(line)
        if ARC.match(line):
            raise _bad(
                f"line {i + 1}: G2/G3 arcs after the cut are not supported (slice without arc fitting)"
            )
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
            want = snaps[i].get(tool, 0.0)
            if abs(out_r.get(tool, 0.0) - want) > 1e-4:
                correct(
                    out_r.get(tool, 0.0) - want,
                    f"T{tool} parks {fmt(want)} mm retracted",
                )
                out_r[tool] = want
            tool = int(mt.group(1))
            changes += 1
            last_xy = None  # the firmware parks/picks: position unknown here
            emit(line)
            if no_tower:
                # the tower prime goes, so every pick purges off the bed; the slicer writes no
                # purge line when it switches back to the job's starting head
                put("; --no-tower: prime off the bed instead of on the tower")
                put(
                    f"{purge} INDEX={tool} FORCE=1 LENGTH="
                    f"{fmt(out_r.get(tool, 0.0) + profile.no_tower_prime_mm)} "
                    f"RETRACT_LENGTH={fmt(profile.purge_retract_mm)}"
                )
                used[tool] = used.get(tool, 0.0) + profile.no_tower_prime_mm
                out_r[tool] = profile.purge_retract_mm
            continue
        if line.startswith(b"M73 P"):
            # the original's P counts from the first layer; the printer and its dashboards must
            # show the progress of THIS file, so P is rescaled over the minutes left at the cut
            r = int(words(line).get("R", 0))
            put(f"M73 P{max(0, min(100, round(100 * (1 - r / left)))) if left else 100} R{r}")
            continue
        if no_tower and line.startswith(purge.encode()):
            continue  # replaced by the purge after the T<n> above
        if m is not None:
            if "F" in m:
                orig_f = m["F"]
            if (
                tower
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
                if (
                    "Z" in m
                ):  # a layer change or lift on a dropped line still sets the modal Z
                    pending_z = m["Z"]
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
                put(f"G1 Z{fmt(pending_z)}")
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
            if "Z" in m:
                cur_z = cur_z + m["Z"] if rel else m["Z"]
                zmin_body = min(zmin_body, cur_z)
            if "X" in m or "Y" in m:
                nxt = (
                    m.get("X", last_xy[0] if last_xy else math.nan),
                    m.get("Y", last_xy[1] if last_xy else math.nan),
                )
                if (
                    tower
                    and not rel
                    and last_xy
                    and not math.isnan(nxt[0] + nxt[1])
                    and seg_hits_box(last_xy, nxt, tower)
                ):
                    tower_hits.append(i + 1)
                last_xy = None if math.isnan(nxt[0] + nxt[1]) else nxt
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
        notes.append(
            f"--no-tower: {len(tower_hits)} travels cross the tower area (lines {tower_hits[:5]}...)"
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
