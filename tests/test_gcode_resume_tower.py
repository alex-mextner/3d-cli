"""Regression tests for the old-prime-tower handling of lib/gcode_resume.py (issue #238), ported from the
babushkin-dom gen/test_resume_gcode.py on its small U1-style plate (two heads, prime tower, 6 layers).

Each test is a bug that reached review or the printer: the old prime tower standing above the resume layer, the
firmware tool change moving X/Y at a Z below the old tower, Orca's one-axis tower moves, G2/G3 spiral travels,
header lifts past the Z axis, the 1.5 mm tool-change hop past Z max, RETRACT_LENGTH in the purge macro (the U1
macro does `| abs` on it and cancels the job)."""
from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

import gcode_resume as gr
from errors import GateFailure, UsageError
from gcode_resume_fixtures import U1_RESUME_YAML
from printer_ops import devices

_PROFILE = devices._parse(Path("printer-devices.yaml"), yaml.safe_load(U1_RESUME_YAML))["u1"].resume
assert _PROFILE is not None
PROFILE = _PROFILE
TOWER = (27.0, 203.0, 54.0, 230.0)  # x0 y0 x1 y1 of the synthetic prime tower
NUM = re.compile(r"([XYZEIJ])(-?\d*\.?\d+)")


def synthetic(layers: int = 6, h: float = 0.2, one_axis: bool = False, arcs: bool = False) -> str:
    out = ["; HEADER_BLOCK_START", "; HEADER_BLOCK_END", "; EXECUTABLE_BLOCK_START",
           "EXCLUDE_OBJECT_DEFINE NAME=A CENTER=100,50 POLYGON=[[90,40],[110,40],[110,60],[90,60]]",
           "M140 S65", "M104 T0 S215", "M109 T0 S215", "G90", "M83", "T0", "M104 S215 T1"]
    tool = 0
    for n in range(1, layers + 1):
        z = round(n * h, 3)
        out += [";LAYER_CHANGE", f";Z:{z}", f";HEIGHT:{h}", ";BEFORE_LAYER_CHANGE", "G92 E0", "G1 E-.5 F2400"]
        if n > 1:
            out.append("EXCLUDE_OBJECT_END NAME=A")
        out += [";AFTER_LAYER_CHANGE", f"SET_PRINT_STATS_INFO TOTAL_LAYER={layers} CURRENT_LAYER={n}",
                f"M73 P{round(100 * (n - 1) / layers)} R{10 * (layers - n + 1)}",
                "EXCLUDE_OBJECT_START NAME=A", f"G1 X90 Y40 Z{z + 0.6} F30000", f"G1 Z{z}", "G1 E.5 F1800",
                ";TYPE:Outer wall", "G1 X110 Y40 E1", "G1 X110 Y60 E1", "G1 X90 Y60 E1", "G1 X90 Y40 E1",
                "G1 E-.5 F2400", *(["G2 X40 Y210 I-25 J85 F30000", "G2 X90 Y40 I25 J-85"] if arcs else []),
                "EXCLUDE_OBJECT_END NAME=A",
                f"G1 X{TOWER[0]} Y{TOWER[1]} Z{z + 0.6} F30000", f"G1 Z{z}", "G1 E.5 F1800", ";TYPE:Prime tower",
                f"G1 X{TOWER[2]}{'' if one_axis else f' Y{TOWER[1]}'} E1", "G1 E-10", "G91", "G1 Z1.5 F1800", "G90",
                f"M109 S215 T{1 - tool}", "M400", f"T{1 - tool}", f"SM_PRINT_PREEXTRUDE_FILAMENT INDEX={1 - tool}",
                f"G1 X{TOWER[0]} Y{TOWER[3]}", f"G1 Z{z}", "G1 E10 F1800",
                f"G1 X{TOWER[2]}{'' if one_axis else f' Y{TOWER[3]}'} E1", "G1 E-.5 F2400",
                "EXCLUDE_OBJECT_START NAME=A"]
        tool = 1 - tool
    out += ["EXCLUDE_OBJECT_END NAME=A", "M73 P100 R0", "PRINT_END", "; EXECUTABLE_BLOCK_END",
            "; filament used [mm] = 10.00, 10.00", "; total filament change = 6", "; CONFIG_BLOCK_START",
            "; CONFIG_BLOCK_END", ""]
    return "\n".join(out)


def run(src: str | None = None, z_max: float | None = None, *, layer: int, tower_top: float | None = None,
        no_tower: bool = False) -> str:
    profile = PROFILE if z_max is None else dataclasses.replace(PROFILE, z_max_mm=z_max)
    res = gr.build(gr.Walk((src or synthetic()).encode().split(b"\n")), profile, name="in.gcode", layer=layer,
                   tower_top=tower_top, no_tower=no_tower)
    return b"\n".join(res.lines).decode()


def body(text: str) -> str:
    return text.split(gr.HEADER_END, 1)[1]


def header(text: str) -> str:
    return text.split(gr.HEADER_START, 1)[1].split(gr.HEADER_END, 1)[0]


def walk(text: str) -> Iterator[tuple[str, float | None, tuple[float | None, float | None] | None,
                                      tuple[float | None, float | None] | None]]:
    """(line, z, xy, start) with absolute Z / X / Y tracked over the resume part of the file (G0-G3); start is
    the X/Y before the line"""
    z: float | None = None
    xy: tuple[float | None, float | None] | None = None
    rel = False
    for line in text.split(gr.HEADER_START, 1)[1].split("\n"):
        c = line.split(";")[0].strip()
        start = xy
        if c.startswith("G91"):
            rel = True
        elif c.startswith("G90"):
            rel = False
        elif re.match(r"^G[0-3]\b", c):
            p = {k: float(v) for k, v in NUM.findall(c)}
            if "Z" in p:
                z = (z or 0) + p["Z"] if rel else p["Z"]
            if ("X" in p or "Y" in p) and not rel:
                xy = (p.get("X", xy[0] if xy else None), p.get("Y", xy[1] if xy else None))
        elif re.match(r"^T\d+$", c) or c.startswith("SM_PRINT_PREEXTRUDE"):
            xy = None
        yield c, z, xy, start


def _picks(text: str) -> list[tuple[str, float | None]]:
    return [(c, z) for c, z, _, _ in walk(text) if re.match(r"^T\d+$", c) or c.startswith("SM_PRINT_PREEXTRUDE")]


def test_no_retract_length() -> None:
    assert "RETRACT_LENGTH" not in run(layer=3, tower_top=1.2)
    assert "RETRACT_LENGTH" not in run(layer=3, tower_top=1.2, no_tower=True)


def test_header_clears_tower_given_by_tower_top_only() -> None:
    first_z = next(float(m) for m in re.findall(r"^G1 Z([\d.]+)", header(run(layer=2, tower_top=5.0)), re.M))
    assert first_z >= 5.0 + 2.0


def test_tool_change_lifted_above_old_tower() -> None:
    for c, z in _picks(run(layer=2, tower_top=1.2)):
        assert z is not None and z >= 1.2 + 2.0 - 1e-6, (c, z)


def test_no_tower_extrusion_or_low_crossing_below_old_top() -> None:
    out = run(layer=2, tower_top=1.0)
    feat, layer_z = None, 0.0
    for line in body(out).split("\n"):
        if line.startswith(";Z:"):
            layer_z = float(line[3:])
        elif line.startswith(";TYPE:"):
            feat = line[6:]
        elif feat == "Prime tower" and (m := gr.move(line.encode())) and m.get("E", 0) > 0 and ("X" in m or "Y" in m):
            assert layer_z > 1.0, line
    prev: tuple[float, float] | None = None
    for c, z, xy, _ in walk(out):
        x0, y0 = xy if xy else (None, None)
        cur = (x0, y0) if x0 is not None and y0 is not None else None
        # an X/Y move over the old tower at or just above its top (1.0) would hit it
        if cur and z is not None and z < 1.0 + 0.1 and prev:
            for k in range(51):
                x = prev[0] + (cur[0] - prev[0]) * k / 50
                y = prev[1] + (cur[1] - prev[1]) * k / 50
                assert not (TOWER[0] <= x <= TOWER[2] and TOWER[1] <= y <= TOWER[3]), (c, z, prev, cur)
        prev = cur


def test_tower_continues_above_old_top() -> None:
    assert re.search(r";TYPE:Prime tower\nG1 X54.0 Y203.0 E1", body(run(layer=2, tower_top=0.4)))


@pytest.mark.parametrize("no_tower", [False, True])
def test_needs_tower_height(no_tower: bool) -> None:
    # --no-tower still parks heads past the old tower, so its height is needed there too
    with pytest.raises(UsageError, match="tower height is unknown"):
        run(layer=3, no_tower=no_tower)


def test_no_tower_tool_changes_clear_the_old_tower() -> None:
    for c, z in _picks(run(layer=2, no_tower=True, tower_top=1.0)):
        assert z is not None and z >= 1.0 + 2.0 - 1e-6, (c, z)


def test_tower_found_from_one_axis_moves() -> None:
    # Orca writes tower extrusions with one axis (modal Y); the tower must still be found
    src = synthetic(one_axis=True)
    assert not re.search(r";TYPE:Prime tower\nG1 X[\d.]+ Y[\d.]+ E", src)  # fixture really is one-axis
    with pytest.raises(UsageError, match="tower height is unknown"):
        run(src, layer=3)
    for c, z in _picks(run(src, layer=2, tower_top=1.2)):
        assert z is not None and z >= 1.2 + 2.0 - 1e-6, (c, z)


def test_arc_travels_over_old_tower_are_lifted() -> None:
    # Snapmaker Orca writes spiral/arc travels (G2/G3); they must be tracked and lifted like G1
    src = synthetic(arcs=True)
    out = run(src, layer=2, no_tower=True, tower_top=1.2)
    for c, z, _, start in walk(out):
        if re.match(r"^G[23]\b", c):
            p = {k: float(v) for k, v in NUM.findall(c)}
            sx, sy = start if start else (None, None)
            assert sx is not None and sy is not None, ("arc from an unknown position", c)
            cx, cy = sx + p.get("I", 0), sy + p.get("J", 0)
            rad = math.hypot(sx - cx, sy - cy)
            over = cx - rad <= TOWER[2] and cx + rad >= TOWER[0] and cy - rad <= TOWER[3] and cy + rad >= TOWER[1]
            assert not over or (z is not None and z >= 1.2 + 0.1), (c, z, start)
    assert src.count("\nG2 ") and out.count("; over the old tower") >= 2


def test_progress_restarts_at_zero() -> None:
    # the resumed job showed the original 85 % from its first line; P must run 0..100 over the resumed part
    # (against the minutes left at the cut), R stays the slicer's minutes left
    out = run(layer=4, tower_top=0.8)
    m = [(int(p), int(q)) for p, q in re.findall(r"^M73 P(\d+) R(\d+)", out.split(gr.HEADER_START, 1)[1], re.M)]
    assert m[0] == (0, 30), m  # layer 4 of 6: 30 min left at the cut
    assert m[-1] == (100, 0), m
    assert [q for _, q in m[1:]] == [30, 20, 10, 0], m
    assert [p for p, _ in m[1:]] == [0, 33, 67, 100], m


def test_z_limit() -> None:
    # the header must not lift past stepper_z.position_max - 1
    out = run(layer=3, tower_top=1.2, z_max=4.5)
    assert max(float(z) for z in re.findall(r"^G1 Z([\d.]+)", out, re.M)) <= 4.5 - 1.0 + 1e-6
    with pytest.raises(UsageError, match="no room above the part"):
        run(layer=3, tower_top=1.2, z_max=3.5)


def test_tower_at_z_ceiling_refused() -> None:
    # the old tower near the Z ceiling plus the original 1.5 mm tool-change hop went past position_max; the
    # final pass must refuse. Two tool changes in a row: the second hop starts from the tower lift (1.0 + 2 = 3.0)
    src = synthetic().replace("SM_PRINT_PREEXTRUDE_FILAMENT INDEX=0\n",
                              "SM_PRINT_PREEXTRUDE_FILAMENT INDEX=0\nG91\nG1 Z1.5 F1800\nG90\nM400\nT1\nG91\n"
                              "G1 Z1.5 F1800\nG90\nM400\nT0\n", 1)
    run(src, layer=2, tower_top=1.0)  # same input fits under a high ceiling
    with pytest.raises(GateFailure, match=r"outside 0\.\..*nothing written"):
        run(src, layer=2, tower_top=1.0, z_max=4.5)


@pytest.mark.parametrize(("old", "new", "match"), [
    ("G1 X90 Y40 E1\n", "G91\nG1 X1 E0.1\nG90\nG1 X90 Y40 E1\n", "relative X/Y"),   # G91 extrusion (tower or not)
    ("G1 X90 Y40 E1\n", "G91\nG2 X1 Y1 I1 J0\nG90\nG1 X90 Y40 E1\n", "relative X/Y"),  # G91 arc travel
    ("G92 E0\n", "G92 Z0.5 E0\n", "G92 Z"),
])
def test_untracked_moves_are_refused(old: str, new: str, match: str) -> None:
    # issue #238 review 8: relative X/Y and G92 Z are not tracked by the tower/arc/Z passes: fail closed
    src = synthetic()
    at = src.index(";Z:1.0")                      # layer 5, after the resume layer 3
    with pytest.raises(UsageError, match=match):
        run(src[:at] + src[at:].replace(old, new, 1), layer=3, tower_top=0.4)


def test_relative_tower_moves_before_the_cut_are_refused() -> None:
    # a G91 tower extrusion below the cut would leave the old tower area unknown
    src = synthetic().replace(";TYPE:Prime tower\n", ";TYPE:Prime tower\nG91\nG1 Y5 E0.3\nG90\n", 1)
    with pytest.raises(UsageError, match="relative X/Y"):
        run(src, layer=4, tower_top=0.8)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_tower_top_must_be_a_real_height(bad: float) -> None:
    # review: --tower-top nan made every `layer_z <= top` false and printed the tower into the old one
    with pytest.raises(UsageError, match="finite Z"):
        run(layer=2, tower_top=bad)


def test_g92_z_in_the_start_gcode_is_refused() -> None:
    # review: the start G-code is not copied, so a Z offset there would silently change the frame
    with pytest.raises(UsageError, match="G92 Z"):
        run(synthetic().replace("G90\nM83\nT0", "G90\nG92 Z10\nM83\nT0", 1), layer=3, tower_top=0.4)
