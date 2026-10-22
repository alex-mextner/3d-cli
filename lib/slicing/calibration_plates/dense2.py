"""calibration_plates/dense2.py — the second dense plate: holes, fits and adhesion on one 220x220 bed.

WHAT (tests, ids for --only/--skip): only what the first dense plate (dense.py) did not settle:
    hole_quality  the same holes four ways side by side: plain mesh (PL), polyholes (PH), arc
                  fitting (AF), xy hole compensation +0.1 mm (HC)
    ovality       holes Ø3/5/8/10 between straight X/Y tile edges, a 5 mm ruler and a Ø10 ring
    fit           pins printed in holes: gaps 0.15/0.25/0.35, and 0.10/0.15/0.20 with a 0.4 mm
                  first-layer chamfer on pin and hole (elephant foot)
    elephant_foot two 10 mm blocks, without and with a 0.4 mm first-layer chamfer
    screw         M3 holes 2.7/3.0/3.6, M4 3.5/4.0/4.7
    slots         slots 2.05/2.15/2.25 for the 2.0 mm tab
    hinge         print-in-place hinges 0.25/0.35
    thin_wall     walls 0.25/0.35/0.45 along Y and along X
    brim          the 14 mm bridge and the 60° overhang again, on 8 mm bases with a 3 mm brim
                  (both peeled or leaned on the first plate)
    mvs           max volumetric speed strip 24–32 mm³/s (the first plate was clean up to 24)
    stringing     two pillars at the preset retraction
    top_surface   one top-surface tile
    fiducials     corner tiles: one-layer patch, 5 mm rulers along the bed edges, a Ø10 ring
    first_layer   20 mm one-layer patches on every free cell (a first-layer map of the bed)
    temp          temperature mini-tower in the back-right cell, sequential and last: only
                  one-layer objects lie within the 45 mm head clearance around it
  Layout: a 7x7 grid of 30 mm cells; every sample faces the same way and carries its label at
  the front of its cell: raised seven-segment characters of one size (on the tile, or on the
  bed for samples without a tile). The map (NAME.json) gives object → cell → parameters, item
  bboxes and how to read each one.
HOW the variants differ: Orca's CLI takes no per-object settings for STL inputs (only 3MF
  projects carry them), so polyholes and hole compensation are geometry (the holes are drawn
  the way Orca would turn them), and arc fitting — a G-code export option — comes from a second
  slice with enable_arc_fitting on whose visits of the AF tile replace the plain ones
  (calibration_plate.swap_visits). Brims are hand-written (calibration_plate.brim_block); the
  temperature tower's is Orca's own in its sequential slice (calibration_plate.seq_brim).
WHY: the user's presets (Orange PLA @K1SE, accel 8900 process) are new to these tests, and
  every sample is set up to be read from a photo, by hand and by a script (#269, #293, #301).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from typing import NamedTuple

from slicing.calibration_gen import Box, GenError, digit_boxes, prism_stl
from slicing.calibration_plate import (
    CustomBlock,
    Item,
    Plate,
    PlateContext,
    PlateObject,
    Rect,
    box_rect,
    brim_rects,
    brim_width,
)
from slicing.calibration_plates import filament
from slicing.calibration_plates.dense import (
    HINGE_L,
    HINGE_LEAF,
    HINGE_R,
    _tenths,
    hinge_scad,
    openscad_stl,
)

PLATE_ID = "dense2"
TITLE = "dense plate 2: holes, fits, elephant foot, brims"
TESTS = {
    "fiducials": "corner fiducials: one-layer patch, 5 mm rulers, Ø10 ring",
    "mvs": "max volumetric speed strip 24–32 mm³/s",
    "brim": "14 mm bridge and 60° overhang on 8 mm bases with a 3 mm brim",
    "hole_quality": "holes: plain / polyholes / arc fitting / xy hole compensation",
    "ovality": "hole ovality Ø3/5/8/10 with X/Y edges, ruler and ring",
    "fit": "pins in holes 0.15/0.25/0.35 and chamfered 0.10/0.15/0.20",
    "elephant_foot": "10 mm blocks without/with a 0.4 mm first-layer chamfer",
    "screw": "M3 2.7/3.0/3.6 and M4 3.5/4.0/4.7 holes",
    "slots": "slots 2.05/2.15/2.25 and the 2.0 mm tab",
    "hinge": "print-in-place hinges 0.25/0.35",
    "thin_wall": "thin walls 0.25/0.35/0.45 along Y and X",
    "stringing": "stringing pillars at the preset retraction",
    "top_surface": "top surface tile",
    "first_layer": "one-layer 20 mm patches on the free cells",
    "temp": "temperature mini-tower (sequential, last)",
}
BED = (220.0, 220.0)
EDGE = 5.0  # the usable bed: BED minus this on every side (footprint share)
GRID = 7
PITCH = 30.0  # cell pitch
ORIGIN = 5.0  # front-left corner of cell (0, 0)
GAP = 1.5  # between tiles
TILE = PITCH - GAP
# Labels: raised seven-segment characters, one size everywhere (width, height, gap, stroke):
# 3.5 x 5.1 mm outside, 2 layers high.
LBL = (2.6, 4.2, 1.0, 0.9)
EMBOSS = 0.4
LBL_Y = 1.2  # bottom of the label row above the tile's front edge
FEAT_Y = 7.5  # samples start this far behind the tile's front edge
XS3 = (5.25, 14.25, 23.25)  # centres of three items across a tile

HQ_D = (3.0, 4.0, 5.0, 6.0, 8.0)
HQ_AT = (
    (4.5, 12.0),
    (10.5, 12.0),
    (17.0, 12.0),
    (24.5, 12.0),
    (7.0, 21.5),
)  # (x, y) in the tile
HQ_FN = 64  # facets of a plain hole: an STL export's usual circle
HQ_COMP = 0.1  # mm of xy_hole_compensation the HC tile is drawn with
HQ_VARIANTS = ("PL", "PH", "AF", "HC")
HQ_H = 3.0
RING = (10.0, 7.6)  # target ring outer/inner Ø, raised EMBOSS
OVAL_A = ((3.0, 4.5), (5.0, 11.5), (8.0, 21.5))  # (Ø, x)
OVAL_B = 10.0
RULER_STEP, RULER_N = 5.0, 6  # ticks 0…25 mm
FIT_PIN_D, FIT_H, FIT_Y = 3.5, 3.0, 16.0
FIT_PLAIN = (0.15, 0.25, 0.35)  # the first plate had 0.1–0.5 in steps of 0.1
FIT_CHAMFERED = (0.10, 0.15, 0.20)
CHAMFER = 0.4
EF_BLOCK, EF_HOLE, EF_H = 10.0, 5.0, 3.0
SCREW_M3 = (2.7, 3.0, 3.6)  # the first plate had 2.5/3.2/3.4
SCREW_M4 = (3.5, 4.0, 4.7)  # … and 3.3/4.3/4.5
SCREW_H = 4.0
SLOTS = (2.05, 2.15, 2.25)  # the first plate had 2.1–2.4
SLOT_H, SLOT_DEPTH, SLOT_LEN = 4.0, 3.0, 10.0
INSERT = (12.0, 10.0, 2.0)  # 10 mm deep: no brim under the default rule
HINGES = (0.25, 0.35)  # the first plate had 0.3/0.4
THIN = (0.25, 0.35, 0.45)  # the first plate had 0.3–1.0; X 0.3 went missing
THIN_LEN_Y, THIN_LEN_X, THIN_H, BASE_H = 12.0, 8.0, 3.0, 0.6
BRIDGE = 14.0
BRIDGE_PILLAR, BRIDGE_PILLAR_H, BRIDGE_SLAB = 8.0, 3.0, 0.8
OVERHANG, OVERHANG_BASE, OVERHANG_H = 60, 8.0, 5.0
MVS_VALUES = (24, 26, 28, 30, 32)
MVS_LINES = 24
STRING_D, STRING_H = 4.0, 8.0
TOP_H = 2.0  # 2 bottom + 5 sparse + 3 top layers at 0.2 mm
PATCH_H = 0.2
PATCH = 20.0  # one-layer patches, centred in their cells
BRIM = 3.0
# The temperature tower prints alone at the end: 5 s per layer (Orca's PLA preset: 8) still
# cools its 5 mm pillars with the fan at 100 % (as on the first dense plate).
SEQ_MIN_LAYER_S = 5
# MVS 20 and inner-wall ACCEL 8000 come from the user presets themselves (#266).
PROCESS = {
    "top_shell_layers": 3,
    "bottom_shell_layers": 2,
    "sparse_infill_density": "15%",
    "brim_type": "no_brim",  # brims are hand-written, only where needed
}


class Spot(NamedTuple):
    """A sample's place on the grid: column, row (0, 0 = front-left) and width in cells."""

    col: int
    row: int
    span: int = 1


# spot -> its place; every other cell gets a first-layer patch
CELLS: dict[str, Spot] = {
    "fiducial_1": Spot(0, 0),
    "mvs": Spot(1, 0, 2),
    "brim_bridge": Spot(3, 0, 2),
    "brim_overhang": Spot(5, 0),
    "fiducial_2": Spot(6, 0),
    "hq_pl": Spot(0, 1),
    "hq_ph": Spot(1, 1),
    "hq_af": Spot(2, 1),
    "hq_hc": Spot(3, 1),
    "oval_a": Spot(4, 1),
    "oval_b": Spot(5, 1),
    "elephant_foot": Spot(6, 1),
    "fit_plain": Spot(0, 2),
    "fit_chamfered": Spot(1, 2),
    "screw_m3": Spot(2, 2),
    "screw_m4": Spot(3, 2),
    "slots": Spot(4, 2),
    "hinge": Spot(5, 2),
    "stringing": Spot(6, 2),
    "thin_y": Spot(0, 3),
    "thin_x": Spot(1, 3),
    "top": Spot(2, 3),
    "fiducial_3": Spot(0, 6),
    "fiducial_4": Spot(6, 6),
    # the gantry rules: only one-layer objects (patches, fiducials) within 45 mm of the tower
    "temp": Spot(5, 6),
}
SPOTS: dict[str, tuple[str, ...]] = {  # test -> its spots
    "fiducials": ("fiducial_1", "fiducial_2", "fiducial_3", "fiducial_4"),
    "mvs": ("mvs",),
    "brim": ("brim_bridge", "brim_overhang"),
    "hole_quality": ("hq_pl", "hq_ph", "hq_af", "hq_hc"),
    "ovality": ("oval_a", "oval_b"),
    "fit": ("fit_plain", "fit_chamfered"),
    "elephant_foot": ("elephant_foot",),
    "screw": ("screw_m3", "screw_m4"),
    "slots": ("slots",),
    "hinge": ("hinge",),
    "thin_wall": ("thin_y", "thin_x"),
    "stringing": ("stringing",),
    "top_surface": ("top",),
    "temp": ("temp",),
}

Objs = tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]
Cell = tuple[float, float, float, float]  # tile x0, y0, x1, y1


def cell(col: int, row: int, span: int = 1) -> Cell:
    x0 = ORIGIN + col * PITCH + GAP / 2
    y0 = ORIGIN + row * PITCH + GAP / 2
    return x0, y0, x0 + span * PITCH - GAP, y0 + TILE


def spot_cell(spot: str) -> Cell:
    s = CELLS[spot]
    return cell(s.col, s.row, s.span)


def patch_cells() -> list[tuple[int, int]]:
    taken = {(s.col + k, s.row) for s in CELLS.values() for k in range(s.span)}
    return [(c, r) for r in range(GRID) for c in range(GRID) if (c, r) not in taken]


# ---- labels and OpenSCAD helpers ---------------------------------------------------------------


def text_width(text: str) -> float:
    """Nominal width of a label (strokes reach half a stroke further on both sides)."""
    w, _, gap, s = LBL
    return sum(s if ch == "." else w for ch in text) + gap * (len(text) - 1)


def label_boxes(text: str, cx: float, y: float, z: float) -> list[Box]:
    """Raised label centred on cx, its outer bottom edge at y, standing on z."""
    s = LBL[3]
    return digit_boxes(text, cx - text_width(text) / 2, y + s / 2, z, LBL, EMBOSS)


def scad_cubes(boxes: list[Box]) -> str:
    return "".join(
        f"translate([{b[0]:.4f},{b[1]:.4f},{b[2]:.4f}]) cube([{b[3] - b[0]:.4f},{b[4] - b[1]:.4f},{b[5] - b[2]:.4f}]);"
        for b in boxes
    )


def scad_ring(cx: float, cy: float, z: float, outer: float, inner: float) -> str:
    return (
        f"translate([{cx},{cy},{z}]) difference() {{ cylinder(d={outer},h={EMBOSS},$fn=96); "
        f"translate([0,0,-1]) cylinder(d={inner},h={EMBOSS + 2},$fn=96); }}"
    )


def ruler_boxes(x0: float, y_edge: float, z: float, toward: float) -> list[Box]:
    """A ruler along X: a bar at y_edge with ticks every RULER_STEP mm from x0 (long ones every
    10 mm) pointing `toward` (+1 behind the bar, -1 in front of it)."""
    s = LBL[3]
    length = RULER_STEP * (RULER_N - 1)
    ya, yb = sorted((y_edge, y_edge + toward * s))
    out: list[Box] = [(x0 - s / 2, ya, z, x0 + length + s / 2, yb, z + EMBOSS)]
    for k in range(RULER_N):
        x = x0 + k * RULER_STEP
        tick = 3.0 if k % 2 == 0 else 2.0
        ta, tb = sorted((y_edge + toward * s, y_edge + toward * (s + tick)))
        out.append((x - s / 2, ta, z, x + s / 2, tb, z + EMBOSS))
    return out


def _swap_xy(b: Box) -> Box:
    return (b[1], b[0], b[2], b[4], b[3], b[5])


def _circle(x: float, y: float, d: float, z0: float, z1: float) -> Box:
    return (x - d / 2, y - d / 2, z0, x + d / 2, y + d / 2, z1)


def _label_y(t: Cell) -> float:
    """Bottom of the label row of a tile or cell."""
    return t[1] + LBL_Y


def _hundredths(v: float) -> str:
    return f"{round(v * 100)}"


# ---- samples ---------------------------------------------------------------------------------


def _tile_obj(
    name: str,
    kind: str,
    t: Cell,
    h: float,
    scad_extra: str,
    cuts: str,
    labels: list[tuple[str, float]],
    *,
    params: dict[str, float | str],
    read: str,
    top: float = 0.0,
    items: tuple[Item, ...] = (),
    record: str = "",
    arc_fitting: bool = False,
) -> PlateObject:
    """A tile of height h with `cuts` removed, `scad_extra` added (reaching up to `top`) and
    raised labels at its front (text, x in the tile)."""
    lbl: list[Box] = []
    for text, x in labels:
        lbl += label_boxes(text, t[0] + x, _label_y(t), h)
    src = (
        f"union() {{ difference() {{ translate([{t[0]},{t[1]},0]) cube([{t[2] - t[0]},{t[3] - t[1]},{h}]);"
        f" {cuts} }} {scad_extra} {scad_cubes(lbl)} }}"
    )
    return PlateObject(
        name,
        kind,
        stl=openscad_stl(src),
        stl_bbox=(t[0], t[1], 0.0, t[2], t[3], max(h + EMBOSS, top)),
        params=params,
        items=items,
        read=read,
        record=record,
        arc_fitting=arc_fitting,
    )


def _front_labels(
    name: str, t: Cell, labels: list[tuple[str, float]], read: str
) -> PlateObject:
    """Raised labels standing on the bed at the cell's front (for samples without a tile)."""
    boxes: list[Box] = []
    for text, x in labels:
        boxes += label_boxes(text, t[0] + x, _label_y(t), 0.0)
    return PlateObject(
        name,
        "label",
        tuple(boxes),
        # every stroke is its own tiny base: the default rule would ring each one with a brim
        brim=0.0,
        params={"texts": " ".join(text for text, _ in labels)},
        read=read,
    )


def _cell_params(spot: str) -> dict[str, float | str]:
    s = CELLS[spot]
    return {"cell": f"{s.col},{s.row}" + (f"+{s.span - 1}" if s.span > 1 else "")}


def _fiducial(spot: str) -> Objs:
    col, row, _ = CELLS[spot]
    t = spot_cell(spot)
    k = spot.rsplit("_", 1)[1]
    left, front = col == 0, row == 0
    z = PATCH_H
    cx, cy = (t[0] + t[2]) / 2, (t[1] + t[3]) / 2
    inset = 0.6
    s = LBL[3]
    corner_x = t[0] + inset + s / 2 if left else t[2] - inset - s / 2
    corner_y = t[1] + inset + s / 2 if front else t[3] - inset - s / 2
    length = RULER_STEP * (RULER_N - 1)
    # ruler along X on the outer (front/back) edge, ruler along Y on the outer (left/right) edge
    x_from = corner_x if left else corner_x - length
    marks = ruler_boxes(
        x_from, t[1] + inset if front else t[3] - inset, z, 1.0 if front else -1.0
    )
    y_from = corner_y if front else corner_y - length
    marks += [
        _swap_xy(b)
        for b in ruler_boxes(
            y_from, t[0] + inset if left else t[2] - inset, z, 1.0 if left else -1.0
        )
    ]
    ly = (
        t[3] - LBL_Y - (LBL[1] + s) if front else _label_y(t)
    )  # away from the edge ruler
    marks += label_boxes(k, cx, ly, z)
    src = (
        f"union() {{ translate([{t[0]},{t[1]},0]) cube([{TILE},{TILE},{z}]); {scad_cubes(marks)} "
        f"{scad_ring(cx, cy, z, *RING)} translate([{cx},{cy},{z}]) cylinder(d=2,h={EMBOSS},$fn=32); }}"
    )
    return (
        PlateObject(
            spot,
            "fiducial",
            stl=openscad_stl(src),
            stl_bbox=(t[0], t[1], 0.0, t[2], t[3], z + EMBOSS),
            params={
                **_cell_params(spot),
                "corner": k,
                "patch_mm": z,
                "ruler_tick_mm": RULER_STEP,
                "ring_outer_d_mm": RING[0],
                "ring_inner_d_mm": RING[1],
            },
            items=(
                Item(
                    "ring",
                    _circle(cx, cy, RING[0], z, z + EMBOSS),
                    {"outer_d_mm": RING[0]},
                ),
            ),
            read=f"Corner fiducial {k} (1 front-left, 2 front-right, 3 back-left, 4 back-right): a one-layer "
            f"patch (judge the first layer here like on the patches), a Ø{RING[0]:g} ring with a centre dot "
            f"(photo registration: the four dots are the plate's reference frame) and {RULER_STEP:g} mm rulers "
            "along both bed edges (long ticks every 10 mm) for the photo scale in X and Y.",
        ),
    ), ()


def _patch(col: int, row: int) -> Objs:
    c = cell(col, row)
    cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
    t = (cx - PATCH / 2, cy - PATCH / 2, cx + PATCH / 2, cy + PATCH / 2)
    text = f"{col}{row}"
    lbl = label_boxes(text, cx, _label_y(t), PATCH_H)
    return (
        PlateObject(
            f"patch_{col}{row}",
            "first_layer",
            ((t[0], t[1], 0.0, t[2], t[3], PATCH_H), *lbl),
            params={"cell": f"{col},{row}", "layer_mm": PATCH_H},
            read=f"One-layer patch in cell column {col}, row {row} (label = column, row; 00 = front-left). "
            "Even, closed lines are right; gaps between lines = nozzle too high there, ridges and a "
            "rough, thin surface = too low. Compare the patches across the bed: a first-layer map.",
        ),
    ), ()


def _temp(ctx: PlateContext, t: Cell) -> Objs:
    sec = filament._temp(ctx)
    # 1.5 mm left of the cell's centre: its brim (24 mm tower + 2 x 3 mm) clears the fiducial tile
    cx, cy = (t[0] + t[2]) / 2 - 1.5, (t[1] + t[3]) / 2
    objs, blocks = sec.build(cx - sec.width / 2, cy - sec.depth / 2)
    return tuple(
        replace(o, params={**_cell_params("temp"), **o.params}) for o in objs
    ), blocks


def _mvs(ctx: PlateContext) -> Objs:
    t = spot_cell("mvs")
    sec = filament._mvs(ctx, values=MVS_VALUES, lines=MVS_LINES, labels=False)
    x0 = t[0] + (t[2] - t[0] - sec.width) / 2
    _, blocks = sec.build(x0, t[1] + FEAT_Y)
    seg = sec.width / len(MVS_VALUES)
    labels = [(str(v), x0 - t[0] + seg * (k + 0.5)) for k, v in enumerate(MVS_VALUES)]
    return (
        _front_labels(
            "mvs_labels",
            t,
            labels,
            "Labels of the MVS strip behind them: mm³/s of each segment.",
        ),
    ), blocks


def _brim_bridge(t: Cell) -> Objs:
    p, h = BRIDGE_PILLAR, BRIDGE_PILLAR_H
    x, y = t[0] + 4.0, t[1] + 12.0
    w = 2 * p + BRIDGE
    boxes: tuple[Box, ...] = (
        (x, y, 0.0, x + p, y + p, h),
        (x + p + BRIDGE, y, 0.0, x + w, y + p, h),
        (x, y, h, x + w, y + p, h + BRIDGE_SLAB),
    )
    ix, iy, iz = INSERT
    tx = t[0] + 44.0
    tab: Box = (tx, t[1] + 9.0, 0.0, tx + ix, t[1] + 9.0 + iy, iz)
    return (
        PlateObject(
            "bridge_14_brim",
            "bridge",
            boxes,
            params={
                **_cell_params("brim_bridge"),
                "bridge_mm": BRIDGE,
                "pillar_mm": f"{p:g}x{p:g}x{h:g}",
            },
            items=(
                Item(
                    f"{BRIDGE:g}",
                    (x + p, y, h, x + p + BRIDGE, y + p, h + BRIDGE_SLAB),
                    {"bridge_mm": BRIDGE},
                ),
            ),
            read=f"{BRIDGE:g} mm bridge again, now on {p:g} mm pillars with a {BRIM:g} mm brim (the first plate's 4x6 mm "
            "pillar leaned in under the bridge's pull). Both pillars should stand vertical after peeling the brim; "
            "the bridge's underside should show straight, tight lines.",
        ),
        PlateObject(
            "insert_tab",
            "fit",
            (tab,),
            params={**_cell_params("brim_bridge"), "thickness_mm": iz},
            read=f"The {iz:g} mm tab for the slots (printed flat, its thickness is a Z size): measure it, then "
            "push it into the slots.",
        ),
        _front_labels(
            "bridge_labels",
            t,
            [(f"{BRIDGE:g}", x - t[0] + w / 2), (f"{iz:.1f}", tx - t[0] + ix / 2)],
            f"Labels: {BRIDGE:g} = the bridge's span in mm; {iz:.1f} = the tab's thickness in mm.",
        ),
    ), ()


def _brim_overhang(t: Cell) -> Objs:
    x, y = t[0] + 4.5, t[1] + 12.0
    b, h = OVERHANG_BASE, OVERHANG_H
    reach = h * math.tan(math.radians(OVERHANG))
    profile = [(x, 0.0), (x + b, 0.0), (x + b + reach, h), (x, h)]
    bbox = (x, y, 0.0, x + b + reach, y + b, h)
    return (
        PlateObject(
            f"overhang_{OVERHANG}_brim",
            "overhang",
            stl=prism_stl(profile, y, y + b),
            stl_bbox=bbox,
            base=((x, y, x + b, y + b),),
            params={
                **_cell_params("brim_overhang"),
                "overhang_deg": OVERHANG,
                "base_mm": f"{b:g}x{b:g}",
            },
            items=(Item(str(OVERHANG), bbox, {"overhang_deg": OVERHANG}),),
            read=f"{OVERHANG}° overhang again (right face, from vertical), on a {b:g}x{b:g} mm base with a {BRIM:g} mm brim: "
            "the first plate's one on a 4 mm base came off the bed. It should stay put; the face itself was fine.",
        ),
        _front_labels(
            "overhang_labels",
            t,
            [(str(OVERHANG), x - t[0] + (b + reach) / 2)],
            "Label: the overhang angle in degrees from vertical.",
        ),
    ), ()


def _hole_cut(
    x: float, y: float, d: float, h: float, variant: str
) -> tuple[str, float]:
    """OpenSCAD cut of one through hole and the diameter it is drawn with."""
    if variant == "PH":  # Nophead's polyhole: n = 2·Ø sides, circumscribed
        n = max(3, round(2 * d))
        r = d / 2 / math.cos(math.pi / n)
        return f"translate([{x},{y},-1]) cylinder(r={r:.4f},h={h + 2},$fn={n});", 2 * r
    drawn = d + 2 * HQ_COMP if variant == "HC" else d
    return (
        f"translate([{x},{y},-1]) cylinder(d={drawn:.4f},h={h + 2},$fn={HQ_FN});",
        drawn,
    )


HQ_READ = {
    "PL": f"plain {HQ_FN}-facet holes, as an STL export draws them (the reference)",
    "PH": "polyholes (Nophead: 2·Ø sides, the circle inscribed in the polygon), what Orca's "
    "hole_to_polyhole does to vertical holes",
    "AF": "the plain holes printed with arc fitting (G2/G3 from Orca's enable_arc_fitting; the "
    "K1 SE splits arcs back into 1 mm segments, [gcode_arcs] resolution 1.0)",
    "HC": f"holes drawn {HQ_COMP:g} mm larger in radius, what xy_hole_compensation = {HQ_COMP:g} does",
}


def _hole_quality(spot: str, variant: str) -> Objs:
    t = spot_cell(spot)
    cuts: list[str] = []
    items: list[Item] = []
    for d, (dx, dy) in zip(HQ_D, HQ_AT, strict=True):
        cut, drawn = _hole_cut(t[0] + dx, t[1] + dy, d, HQ_H, variant)
        cuts.append(cut)
        items.append(
            Item(
                f"{d:g}",
                _circle(t[0] + dx, t[1] + dy, d, 0.0, HQ_H),
                {"diameter_mm": d, "drawn_mm": round(drawn, 3)},
            )
        )
    rx, ry = t[0] + 21.0, t[1] + 21.5
    items.append(
        Item(
            "ring",
            _circle(rx, ry, RING[0], HQ_H, HQ_H + EMBOSS),
            {"outer_d_mm": RING[0], "inner_d_mm": RING[1]},
        )
    )
    return (
        _tile_obj(
            spot,
            "holes",
            t,
            HQ_H,
            scad_ring(rx, ry, HQ_H, *RING),
            " ".join(cuts),
            [(variant, TILE / 2)],
            arc_fitting=variant == "AF",
            params={
                **_cell_params(spot),
                "variant": variant,
                "diameters_mm": "3,4,5,6,8",
            },
            items=tuple(items),
            read=f"Hole tile {variant}: {HQ_READ[variant]}. Holes Ø3/4/5/6 front row left to right, Ø8 behind "
            f"them; the raised ring is Ø{RING[0]:g} outside / Ø{RING[1]:g} inside (photo scale next to the holes). "
            "The four tiles PL/PH/AF/HC stand side by side in this order: compare each Ø across them with "
            "pins or drill shanks (largest that slides in) and the roundness in a top photo. The best "
            "variant becomes the profile setting (hole_to_polyhole, enable_arc_fitting, xy_hole_compensation).",
            record="best variant and measured Ø → profile notes; xy_hole_compensation from the PL shortfall",
        ),
    ), ()


def _ovality(spot: str) -> Objs:
    t = spot_cell(spot)
    s = LBL[3]
    ruler = ruler_boxes(t[0] + 1.75 + s / 2, t[3] - 1.0, HQ_H, -1.0)
    if spot == "oval_a":
        holes = [(d, t[0] + x, t[1] + 13.0) for d, x in OVAL_A]
        extra = ""
        ring: list[Item] = []
    else:
        holes = [(OVAL_B, t[0] + 8.0, t[1] + 14.0)]
        rx, ry = t[0] + 21.0, t[1] + 14.0
        extra = scad_ring(rx, ry, HQ_H, OVAL_B, OVAL_B - 2.4)
        ring = [
            Item(
                "ring",
                _circle(rx, ry, OVAL_B, HQ_H, HQ_H + EMBOSS),
                {"outer_d_mm": OVAL_B},
            )
        ]
    cuts = " ".join(
        f"translate([{x},{y},-1]) cylinder(d={d},h={HQ_H + 2},$fn={HQ_FN});"
        for d, x, y in holes
    )
    return (
        _tile_obj(
            spot,
            "holes",
            t,
            HQ_H,
            extra + scad_cubes(ruler),
            cuts,
            [(f"{d:g}", x - t[0]) for d, x, _ in holes],
            params={
                **_cell_params(spot),
                "diameters_mm": ",".join(f"{d:g}" for d, _, _ in holes),
            },
            items=tuple(
                Item(f"{d:g}", _circle(x, y, d, 0.0, HQ_H), {"diameter_mm": d})
                for d, x, y in holes
            )
            + tuple(ring),
            read="Ovality targets, label = Ø in mm. The tile's front and left edges are the X and Y "
            f"references; the raised ruler at the back has ticks every {RULER_STEP:g} mm (long every 10). Measure "
            "each hole in X and in Y (calipers' inside jaws, or a top photo scaled by the ruler): X ≠ Y = "
            "ovality from belt tension or skew; both short = hole shrinkage (xy_hole_compensation)."
            + (
                " The raised ring beside the Ø10 hole is Ø10 outside: the same size drawn outward."
                if ring
                else ""
            ),
            record="X/Y diameters → printer history (kind dimension)",
        ),
    ), ()


def _fit(spot: str, gaps: tuple[float, ...], chamfer: float) -> Objs:
    t = spot_cell(spot)
    yc = t[1] + FIT_Y
    d, h, c = FIT_PIN_D, FIT_H, chamfer
    cuts: list[str] = []
    pins: list[str] = []
    for x, g in zip((t[0] + x for x in XS3), gaps, strict=True):
        hole = d + 2 * g
        cuts.append(
            f"translate([{x},{yc},-1]) cylinder(d={hole:.3f},h={h + 2},$fn=64);"
        )
        if c:
            cuts.append(
                f"translate([{x},{yc},-0.01]) cylinder(d1={hole + 2 * c + 0.02:.3f},d2={hole:.3f},h={c + 0.01},$fn=64);"
            )
            pins.append(
                f"translate([{x},{yc},0]) cylinder(d1={d - 2 * c:.3f},d2={d},h={c},$fn=64);"
                f"translate([{x},{yc},{c}]) cylinder(d={d},h={h - c},$fn=64);"
            )
        else:
            pins.append(f"translate([{x},{yc},0]) cylinder(d={d},h={h},$fn=64);")
    marker = scad_cubes(label_boxes("C", t[0] + 4.0, t[1] + 22.0, h)) if c else ""
    labels = [(_hundredths(g), x) for x, g in zip(XS3, gaps, strict=True)]
    return (
        _tile_obj(
            spot,
            "fit",
            t,
            h,
            " ".join(pins) + marker,
            " ".join(cuts),
            labels,
            params={
                **_cell_params(spot),
                "gaps_mm": ",".join(f"{g:g}" for g in gaps),
                "pin_d_mm": d,
                "chamfer_mm": c,
            },
            items=tuple(
                Item(
                    text,
                    _circle(t[0] + x, yc, d + 2 * g, 0.0, h),
                    {"gap_mm": g, "chamfer_mm": c},
                )
                for (text, x), g in zip(labels, gaps, strict=True)
            ),
            read=f"Ø{d:g} mm pins printed in their holes, label = radial gap in hundredths of a mm"
            + (
                f"; the C tile has a {c:g} mm 45° chamfer at the bottom of every pin and hole (elephant foot "
                "relief). Compare its 15 with the plain tile's 15"
                if c
                else ""
            )
            + ". Push each pin out: the smallest gap that frees by hand is the moving-fit clearance; the "
            "first plate freed 0.3 and held 0.2 with force, 0.1 welded.",
            record="clearances → design notes (fit_clearance_mm)",
        ),
    ), ()


def _elephant_foot(t: Cell) -> Objs:
    b, hd, h, c = EF_BLOCK, EF_HOLE, EF_H, CHAMFER
    y = t[1] + 9.0
    objs: list[PlateObject] = []
    labels: list[tuple[str, float]] = []
    for name, x, ch in (("ef_plain", t[0] + 2.5, 0.0), ("ef_chamfer", t[0] + 16.0, c)):
        cx, cy = x + b / 2, y + b / 2
        if ch:
            body = (
                f"hull() {{ translate([{x + ch},{y + ch},0]) cube([{b - 2 * ch},{b - 2 * ch},0.01]);"
                f" translate([{x},{y},{ch}]) cube([{b},{b},{h - ch}]); }}"
            )
            cut = (
                f"translate([{cx},{cy},-1]) cylinder(d={hd},h={h + 2},$fn=64);"
                f"translate([{cx},{cy},-0.01]) cylinder(d1={hd + 2 * ch + 0.02},d2={hd},h={ch + 0.01},$fn=64);"
            )
        else:
            body = f"translate([{x},{y},0]) cube([{b},{b},{h}]);"
            cut = f"translate([{cx},{cy},-1]) cylinder(d={hd},h={h + 2},$fn=64);"
        objs.append(
            PlateObject(
                name,
                "elephant_foot",
                stl=openscad_stl(f"difference() {{ {body} {cut} }}"),
                stl_bbox=(x, y, 0.0, x + b, y + b, h),
                params={
                    **_cell_params("elephant_foot"),
                    "block_mm": b,
                    "hole_d_mm": hd,
                    "chamfer_mm": ch,
                },
                read=f"{b:g} mm block with a Ø{hd:g} hole, "
                + (
                    f"{ch:g} mm 45° chamfer on the bottom edges (outside and in the hole)"
                    if ch
                    else "no chamfer"
                )
                + f". Measure the width at the first layer and at the top (both should read {b:g}) and the hole "
                "at the bottom: the plain block's flare is the elephant foot; the chamfer should take it away.",
            )
        )
        labels.append((_hundredths(ch), x - t[0] + b / 2))
    objs.append(
        _front_labels(
            "ef_labels",
            t,
            labels,
            "Labels: the first-layer chamfer of the block behind, in hundredths of a mm.",
        )
    )
    return tuple(objs), ()


def _screw(spot: str, holes: tuple[float, ...], m: int) -> Objs:
    t = spot_cell(spot)
    yc = t[1] + FIT_Y
    cuts = " ".join(
        f"translate([{t[0] + x},{yc},-1]) cylinder(d={d},h={SCREW_H + 2},$fn=64);"
        for x, d in zip(XS3, holes, strict=True)
    )
    marker = scad_cubes(label_boxes(str(m), t[0] + 4.0, t[1] + 22.0, SCREW_H))
    labels = [(_tenths(d), x) for x, d in zip(XS3, holes, strict=True)]
    return (
        _tile_obj(
            spot,
            "fit",
            t,
            SCREW_H,
            marker,
            cuts,
            labels,
            params={
                **_cell_params(spot),
                "screw": f"M{m}",
                "holes_mm": ",".join(f"{d:g}" for d in holes),
            },
            items=tuple(
                Item(text, _circle(t[0] + x, yc, d, 0.0, SCREW_H), {"diameter_mm": d})
                for (text, x), d in zip(labels, holes, strict=True)
            ),
            read=f"M{m} holes (the raised {m} at the back), label = Ø in tenths of a mm, {SCREW_H:g} mm deep. Drive an "
            f"M{m} screw into each: the largest that still cuts a thread that holds is the tap size, the smallest "
            "the screw slides through without play is the clearance size. New sizes between the first plate's "
            "(M3 2.5/3.2/3.4, M4 3.3/4.3/4.5).",
            record="tap/clearance Ø → design notes",
        ),
    ), ()


def _slots(t: Cell) -> Objs:
    cuts = " ".join(
        f"translate([{t[0] + x - w / 2},{t[1] + 10.0},{SLOT_H - SLOT_DEPTH}]) cube([{w},{SLOT_LEN},{SLOT_DEPTH + 1}]);"
        for x, w in zip(XS3, SLOTS, strict=True)
    )
    labels = [(_hundredths(w - INSERT[2]), x) for x, w in zip(XS3, SLOTS, strict=True)]
    return (
        _tile_obj(
            "slots",
            "fit",
            t,
            SLOT_H,
            "",
            cuts,
            labels,
            params={
                **_cell_params("slots"),
                "slot_widths_mm": ",".join(f"{w:g}" for w in SLOTS),
                "tab_mm": INSERT[2],
            },
            items=tuple(
                Item(
                    text,
                    (
                        t[0] + x - w / 2,
                        t[1] + 10.0,
                        SLOT_H - SLOT_DEPTH,
                        t[0] + x + w / 2,
                        t[1] + 10.0 + SLOT_LEN,
                        SLOT_H,
                    ),
                    {"slot_mm": w},
                )
                for (text, x), w in zip(labels, SLOTS, strict=True)
            ),
            read=f"Slots {', '.join(f'{w:g}' for w in SLOTS)} mm wide, label = width over the {INSERT[2]:g} mm tab in "
            "hundredths (5 = 2.05). Push the printed tab (front row, next to the bridge) into each: the narrowest "
            "it enters by hand is the sliding fit; the first plate's 2.1 was the narrowest tried.",
        ),
    ), ()


def _hinges(t: Cell) -> Objs:
    objs: list[PlateObject] = []
    labels: list[tuple[str, float]] = []
    length, leaf, r = HINGE_L, HINGE_LEAF, HINGE_R  # the geometry hinge_scad draws
    for g, dx in zip(HINGES, (2.5, 16.0), strict=True):
        hx, ay = t[0] + dx, t[1] + FEAT_Y + leaf
        objs.append(
            PlateObject(
                f"hinge_{_hundredths(g)}",
                "hinge",
                stl=openscad_stl(hinge_scad(hx, ay, g)),
                stl_bbox=(hx, ay - leaf, 0.0, hx + length, ay + leaf, 2 * r),
                params={**_cell_params("hinge"), "gap_mm": g},
                read=f"Print-in-place hinge, gap {g:g} mm (label in hundredths): fold it; the smallest gap that turns "
                "freely after a firm first twist is the print-in-place clearance (the first plate had 0.3/0.4).",
            )
        )
        labels.append((_hundredths(g), dx + length / 2))
    objs.append(
        _front_labels(
            "hinge_labels",
            t,
            labels,
            "Labels: the hinge gap behind, in hundredths of a mm.",
        )
    )
    return tuple(objs), ()


def _thin(spot: str, along: str) -> Objs:
    t = spot_cell(spot)
    walls: list[Box] = []
    for x, w in zip(XS3, THIN, strict=True):
        cx = t[0] + x
        if along == "Y":
            walls.append(
                (
                    cx - w / 2,
                    t[1] + 9.0,
                    0.0,
                    cx + w / 2,
                    t[1] + 9.0 + THIN_LEN_Y,
                    BASE_H + THIN_H,
                )
            )
        else:
            cy = t[1] + 15.0
            walls.append(
                (
                    cx - THIN_LEN_X / 2,
                    cy - w / 2,
                    0.0,
                    cx + THIN_LEN_X / 2,
                    cy + w / 2,
                    BASE_H + THIN_H,
                )
            )
    labels = [(_hundredths(w), x) for x, w in zip(XS3, THIN, strict=True)]
    return (
        _tile_obj(
            spot,
            "thin_wall",
            t,
            BASE_H,
            scad_cubes(walls),
            "",
            labels,
            top=BASE_H + THIN_H,
            params={
                **_cell_params(spot),
                "walls_mm": ",".join(f"{w:g}" for w in THIN),
                "along": along,
            },
            items=tuple(
                Item(text, (b[0], b[1], BASE_H, b[3], b[4], b[5]), {"wall_mm": w})
                for (text, _), w, b in zip(labels, THIN, walls, strict=True)
            ),
            read=f"Thin walls along {along} on a {BASE_H:g} mm tile, label = thickness in hundredths of a mm. A missing "
            "wall = the slicer dropped it; measure the ones that printed (the first plate printed 0.3 along Y "
            "but lost it along X).",
        ),
    ), ()


def _stringing(ctx: PlateContext, t: Cell) -> Objs:
    cy = t[1] + 18.0
    pillars = " ".join(
        f"translate([{t[0] + x},{cy},0]) cylinder(d={STRING_D},h={STRING_H},$fn=48);"
        for x in (7.0, 21.5)
    )
    text = f"{ctx.retract:g}"
    return (
        _tile_obj(
            "stringing",
            "retraction",
            t,
            BASE_H,
            pillars,
            "",
            [(text, TILE / 2)],
            top=STRING_H,
            params={
                **_cell_params("stringing"),
                "retraction_mm": ctx.retract,
                "pillar_h_mm": STRING_H,
            },
            read=f"Two Ø{STRING_D:g} mm pillars {21.5 - 7.0 - STRING_D:g} mm apart, printed at the preset retraction "
            f"({text} mm = the label). Strings or hairs between them = more retraction or a drier spool; the "
            "first plate's tower showed single strings at every length.",
        ),
    ), ()


def _top(t: Cell) -> Objs:
    return (
        _tile_obj(
            "top_surface",
            "top_surface",
            t,
            TOP_H,
            "",
            "",
            [("3", TILE / 2)],
            params={
                **_cell_params("top"),
                "top_layers": 3,
                "sparse_infill": "15%",
                "height_mm": TOP_H,
            },
            read="Top skin (3 layers, label 3) over 15 % sparse infill with the user's flow 0.931: smooth, closed "
            "lines are good; pillowing or gaps mean more top layers or flow, ridges less flow.",
        ),
    ), ()


# ---- footprint and layout --------------------------------------------------------------------


def union_area(rects: list[Rect]) -> float:
    """Area of the union of axis-aligned rectangles (sweep over x)."""
    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    total = 0.0
    for xa, xb in zip(xs, xs[1:], strict=False):
        spans = sorted((r[1], r[3]) for r in rects if r[0] <= xa and r[2] >= xb)
        covered, end = 0.0, -math.inf
        for a, b in spans:
            if b <= end:
                continue
            covered += b - max(a, end)
            end = b
        total += covered * (xb - xa)
    return total


def footprint(plate: Plate, line_w: float) -> tuple[float, float, float]:
    """(mm², share of the bed, share of the usable bed): the bed area under objects (their base
    outlines, holes not subtracted), brims and hand-written blocks."""
    rects: list[Rect] = []
    for o in plate.objects:
        rects += o.base_rects()
        width = brim_width(o, plate.brim)
        if width > 0:
            rects += brim_rects(o, width, line_w)
    rects += [box_rect(c.bbox()) for c in plate.custom]
    area = union_area(rects)
    usable = (BED[0] - 2 * EDGE) * (BED[1] - 2 * EDGE)
    return area, area / (BED[0] * BED[1]), area / usable


def _check_layout(plate: Plate, line_w: float) -> None:
    """Inside the bed and ≥ 1 mm between different objects (bbox and brim outlines)."""
    outlines: list[tuple[str, list[Rect]]] = []
    for o in plate.objects:
        rs = [box_rect(o.bbox())]
        width = brim_width(o, plate.brim)
        if width > 0:
            rs += brim_rects(o, width, line_w)
        outlines.append((o.name, rs))
    outlines += [(c.name, [box_rect(c.bbox())]) for c in plate.custom]
    for i, (na, ra) in enumerate(outlines):
        for a in ra:
            if not (a[0] >= 0 and a[2] <= BED[0] and a[1] >= 0 and a[3] <= BED[1]):
                raise GenError(f"dense2 plate: {na} leaves the bed ({a})")
        for nb, rb in outlines[i + 1 :]:
            for a in ra:
                for r in rb:
                    if (
                        a[0] < r[2] + 1
                        and r[0] < a[2] + 1
                        and a[1] < r[3] + 1
                        and r[1] < a[3] + 1
                    ):
                        raise GenError(
                            f"dense2 plate: {na} and {nb} are closer than 1 mm"
                        )


def build(ctx: PlateContext) -> Plate:
    if tuple(ctx.p.bed) != BED:
        raise GenError(
            f"the dense2 plate is laid out for a {BED[0]:g}x{BED[1]:g} bed, not {ctx.p.bed}"
        )
    if not any(ctx.wants(t) for t in TESTS if t != "temp"):
        raise GenError(
            f"{PLATE_ID}: --only/--skip must keep a test besides temp (the plate is sliced by layer "
            "around them); the temperature tower alone is `gen temp`"
        )
    makers: dict[str, Callable[[str], Objs]] = {
        "fiducials": _fiducial,
        "mvs": lambda _s: _mvs(ctx),
        "brim": lambda s: (
            _brim_bridge(spot_cell(s))
            if s == "brim_bridge"
            else _brim_overhang(spot_cell(s))
        ),
        "hole_quality": lambda s: _hole_quality(s, s.rsplit("_", 1)[1].upper()),
        "ovality": _ovality,
        "fit": lambda s: (
            _fit(s, FIT_PLAIN, 0.0)
            if s == "fit_plain"
            else _fit(s, FIT_CHAMFERED, CHAMFER)
        ),
        "elephant_foot": lambda s: _elephant_foot(spot_cell(s)),
        "screw": lambda s: (
            _screw(s, SCREW_M3, 3) if s == "screw_m3" else _screw(s, SCREW_M4, 4)
        ),
        "slots": lambda s: _slots(spot_cell(s)),
        "hinge": lambda s: _hinges(spot_cell(s)),
        "thin_wall": lambda s: _thin(s, "Y" if s == "thin_y" else "X"),
        "stringing": lambda s: _stringing(ctx, spot_cell(s)),
        "top_surface": lambda s: _top(spot_cell(s)),
        "temp": lambda s: _temp(ctx, spot_cell(s)),
    }
    objects: list[PlateObject] = []
    custom: list[CustomBlock] = []
    for test in TESTS:
        if not ctx.wants(test):
            continue
        if test == "first_layer":
            for col, row in patch_cells():
                objs, _ = _patch(col, row)
                objects += objs
            continue
        for spot in SPOTS[test]:
            objs, blocks = makers[test](spot)
            objects += objs
            custom += blocks
    plate = Plate(
        PLATE_ID,
        TITLE,
        tuple(objects),
        custom=tuple(custom),
        layer_process=PROCESS,
        seq_process={
            "sparse_infill_density": "15%",
            "layer_height": filament.SEQ_LAYER_H,
        },
        seq_filament={"slow_down_layer_time": SEQ_MIN_LAYER_S},
        brim=BRIM,
    )
    line_w = ctx.ex.line_w
    _check_layout(plate, line_w)
    area, bed_share, usable_share = footprint(plate, line_w)
    return replace(
        plate,
        notes=(
            "Tests: " + ", ".join(t for t in TESTS if ctx.wants(t)) + ".",
            f"Footprint: {area / 100:.0f} cm² = {bed_share * 100:.0f} % of the bed, {usable_share * 100:.0f} % of "
            f"the usable {BED[0] - 2 * EDGE:g}x{BED[1] - 2 * EDGE:g} mm (base outlines, brims, hand-written strip).",
            "Grid: 7x7 cells of 30 mm, column 0 at the left, row 0 at the front; each sample's label sits at the "
            "front of its cell. Units: gaps, walls, chamfers and slot clearances in hundredths of a mm; screw "
            "holes in tenths; other diameters, the bridge and the tab in mm; the overhang in degrees.",
            "Variants: polyholes and hole compensation are drawn into the meshes (Orca's CLI takes no per-object "
            "settings for STLs); the AF tile's G-code comes from a second slice with arc fitting on.",
            "Photograph top-down with light from one side (raking light shows the first layer, the labels and "
            "the hole edges), then the bridge, overhang, hinges and pillars from the front.",
        ),
    )
