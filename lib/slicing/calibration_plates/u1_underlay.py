"""calibration_plates/u1_underlay.py — Snapmaker U1 plate 1: every test in black on one white underlay.

WHAT: one Snapmaker Orca project for the U1 (4 heads, 270 mm bed) and its G-code:
    underlay  1–2 solid layers (`calibrations.underlay_layers`, default 1) in a filament that
              contrasts with the tests' (head T4 white by default, chosen by the contrast check of
              #263, `--underlay-tool`), no brim; part fan 0 and bed 70 °C on its layers (#320). The underlay between
              the tests is the first-layer map (no separate first-layer patches, tg#22369)
    label     front strip on the underlay (#322): session code in groups, date DD.MM.YYYY and
              printer, one line per head (T1 black #2, T4 white #1, brand/type), the process;
              a readable bold font with strokes of ≥ 2 perimeters, QR of the code (segno)
    tests     in black (head T1) from the first layer above the underlay, packed 1 mm apart,
              combined where one sample serves two tests (tg#22397): holes PL/PH/HC tiles carry
              the ovality edges, ruler and Ø10 ring; flow tiles are top-skin tiles, plain and
              ironed halves; thin walls are the dividers of the flow block; fits and hinges
              share one tile; PA lines and small holes fill the 50 mm frame below its walls;
              the codes double as the small-text test (2–3 mm). Also temperature, retraction,
              ringing and VFA towers, MVS strip, M3/M4, slots, elephant foot, pins, bridges
              10–25 over bare underlay, overhangs 35–75°, corner fiducials. Every test is read
              by the top camera unless it cannot be (`read_by`, tg#22399: ≥ 80 % camera_top)
  Exactly one tool change, T4 -> T1, at the first layer above the underlay (#308): the
  underlay layers print with one head, no prime tower (the U1's change G-code purges the new
  head, SM_PRINT_PREEXTRUDE_FILAMENT).
HOW: every test is its own Orca object (its own EXCLUDE_OBJECT name): a white base exactly
  under its footprint (2 layers, part of the sheet: the main underlay object has a hole there,
  the base overlaps it by SEAM so the perimeters fuse like neighbouring lines) and its black
  parts on top, so Orca keeps the black parts on the underlay top (it drops objects, not
  parts). Loose pieces (pins, tab, hinges, elephant-foot blocks) stand on white feet in
  cut-outs of their base (#320 `cutout`); through holes cut the base. Per-object parameters:
  ironing is an object setting of the project; the others are G-code hooks on the object's
  black visits (apply_hooks): flow scales the extrusion E, ringing/VFA set ACCEL/speed per Z
  band (band chosen by the layer's mid height, #301), retraction scales the visit's
  retractions and carries the difference to the next de-retraction, temperature bands start
  above every other object so only the tower prints while the temperature differs (the
  reason the K1 prints it sequentially). PA lines and the MVS strip are hand-written G-code
  spliced at the end of the first black layers.
WHY: tg#22330 / tg#22369 / tg#22397 / tg#22399 — the plate comes off as one labelled sheet kept as
  the sample of these spools, dense, read by the top camera without a person; the U1 night
  failure of 2026-10-16 (heads swapped on the first layer) must not repeat.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from cli.resources import max_workers
from slicing.calibration_gen import Box, GenError, prism_stl
from slicing.calibration_plates.dense import (
    motion,
    openscad_stl,
    square_tube_boxes,
    tubes_stl,
    velocity_line,
)
from slicing.calibration_underlay import (
    MIN_CONTRAST_DE,
    pick_underlay_tool,
    rect_difference,
    underlay_height,
    validate_layers,
)

PLATE_ID = "u1_underlay"
BED = 270.0
EDGE = 5.0  # usable bed: BED minus this on every side
FIRST_H, LAYER_H = 0.25, 0.2  # the U1 0.20 mm process (initial_layer_print_height 0.25)
UNDERLAY_LAYERS = 1  # `calibrations.underlay_layers`, set by configure()
U = (
    FIRST_H + (UNDERLAY_LAYERS - 1) * LAYER_H
)  # underlay top: every black test starts here
UNDERLAY_BRIM = (
    0.0  # none: the underlay holds the tests (#320); only a bare-bed plate has brims
)
AREA = (12.0, 12.0, 258.0, 258.0)  # tests are packed inside this
PACK_GAP = 1.0  # bare underlay between neighbouring tests (tg#22397: minimal)
SEAM = (
    0.05  # a test's white base overlaps the sheet: perimeter spacing, not a double line
)
CUT_MARGIN = 1.0  # a loose piece's white foot clears its base by this
HOLE_CUT = 0.4  # through holes cut the base this much wider
EMBOSS = 0.4  # raised marks: 2 layers
FONT = "Liberation Sans:style=Bold"
CAP_PER_SIZE, STEM_PER_SIZE = 0.955, 0.19  # measured on this font (OpenSCAD 2026.10)
STROKE = 0.9  # ≥ 2 perimeters of 0.42 (tg#22369)
THIN_STROKE = 0.6  # 2 mm codes: 2-perimeter strokes would close their counters
MARK_CAP = 3.0  # marks (tg#22369: 3 mm, not 5)
BAND = 4.6  # front band with the test's mark only
BAND2 = 8.5  # front band with the mark (front line) and the parameter codes (behind it)
TOWER_SCV = 8.0  # the printer's square_corner_velocity (live toolhead, 2026-10-16)
PRINTER_PA = (
    0.02  # the printer's pressure_advance (live extruder; the preset leaves it off)
)
TOP, SIDE, MANUAL = "camera_top", "camera_side", "manual"

HQ_D = (3.0, 4.0, 5.0, 6.0, 8.0)
HQ_FRONT = ((3.0, 4.5), (4.0, 10.5), (5.0, 17.0), (6.0, 24.5))  # (Ø, x), y = 6
HQ_BACK_Y, HQ_FN, HQ_COMP, HQ_H = 17.0, 64, 0.1, 1.6
RING = (10.0, 7.6)
SMALL_D = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
RULER_STEP = 5.0
PLATE_T = 1.2
FIT_PIN_D, FIT_H, FIT_BOSS, CHAMFER = 3.5, 3.0, 3.0, 0.4
FIT_GAPS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35)
FIT_PITCH, FIT_ROW = 8.0, 9.0
SCREW_M3, SCREW_M4, SCREW_H, SCREW_BOSS = (2.7, 3.0, 3.6), (3.5, 4.0, 4.7), 4.0, 4.0
XS3 = (5.25, 14.25, 23.25)
SLOTS = (2.00, 2.10, 2.20)
TAB = (12.0, 6.0, U + 7 * LAYER_H)  # 1.85 mm thick: a whole number of layers
RIB, SLOT_DEPTH, SLOT_LEN = 1.2, 3.0, 10.0
HINGE_R, HINGE_PIN_R, HINGE_L, HINGE_LEAF, HINGE_T = 2.0, 0.8, 10.0, 5.0, 1.0
HINGE_RADIAL = (0.20, 0.30, 0.40)
HINGE_AXIAL = (0.20, 0.30, 0.40)
HINGE_PX, HINGE_PY, HINGE_X0 = 12.5, 13.0, 6.0  # grid pitch, room for the row codes
EF_BLOCK, EF_HOLE, EF_H = 10.0, 5.0, 2.0
THIN = (0.25, 0.35, 0.45)
THIN_H, THIN_LEN = 3.0, 13.0
FRAME = (50.0, 2.0, 1.6)  # side, wall, height
FRAME_CLEAR = 3.0  # nothing inside the frame closer to its walls (calipers' jaws)
BRIDGES = (
    10.0,
    15.0,
    20.0,
    25.0,
)  # the U1's bridge_max_mm is 25 (print-preflight gate)
PILLAR, BRIDGE_W, BRIDGE_H, BRIDGE_SLAB, WALL = 3.6, 8.0, 3.0, 0.8, 0.9
# up to 70°: a layer steps out 0.2·tan 70° = 0.55 mm, inside print-preflight's 0.6 mm edge
# step-out; 75° (0.75 mm) counts as printing on nothing and fails the overhangs gate
OVERHANGS, OVERHANG_H, OVERHANG_D = (35, 45, 55, 60, 65, 70), 2.4, 8.0
FLOWS = (92, 94, 96, 98, 100, 102, 104, 106, 108)  # % of the preset flow
FLOW_TILE, FLOW_H = 15.0, 1.0
FLOW_HALF, FLOW_GAP = 8.5, 2.0  # plain / ironed half, the gap that holds a divider wall
FLOW_D = FLOW_TILE + BAND
FLOW_BLOCK = (3 * 2 * FLOW_HALF + 2 * FLOW_GAP, BAND + 3 * FLOW_D + 2 * FLOW_GAP)
TOWER_BASE, TOWER_BAND = 1.0, 2.0
RING_SIDE, RING_WALL, STEP_IN = 22.0, 0.84, 0.2
RING_ACCELS = (2000, 4000, 6000, 8000, 10000)
RING_SPEED = 150.0
VFA_R, VFA_SPEEDS, VFA_ACCEL, VFA_SCV = 13.0, (60, 120, 180, 240, 300), 6000.0, 20.0
RETRACTS = (0.4, 0.8, 1.2, 1.6, 2.0)  # mm; the preset's is 1.2
STRING_D, STRING_GAP = 4.0, 14.0
TEMPS = (235, 230, 225, 220, 215, 210, 205)  # °C bottom → top; the preset's is 220
TEMP_BAND = 2.4
TEMP_BLOCK = (18.0, 8.0)
PINS = (1.0, 1.5, 2.0, 3.0)
PIN_H = 6.0
MVS_VALUES = (16, 20, 24, 28, 32)  # mm³/s; the SnapSpeed preset caps at 20
MVS_W, MVS_LINES, MVS_SPACING, MVS_BASE_SPEED, MVS_SEG = 0.5, 8, 0.45, 25.0, 10.0
MVS_LAYERS = 3
MVS_ACCEL = 10000
PA_VALUES = tuple(round(0.01 * k, 2) for k in range(11))  # 0 … 0.10
PA_SLOW, PA_FAST, PA_SLOW_L, PA_FAST_L, PA_PITCH, PA_ACCEL = (
    20.0,
    120.0,
    7.0,
    16.0,
    2.6,
    5000,
)
QR_MODULE = 1.2

SLICER = os.environ.get("SLICER") or os.path.expanduser(
    "~/Applications/snapmaker_orca/AppRun"
)
PROFILES = os.environ.get("ORCA_RES") or os.path.expanduser(
    "~/Applications/snapmaker_orca/resources/profiles"
)
MACHINE = "Snapmaker U1 (0.4 nozzle)"
PROCESS = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
FILAMENT = "Snapmaker PLA SnapSpeed @U1"  # what the U1 reports loaded on T1, T3 and T4
BLACK, WHITE = (
    1,
    4,
)  # 1-based extruders = head labels: BLACK (T1) the tests, WHITE the underlay (T4)
SLOT_COLOURS = ["#000000", "#FFFFFF", "#2D9E59", "#FFFFFF"]  # live print_task_config
HEAD_SPOOLS = {
    1: 2,
    4: 1,
}  # Spoolman spool ids the label names (heads not listed: none)
# User 2026-10-16: bed 70 °C for the first layer, fan off on the underlay; vendor values else.
# Toolchange ooze: babushkin-dom docs/u1_toolchange_ooze.md (parked head at −40, 15 s preheat).
PROCESS_OVERRIDES: dict[str, Any] = {
    "enable_prime_tower": "0",
    "brim_type": "no_brim",
    "enable_support": "0",
    "top_shell_layers": "3",
    "bottom_shell_layers": "2",
    "sparse_infill_density": "15%",
    "standby_temperature_delta": "-40",
    "preheat_time": "15",
    "curr_bed_type": "Textured PEI Plate",
}
FILAMENT_OVERRIDES = {
    "close_fan_the_first_x_layers": str(UNDERLAY_LAYERS),
    "textured_plate_temp_initial_layer": "70",
    "hot_plate_temp_initial_layer": "70",
}


def configure(layers: int, tool: int) -> None:
    """The underlay's layer count (`calibrations.underlay_layers`) and head (1-based extruder:
    T4 = 4) for this run. U, TAB, the fan setting and WHITE are module state read at call time
    by everything that builds the plate, so they are rebound here, once, before build_tests()."""
    global UNDERLAY_LAYERS, U, TAB, WHITE
    UNDERLAY_LAYERS = validate_layers(layers)
    U = underlay_height(FIRST_H, LAYER_H, UNDERLAY_LAYERS)
    TAB = (TAB[0], TAB[1], U + 7 * LAYER_H)
    if tool not in (1, 2, 3, 4) or tool == BLACK:
        raise GenError(
            f"the underlay head must be one of T2, T3, T4 (T1 prints the tests), got T{tool}"
        )
    WHITE = tool
    FILAMENT_OVERRIDES["close_fan_the_first_x_layers"] = str(UNDERLAY_LAYERS)


IRONING = {"ironing_type": "top"}  # object setting of the ironed flow halves

Rect = tuple[float, float, float, float]


@dataclass
class Band:
    z0: float  # absolute, bed = 0
    z1: float
    enter: tuple[str, ...] = ()
    transform: Callable[[str], str] | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Test:
    """One sample = one Orca object: black parts, white feet and cuts of its base (OpenSCAD in
    bed coordinates; raw STL text for prisms), hooks on its black visits, its map entry. `subs`
    lists the tests a combined sample serves (name, what it reads, read_by)."""

    name: str
    kind: str
    caption: str  # preview: what it is and which parameter
    rect: Rect
    read_by: str
    black: list[str] = field(default_factory=list)
    black_stl: list[str] = field(default_factory=list)
    feet: list[str] = field(default_factory=list)
    cuts: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    underlay: str = "on"
    read: str = ""
    top: float = 0.0  # highest black point (bed = 0)
    flow: float = 1.0
    settings: dict[str, str] = field(default_factory=dict)  # Orca object settings
    subs: list[tuple[str, str, str]] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    retract_bands: list[tuple[float, float, float]] = field(
        default_factory=list
    )  # z0, z1, mm


# ---- text and small shapes -------------------------------------------------------------------


def text(
    txt: str,
    x: float,
    y: float,
    z: float,
    cap: float = MARK_CAP,
    halign: str = "left",
    stroke: float | None = None,
) -> str:
    """Raised bold text, baseline at y, strokes thickened to `stroke` (2 perimeters by default,
    less below 2.5 mm where 2-perimeter strokes would fill the counters)."""
    size = cap / CAP_PER_SIZE
    st = stroke if stroke is not None else (STROKE if cap >= 2.5 else THIN_STROKE)
    r = max(0.0, (st - STEM_PER_SIZE * size) / 2)
    safe = txt.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"translate([{x:.3f},{y:.3f},{z:.3f}]) linear_extrude({EMBOSS}) offset(r={r:.3f}) "
        f'text("{safe}", size={size:.3f}, font="{FONT}", halign="{halign}", valign="baseline");'
    )


def mark(t: Rect, txt: str) -> str:
    """The test's mark at the left of its front band, on the underlay."""
    return text(txt, t[0] + 0.6, t[1] + 0.9, U)


def code(txt: str, x: float, t: Rect, cap: float) -> str:
    """A parameter code centred on x, on the second line of the front band (a small-text test:
    each test's codes have their own cap height, recorded in the map)."""
    return text(txt, x, t[1] + 4.7, U, cap=cap, halign="center")


def cubes(boxes: Iterable[Box]) -> str:
    return "".join(
        f"translate([{b[0]:.4f},{b[1]:.4f},{b[2]:.4f}]) cube([{b[3] - b[0]:.4f},{b[4] - b[1]:.4f},{b[5] - b[2]:.4f}]);"
        for b in boxes
    )


def cyl(x: float, y: float, d: float, z0: float, z1: float, fn: int = 64) -> str:
    return f"translate([{x:.4f},{y:.4f},{z0:.4f}]) cylinder(d={d:.4f},h={z1 - z0:.4f},$fn={fn});"


def ring(cx: float, cy: float, z: float, outer: float, inner: float) -> str:
    return (
        f"translate([{cx},{cy},{z}]) difference() {{ cylinder(d={outer},h={EMBOSS},$fn=96); "
        f"translate([0,0,-1]) cylinder(d={inner},h={EMBOSS + 2},$fn=96); }}"
    )


def body(t: Rect) -> Rect:
    """The test's area behind its two-line front band (mark + codes)."""
    return t[0], t[1] + BAND2, t[2], t[3]


def body1(t: Rect) -> Rect:
    """The test's area behind a one-line front band (mark only)."""
    return t[0], t[1] + BAND, t[2], t[3]


def tile(b: Rect, h: float) -> str:
    return f"translate([{b[0]},{b[1]},{U}]) cube([{b[2] - b[0]},{b[3] - b[1]},{h}]);"


def r3(vals: Iterable[float]) -> list[float]:
    return [round(v, 3) for v in vals]


def circ(x: float, y: float, d: float, z0: float, z1: float) -> list[float]:
    return r3((x - d / 2, y - d / 2, z0, x + d / 2, y + d / 2, z1))


def hund(v: float) -> str:
    return f"{round(v * 100)}"


def base_hole(x: float, y: float, d: float) -> str:
    return cyl(x, y, d + HOLE_CUT, -1, U + 0.01, 96)


def ruler_boxes(x0: float, y_bar: float, z: float) -> list[Box]:
    """A 25 mm ruler along X: a bar with its back edge at y_bar, ticks every 5 mm toward the front."""
    s = 0.9
    out: list[Box] = [(x0, y_bar - s, z, x0 + 25 + s, y_bar, z + EMBOSS)]
    for k in range(6):
        x = x0 + k * RULER_STEP
        out.append(
            (
                x,
                y_bar - s - (3.0 if k % 2 == 0 else 2.0),
                z,
                x + s,
                y_bar - s,
                z + EMBOSS,
            )
        )
    return out


# ---- tests -------------------------------------------------------------------------------------


def t_holes(t: Rect, variant: str, suffix: str = "") -> Test:
    """Holes Ø3/4/5/6 in front, Ø8 and Ø10 (PL) or the raised Ø10 ring (PH, HC) behind, the
    tile's straight front/left edges and a ruler: the ovality test on the same tile."""
    b = body(t)
    cuts, base, items, codes = [], [], [], []
    holes = [(d, b[0] + x, b[1] + 6.0) for d, x in HQ_FRONT] + [
        (8.0, b[0] + 7.0, b[1] + HQ_BACK_Y)
    ]
    if variant == "PL":
        holes.append((10.0, b[0] + 20.5, b[1] + HQ_BACK_Y))
    cc = 3.0
    for d, x, y in holes:
        if variant == "PH":
            n = max(3, round(2 * d))
            drawn = d / math.cos(math.pi / n)
            cuts.append(cyl(x, y, drawn, U - 1, U + HQ_H + 1, n))
        else:
            drawn = d + 2 * HQ_COMP if variant == "HC" else d
            cuts.append(cyl(x, y, drawn, U - 1, U + HQ_H + 1, HQ_FN))
        base.append(base_hole(x, y, drawn))
        items.append(
            {
                "label": f"{d:g}",
                "bbox": circ(x, y, d, U, U + HQ_H),
                "diameter_mm": d,
                "drawn_mm": round(drawn, 3),
            }
        )
        if y < b[1] + 10:
            codes.append(code(f"{d:g}", x, t, cc))
    extra = ""
    if variant != "PL":
        rx, ry = b[0] + 20.5, b[1] + HQ_BACK_Y
        extra = ring(rx, ry, U + HQ_H, *RING)
        items.append(
            {
                "label": "ring",
                "bbox": circ(rx, ry, RING[0], U + HQ_H, U + HQ_H + EMBOSS),
                "outer_d_mm": RING[0],
                "inner_d_mm": RING[1],
            }
        )
    rul = ruler_boxes(b[0] + 1.75, b[3] - 0.6, U + HQ_H)
    what = {
        "PL": f"plain {HQ_FN}-facet holes (reference), Ø10 at the back right",
        "PH": "polyholes, 2·Ø sides (Orca hole_to_polyhole), raised ring Ø10 at the back right",
        "HC": f"holes +{HQ_COMP:g} mm radius (xy_hole_compensation), raised ring Ø10 at the back right",
    }[variant]
    src = (
        f"union() {{ difference() {{ {tile(b, HQ_H)} {' '.join(cuts)} }} {extra} {cubes(rul)} }} "
        f"{mark(t, variant)} {' '.join(codes)}"
    )
    return Test(
        f"holes_{variant.lower()}{suffix}",
        "holes",
        f"{variant} holes Ø3–{'10' if variant == 'PL' else '8'} + ovality edges, ruler",
        t,
        TOP,
        [src],
        cuts=base,
        items=items,
        top=U + HQ_H + EMBOSS,
        params={"variant": variant, "code_cap_mm": cc, "tile_mm": HQ_H},
        subs=[
            (f"holes_{variant.lower()}{suffix}", "hole Ø per variant", TOP),
            (
                f"ovality_{variant.lower()}{suffix}",
                "X vs Y Ø against the tile edges and the ruler",
                TOP,
            ),
        ],
        read=f"Hole tile {variant}: {what}. Codes = Ø of the front row (3 mm codes); Ø8 behind on "
        "the left. Ovality on the same tile: the front and left tile edges are the X/Y "
        "references, the ruler at the back ticks every 5 mm. Through holes (the base is cut).",
    )


def t_frame(t: Rect) -> Test:
    """50 mm frame; inside, ≥ 3 mm from its walls and no taller than them (the calipers' jaws and
    a beam laid across the diagonals stay free): PA codes (the lines are hand-written) and the
    small-holes strip."""
    side, w, h = FRAME
    b = body1(t)
    x0, y0 = b[0], b[1]
    ix0, iy0 = x0 + w + FRAME_CLEAR, y0 + w + FRAME_CLEAR  # inner free area
    pa_x = ix0 + 5.0
    pa_y = iy0 + 1.0
    pa_codes = []
    for k, v in enumerate(PA_VALUES):
        if k % 2 == 0:  # every other line: 2 mm codes need more than one line pitch
            pa_codes.append(
                text(
                    f"{round(v * 100)}",
                    ix0 + 2.0,
                    pa_y + k * PA_PITCH - 0.8,
                    U,
                    cap=2.0,
                    halign="center",
                )
            )
    sy = (
        pa_y + (len(PA_VALUES) - 1) * PA_PITCH + 3.0
    )  # small holes: codes, then the strip
    pitch = 6.5
    strip = (ix0, sy + 3.0, ix0 + pitch * len(SMALL_D), sy + 3.0 + 7.0)
    cuts, base, items, scodes = [], [], [], []
    for k, d in enumerate(SMALL_D):
        x, y = strip[0] + pitch * (k + 0.5), strip[1] + 3.5
        cuts.append(cyl(x, y, d, U - 1, U + h + 1))
        base.append(base_hole(x, y, d))
        items.append(
            {"label": f"{d:g}", "bbox": circ(x, y, d, U, U + h), "diameter_mm": d}
        )
        scodes.append(text(f"{d:g}", x, sy + 0.4, U, cap=2.0, halign="center"))
    inner_limit = x0 + side - w - FRAME_CLEAR, y0 + side - w - FRAME_CLEAR
    if (
        strip[2] > inner_limit[0]
        or strip[3] > inner_limit[1]
        or pa_x + 2 * PA_SLOW_L + PA_FAST_L > inner_limit[0]
    ):
        raise GenError(
            "frame_50: its inner tests reach the 3 mm clearance of the walls"
        )
    src = (
        f"difference() {{ translate([{x0},{y0},{U}]) cube([{side},{side},{h}]); "
        f"translate([{x0 + w},{y0 + w},{U - 1}]) cube([{side - 2 * w},{side - 2 * w},{h + 2}]); }} "
        f"difference() {{ {tile(strip, h)} {' '.join(cuts)} }} {mark(t, 'FRAME 50 · PA · Ø1–4')} "
        f"{' '.join(pa_codes)} {' '.join(scodes)}"
    )
    pa_items = [
        {
            "label": f"{round(v * 100)}",
            "bbox": r3(
                (
                    pa_x,
                    pa_y + k * PA_PITCH - 0.25,
                    U,
                    pa_x + 2 * PA_SLOW_L + PA_FAST_L,
                    pa_y + k * PA_PITCH + 0.25,
                    U + LAYER_H,
                )
            ),
            "pressure_advance": v,
        }
        for k, v in enumerate(PA_VALUES)
    ]
    return Test(
        "frame_50",
        "dimension",
        "50 mm frame (size, skew); inside: PA lines 0–0.10, small holes Ø1–4",
        t,
        TOP,
        [src],
        cuts=base,
        top=U + h,
        params={
            "side_mm": side,
            "wall_mm": w,
            "pa_origin": [pa_x, pa_y],
            "pa_pitch_mm": PA_PITCH,
            "pa_values": ",".join(f"{v:g}" for v in PA_VALUES),
            "small_holes_mm": ",".join(f"{d:g}" for d in SMALL_D),
            "code_cap_mm": 2.0,
        },
        items=[
            {"label": "frame", "bbox": r3((x0, y0, U, x0 + side, y0 + side, U + h))},
            *pa_items,
            *items,
        ],
        subs=[
            ("frame_50", "outside X/Y and diagonals (skew)", TOP),
            (
                "pa_lines",
                f"PA 0–0.10: even line width at the {PA_SLOW:g}↔{PA_FAST:g} mm/s steps",
                TOP,
            ),
            ("holes_small", "smallest open hole Ø1–4", TOP),
        ],
        read="50 mm square frame: outside X and Y and both diagonals (skew = their difference); "
        "everything inside keeps 3 mm from the walls and stays below them. Inside: PA lines "
        f"(hand-written in layer 3; codes = PA in hundredths on every other line, 2 mm): each "
        f"line {PA_SLOW_L:g} mm at {PA_SLOW:g} mm/s, {PA_FAST_L:g} at {PA_FAST:g}, {PA_SLOW_L:g} at "
        f"{PA_SLOW:g}; the line even at both steps is the PA (printer's own {PRINTER_PA:g}, restored "
        "after). Behind them the small through holes Ø1–4 (codes = Ø).",
    )


def t_fit_hinge(t: Rect) -> Test:
    """Fits and hinges on one sample: a tile with two rows of pins in bossed holes (P plain, C with
    a 0.4 mm chamfer at pin foot and hole bottom; columns = radial gap) and, behind it, a 3 × 3
    grid of print-in-place hinges (rows radial, columns axial gap). Pins and hinges stand on
    white feet in cut-outs of the base (#320 cutout)."""
    cc = 2.5
    b = body(t)
    d = FIT_PIN_D
    x_at = [b[0] + 6.0 + FIT_PITCH * (k + 0.5) for k in range(len(FIT_GAPS))]
    fit_d = 2 * FIT_ROW + 1.0
    plate_b = (b[0], b[1], b[0] + 6.0 + FIT_PITCH * len(FIT_GAPS) + 0.5, b[1] + fit_d)
    bosses, holes_, pins, feet, base, items, codes = [], [], [], [], [], [], []
    for row, (series, c) in enumerate((("P", 0.0), ("C", CHAMFER))):
        yc = b[1] + 0.5 + FIT_ROW * (row + 0.5)
        codes.append(text(series, b[0] + 0.6, yc - 1.25, U + PLATE_T, cap=cc))
        for x, g in zip(x_at, FIT_GAPS, strict=True):
            hole = d + 2 * g
            bosses.append(cyl(x, yc, hole + FIT_BOSS, U, U + FIT_H))
            holes_.append(cyl(x, yc, hole, U - 1, U + FIT_H + 1))
            if c:
                holes_.append(
                    f"translate([{x},{yc},{U - 0.01}]) cylinder(d1={hole + 2 * c:.3f},d2={hole:.3f},h={c + 0.01},$fn=64);"
                )
                feet.append(
                    f"translate([{x},{yc},0]) cylinder(d1={d - 2 * c:.3f},d2={d - 2 * c + 2 * U:.3f},h={U},$fn=64);"
                )
            else:
                feet.append(cyl(x, yc, d, 0, U))
            pins.append(cyl(x, yc, d, U, U + FIT_H))
            base.append(cyl(x, yc, d + 2 * CUT_MARGIN, -1, U + 0.01, 96))
            items.append(
                {
                    "label": f"{series}{hund(g)}",
                    "bbox": circ(x, yc, hole, 0, U + FIT_H),
                    "gap_mm": g,
                    "chamfer_mm": c,
                }
            )
    for x, g in zip(x_at, FIT_GAPS, strict=True):
        codes.append(code(hund(g), x, t, cc))
    plate = f"difference() {{ union() {{ {tile(plate_b, PLATE_T)} {' '.join(bosses)} }} {' '.join(holes_)} }}"
    hb = (b[0], plate_b[3] + 1.0)
    blacks: list[str] = []
    for i, gr in enumerate(HINGE_RADIAL):
        ay = hb[1] + CUT_MARGIN + HINGE_LEAF + i * HINGE_PY
        codes.append(text(hund(gr), t[0] + 0.6, ay - 1.2, U, cap=cc))
        for j, ga in enumerate(HINGE_AXIAL):
            hx = hb[0] + HINGE_X0 + CUT_MARGIN + j * HINGE_PX
            h = hinge_scad(hx, ay, gr, ga)
            blacks.append(
                f"intersection() {{ {h} translate([{hx - 1},{ay - 10},{U}]) cube([{HINGE_L + 2},20,10]); }}"
            )
            feet.append(
                f"intersection() {{ {h} translate([{hx - 1},{ay - 10},-1]) cube([{HINGE_L + 2},20,{U + 1}]); }}"
            )
            base.append(
                cubes(
                    [
                        (
                            hx - CUT_MARGIN,
                            ay - HINGE_LEAF - CUT_MARGIN,
                            -1.0,
                            hx + HINGE_L + CUT_MARGIN,
                            ay + HINGE_LEAF + CUT_MARGIN,
                            U + 0.01,
                        )
                    ]
                )
            )
            items.append(
                {
                    "label": f"H{hund(gr)}/{hund(ga)}",
                    "bbox": r3(
                        (
                            hx,
                            ay - HINGE_LEAF,
                            0.0,
                            hx + HINGE_L,
                            ay + HINGE_LEAF,
                            2 * HINGE_R,
                        )
                    ),
                    "radial_gap_mm": gr,
                    "axial_gap_mm": ga,
                }
            )
            if i == len(HINGE_RADIAL) - 1:
                codes.append(
                    text(
                        hund(ga),
                        hx + HINGE_L / 2,
                        ay + HINGE_LEAF + CUT_MARGIN + 0.6,
                        U,
                        cap=cc,
                        halign="center",
                    )
                )
    return Test(
        "fit_hinge",
        "fit",
        "fits P/C gap 0.10–0.35 + hinges radial × axial 0.2–0.4",
        t,
        MANUAL,
        [
            plate,
            f"union() {{ {' '.join(pins)} }}",
            *blacks,
            f"union() {{ {mark(t, 'FIT P/C · HINGE r×a')} {' '.join(codes)} }}",
        ],
        feet=feet,
        cuts=base,
        items=items,
        underlay="cutout",
        top=U + FIT_H,
        params={
            "fit_gaps_mm": ",".join(f"{g:g}" for g in FIT_GAPS),
            "pin_d_mm": d,
            "chamfer_mm": CHAMFER,
            "hinge_radial_mm": ",".join(f"{g:g}" for g in HINGE_RADIAL),
            "hinge_axial_mm": ",".join(f"{g:g}" for g in HINGE_AXIAL),
            "code_cap_mm": cc,
        },
        subs=[
            ("fit_p", "radial fit clearance, plain", MANUAL),
            ("fit_c", "radial fit clearance, chamfered", MANUAL),
            ("hinges", "print-in-place radial and axial clearance", MANUAL),
        ],
        read="Front tile: pins Ø3.5 in holes through 3 mm bosses, row P plain, row C with a 0.4 mm "
        "chamfer at the pin foot and hole bottom; codes in front = radial gap in hundredths (2.5 mm). "
        "Push each pin out from below: the smallest gap that frees by hand is the moving fit. "
        "Behind: hinges, rows = radial gap (codes on the left), columns = axial gap (codes at the "
        "back); fold each, the smallest gaps that turn freely after a firm first twist are the "
        "clearances (#269). Pins and hinges stand on white 2-layer feet in cut-outs.",
    )


def hinge_scad(hx: float, ay: float, gr: float, ga: float) -> str:
    """A print-in-place hinge from the bed up: left end hx, axis along X at y = ay, radial gap gr
    (pin in the bore, knuckles to leaves), axial gap ga (between knuckles)."""
    r, rp, length, leaf, th = HINGE_R, HINGE_PIN_R, HINGE_L, HINGE_LEAF, HINGE_T
    k1, k2 = hx + length / 3, hx + 2 * length / 3

    def knuckle(xa: float, xb: float) -> str:
        return (
            f"translate([{xa},{ay},{r}]) rotate([0,90,0]) cylinder(r={r},h={xb - xa},$fn=48);"
            f"translate([{xa},{ay - r},0]) cube([{xb - xa},{2 * r},{r}]);"
        )

    hole = (
        f"translate([{hx - 1},{ay},{r}]) rotate([0,90,0]) rotate([0,0,45]) "
        f"{{ cylinder(r={rp + gr},h={length + 2},$fn=48); cube([{rp + gr},{rp + gr},{length + 2}]); }}"
    )
    a = (
        f"union() {{ {knuckle(hx, k1 - ga / 2)} {knuckle(k2 + ga / 2, hx + length)}"
        f" translate([{hx},{ay - leaf},0]) cube([{length},{leaf - r - gr},{th}]);"
        f" translate([{hx},{ay - r - gr},0]) cube([{k1 - ga / 2 - hx},{r + gr},{th}]);"
        f" translate([{k2 + ga / 2},{ay - r - gr},0]) cube([{hx + length - k2 - ga / 2},{r + gr},{th}]);"
        f" translate([{hx},{ay},{r}]) rotate([0,90,0]) cylinder(r={rp},h={length},$fn=32); }}"
    )
    bb = (
        f"difference() {{ union() {{ {knuckle(k1 + ga / 2, k2 - ga / 2)}"
        f" translate([{hx},{ay + r + gr},0]) cube([{length},{leaf - r - gr},{th}]);"
        f" translate([{k1 + ga / 2},{ay},0]) cube([{k2 - k1 - ga},{r + gr},{th}]); }} {hole} }}"
    )
    return f"union() {{ {a} {bb} }}"


def t_screws(t: Rect, sizes: tuple[float, ...], m: int) -> Test:
    b = body(t)
    yc = b[1] + 8.0
    cc = 2.5
    bosses, cuts, base, items, codes = [], [], [], [], []
    for dx, d in zip(XS3, sizes, strict=True):
        x = b[0] + dx
        bosses.append(cyl(x, yc, d + SCREW_BOSS, U, U + SCREW_H))
        cuts.append(cyl(x, yc, d, U - 1, U + SCREW_H + 1))
        base.append(base_hole(x, yc, d))
        items.append(
            {
                "label": f"{d:g}",
                "bbox": circ(x, yc, d, U, U + SCREW_H),
                "diameter_mm": d,
            }
        )
        codes.append(code(f"{d:g}", x, t, cc))
    src = (
        f"difference() {{ union() {{ {tile(b, PLATE_T)} {' '.join(bosses)} }} {' '.join(cuts)} }} "
        f"{mark(t, f'M{m}')} {' '.join(codes)}"
    )
    return Test(
        f"screw_m{m}",
        "fit",
        f"M{m} holes {sizes[0]:g}/{sizes[1]:g}/{sizes[2]:g}",
        t,
        TOP,
        [src],
        cuts=base,
        items=items,
        top=U + SCREW_H,
        params={
            "screw": f"M{m}",
            "holes_mm": ",".join(f"{d:g}" for d in sizes),
            "code_cap_mm": cc,
        },
        read=f"M{m} holes in {SCREW_H:g} mm bosses, codes = Ø: the camera reads the printed Ø; by hand, "
        "the largest that still cuts a holding thread = tap size, the smallest the screw slides "
        "through = clearance.",
    )


def t_slots(t: Rect) -> Test:
    b = body(t)
    ys = b[1] + 1.0
    cc = 2.5
    ribs, items, codes = [], [], []
    for dx, w in zip(XS3, SLOTS, strict=True):
        x = b[0] + dx
        ribs += [
            (
                x - w / 2 - RIB,
                ys,
                U + PLATE_T,
                x - w / 2,
                ys + SLOT_LEN,
                U + PLATE_T + SLOT_DEPTH,
            ),
            (
                x + w / 2,
                ys,
                U + PLATE_T,
                x + w / 2 + RIB,
                ys + SLOT_LEN,
                U + PLATE_T + SLOT_DEPTH,
            ),
        ]
        clear = w - TAB[2]
        items.append(
            {
                "label": hund(clear),
                "bbox": r3(
                    (
                        x - w / 2,
                        ys,
                        U + PLATE_T,
                        x + w / 2,
                        ys + SLOT_LEN,
                        U + PLATE_T + SLOT_DEPTH,
                    )
                ),
                "slot_mm": w,
                "clearance_mm": round(clear, 3),
            }
        )
        codes.append(code(hund(clear), x, t, cc))
    plate_b = (b[0], b[1], b[2], b[1] + SLOT_LEN + 2.0)
    tx0, ty0 = (b[0] + b[2]) / 2 - TAB[0] / 2, plate_b[3] + 1.0 + CUT_MARGIN
    tab = (tx0, ty0, U, tx0 + TAB[0], ty0 + TAB[1], TAB[2])
    items.append(
        {
            "label": "tab",
            "bbox": r3((tx0, ty0, 0.0, tx0 + TAB[0], ty0 + TAB[1], TAB[2])),
            "thickness_mm": TAB[2],
        }
    )
    src = f"union() {{ {tile(plate_b, PLATE_T)} {cubes(ribs)} }} {mark(t, 'SL')} {' '.join(codes)}"
    return Test(
        "slots",
        "fit",
        f"slots {SLOTS[0]:g}/{SLOTS[1]:g}/{SLOTS[2]:g} + tab {TAB[2]:g}",
        t,
        MANUAL,
        [src, cubes([tab])],
        feet=[cubes([(tx0, ty0, 0.0, tx0 + TAB[0], ty0 + TAB[1], U)])],
        cuts=[
            cubes(
                [
                    (
                        tx0 - CUT_MARGIN,
                        ty0 - CUT_MARGIN,
                        -1.0,
                        tx0 + TAB[0] + CUT_MARGIN,
                        ty0 + TAB[1] + CUT_MARGIN,
                        U + 0.01,
                    )
                ]
            )
        ],
        items=items,
        underlay="cutout",
        top=U + PLATE_T + SLOT_DEPTH,
        params={
            "slot_widths_mm": ",".join(f"{w:g}" for w in SLOTS),
            "tab_mm": TAB[2],
            "code_cap_mm": cc,
        },
        read=f"Slots between ribs, codes = clearance over the {TAB[2]:g} mm tab in hundredths. The tab "
        "(behind, on a white foot in a cut-out) goes into each by hand: the narrowest it enters is "
        "the sliding fit.",
    )


def t_elephant(t: Rect, suffix: str = "") -> Test:
    b = body(t)
    cc = 3.0
    blacks, feet, base, items, codes = [], [], [], [], []
    for k, ch in enumerate((0.0, CHAMFER)):
        x, y = b[0] + 2.0 + k * (EF_BLOCK + 4.0), b[1] + 1.0 + CUT_MARGIN
        cx, cy = x + EF_BLOCK / 2, y + EF_BLOCK / 2
        if ch:
            solid = (
                f"hull() {{ translate([{x + ch},{y + ch},0]) cube([{EF_BLOCK - 2 * ch},{EF_BLOCK - 2 * ch},0.01]);"
                f" translate([{x},{y},{ch}]) cube([{EF_BLOCK},{EF_BLOCK},{EF_H - ch}]); }}"
            )
            hole = (
                f"translate([{cx},{cy},-1]) cylinder(d={EF_HOLE},h={EF_H + 2},$fn=64);"
                f"translate([{cx},{cy},-0.01]) cylinder(d1={EF_HOLE + 2 * ch + 0.02},d2={EF_HOLE},h={ch + 0.01},$fn=64);"
            )
        else:
            solid = f"translate([{x},{y},0]) cube([{EF_BLOCK},{EF_BLOCK},{EF_H}]);"
            hole = (
                f"translate([{cx},{cy},-1]) cylinder(d={EF_HOLE},h={EF_H + 2},$fn=64);"
            )
        blk = f"difference() {{ {solid} {hole} }}"
        blacks.append(
            f"intersection() {{ {blk} translate([{x - 1},{y - 1},{U}]) cube([{EF_BLOCK + 2},{EF_BLOCK + 2},10]); }}"
        )
        feet.append(
            f"intersection() {{ {blk} translate([{x - 1},{y - 1},-1]) cube([{EF_BLOCK + 2},{EF_BLOCK + 2},{U + 1}]); }}"
        )
        base.append(
            cubes(
                [
                    (
                        x - CUT_MARGIN,
                        y - CUT_MARGIN,
                        -1.0,
                        x + EF_BLOCK + CUT_MARGIN,
                        y + EF_BLOCK + CUT_MARGIN,
                        U + 0.01,
                    )
                ]
            )
        )
        items.append(
            {
                "label": hund(ch),
                "bbox": r3((x, y, 0.0, x + EF_BLOCK, y + EF_BLOCK, EF_H)),
                "chamfer_mm": ch,
            }
        )
        codes.append(code(hund(ch), cx, t, cc))
    return Test(
        f"elephant_foot{suffix}",
        "elephant_foot",
        "elephant foot: 10 mm blocks, chamfer 0/0.4",
        t,
        TOP,
        blacks + [f"union() {{ {mark(t, 'EF')} {' '.join(codes)} }}"],
        feet=feet,
        cuts=base,
        items=items,
        underlay="cutout",
        top=EF_H,
        params={"block_mm": EF_BLOCK, "code_cap_mm": cc},
        read="10 mm blocks with a Ø5 hole printed from the bed (bottom 2 layers white, in cut-outs), "
        "codes = first-layer chamfer. From the top the flare shows as a white-edged rim around "
        "the plain block's top outline; the chamfered one should have none.",
    )


def frame_box(x0: float, y0: float, w: float, d: float, h: float) -> str:
    return (
        f"difference() {{ translate([{x0},{y0},{U}]) cube([{w},{d},{h}]); "
        f"translate([{x0 + WALL},{y0 + WALL},{U - 1}]) cube([{w - 2 * WALL},{d - 2 * WALL},{h + 2}]); }}"
    )


def t_bridges(t: Rect) -> Test:
    """Bridges over bare underlay (tg#22399: nothing read from the top lies under them)."""
    b = body(t)
    cc = 3.0
    x = b[0] + 0.5
    parts, items, codes = [], [], []
    for s in BRIDGES:
        x1 = x + 2 * PILLAR + s
        y0 = b[1] + 1.0
        parts.append(
            f"{frame_box(x, y0, PILLAR, BRIDGE_W, BRIDGE_H)} {frame_box(x1 - PILLAR, y0, PILLAR, BRIDGE_W, BRIDGE_H)} "
            f"translate([{x},{y0},{U + BRIDGE_H}]) cube([{x1 - x},{BRIDGE_W},{BRIDGE_SLAB}]);"
        )
        items.append(
            {
                "label": f"{s:g}",
                "bbox": r3(
                    (
                        x + PILLAR,
                        y0,
                        U + BRIDGE_H,
                        x1 - PILLAR,
                        y0 + BRIDGE_W,
                        U + BRIDGE_H + BRIDGE_SLAB,
                    )
                ),
                "span_mm": s,
            }
        )
        codes.append(code(f"{s:g}", (x + x1) / 2, t, cc))
        x = x1 + 1.5
    return Test(
        "bridges",
        "bridge",
        f"bridges {'/'.join(f'{s:g}' for s in BRIDGES)} mm over bare underlay",
        t,
        SIDE,
        [f"union() {{ {' '.join(parts)} }} {mark(t, 'BR')} {' '.join(codes)}"],
        items=items,
        top=U + BRIDGE_H + BRIDGE_SLAB,
        params={"spans_mm": ",".join(f"{s:g}" for s in BRIDGES), "code_cap_mm": cc},
        read="Bridges (codes = span mm) on hollow 0.9 mm-wall pillars over bare underlay: sag and "
        "the underside lines from the side camera or by hand; the U1's bridge_max_mm is 25.",
    )


def overhang_step(a: int) -> float:
    return max(
        WALL / math.cos(math.radians(a)) + OVERHANG_H * math.tan(math.radians(a)) + 1.6,
        7.0,
    )


def t_overhangs(t: Rect) -> Test:
    b = body(t)
    cc = 2.5
    x = b[0] + 0.5
    y0, y1 = b[1] + 1.0, b[1] + 1.0 + OVERHANG_D
    stls, items, codes = [], [], []
    for a in OVERHANGS:
        th = WALL / math.cos(math.radians(a))
        reach = OVERHANG_H * math.tan(math.radians(a))
        stls.append(
            prism_stl(
                [
                    (x, U),
                    (x + th, U),
                    (x + th + reach, U + OVERHANG_H),
                    (x + reach, U + OVERHANG_H),
                ],
                y0,
                y1,
            )
        )
        items.append(
            {
                "label": f"{a}",
                "bbox": r3((x, y0, U, x + th + reach, y1, U + OVERHANG_H)),
                "angle_deg": a,
            }
        )
        codes.append(code(f"{a}", x + (th + reach) / 2, t, cc))
        x += overhang_step(a)
    return Test(
        "overhangs",
        "overhang",
        "overhang slabs 35–75° from vertical",
        t,
        SIDE,
        [f"union() {{ {mark(t, 'OH°')} {' '.join(codes)} }}"],
        black_stl=stls,
        items=items,
        top=U + OVERHANG_H,
        params={"angles_deg": ",".join(str(a) for a in OVERHANGS), "code_cap_mm": cc},
        read="Slabs leaning 35…75° from vertical (codes), 0.9 mm thick: underside quality per angle "
        "from the side camera; curl or droop = more cooling or slower overhangs.",
    )


def t_pins(t: Rect, suffix: str = "") -> Test:
    b = body(t)
    cc = 2.0
    pitch = (b[2] - b[0]) / len(PINS)
    posts, items, codes = [], [], []
    for k, dd in enumerate(PINS):
        x, y = b[0] + pitch * (k + 0.5), b[1] + 4.0
        posts.append(cyl(x, y, dd, U, U + PIN_H, 48))
        items.append(
            {
                "label": f"{dd:g}",
                "bbox": circ(x, y, dd, U, U + PIN_H),
                "diameter_mm": dd,
            }
        )
        codes.append(code(f"{dd:g}", x, t, cc))
    return Test(
        f"pins{suffix}",
        "pins",
        f"pins Ø{PINS[0]:g}–{PINS[-1]:g}, {PIN_H:g} mm tall",
        t,
        TOP,
        [f"union() {{ {' '.join(posts)} }} {mark(t, 'PIN')} {' '.join(codes)}"],
        items=items,
        top=U + PIN_H,
        params={
            "diameters_mm": ",".join(f"{dd:g}" for dd in PINS),
            "height_mm": PIN_H,
            "code_cap_mm": cc,
        },
        read="Free-standing pins (codes = Ø, 2 mm): from the top a whole pin shows a round top on "
        "the white underlay, a broken one a stub; the thinnest whole one is the smallest pin.",
    )


SQ_HOLES = (4.0, 6.0, 8.0, 10.0)


def t_sq_holes(t: Rect, suffix: str = "") -> Test:
    """Square through holes 4/6/8/10 mm: X and Y of each from a top photo (no facets, unlike
    round holes) — the XY shrink and skew of small inner features."""
    b = body(t)
    cc = 2.5
    pitch = (b[2] - b[0]) / len(SQ_HOLES)
    cuts, base, items, codes = [], [], [], []
    for k, s in enumerate(SQ_HOLES):
        x, y = b[0] + pitch * (k + 0.5), b[1] + 6.0
        cuts.append(
            f"translate([{x - s / 2},{y - s / 2},{U - 1}]) cube([{s},{s},{HQ_H + 2}]);"
        )
        base.append(
            f"translate([{x - s / 2},{y - s / 2},-1]) cube([{s},{s},{U + 1.01}]);"
        )
        items.append(
            {
                "label": f"{s:g}",
                "bbox": r3((x - s / 2, y - s / 2, U, x + s / 2, y + s / 2, U + HQ_H)),
                "side_mm": s,
            }
        )
        codes.append(code(f"{s:g}", x, t, cc))
    src = f"difference() {{ {tile(b, HQ_H)} {' '.join(cuts)} }} {mark(t, 'SQ')} {' '.join(codes)}"
    return Test(
        f"sq_holes{suffix}",
        "holes",
        "square holes 4/6/8/10: X/Y of inner features",
        t,
        TOP,
        [src],
        cuts=base,
        items=items,
        top=U + HQ_H,
        params={"sides_mm": ",".join(f"{s:g}" for s in SQ_HOLES), "code_cap_mm": cc},
        read="Square through holes (codes = side mm): X and Y of each from the top photo; both short "
        "= inner-feature shrink, X ≠ Y = skew or belt tension.",
    )


def t_engrave(t: Rect) -> Test:
    """Text engraved 0.4 mm into a black tile next to the same text raised 0.4 mm: which marking
    style the top camera reads better on black (both read by their shadows)."""
    b = body1(t)
    h = PLATE_T
    eng = (
        " ".join(  # cut from 0.38 below the tile top to 0.02 above it: no coplanar skin
            text(s, b[0] + 1.0, b[1] + y, U + h - EMBOSS + 0.02, cap=cap)
            for s, y, cap in (("3 Ab8", 6.0, 3.0), ("2.5 Ab8", 1.5, 2.5))
        )
    )
    raised = text("3 Ab8", b[0] + 15.0, b[1] + 6.0, U + h, cap=3.0)
    src = f"union() {{ difference() {{ {tile(b, h)} {eng} }} {raised} }} {mark(t, 'ENGR')}"
    return Test(
        "engrave",
        "label",
        "engraved 2.5/3 mm vs raised 3 mm text on a tile",
        t,
        TOP,
        [src],
        top=U + h + EMBOSS,
        params={"caps_mm": "2.5,3", "depth_mm": EMBOSS},
        read="Left: text engraved 0.4 mm into the tile (3 and 2.5 mm caps); right: the same 3 mm "
        "text raised 0.4 mm. The style the top camera reads better becomes the label style for "
        "black-on-black marks.",
    )


def flow_e(factor: float) -> Callable[[str], str]:
    """Extruding XY moves' E scaled (retractions untouched: no imbalance after the visit)."""

    def tf(line: str) -> str:
        if not line.startswith(("G1 ", "G2 ", "G3 ")):
            return line
        code_, sep, comment = line.partition(";")
        m = re.search(r" E(\d*\.?\d+)", code_)
        if m is None or not re.search(r" [XY]-?[\d.]", code_):
            return line
        return (
            code_[: m.start()]
            + f" E{float(m.group(1)) * factor:.5f}"
            + code_[m.end() :]
            + sep
            + comment
        )

    return tf


def t_flow(t: Rect, pct: int, ironed: bool) -> Test:
    """One half of a flow tile: a 3-layer top skin over 15 % infill at pct % flow, plain or ironed
    (an object setting): the flow and the top-surface / ironing test at once."""
    b = body1(t)
    tl = (b[0], b[1], b[2], b[1] + FLOW_TILE)
    cc = 2.0
    lbl = (
        text(str(pct), (t[0] + t[2]) / 2, t[1] + 0.9, U, cap=cc, halign="center")
        if not ironed
        else text("i", (t[0] + t[2]) / 2 + 2.0, t[1] + 0.9, U, cap=cc, halign="center")
    )
    name = f"flow_{pct}" + ("_i" if ironed else "")
    test = Test(
        name,
        "flow",
        f"flow {pct} %" + (" ironed" if ironed else ""),
        t,
        TOP,
        [f"{tile(tl, FLOW_H)} {lbl}"],
        top=U + FLOW_H,
        settings=dict(IRONING) if ironed else {},
        params={
            "flow_pct": pct,
            "ironing": ironed,
            "tile_mm": [FLOW_HALF, FLOW_TILE],
            "height_mm": FLOW_H,
            "code_cap_mm": cc,
        },
        read=f"Flow {pct} % (code between the halves), left half plain top skin, right half ironed "
        "(i): the smoothest closed plain top without ridges is the flow; the ironed half shows "
        "whether ironing hides the remaining lines at that flow.",
    )
    test.flow = pct / 100
    return test


def tower_bands(n: int, z_base: float, band: float) -> list[tuple[float, float]]:
    """[z0, z1) bed heights of n bands from z_base; the last runs past the top."""
    zs = [z_base + k * band for k in range(n + 1)]
    return [(zs[k], zs[k + 1] if k < n - 1 else zs[n] + 1.0) for k in range(n)]


def t_ringing(t: Rect) -> Test:
    b = body1(t)
    x0, y0 = b[0] + 1.0, b[1] + 1.0
    zb = tower_bands(len(RING_ACCELS), U + TOWER_BASE, TOWER_BAND)
    boxes: list[Box] = []
    for k in range(len(RING_ACCELS)):
        inset = k * STEP_IN
        boxes += square_tube_boxes(
            x0 + inset,
            y0 + inset,
            RING_SIDE - 2 * inset,
            RING_WALL,
            U if k == 0 else zb[k][0],
            zb[k][0] + TOWER_BAND,
        )
    test = Test(
        "ringing",
        "ringing",
        f"ringing: ACCEL {RING_ACCELS[0]}–{RING_ACCELS[-1]} by 2 mm bands",
        t,
        SIDE,
        [f"union() {{ {cubes(boxes)} }} {mark(t, 'RING')}"],
        top=zb[-1][0] + TOWER_BAND,
        params={
            "wall_speed_mm_s": RING_SPEED,
            "square_corner_velocity": TOWER_SCV,
            "band_mm": TOWER_BAND,
        },
        items=[
            {
                "label": str(a),
                "bbox": r3(
                    (x0, y0, z0, x0 + RING_SIDE, y0 + RING_SIDE, z0 + TOWER_BAND)
                ),
                "accel": a,
            }
            for a, (z0, _) in zip(RING_ACCELS, zb, strict=True)
        ],
        read="Ringing tower, ACCEL by height (bottom → top "
        + ", ".join(map(str, RING_ACCELS))
        + "), each band starts at a 0.2 mm inward step. Ripples after corners that fade = ringing; "
        f"crest spacing s gives f = {RING_SPEED:g}/s Hz. Highest clean band = wall ACCEL.",
    )
    test.bands = [
        Band(
            z0,
            z1,
            (velocity_line(a, TOWER_SCV),),
            motion(RING_SPEED, a, TOWER_SCV),
            {"accel": a},
        )
        for a, (z0, z1) in zip(RING_ACCELS, zb, strict=True)
    ]
    return test


def t_vfa(t: Rect) -> Test:
    b = body1(t)
    cx, cy = b[0] + 1.0 + VFA_R, b[1] + 1.0 + VFA_R
    zb = tower_bands(len(VFA_SPEEDS), U + TOWER_BASE, TOWER_BAND)
    rings = [
        (
            VFA_R - k * STEP_IN,
            VFA_R - k * STEP_IN - RING_WALL,
            U if k == 0 else zb[k][0],
            zb[k][0] + TOWER_BAND,
        )
        for k in range(len(VFA_SPEEDS))
    ]
    test = Test(
        "vfa",
        "vfa",
        f"VFA: wall speed {VFA_SPEEDS[0]}–{VFA_SPEEDS[-1]} mm/s by bands",
        t,
        SIDE,
        [mark(t, "VFA")],
        black_stl=[tubes_stl(cx, cy, rings, 360)],
        top=zb[-1][0] + TOWER_BAND,
        params={
            "accel": VFA_ACCEL,
            "square_corner_velocity": VFA_SCV,
            "band_mm": TOWER_BAND,
        },
        items=[
            {
                "label": str(v),
                "bbox": r3(
                    (
                        cx - VFA_R,
                        cy - VFA_R,
                        z0,
                        cx + VFA_R,
                        cy + VFA_R,
                        z0 + TOWER_BAND,
                    )
                ),
                "speed_mm_s": v,
            }
            for v, (z0, _) in zip(VFA_SPEEDS, zb, strict=True)
        ],
        read="VFA tube, outer wall speed by height (bottom → top "
        + ", ".join(map(str, VFA_SPEEDS))
        + f" mm/s) at ACCEL {VFA_ACCEL:g}: fine evenly spaced vertical lines that come and go with the "
        "band = VFA; avoid those speeds for outer walls.",
    )
    test.bands = [
        Band(
            z0,
            z1,
            (velocity_line(VFA_ACCEL, VFA_SCV),),
            motion(v, VFA_ACCEL, VFA_SCV),
            {"speed_mm_s": v},
        )
        for v, (z0, z1) in zip(VFA_SPEEDS, zb, strict=True)
    ]
    return test


def t_retraction(t: Rect) -> Test:
    b = body1(t)
    zb = tower_bands(len(RETRACTS), U + 0.6, TOWER_BAND)
    top = zb[-1][0] + TOWER_BAND
    cx = (t[0] + t[2]) / 2
    cy = b[1] + 1.0 + STRING_D / 2
    xs = (cx - STRING_GAP / 2 - STRING_D / 2, cx + STRING_GAP / 2 + STRING_D / 2)
    pillars = " ".join(cyl(x, cy, STRING_D, U, top, 48) for x in xs)
    grooves = " ".join(
        f"translate([{t[0]},{cy - 4},{z0 - 0.1}]) cube([{t[2] - t[0]},8,0.2]);"
        for z0, _ in zb[1:]
    )
    cores = " ".join(cyl(x, cy, STRING_D - 0.6, 0, 50) for x in xs)
    base = (b[0], b[1], b[2], b[1] + 2.0 + STRING_D)
    src = (
        f"union() {{ {tile(base, 0.6)} difference() {{ union() {{ {pillars} }} "
        f"difference() {{ union() {{ {grooves} }} {cores} }} }} }} {mark(t, 'RETR')}"
    )
    test = Test(
        "retraction",
        "retraction",
        "retraction 0.4–2.0 mm by bands (stringing)",
        t,
        TOP,
        [src],
        top=top,
        params={
            "retractions_mm": ",".join(f"{r:g}" for r in RETRACTS),
            "band_mm": TOWER_BAND,
        },
        items=[
            {
                "label": f"{r:g}",
                "bbox": r3(
                    (
                        t[0],
                        cy - STRING_D / 2,
                        z0,
                        t[2],
                        cy + STRING_D / 2,
                        z0 + TOWER_BAND,
                    )
                ),
                "retraction_mm": r,
            }
            for r, (z0, _) in zip(RETRACTS, zb, strict=True)
        ],
        read="Two Ø4 pillars 14 mm apart; retraction by height (bottom → top "
        + ", ".join(f"{r:g}" for r in RETRACTS)
        + " mm), bands marked by a shallow groove. Black strings "
        "between the pillars show on the white underlay from the top; the lowest band without "
        "them (side view) is the retraction length.",
    )
    test.retract_bands = [(z0, z1, r) for r, (z0, z1) in zip(RETRACTS, zb, strict=True)]
    return test


def t_temp(t: Rect, z_start: float) -> Test:
    """Temperature blocks from z_start up, above every other object: only the tower prints while
    the temperature is not the preset's, so no other test sees it (#269: temperature is global)."""
    w, d = TEMP_BLOCK
    b = body(t)
    x0, y0 = b[0] + 1.0, b[1] + 0.5
    zb = tower_bands(len(TEMPS), z_start, TEMP_BAND)
    ped = (
        f"difference() {{ translate([{x0},{y0},{U}]) cube([{w},{d},{z_start - U}]); "
        f"translate([{x0 + WALL},{y0 + WALL},{U - 1}]) cube([{w - 2 * WALL},{d - 2 * WALL},{z_start - U + 2}]); }}"
    )
    blocks = []
    for z0, _ in zb:
        # a full-height block (no layer may be empty: Orca would skip it and float the next
        # band) with a 45° lip on the right whose underside starts 0.2 mm above the band edge
        blocks.append(f"translate([{x0},{y0},{z0}]) cube([{w - 4},{d},{TEMP_BAND}]);")
        blocks.append(
            f"translate([{x0 + w - 4},{y0},{z0 + 0.2}]) rotate([-90,0,0]) linear_extrude({d}) "
            f"polygon([[0,0],[0,{-(TEMP_BAND - 0.2)}],[{TEMP_BAND - 0.2},{-(TEMP_BAND - 0.2)}]]);"
        )
    codes = f"{mark(t, 'TEMP')} {code(str(TEMPS[0]) + '→' + str(TEMPS[-1]), (t[0] + t[2]) / 2, t, 2.5)}"
    test = Test(
        "temp_tower",
        "temp",
        f"temperature {TEMPS[0]}→{TEMPS[-1]} °C, 2.4 mm bands from {z_start:.1f} mm",
        t,
        SIDE,
        [f"union() {{ {ped} {' '.join(blocks)} }} {codes}"],
        top=zb[-1][0] + TEMP_BAND,
        params={
            "temps_c": ",".join(map(str, TEMPS)),
            "band_mm": TEMP_BAND,
            "first_band_z_mm": round(z_start, 3),
            "code_cap_mm": 2.5,
        },
        items=[
            {
                "label": str(c),
                "bbox": r3((x0, y0, z0, x0 + w, y0 + d, z0 + TEMP_BAND)),
                "temp_c": c,
            }
            for c, (z0, _) in zip(TEMPS, zb, strict=True)
        ],
        read=f"Temperature tower: hollow pedestal at the preset 220 °C up to {z_start:.2f} mm (above "
        "every other test), then blocks at "
        + ", ".join(map(str, TEMPS))
        + " °C bottom → top (M109 "
        "waits at each step). Per block: 45° lip on the right, gloss, strings, layer bond. The best "
        "block's temperature is the print temperature.",
    )
    test.bands = [
        Band(z0, z1, (f"M104 S{c} T0", f"M109 S{c} T0"), None, {"temp_c": c})
        for c, (z0, z1) in zip(TEMPS, zb, strict=True)
    ]
    return test


def t_fiducial(t: Rect, num: str, corner: str) -> Test:
    left, front = corner[1] == "l", corner[0] == "f"
    s = 0.9
    ox = t[0] + 1.0 if left else t[2] - 1.0 - 15.0
    oy = t[1] + 1.0 if front else t[3] - 1.0 - s
    bx = [(ox, oy, U, ox + 15.0 + s, oy + s, U + EMBOSS)]
    for k in range(4):
        x = ox + k * 5.0
        y_a, y_b = (oy + s, oy + s + 2.5) if front else (oy - 2.5, oy)
        bx.append((x, y_a, U, x + s, y_b, U + EMBOSS))
    vx = t[0] + 1.0 if left else t[2] - 1.0 - s
    vy0 = t[1] + 1.0 if front else t[3] - 1.0 - 15.0
    bx.append((vx, vy0, U, vx + s, vy0 + 15.0 + s, U + EMBOSS))
    for k in range(4):
        y = vy0 + k * 5.0
        x_a, x_b = (vx + s, vx + s + 2.5) if left else (vx - 2.5, vx)
        bx.append((x_a, y, U, x_b, y + s, U + EMBOSS))
    rc = ((t[0] + t[2]) / 2, (t[1] + t[3]) / 2)
    # the code in the corner opposite the rulers, clear of the ring
    lx = t[2] - 0.8 if left else t[0] + 0.8
    ly = t[3] - 3.6 if front else t[1] + 0.8
    lbl = text(num, lx, ly, U, halign="right" if left else "left")
    return Test(
        f"fiducial_{corner}",
        "fiducial",
        f"fiducial {num}: Ø10 ring, 5 mm rulers",
        t,
        TOP,
        [f"union() {{ {ring(rc[0], rc[1], U, *RING)} {cubes(bx)} }} {lbl}"],
        top=U + EMBOSS,
        params={"corner": corner, "ring_d_mm": RING[0]},
        items=[{"label": "ring", "bbox": circ(rc[0], rc[1], RING[0], U, U + EMBOSS)}],
        read=f"Fiducial {num}: Ø{RING[0]:g} ring and 15 mm rulers (ticks 5 mm) along both edges: scale "
        "and skew of a top photo.",
    )


def t_mvs(t: Rect) -> Test:
    b = body(t)
    x0, y0 = b[0] + 1.0, b[1] + 1.0
    cc = 2.5
    codes = " ".join(
        code(str(v), x0 + MVS_SEG * (k + 0.5), t, cc) for k, v in enumerate(MVS_VALUES)
    )
    return Test(
        "mvs",
        "mvs",
        f"MVS strip {MVS_VALUES[0]}–{MVS_VALUES[-1]} mm³/s (hand-written)",
        t,
        TOP,
        [f"union() {{ {mark(t, 'MVS')} {codes} }}"],
        top=U + EMBOSS,
        params={
            "values_mm3_s": ",".join(map(str, MVS_VALUES)),
            "line_w_mm": MVS_W,
            "layers": MVS_LAYERS,
            "origin": [x0, y0],
            "segment_mm": MVS_SEG,
            "code_cap_mm": cc,
        },
        items=[
            {
                "label": str(v),
                "bbox": r3(
                    (
                        x0 + MVS_SEG * k,
                        y0,
                        U,
                        x0 + MVS_SEG * (k + 1),
                        y0 + (MVS_LINES - 1) * MVS_SPACING + MVS_W,
                        U + MVS_LAYERS * LAYER_H,
                    )
                ),
                "mm3_s": v,
            }
            for k, v in enumerate(MVS_VALUES)
        ],
        read=f"MVS strip, hand-written in layers 3–{2 + MVS_LAYERS}: a {MVS_BASE_SPEED:g} mm/s base, then "
        "segments whose speed gives the coded mm³/s. The first matte/thin/gappy segment is above "
        "the flow limit.",
    )


# ---- label strip and the flow block's band ---------------------------------------------------


def qr_boxes(token: str, x0: float, y0: float) -> tuple[list[Box], int] | None:
    try:
        import segno  # noqa: PLC0415 — optional: the label prints without its QR when missing
    except ImportError:
        return None
    rows = list(segno.make(token, error="m", micro=False).matrix)
    n = len(rows)
    out: list[Box] = []
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if v:
                x, y = x0 + c * QR_MODULE, y0 + (n - 1 - r) * QR_MODULE
                out.append((x, y, U, x + QR_MODULE, y + QR_MODULE, U + EMBOSS))
    return out, n


def grouped(token: str) -> str:
    """C20261016P1 -> 'C 2026 1016 P1': the code read aloud in groups."""
    m = re.fullmatch(r"C(\d{4})(\d{4})P(\d+)", token)
    return f"C {m.group(1)} {m.group(2)} P{m.group(3)}" if m else token


def t_label(
    t: Rect, token: str, date: str, heads: tuple[str, str], process: str
) -> tuple[Test, dict[str, Any]]:
    qr = qr_boxes(token, t[0] + 1.0, t[1] + (t[3] - t[1] - 21 * QR_MODULE) / 2)
    tx = t[0] + 1.0 + (qr[1] * QR_MODULE + 4.0 if qr else 0.0)
    day = _dt.date.fromisoformat(date).strftime("%d.%m.%Y")
    lines = [
        (grouped(token), 5.0),
        (f"{day}   U1", 4.0),
        (heads[0], 3.5),
        (heads[1], 3.5),
        (process, 3.5),
    ]
    y = t[3] - 1.0
    srcs = []
    for txt, cap in lines:
        y -= cap
        srcs.append(text(txt, tx, y, U, cap=cap))
        y -= 1.6
    if y < t[1]:
        raise GenError(f"label strip too low for its lines ({y:.1f} < {t[1]:.1f})")
    src = f"union() {{ {cubes(qr[0]) if qr else ''} {' '.join(srcs)} }}"
    info: dict[str, Any] = {
        "token": token,
        "date": day,
        "printer": "u1",
        "lines": [ln for ln, _ in lines],
        "qr": {"modules": qr[1], "module_mm": QR_MODULE, "decodes_to": token}
        if qr
        else None,
        "qr_note": None if qr else "segno not installed: no QR on this plate",
        "bbox": r3(t),
    }
    return Test(
        "plate_label",
        "label",
        "plate label + QR",
        t,
        TOP,
        [src],
        top=U + EMBOSS,
        read="Plate label (#322): session code in groups, date, printer, one line per head, the "
        "process; QR of the code at the left. Bold font, strokes ≥ 2 perimeters, black on the "
        "white underlay.",
    ), info


def flow_block_extras(rect: Rect) -> tuple[list[str], list[dict[str, Any]]]:
    """The flow block's front band text and its divider walls (the thin-wall test, tg#22397): along
    Y in the gaps between the columns (thickness by row), along X in the gaps between the rows
    (thickness by column). They stand on the sheet, parts of the underlay object."""
    x0, y0 = rect[0], rect[1] + BAND
    srcs = [
        text(
            "FLOW % · i=iron · W25/35/45",
            rect[0] + 0.6,
            rect[1] + 0.9,
            U,
            cap=2.5,
        )
    ]
    walls: list[dict[str, Any]] = []
    pw = 2 * FLOW_HALF
    for r in range(3):
        ry = y0 + r * (FLOW_D + FLOW_GAP)
        for c in range(1, 3):
            gx = x0 + c * pw + (c - 1) * FLOW_GAP + FLOW_GAP / 2
            w = THIN[r]
            bx = (
                gx - w / 2,
                ry + BAND + 1.0,
                U,
                gx + w / 2,
                ry + BAND + 1.0 + THIN_LEN,
                U + THIN_H,
            )
            walls.append(
                {"label": f"Y{hund(w)}", "bbox": r3(bx), "wall_mm": w, "along": "Y"}
            )
    for r in range(1, 3):
        gy = y0 + r * FLOW_D + (r - 1) * FLOW_GAP + FLOW_GAP / 2
        for c in range(3):
            cx = x0 + c * (pw + FLOW_GAP) + pw / 2
            w = THIN[c]
            bx = (
                cx - THIN_LEN / 2,
                gy - w / 2,
                U,
                cx + THIN_LEN / 2,
                gy + w / 2,
                U + THIN_H,
            )
            walls.append(
                {"label": f"X{hund(w)}", "bbox": r3(bx), "wall_mm": w, "along": "X"}
            )
    srcs.append(cubes([tuple(wl["bbox"]) for wl in walls]))
    return srcs, walls


# ---- layout ------------------------------------------------------------------------------------

Spec = tuple[str, float, float]  # name, width, depth (front band included)
Entry = Spec | list[Spec]  # one item, or a column of items stacked front → back
H = 28.5
ROWS: list[
    list[Entry]
] = [  # front → back, left → right; PACK_GAP between items and rows
    [
        ("fiducial_fl", 20, 20),
        ("plate_label", 150, 33),
        ("holes_pl", H, H + BAND2),
        ("temp_tower", 20, 19),
        ("fiducial_fr", 20, 20),
    ],
    [
        ("holes_ph", H, H + BAND2),
        ("holes_hc", H, H + BAND2),
        ("screw_m3", H, 26),
        ("screw_m4", H, 26),
        ("slots", H, 34),
        ("elephant_foot", H, 24),
        ("vfa", 28, 32.6),
        ("ringing", 24, 28.6),
    ],
    [
        ("frame_50", 51, 56),
        ("flow_block", *FLOW_BLOCK),
        (
            "fit_hinge",
            6.0 + FIT_PITCH * len(FIT_GAPS) + 0.5,
            BAND2 + 2 * FIT_ROW + 2.0 + 3 * HINGE_PY + 2 * CUT_MARGIN + 4.0,
        ),
        [("mvs", 52, 17), ("pins", 36, 18), ("retraction", 26, 12.6)],
    ],
    [
        ("bridges", 0.5 + sum(2 * PILLAR + s + 1.5 for s in BRIDGES), 18),
        ("overhangs", 1.0 + sum(overhang_step(a) for a in OVERHANGS), 18),
    ],
    [
        ("fiducial_bl", 20, 20),
        ("holes_pl_back", H, H + BAND2),
        ("elephant_foot_back", H, 24),
        ("fiducial_br", 20, 20),
    ],
]


def pack() -> tuple[dict[str, Rect], Rect]:
    """(test rectangles, underlay). Rows left → right from the front-left corner of AREA, the
    last item of the first and last rows pinned to the right edge (fiducials in the corners); a
    column entry stacks its items. The underlay is the rows' extent plus 3 mm (#320)."""
    rects: dict[str, Rect] = {}
    y = AREA[1]
    for i, row in enumerate(ROWS):
        x = AREA[0]
        depth = 0.0
        for k, entry in enumerate(row):
            col = entry if isinstance(entry, list) else [entry]
            w = max(e[1] for e in col)
            if k == len(row) - 1 and i in (0, len(ROWS) - 1):
                x = AREA[2] - w
            yy = y
            for name, ew, ed in col:
                if name == "flow_block":
                    _flow_grid(rects, x, yy)
                rects[name] = (x, yy, x + ew, yy + ed)
                yy += ed + PACK_GAP
            depth = max(depth, yy - PACK_GAP - y)
            x += w + PACK_GAP
        if x - PACK_GAP > AREA[2] + 1e-6:
            raise GenError(
                f"row {i} is {x - PACK_GAP - AREA[0]:.1f} mm wide (> {AREA[2] - AREA[0]:g})"
            )
        y += depth + PACK_GAP
    top = y - PACK_GAP
    if top > AREA[3] + 1e-6:
        raise GenError(f"rows reach y {top:.1f} (> {AREA[3]:g})")
    extent = (AREA[0], AREA[1], AREA[2], top)
    for name, w, d in FILLERS:
        spot = _free_spot(rects, w, d, extent)
        if spot is not None:
            rects[name] = spot
    return rects, (AREA[0] - 3.0, AREA[1] - 3.0, AREA[2] + 3.0, top + 3.0)


# useful top-camera tests that go into the gaps the rows leave (tg#22397: no empty places)
FILLERS: list[Spec] = [
    ("sq_holes", H, 20),
    ("engrave", H, 18),
    ("holes_ph_back", H, H + BAND2),
    ("holes_hc_back", H, H + BAND2),
    ("sq_holes_back", H, 20),
    ("pins_back", 36, 18),
]
FILL_BUILD: dict[str, Callable[[Rect], Test]] = {
    "sq_holes": t_sq_holes,
    "engrave": t_engrave,
    "holes_ph_back": lambda r: t_holes(r, "PH", "_back"),
    "holes_hc_back": lambda r: t_holes(r, "HC", "_back"),
    "sq_holes_back": lambda r: t_sq_holes(r, "_back"),
    "pins_back": lambda r: t_pins(r, "_back"),
}


def _free_spot(rects: dict[str, Rect], w: float, d: float, extent: Rect) -> Rect | None:
    """The first w × d spot inside extent that keeps PACK_GAP to every placed rectangle,
    scanning front → back, left → right in 0.5 mm steps."""
    y = extent[1]
    while y + d <= extent[3] + 1e-9:
        x = extent[0]
        while x + w <= extent[2] + 1e-9:
            r = (x, y, x + w, y + d)
            if all(
                r[2] + PACK_GAP <= o[0]
                or o[2] + PACK_GAP <= r[0]
                or r[3] + PACK_GAP <= o[1]
                or o[3] + PACK_GAP <= r[1]
                for o in rects.values()
            ):
                return r
            x += 0.5
        y += 0.5
    return None


def _flow_grid(rects: dict[str, Rect], x0: float, y0: float) -> None:
    """Flow tiles 3 × 3 behind the block's band, values rising left → right, front → back; each
    tile is a plain and an ironed half; FLOW_GAP between tiles holds the divider walls."""
    for k, p in enumerate(FLOWS):
        x = x0 + (k % 3) * (2 * FLOW_HALF + FLOW_GAP)
        y = y0 + BAND + (k // 3) * (FLOW_D + FLOW_GAP)
        rects[f"flow_{p}"] = (x, y, x + FLOW_HALF, y + FLOW_D)
        rects[f"flow_{p}_i"] = (x + FLOW_HALF, y, x + 2 * FLOW_HALF, y + FLOW_D)


COLOUR_NAMES = {"#000000": "чёрный", "#FFFFFF": "белый"}


def head_line(tool: int) -> str:
    """One label line for a head: `T4  белый  #1  Snapmaker PLA SnapSpeed`."""
    colour = SLOT_COLOURS[tool - 1].upper()
    spool = HEAD_SPOOLS.get(tool)
    return (
        f"T{tool}  {COLOUR_NAMES.get(colour, colour)}"
        + (f"  #{spool}" if spool else "")
        + "  Snapmaker PLA SnapSpeed"
    )


def build_tests(token: str, date: str) -> tuple[list[Test], dict[str, Any]]:
    rects, underlay = pack()
    tests = [
        t_fiducial(rects["fiducial_fl"], "F1", "fl"),
        t_fiducial(rects["fiducial_fr"], "F2", "fr"),
        t_fiducial(rects["fiducial_bl"], "F3", "bl"),
        t_fiducial(rects["fiducial_br"], "F4", "br"),
        t_holes(rects["holes_pl"], "PL"),
        t_holes(rects["holes_ph"], "PH"),
        t_holes(rects["holes_hc"], "HC"),
        t_screws(rects["screw_m3"], SCREW_M3, 3),
        t_screws(rects["screw_m4"], SCREW_M4, 4),
        t_slots(rects["slots"]),
        t_elephant(rects["elephant_foot"]),
        t_elephant(rects["elephant_foot_back"], "_back"),
        t_holes(rects["holes_pl_back"], "PL", "_back"),
        *[FILL_BUILD[name](rects[name]) for name, _, _ in FILLERS if name in rects],
        t_vfa(rects["vfa"]),
        t_ringing(rects["ringing"]),
        t_frame(rects["frame_50"]),
        t_fit_hinge(rects["fit_hinge"]),
        t_mvs(rects["mvs"]),
        t_pins(rects["pins"]),
        t_retraction(rects["retraction"]),
        t_bridges(rects["bridges"]),
        t_overhangs(rects["overhangs"]),
    ]
    for p in FLOWS:
        tests += [
            t_flow(rects[f"flow_{p}"], p, False),
            t_flow(rects[f"flow_{p}_i"], p, True),
        ]
    z_start = max(t.top for t in tests) + 0.6
    z_start = (
        U + math.ceil((z_start - U) / LAYER_H - 1e-9) * LAYER_H
    )  # a layer boundary
    tests.append(t_temp(rects["temp_tower"], z_start))
    label, info = t_label(
        rects["plate_label"],
        token,
        date,
        (
            head_line(BLACK),
            head_line(WHITE),
        ),
        "процесс: 0.20mm Standard @U1",
    )
    extras, walls = flow_block_extras(rects["flow_block"])
    label.black += extras
    return tests, {
        "label_test": label,
        "label": info,
        "rects": rects,
        "underlay": underlay,
        "walls": walls,
        "flow_block": rects["flow_block"],
    }


def read_by_share(tests: list[Test], label: Test) -> dict[str, Any]:
    """tg#22399: the share of the plate's tests the top camera reads alone. A combined sample
    counts each test it serves (`subs`); the divider walls and the label count once each."""
    entries = [
        (s[0], s[2])
        for t in tests
        for s in (t.subs or [(t.name, t.caption, t.read_by)])
    ]
    entries += [
        ("thin_walls", TOP),
        ("plate_label", label.read_by),
        ("codes_small_text", TOP),
    ]
    by: dict[str, int] = {}
    for _, rb in entries:
        by[rb] = by.get(rb, 0) + 1
    return {
        "tests": len(entries),
        "by": by,
        "camera_top_share": round(by.get(TOP, 0) / len(entries), 3),
        "entries": [{"test": n, "read_by": rb} for n, rb in entries],
    }


# ---- meshes, preview ---------------------------------------------------------------------------


@dataclass
class Mesh:
    obj: str
    name: str
    extruder: int
    vertices: Any
    faces: Any


def _mesh(obj: str, name: str, extruder: int, stl: str) -> Mesh:
    import trimesh  # noqa: PLC0415 — heavy, only when generating

    m = cast(
        trimesh.Trimesh,
        trimesh.load(
            io.BytesIO(stl.encode()), file_type="stl", process=True, force="mesh"
        ),
    )
    m.merge_vertices()
    if len(m.faces) == 0:
        raise GenError(f"{obj}/{name}: empty mesh")
    return Mesh(obj, name, extruder, m.vertices.copy(), m.faces.copy())


def base_scad(t: Test) -> str:
    x0, y0, x1, y1 = t.rect
    s = SEAM
    return (
        f"union() {{ difference() {{ translate([{x0 - s},{y0 - s},0]) cube([{x1 - x0 + 2 * s},{y1 - y0 + 2 * s},{U}]); "
        f"{' '.join(t.cuts)} }} {' '.join(t.feet)} }}"
    )


def sheet_scad(tests: list[Test], underlay: Rect) -> str:
    x0, y0, x1, y1 = underlay
    holes = " ".join(
        f"translate([{t.rect[0]},{t.rect[1]},-1]) cube([{t.rect[2] - t.rect[0]},{t.rect[3] - t.rect[1]},{U + 2}]);"
        for t in tests
    )
    return f"difference() {{ translate([{x0},{y0},0]) cube([{x1 - x0},{y1 - y0},{U}]); {holes} }}"


def render_meshes(tests: list[Test], label: Test, underlay: Rect) -> list[Mesh]:
    jobs: list[tuple[str, str, int, str, bool]] = [
        ("underlay", "sheet", WHITE, sheet_scad(tests, underlay), True)
    ]
    jobs += [
        ("underlay", f"label_{k}", BLACK, s, True) for k, s in enumerate(label.black)
    ]
    for t in tests:
        jobs.append((t.name, "base", WHITE, base_scad(t), True))
        jobs += [(t.name, f"black_{k}", BLACK, s, True) for k, s in enumerate(t.black)]
        jobs += [
            (t.name, f"prism_{k}", BLACK, s, False) for k, s in enumerate(t.black_stl)
        ]

    def one(job: tuple[str, str, int, str, bool]) -> Mesh:
        obj, name, ex, src, scad = job
        return _mesh(obj, name, ex, openscad_stl(src) if scad else src)

    with ThreadPoolExecutor(max_workers=min(6, max_workers())) as pool:
        return list(pool.map(one, jobs))


def _draw_meshes(
    meshes: list[Mesh], scale: float, origin: tuple[float, float], size: tuple[int, int]
) -> Any:
    from PIL import Image, ImageDraw  # noqa: PLC0415

    img = Image.new("RGB", size, (150, 150, 155))
    d = ImageDraw.Draw(img)

    def pt(x: float, y: float) -> tuple[float, float]:
        return (x - origin[0]) * scale, (origin[1] - y) * scale

    sheet = next(m for m in meshes if m.obj == "underlay" and m.name == "sheet")
    lo, hi = sheet.vertices.min(0), sheet.vertices.max(0)
    d.rectangle(
        [
            pt(lo[0] - UNDERLAY_BRIM, hi[1] + UNDERLAY_BRIM),
            pt(hi[0] + UNDERLAY_BRIM, lo[1] - UNDERLAY_BRIM),
        ],
        fill=(205, 205, 205),
    )
    for m in sorted(meshes, key=lambda m: m.extruder != WHITE):
        tri = m.vertices[m.faces]
        up = (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - (
            tri[:, 1, 1] - tri[:, 0, 1]
        ) * (tri[:, 2, 0] - tri[:, 0, 0]) > 1e-9
        zs = tri[:, :, 2].mean(axis=1)
        for k in sorted(range(len(m.faces)), key=lambda i: zs[i]):
            if not up[k]:
                continue
            if m.extruder == WHITE:
                col = (250, 250, 248) if zs[k] > U - 0.05 else (232, 232, 232)
            else:
                g = int(min(95, 18 + 6 * (zs[k] - U)))
                col = (g, g, g + 8)
            d.polygon([pt(p[0], p[1]) for p in tri[k]], fill=col)
    return img, d, pt


def _font(px: int) -> Any:
    from PIL import ImageFont  # noqa: PLC0415

    for f in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if os.path.exists(f):
            return ImageFont.truetype(f, px)
    return ImageFont.load_default()


RB_COLOUR = {TOP: (255, 210, 0), SIDE: (120, 200, 255), MANUAL: (255, 140, 140)}


def preview_png(
    meshes: list[Mesh],
    tests: list[Test],
    label: Test,
    path: Path,
    title: str,
    hand: list[Rect],
    share: dict[str, Any],
) -> None:
    """Plate top view at 5 px/mm, a caption on every test coloured by who reads it, a legend;
    below it the label strip and three tests at 10 px/mm (print-scale crops)."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    sc = 5.0
    plate_px = int(BED * sc)
    img, d, pt = _draw_meshes(meshes, sc, (0.0, BED), (plate_px, plate_px))
    for r in hand:
        d.rectangle([pt(r[0], r[3]), pt(r[2], r[1])], fill=(30, 30, 40))
    small = _font(13)
    order = sorted(tests, key=lambda t: (t.rect[1], t.rect[0]))
    for k, t in enumerate(order, start=1):
        x, y = pt(t.rect[0], t.rect[3])
        cap = f"{k}" if t.kind == "flow" else f"{k} {t.name}"
        tw = small.getbbox(cap)[2] + 4
        d.rectangle([x, y, x + tw, y + 15], fill=RB_COLOUR[t.read_by])
        d.text((x + 2, y), cap, fill=(0, 0, 0), font=small)
    legend_w, crops_h = 660, 1250
    out = Image.new("RGB", (plate_px + legend_w, plate_px + crops_h), (255, 255, 255))
    out.paste(img, (0, 0))
    od = ImageDraw.Draw(out)
    f16, f14 = _font(17), _font(13)
    od.text((plate_px + 12, 8), title, fill=(0, 0, 0), font=f16)
    by = share["by"]
    od.text(
        (plate_px + 12, 32),
        f"read by: top camera {by.get(TOP, 0)}, side camera {by.get(SIDE, 0)}, "
        f"by hand {by.get(MANUAL, 0)} of {share['tests']} tests = {share['camera_top_share']:.0%} top",
        fill=(0, 0, 0),
        font=f16,
    )
    yy = 62
    for k, t in enumerate(order, start=1):
        od.rectangle(
            [plate_px + 8, yy + 2, plate_px + 16, yy + 12], fill=RB_COLOUR[t.read_by]
        )
        od.text(
            (plate_px + 20, yy),
            f"{k:>2} {t.name}: {t.caption}",
            fill=(0, 0, 0),
            font=f14,
        )
        yy += 17
    yy += 8
    for line in (
        "Caption colour: yellow = top camera, blue = side camera, red = by hand.",
        f"Grey: bed. Underlay: T{WHITE}, {UNDERLAY_LAYERS} layer(s), no brim (fan 0, bed 70 °C).",
        f"Black: T1 tests from layer {UNDERLAY_LAYERS + 1} (one tool change T{WHITE}→T1).",
        "Darkest bars: hand-written lines (PA inside the frame, MVS).",
        "Flow block: left half plain, right half ironed; walls between = thin walls.",
        "Bare underlay between tests = first-layer map (no patches).",
        "Below: print-scale crops at 10 px/mm.",
    ):
        od.text((plate_px + 12, yy), line, fill=(60, 60, 60), font=f14)
        yy += 17
    zs = 10.0
    lr = label.rect
    lh = int((lr[3] - lr[1] + 4) * zs)
    zimg, _, _ = _draw_meshes(
        meshes, zs, (lr[0] - 2, lr[3] + 2), (int((lr[2] - lr[0] + 4) * zs), lh)
    )
    out.paste(zimg, (0, plate_px + 10))
    by_name = {t.name: t for t in tests}
    x_at = 0
    for nm in ("fit_hinge", "frame_50", "holes_pl"):
        r = by_name[nm].rect
        cw, ch = int((r[2] - r[0] + 4) * zs), int((r[3] - r[1] + 4) * zs)
        if x_at + cw > out.width or lh + ch + 30 > crops_h:
            continue
        cimg, cd, cpt = _draw_meshes(meshes, zs, (r[0] - 2, r[3] + 2), (cw, ch))
        for q in hand:
            cd.rectangle([cpt(q[0], q[3]), cpt(q[2], q[1])], fill=(30, 30, 40))
        out.paste(cimg, (x_at, plate_px + 20 + lh))
        x_at += cw + 10
    out.save(path)


# ---- Orca project --------------------------------------------------------------------------------


def _profile_index(kind: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in (Path(PROFILES) / "Snapmaker" / kind).glob("*.json"):
        dd = json.loads(f.read_text())
        out[dd.get("name", f.stem)] = dd
    return out


def flat_profile(
    kind: str, name: str, idx: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A system preset with its whole `inherits` chain merged (the CLI resolves only one level)."""
    idx = idx or _profile_index(kind)
    dd = dict(idx[name])
    parent = dd.pop("inherits", None)
    if parent:
        base = flat_profile(kind, parent, idx)
        base.update(dd)
        dd = base
    dd.update(name=name, instantiation="true")
    dd["from"] = "system"
    return dd


def slicer_version() -> str:
    """The CLI refuses projects stamped newer than its own build number (babushkin-dom)."""
    r = subprocess.run([SLICER, "--help"], capture_output=True, text=True, timeout=120)
    m = re.search(r"Snapmaker_Orca-([0-9.]+)", r.stdout + r.stderr)
    return m.group(1) if m else "01.10.01.50"


def project_settings(tmp: Path) -> dict[str, Any]:
    """project_settings.config as Snapmaker Orca writes it for these presets (a probe export),
    then this plate's overrides."""
    paths = []
    for kind, name in (("machine", MACHINE), ("process", PROCESS)):
        p = tmp / f"{kind}.json"
        p.write_text(json.dumps(flat_profile(kind, name)))
        paths.append(str(p))
    fil = tmp / "filament.json"
    fil.write_text(json.dumps(flat_profile("filament", FILAMENT)))
    probe = tmp / "probe.stl"
    probe.write_text(openscad_stl("translate([130,130,0]) cube([10,10,2]);"))
    cmd = [
        SLICER,
        "--debug",
        "1",
        "--datadir",
        str(tmp / "dd"),
        "--load-settings",
        ";".join(paths),
        "--load-filaments",
        ";".join([str(fil)] * 4),
        "--outputdir",
        str(tmp),
        "--export-3mf",
        "probe.3mf",
        str(probe),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    out = tmp / "probe.3mf"
    if not out.is_file():
        raise GenError(
            f"Snapmaker Orca did not write the probe project:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
        )
    cfg: dict[str, Any] = json.loads(
        zipfile.ZipFile(out).read("Metadata/project_settings.config")
    )
    n = len(cfg["filament_colour"])
    ds = list(cfg.get("different_settings_to_system", [""] * (n + 2)))
    ig = list(cfg.get("inherits_group", [""] * (n + 2)))
    cfg["filament_colour"] = list(SLOT_COLOURS)
    cfg["extruder_colour"] = list(SLOT_COLOURS)
    cfg.update(PROCESS_OVERRIDES)
    for k, v in FILAMENT_OVERRIDES.items():
        cfg[k] = [v] * n
    ds[0] = ";".join(
        sorted(set(filter(None, ds[0].split(";"))) | set(PROCESS_OVERRIDES))
    )
    for i in range(n):
        ig[1 + i] = FILAMENT
        ds[1 + i] = ";".join(
            sorted(
                set(filter(None, ds[1 + i].split(";")))
                | set(FILAMENT_OVERRIDES)
                | {"filament_colour"}
            )
        )
    cfg["inherits_group"], cfg["different_settings_to_system"] = ig, ds
    return cfg


def _uid(n: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"3d-cli/{PLATE_ID}/{n}"))


NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"'
)


def write_project(
    meshes: list[Mesh],
    cfg: dict[str, Any],
    path: Path,
    version: str,
    settings_by_obj: dict[str, dict[str, str]],
) -> None:
    """One Orca object per test (+ the sheet), its meshes as parts (white = extruder 4, black = 1),
    each object's own settings (ironing) as object metadata."""
    import numpy as np  # noqa: PLC0415

    objs: dict[str, list[Mesh]] = {}
    for m in meshes:
        objs.setdefault(m.obj, []).append(m)
    res, build, settings, subs, insts = [], [], [], {}, []
    pid = 0
    for oi, (oname, parts) in enumerate(objs.items()):
        allv = np.vstack([m.vertices for m in parts])
        lo, hi = allv.min(0), allv.max(0)
        if abs(lo[2]) > 1e-6:
            raise GenError(f"{oname}: lowest point at z {lo[2]:.3f}, not on the bed")
        oc = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
        comps, pxml, meshes_xml = [], [], []
        for k, m in enumerate(parts):
            pid += 1
            pc = (m.vertices.min(0) + m.vertices.max(0)) / 2
            v = m.vertices - pc
            off = pc - oc
            vs = "\n".join(
                f'     <vertex x="{a:.6g}" y="{b:.6g}" z="{c:.6g}"/>' for a, b, c in v
            )
            ts = "\n".join(
                f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in m.faces
            )
            meshes_xml.append(
                f'  <object id="{pid}" p:UUID="{_uid(pid)}" type="model">\n   <mesh>\n    <vertices>\n{vs}\n'
                f"    </vertices>\n    <triangles>\n{ts}\n    </triangles>\n   </mesh>\n  </object>"
            )
            comps.append(
                f'    <component p:path="/3D/Objects/object_{oi + 1}.model" objectid="{pid}" '
                f'p:UUID="{_uid(100000 + pid)}" transform="1 0 0 0 1 0 0 0 1 {off[0]:.6f} {off[1]:.6f} {off[2]:.6f}"/>'
            )
            pxml.append(
                f'    <part id="{pid}" subtype="normal_part">\n      <metadata key="name" value="{oname}_{m.name}"/>\n'
                f'      <metadata key="matrix" value="1 0 0 {off[0]:.6f} 0 1 0 {off[1]:.6f} 0 0 1 {off[2]:.6f} 0 0 0 1"/>\n'
                f'      <metadata key="source_object_id" value="{oi}"/>\n      <metadata key="source_volume_id" value="{k}"/>\n'
                f'      <metadata key="extruder" value="{m.extruder}"/>\n    </part>'
            )
        subs[f"3D/Objects/object_{oi + 1}.model"] = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
            + "\n".join(meshes_xml)
            + "\n </resources>\n <build/>\n</model>\n"
        )
        obj_id = 1000 + oi
        res.append(
            f'  <object id="{obj_id}" p:UUID="{_uid(obj_id)}" type="model">\n   <components>\n'
            + "\n".join(comps)
            + "\n   </components>\n  </object>"
        )
        build.append(
            f'  <item objectid="{obj_id}" p:UUID="{_uid(5000 + oi)}" '
            f'transform="1 0 0 0 1 0 0 0 1 {oc[0]:.6f} {oc[1]:.6f} 0" printable="1"/>'
        )
        own = "".join(
            f'\n    <metadata key="{k}" value="{v}"/>'
            for k, v in settings_by_obj.get(oname, {}).items()
        )
        settings.append(
            f'  <object id="{obj_id}">\n    <metadata key="name" value="{oname}"/>\n'
            f'    <metadata key="extruder" value="{WHITE}"/>{own}\n'
            + "\n".join(pxml)
            + "\n  </object>"
        )
        insts.append(
            f'    <model_instance>\n      <metadata key="object_id" value="{obj_id}"/>\n'
            f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{2000 + oi}"/>\n'
            "    </model_instance>"
        )
    main = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n'
        f' <metadata name="Application">Snapmaker_Orca-{version}</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        ' <metadata name="Title">U1 calibration plate 1 (underlay)</metadata>\n <resources>\n'
        + "\n".join(res)
        + f'\n </resources>\n <build p:UUID="{_uid(9999)}">\n'
        + "\n".join(build)
        + "\n </build>\n</model>\n"
    )
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "\n".join(settings)
        + '\n  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value="u1_plate1"/>\n'
        '    <metadata key="locked" value="false"/>\n    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
        '    <metadata key="filament_maps" value="1 1 1 1"/>\n'
        + "\n".join(insts)
        + "\n  </plate>\n  <assemble>\n  </assemble>\n</config>\n"
    )
    ctypes = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n'
    )
    rel_head = '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    rel_t = '<Relationship Target="/{}" Id="rel-{}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr(
            "_rels/.rels",
            rel_head
            + " "
            + rel_t.format("3D/3dmodel.model", 1)
            + "\n</Relationships>\n",
        )
        z.writestr("3D/3dmodel.model", main)
        z.writestr(
            "3D/_rels/3dmodel.model.rels",
            rel_head
            + "\n".join(" " + rel_t.format(p, i + 1) for i, p in enumerate(subs))
            + "\n</Relationships>\n",
        )
        for p, s in subs.items():
            z.writestr(p, s)
        z.writestr("Metadata/model_settings.config", model_settings)
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps(cfg, indent=4, ensure_ascii=False),
        )


def slice_project(project: Path, tmp: Path) -> tuple[str, str]:
    """(G-code, CLI log). Snapmaker Orca 01.10.01.50's CLI segfaults in
    PartPlate::generate_plate_name_texture() on any project with a <plate> block (babushkin-dom
    verify_slices.cli_copy), so it slices a copy without it."""
    cli = tmp / "cli.3mf"
    with (
        zipfile.ZipFile(project) as zi,
        zipfile.ZipFile(cli, "w", zipfile.ZIP_DEFLATED) as zo,
    ):
        for it in zi.infolist():
            data = zi.read(it.filename)
            if it.filename == "Metadata/model_settings.config":
                data = re.sub(rb"  <plate>.*?</plate>\n", b"", data, flags=re.S)
            zo.writestr(it, data)
    cmd = [
        SLICER,
        "--debug",
        "2",
        "--datadir",
        str(tmp / "dd2"),
        "--arrange",
        "0",
        "--slice",
        "0",
        "--outputdir",
        str(tmp),
        str(cli),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    log = r.stdout + r.stderr
    g = tmp / "plate_1.gcode"
    if not g.is_file() or g.stat().st_size == 0:
        raise GenError(
            f"Snapmaker Orca did not slice the plate (exit {r.returncode}):\n{log[-3000:]}"
        )
    return g.read_text(), log


# ---- G-code post-processing --------------------------------------------------------------------

_Z = re.compile(r"^;Z:([\d.]+)")
_HEIGHT = re.compile(r"^;HEIGHT:([\d.]+)")
_WORD = re.compile(r"\b([XYZEF])(-?\d*\.?\d+)")
_ACCEL = re.compile(r"^SET_VELOCITY_LIMIT\b")
_START = re.compile(r"^EXCLUDE_OBJECT_START NAME=([a-z0-9_]+?)_id_\d+_copy_\d+")
_END = re.compile(r"^EXCLUDE_OBJECT_END NAME=([a-z0-9_]+?)_id_\d+_copy_\d+")


def _first(cfg: dict[str, Any], key: str, i: int = 0) -> float:
    v = cfg[key]
    return float(v[i] if isinstance(v, list) else v)


def apply_hooks(
    text_: str, tests: list[Test], retract_len: float
) -> tuple[str, dict[str, Any]]:
    """Per-object parameters on the black visits (tool T0 above the underlay): flow (extrusion E),
    bands (enter lines, line transform), retraction bands. After each banded visit the slicer's
    own ACCEL, SCV and feedrate are restored. Returns the G-code and a log of what was applied
    per object and layer (the switching proof)."""
    by = {t.name: t for t in tests}
    out: list[str] = []
    z_top, h = 0.0, FIRST_H
    tool: int | None = None
    accel_line = ""
    f_last = 3000.0
    cur: Test | None = None
    band: Band | None = None
    rband: tuple[float, float, float] | None = None
    pending = 0.0  # extra retraction a retraction band left open, owed to the next de-retraction
    open_r = 0.0
    seen: set[tuple[str, float]] = set()
    log: dict[str, list[Any]] = {}
    for ln in text_.split("\n"):
        m = _Z.match(ln)
        if m:
            z_top = float(m.group(1))
        m = _HEIGHT.match(ln)
        if m:
            h = float(m.group(1))
        s = ln.split(";", 1)[0].strip()
        tm = re.match(r"^T(\d+)$", s)
        if tm:
            tool = int(tm.group(1))
        ms, me = _START.match(ln), _END.match(ln)
        if ms:
            out.append(ln)
            t = by.get(ms.group(1))
            if t is None or tool != BLACK - 1 or z_top <= U + 1e-6:
                continue
            cur = t
            zmid = (
                z_top - h / 2
            )  # #301: the band of the layer's own span, not of its top
            band = next((b for b in t.bands if b.z0 <= zmid < b.z1), None)
            rband = next((r for r in t.retract_bands if r[0] <= zmid < r[1]), None)
            if band is not None and (t.name, band.z0) not in seen:
                out.extend(band.enter)
                seen.add((t.name, band.z0))
                log.setdefault(t.name, []).append(
                    {"z": z_top, **band.params, "enter": list(band.enter)}
                )
            elif band is not None and band.transform is not None:
                out.extend(band.enter)
            if t.flow != 1.0 and (t.name, -1.0) not in seen:
                seen.add((t.name, -1.0))
                log.setdefault(t.name, []).append({"z": z_top, "flow": t.flow})
            if rband is not None and (t.name, rband[0]) not in seen:
                seen.add((t.name, rband[0]))
                log.setdefault(t.name, []).append(
                    {"z": z_top, "retraction_mm": rband[2]}
                )
            open_r = 0.0
            continue
        if me and cur is not None:
            if band is not None and band.transform is not None:
                out += [
                    accel_line or "SET_VELOCITY_LIMIT ACCEL=10000",
                    f"SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY={TOWER_SCV:g}",
                    f"G1 F{f_last:.0f}",
                ]
            if rband is not None:
                pending += open_r
            cur, band, rband = None, None, None
            out.append(ln)
            continue
        line = ln
        if _ACCEL.match(s):
            accel_line = s
        if s.startswith(("G0", "G1", "G2", "G3")):
            for k, v in _WORD.findall(s):
                if k == "F":
                    f_last = float(v)
        if cur is not None:
            if band is not None and band.transform is not None:
                line = band.transform(line)
            if cur.flow != 1.0:
                line = flow_e(cur.flow)(line)
            if rband is not None and s.startswith("G1"):
                em = re.search(r" E(-?\d*\.?\d+)", s)
                if em:
                    e = float(em.group(1))
                    xy = re.search(r" [XY]-?[\d.]", s) is not None
                    f = rband[2] / retract_len
                    if e < 0:
                        ne = e * f
                        open_r += ne - e
                        line = s[: em.start()] + f" E{ne:.5f}" + s[em.end() :]
                    elif e > 0 and not xy and open_r:
                        line = s[: em.start()] + f" E{e - open_r:.5f}" + s[em.end() :]
                        open_r = 0.0
        if pending and line.lstrip().startswith("G1"):
            em = re.search(r" E(\d*\.?\d+)", line)
            if em and not re.search(r" [XY]-?[\d.]", line):
                line = (
                    line[: em.start()]
                    + f" E{float(em.group(1)) - pending:.5f}"
                    + line[em.end() :]
                )
                pending = 0.0
        out.append(line)
    return "\n".join(out), log


def hand_rects(tests: list[Test]) -> list[Rect]:
    """Footprints of the hand-written lines (for the preview)."""
    by = {t.name: t for t in tests}
    fr = by["frame_50"].params
    x0, y0 = fr["pa_origin"]
    out = [
        (
            x0,
            y0 + k * PA_PITCH - 0.25,
            x0 + 2 * PA_SLOW_L + PA_FAST_L,
            y0 + k * PA_PITCH + 0.25,
        )
        for k in range(len(PA_VALUES))
    ]
    mx, my = by["mvs"].params["origin"]
    out += [
        (
            mx + MVS_SEG * k + 0.2,
            my,
            mx + MVS_SEG * (k + 1) - 0.2,
            my + (MVS_LINES - 1) * MVS_SPACING + MVS_W,
        )
        for k in range(len(MVS_VALUES))
    ]
    return out


def pa_block(
    cfg: dict[str, Any], origin: tuple[float, float], z: float
) -> tuple[list[str], float]:
    fa = math.pi * (_first(cfg, "filament_diameter", BLACK - 1) / 2) ** 2
    epm = MVS_W * LAYER_H * _first(cfg, "filament_flow_ratio", BLACK - 1) / fa
    r = _first(cfg, "filament_retraction_length", BLACK - 1)
    x0, y0 = origin
    out = [
        f"; 3d calibrate plate: PA lines at Z{z:.2f} ({','.join(f'{v:g}' for v in PA_VALUES)})",
        f"SET_VELOCITY_LIMIT ACCEL={PA_ACCEL}",
    ]
    secs = 0.0
    for k, v in enumerate(PA_VALUES):
        y = y0 + k * PA_PITCH
        xs = [
            x0,
            x0 + PA_SLOW_L,
            x0 + PA_SLOW_L + PA_FAST_L,
            x0 + 2 * PA_SLOW_L + PA_FAST_L,
        ]
        out += [
            f"SET_PRESSURE_ADVANCE ADVANCE={v:g}",
            f"G1 Z{z + 0.6:.3f} F1200",
            f"G1 X{xs[0]:.3f} Y{y:.3f} F18000",
            f"G1 Z{z:.3f} F1200",
            f"G1 E{r:.3f} F1800",
        ]
        for a, b, sp in (
            (xs[0], xs[1], PA_SLOW),
            (xs[1], xs[2], PA_FAST),
            (xs[2], xs[3], PA_SLOW),
        ):
            out.append(f"G1 X{b:.3f} E{(b - a) * epm:.5f} F{sp * 60:.0f}")
            secs += (b - a) / sp
        out += [f"G1 E-{r:.3f} F1800", f"G1 Z{z + 0.6:.3f} F1200"]
        secs += 3.0
    out.append(f"SET_PRESSURE_ADVANCE ADVANCE={PRINTER_PA:g}")
    return out, secs


def mvs_block(
    cfg: dict[str, Any], origin: tuple[float, float], n: int, z: float
) -> tuple[list[str], float]:
    fa = math.pi * (_first(cfg, "filament_diameter", BLACK - 1) / 2) ** 2
    epm = MVS_W * LAYER_H * _first(cfg, "filament_flow_ratio", BLACK - 1) / fa
    r = _first(cfg, "filament_retraction_length", BLACK - 1)
    x0, y0 = origin
    xs = [x0 + k * MVS_SEG for k in range(len(MVS_VALUES) + 1)]
    ys = [y0 + MVS_W / 2 + j * MVS_SPACING for j in range(MVS_LINES)]
    if n % 2:
        ys.reverse()
    speeds = (
        [v / (MVS_W * LAYER_H) for v in MVS_VALUES]
        if n
        else [MVS_BASE_SPEED] * len(MVS_VALUES)
    )
    out = [
        f"; 3d calibrate plate: MVS strip layer {n} at Z{z:.2f}",
        f"SET_VELOCITY_LIMIT ACCEL={MVS_ACCEL}",
        f"G1 Z{z + 0.6:.3f} F1200",
        f"G1 X{xs[0]:.3f} Y{ys[0]:.3f} F18000",
        f"G1 Z{z:.3f} F1200",
        f"G1 E{r:.3f} F1800",
    ]
    secs, x, y = 0.0, xs[0], ys[0]
    for j, ry in enumerate(ys):
        if j:
            out.append(
                f"G1 Y{ry:.3f} E{abs(ry - y) * epm:.5f} F{MVS_BASE_SPEED * 60:.0f}"
            )
            secs += abs(ry - y) / MVS_BASE_SPEED
            y = ry
        fwd = abs(x - xs[0]) < 1e-6
        for k in range(len(MVS_VALUES)) if fwd else range(len(MVS_VALUES) - 1, -1, -1):
            nx = xs[k + 1] if fwd else xs[k]
            out.append(f"G1 X{nx:.3f} E{abs(nx - x) * epm:.5f} F{speeds[k] * 60:.0f}")
            secs += abs(nx - x) / speeds[k]
            x = nx
    out += [f"G1 E-{r:.3f} F1800", f"G1 Z{z + 0.6:.3f} F1200"]
    return out, secs + 4.0


def splice_hand(
    text_: str, cfg: dict[str, Any], tests: list[Test]
) -> tuple[str, float, list[float]]:
    """PA lines (first black layer) and the MVS strip (first MVS_LAYERS black layers) once those
    layers are done: at the next ;LAYER_CHANGE, or right after the object visit still open there
    (Orca closes a visit after the layer change), so the lines never belong to another object.
    Each block runs inside its own object's EXCLUDE_OBJECT_START/END (frame_50 holds the PA
    lines, mvs its strip: excluding the object skips them, print-preflight checks them on that
    object's base); position, retraction state, feedrate and ACCEL are restored after it."""
    by = {t.name: t for t in tests}
    pa_o = tuple(by["frame_50"].params["pa_origin"])
    mv_o = tuple(by["mvs"].params["origin"])
    names = dict(
        re.findall(
            r"^EXCLUDE_OBJECT_DEFINE NAME=(([a-z0-9_]+?)_id_\d+_copy_\d+)", text_, re.M
        )
    )
    full = {short: long for long, short in names.items()}
    if "frame_50" not in full or "mvs" not in full:
        raise GenError(
            "hand-written lines: frame_50 / mvs missing from EXCLUDE_OBJECT_DEFINE"
        )
    out: list[str] = []
    z_cur: float | None = None
    x = y = zpos = 0.0
    f_last = 3000.0
    retracted = False
    accel_line = ""
    done: list[float] = []
    total = 0.0
    black = False
    open_visit = False
    due: float | None = None  # the finished layer whose lines are still to come
    r = _first(cfg, "filament_retraction_length", BLACK - 1)

    def block(z: float) -> list[str]:
        nonlocal total
        lines: list[str] = [] if retracted else [f"G1 E-{r:.3f} F1800"]
        if not done:
            b, sec = pa_block(cfg, pa_o, z)
            lines += [
                f"EXCLUDE_OBJECT_START NAME={full['frame_50']}",
                *b,
                f"EXCLUDE_OBJECT_END NAME={full['frame_50']}",
            ]
            total += sec
        b, sec = mvs_block(cfg, mv_o, len(done), z)
        lines += [
            f"EXCLUDE_OBJECT_START NAME={full['mvs']}",
            *b,
            f"EXCLUDE_OBJECT_END NAME={full['mvs']}",
        ]
        total += sec
        lines += [f"G1 X{x:.3f} Y{y:.3f} F18000", f"G1 Z{zpos:.3f} F1200"]
        if not retracted:
            lines.append(f"G1 E{r:.3f} F1800")
        lines += [
            f"G1 F{f_last:.0f}",
            accel_line or "SET_VELOCITY_LIMIT ACCEL=10000",
            "; 3d calibrate plate: end of hand-written lines",
        ]
        done.append(z)
        return lines

    for ln in text_.split("\n"):
        if (
            ln.startswith(";LAYER_CHANGE")
            and black
            and len(done) < MVS_LAYERS
            and z_cur is not None
            and z_cur > U + 0.01
        ):
            if open_visit:
                due = z_cur
            else:
                out += block(z_cur)
        m = _Z.match(ln)
        if m:
            z_cur = float(m.group(1))
        s = ln.split(";", 1)[0].strip()
        if s == f"T{BLACK - 1}":
            black = True
        if s.startswith("EXCLUDE_OBJECT_START"):
            open_visit = True
        if s.startswith(("G0", "G1", "G2", "G3")):
            for k, v in _WORD.findall(s):
                if k == "X":
                    x = float(v)
                elif k == "Y":
                    y = float(v)
                elif k == "Z":
                    zpos = float(v)
                elif k == "F":
                    f_last = float(v)
                elif k == "E" and float(v) != 0:
                    retracted = float(v) < 0
        if _ACCEL.match(s) and "ACCEL=" in s:
            accel_line = s
        out.append(ln)
        if s.startswith("EXCLUDE_OBJECT_END"):
            open_visit = False
            if due is not None:
                out += block(due)
                due = None
    if len(done) < MVS_LAYERS:
        raise GenError(
            f"hand-written lines: only {len(done)} of {MVS_LAYERS} layers spliced"
        )
    return "\n".join(out), total, done


def scan(text_: str) -> dict[str, Any]:
    """Per layer: tool changes, tools that extrude and the part-fan values (S, 0–255) in force
    while they do (Orca sets the next layer's fan after a layer's last move). Layer 0 = start."""
    layers: list[dict[str, Any]] = []
    cur: dict[str, Any] = {
        "layer": 0,
        "z": None,
        "tool_changes": [],
        "extruding": set(),
        "fan": [],
    }
    tool: int | None = None
    fan = 0.0
    for ln in text_.split("\n"):
        if ln.startswith(";LAYER_CHANGE"):
            layers.append(cur)
            cur = {
                "layer": cur["layer"] + 1,
                "z": None,
                "tool_changes": [],
                "extruding": set(),
                "fan": [],
            }
            continue
        m = _Z.match(ln)
        if m and cur["z"] is None:
            cur["z"] = float(m.group(1))
        s = ln.split(";", 1)[0].strip()
        t = re.match(r"^T(\d+)$", s)
        if t:
            nt = int(t.group(1))
            if nt != tool:
                cur["tool_changes"].append(
                    f"T{tool if tool is not None else '-'}->T{nt}"
                )
            tool = nt
        if (
            s.startswith(("G1", "G2", "G3"))
            and re.search(r"\bE\d*\.?\d", s)
            and re.search(r"\b[XY]-?[\d.]", s)
        ):
            cur["extruding"].add(tool)
            if fan not in cur["fan"]:
                cur["fan"].append(fan)
        f = re.match(r"^M106\b(?!.*\bP[1-9])(?:.*\bS([\d.]+))?", s)
        if f:
            fan = float(f.group(1) or 255)
        if s.startswith("M107") and "P" not in s:
            fan = 0.0
    layers.append(cur)
    for L in layers:
        L["extruding"] = sorted(f"T{t}" for t in L["extruding"] if t is not None)
    return {
        "layers": layers,
        "changes": [(L["layer"], c) for L in layers for c in L["tool_changes"]],
    }


# ---- main ------------------------------------------------------------------------------------------


_TOOL = re.compile(r"^T?([1-4])$", re.IGNORECASE)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def policy_layers() -> int:
    """`calibrations.underlay_layers` of fleet/policy.yaml (1 when the file does not set it)."""
    from printops import policy

    try:
        return policy.section("calibrations").underlay_layers
    except policy.PolicyError as e:
        raise GenError(f"fleet/policy.yaml: {e}") from e


def setup_underlay(
    layers: int | None, tool_arg: str | None, colours_arg: str | None
) -> float:
    """Layers, head and contrast of this run: `--underlay-layers` else the policy's, the head with
    the most contrast against the tests' T1 (light under dark, #263) else `--underlay-tool` — both
    through the same contrast check. Returns the ΔE of the pair."""
    if colours_arg:
        cols = [c.strip() for c in colours_arg.split(",")]
        if len(cols) != 4 or not all(_HEX.match(c) for c in cols):
            raise GenError("--slot-colours wants four #RRGGBB values, T1 to T4")
        SLOT_COLOURS[:] = [c.upper() for c in cols]
    override = None
    if tool_arg is not None:
        m = _TOOL.match(tool_arg.strip())
        if m is None:
            raise GenError(f"--underlay-tool wants T2, T3 or T4, got {tool_arg!r}")
        override = f"T{m.group(1)}"
    tool, delta_e = pick_underlay_tool(
        {f"T{i}": c for i, c in enumerate(SLOT_COLOURS, start=1)}, f"T{BLACK}", override
    )
    configure(layers if layers is not None else policy_layers(), int(tool[1:]))
    return delta_e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="u1_underlay", description=__doc__.split("\n")[0])
    ap.add_argument(
        "-o",
        "--out",
        required=True,
        help="OUT.gcode (NAME.json, NAME.png, NAME.3mf next to it)",
    )
    ap.add_argument("--date", default=_dt.date.today().isoformat(), help="YYYY-MM-DD")
    ap.add_argument("--preview-only", action="store_true")
    ap.add_argument(
        "--underlay-layers",
        type=int,
        default=None,
        help="1 or 2 layers (default: calibrations.underlay_layers of fleet/policy.yaml)",
    )
    ap.add_argument(
        "--underlay-tool",
        default=None,
        help="head of the underlay, T2-T4 (default: the head that contrasts most with the tests' T1; "
        f"the contrast check refuses a pair under ΔE {MIN_CONTRAST_DE:g})",
    )
    ap.add_argument(
        "--slot-colours",
        default=None,
        help="#RRGGBB,#RRGGBB,#RRGGBB,#RRGGBB: what is loaded on T1..T4 (default: the live print_task_config)",
    )
    a = ap.parse_args(argv)
    try:
        contrast_de = setup_underlay(a.underlay_layers, a.underlay_tool, a.slot_colours)
    except GenError as e:
        print(f"u1_underlay: {e}", file=sys.stderr)
        return 2
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    token = f"C{a.date.replace('-', '')}P1"
    tests, extra = build_tests(token, a.date)
    label: Test = extra["label_test"]
    ul: Rect = extra["underlay"]
    meshes = render_meshes(tests, label, ul)
    bare = rect_difference(ul, [t.rect for t in tests] + [label.rect])
    usable = (BED - 2 * EDGE) ** 2
    ux = (ul[2] - ul[0]) * (ul[3] - ul[1])
    used = sum((t.rect[2] - t.rect[0]) * (t.rect[3] - t.rect[1]) for t in tests) + (
        (label.rect[2] - label.rect[0]) * (label.rect[3] - label.rect[1])
    )
    fb = extra["flow_block"]
    used += (fb[2] - fb[0]) * BAND  # the flow block's band
    fill = {
        "usable_bed_mm2": usable,
        "underlay_mm2": round(ux),
        "underlay_share": round(ux / usable, 3),
        "tests_mm2": round(used),
        "tests_share_of_usable": round(used / usable, 3),
        "tests_share_of_underlay": round(used / ux, 3),
        "objects": len(tests),
    }
    share = read_by_share(tests, label)
    png = out.with_suffix(".png")
    preview_png(
        meshes,
        tests,
        label,
        png,
        f"U1 plate 1 — {share['tests']} tests in {len(tests)} objects; tests cover "
        f"{fill['tests_share_of_underlay']:.0%} of the underlay ({fill['underlay_share']:.0%} of the bed)",
        hand_rects(tests),
        share,
    )
    print(
        f"preview: {png}  fill: {json.dumps(fill)}  read_by: {json.dumps(share['by'])} "
        f"top {share['camera_top_share']:.0%}",
        flush=True,
    )
    if a.preview_only:
        return 0
    with tempfile.TemporaryDirectory(prefix="3du1plate.") as tmpd:
        tmp = Path(tmpd)
        cfg = project_settings(tmp)
        proj = out.with_suffix(".3mf")
        write_project(
            meshes,
            cfg,
            proj,
            slicer_version(),
            {t.name: t.settings for t in tests if t.settings},
        )
        raw, slog = slice_project(proj, tmp)
    retract_len = _first(cfg, "filament_retraction_length", BLACK - 1)
    hooked, hook_log = apply_hooks(raw, tests, retract_len)
    final, hand_secs, hand_z = splice_hand(hooked, cfg, tests)
    out.write_text(final)
    (out.parent / f"{out.stem}.slice.log").write_text(slog)
    sc = scan(final)
    est = re.search(r"^; estimated printing time \(normal mode\) = (.+)$", final, re.M)
    under = [L for L in sc["layers"] if L["z"] is not None and L["z"] <= U + 1e-6]
    manifest = {
        "schema": "calib-plate/1",
        "plate": PLATE_ID,
        "printer": "u1",
        "date": a.date,
        "token": token,
        "gcode": str(out),
        "project_3mf": str(proj),
        "preview": str(png),
        "presets": {
            "machine": MACHINE,
            "process": PROCESS,
            "filament": FILAMENT,
            "process_overrides": PROCESS_OVERRIDES,
            "filament_overrides": FILAMENT_OVERRIDES,
            "object_settings": {t.name: t.settings for t in tests if t.settings},
        },
        "heads": {
            "T1": {
                "gcode_tool": "T0",
                "spool": HEAD_SPOOLS.get(BLACK),
                "colour": SLOT_COLOURS[BLACK - 1],
                "role": "tests",
            },
            f"T{WHITE}": {
                "gcode_tool": f"T{WHITE - 1}",
                "spool": HEAD_SPOOLS.get(WHITE),
                "colour": SLOT_COLOURS[WHITE - 1],
                "role": "underlay",
            },
        },
        "underlay": {
            "layers": UNDERLAY_LAYERS,
            "top_mm": round(U, 3),
            "first_layer_mm": FIRST_H,
            "bbox": r3(ul),
            "brim_mm": UNDERLAY_BRIM,
            "brim": "none: the underlay holds the tests",
            "head": f"T{WHITE}",
            "contrast_delta_e": round(contrast_de, 1),
            "contrast_min_delta_e": MIN_CONTRAST_DE,
            "bed_first_layer_c": 70,
            "part_fan": 0,
            "seam_overlap_mm": SEAM,
            "bare": [r3(r) for r in bare],
            "first_layer_map": "the bare underlay between the tests (no first-layer patches)",
            "cutouts": [t.name for t in tests if t.underlay == "cutout"],
            "read": {
                "first_layer_map": "the bare underlay (`bare` rectangles) is the first-layer map: vision scores "
                "its line coverage, gaps and ridges per cell",
                "uniformity": "the spread of the map over the plate is the bed-flatness and z-offset hint",
                "light": "contrast",
                "note": f"multi-head: the underlay is T{WHITE}, a filament that contrasts with the tests' T1 "
                f"(ΔE {contrast_de:.0f}), so the camera reads dark on light whatever the bed colour",
            },
        },
        "label": extra["label"],
        "fill": fill,
        "read_by": share,
        "thin_walls": {
            "read_by": TOP,
            "walls": extra["walls"],
            "read": "Divider walls of the flow block, 3 mm tall: along Y between the columns "
            "(thickness by row, front → back 0.25/0.35/0.45), along X between the rows (by column, "
            "left → right). A missing wall = the slicer dropped it.",
        },
        "small_text": {
            "read_by": TOP,
            "read": "Every test's codes are the small-text test: cap height per test "
            "in params.code_cap_mm (2, 2.5 or 3 mm; 2 mm with 0.6 mm strokes, the rest 0.9).",
        },
        "estimate": {
            "slicer": est.group(1) if est else None,
            "hand_written_s": round(hand_secs),
        },
        "hand_written_at_z": hand_z,
        "hooks_applied": hook_log,
        "toolchanges": [{"layer": n, "change": c} for n, c in sc["changes"]],
        "underlay_layers_scan": [
            {k: L[k] for k in ("layer", "z", "tool_changes", "extruding", "fan")}
            for L in under
        ],
        "simplified": [
            "no arc-fitting hole tile: enable_arc_fitting is plate-wide in the CLI",
            "temperature bands above every other test instead of a sequential tower (same isolation, no "
            "gantry-clearance risk on the toolchanger)",
            "the retraction tower replaces the stringing pair (its 1.2 mm band = the preset)",
            "bridges stop at 25 mm: the U1's bridge_max_mm, longer spans fail print-preflight",
        ],
        "objects": [
            {
                "name": t.name,
                "kind": t.kind,
                "caption": t.caption,
                "read_by": t.read_by,
                "tests": [
                    {"test": s[0], "reads": s[1], "read_by": s[2]} for s in t.subs
                ]
                or None,
                "underlay": t.underlay,
                "rect": r3(t.rect),
                "top_mm": round(t.top, 3),
                "params": t.params,
                "items": t.items,
                "read": t.read,
            }
            for t in tests
        ],
    }
    out.with_suffix(".json").write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False)
    )
    print(
        json.dumps(
            {
                "gcode": str(out),
                "estimate": manifest["estimate"],
                "toolchanges": manifest["toolchanges"],
                "underlay_layers": manifest["underlay_layers_scan"],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
