"""calibration_plates/filament.py — plate 1 "filament": everything a new spool needs, on one bed.

WHAT (tests, ids for --only/--skip):
  pa          PA lines (label = PA x 1000), hand-written on the first layer
  mvs         max-volumetric-speed strip (label = mm³/s), hand-written
  flow        5 flow tiles, 15x15 mm, 5 solid layers, M221 per tile on every layer
  bridge      bridges 8/11/14 mm
  overhang    overhangs 45/50/55/60°
  retraction  retraction tower (length grows per block; the band transform rewrites only its own
              retractions), printed by layer with the objects above
  temp        temperature tower 230→200 °C, alone in the sequential phase at 0.3 mm layers
  Every object sets its parameter on entry and restores it on exit (calibration_plate hooks), so
  no test bleeds into another. Each test is a section; the selected sections are packed from the
  front of the bed (lowest Y first), so a skipped test leaves no hole. The tower of the
  sequential phase goes extruder_clearance_radius behind the front sections.
SIZES: every object is the smallest that still reads well on a photo: flow tiles 1 mm thick,
  bridges/overhangs 3–5 mm high, towers of 2.4 mm blocks.
WHY: express calibration of a new spool in one short print (docs/printops/express-calibration.md).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from slicing.calibration_gen import (
    COMBO_LABEL,
    COMBO_SEGMENTS,
    COMBO_SPEEDS,
    FLOW_STEPS,
    Box,
    GenError,
    TowerShape,
    pa_label,
    pa_label_width,
    _first,
    pa_lines,
    prism_stl,
    temp_steps,
    temp_tower_boxes,
)
from slicing.calibration_plate import (
    Band,
    CustomBlock,
    Item,
    Plate,
    PlateContext,
    PlateObject,
    _union,
    custom_lines,
    label_lines,
    label_width,
    scale_retractions,
)

PLATE_ID = "plate1"
TITLE = "plate 1: filament"
TESTS = {
    "pa": "pressure advance lines",
    "mvs": "max volumetric speed strip",
    "flow": "flow tiles",
    "bridge": "bridges",
    "overhang": "overhangs",
    "retraction": "retraction tower",
    "temp": "temperature tower",
}

MARGIN = 6.0  # from the bed edge
GAP = 6.0  # between sections
LABEL_DY = 5.0  # a row's bed labels take this much in front of its objects
# PA lines
PA_PITCH = 4.5
PA_SEGMENTS = COMBO_SEGMENTS
# max volumetric speed strip
MVS_VALUES = tuple(range(8, 25, 2))  # mm³/s, one segment each
MVS_SEG_L = 10.0
MVS_W, MVS_H = 0.5, 0.3  # line width, test layer height
MVS_LINES = 6
MVS_SPACING = 0.45
MVS_TEST_LAYERS = 2
MVS_BASE_SPEED = 25.0  # mm/s for the first layer of the strip
MVS_LABEL_GAP = 1.5
# flow tiles: 15x15, 5 layers of 0.2 — all solid with the layer phase's shells
TILE = (15.0, 15.0, 1.0)
TILE_GAP = 4.0
# bridges: two pillars + slab; gap = bridge length (print-preflight bridges <= 15 mm)
BRIDGE_GAPS = (8, 11, 14)
BRIDGE_PILLAR = (4.0, 6.0, 3.0)  # x, y, z
BRIDGE_SLAB = 0.8
# overhangs: wedges whose right face leans out by the angle from vertical (<= 60°: carried at
# 0.2 mm layers by the 45° reach + half a line of print-preflight)
OVERHANG_ANGLES = (45, 50, 55, 60)
OVERHANG_BASE, OVERHANG_H, OVERHANG_DEPTH = 4.0, 5.0, 6.0
OBJ_GAP = 4.0
# Towers: alone on its layers a small tower prints at Orca's minimum layer time (8 s for this
# PLA), so height costs minutes; blocks are 2.4 mm (12 layers by layer, 8 at 0.3 mm).
RETRACT_LENGTHS = tuple(
    round(0.2 * i, 1) for i in range(2, 7)
)  # 0.4 … 1.2 mm, one block each
RETRACT_SHAPE = TowerShape(
    pillar=5.0,
    gap=10.0,
    block_h=2.4,
    slab_h=0.8,
    base=(24.0, 8.0, 0.6),
    digit=(1.6, 3.0, 0.6, 0.6),
)
SEQ_LAYER_H = 0.3
TEMP_SHAPE = TowerShape(
    pillar=5.0,
    gap=8.0,
    block_h=2.4,
    slab_h=0.6,
    base=(24.0, 8.0, 0.8),
    digit=(1.6, 3.0, 0.6, 0.6),
)


@dataclass(frozen=True)
class Section:
    """One test's footprint (width x depth, labels included) and how to build it at (x0, y0)."""

    test: str
    width: float
    depth: float
    sequential: bool
    build: Callable[
        [float, float], tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]
    ]


def pack(
    sections: list[Section], bed: tuple[float, float], radius: float
) -> list[tuple[Section, float, float]]:
    """Bottom-left skyline packing: each section at the lowest, then leftmost free spot. Front
    (layer) sections first; sequential ones behind the front's deepest point + `radius` and
    `radius` apart from each other (K1 gantry clearance)."""
    placed: list[tuple[Section, float, float]] = []

    def fits(
        x: float,
        y: float,
        w: float,
        d: float,
        among: list[tuple[Section, float, float]],
        gap: float,
    ) -> bool:
        if x + w > bed[0] - MARGIN or y + d > bed[1] - MARGIN:
            return False
        return all(
            x >= px + s.width + gap
            or x + w + gap <= px
            or y >= py + s.depth + gap
            or y + d + gap <= py
            for s, px, py in among
        )

    def place(
        s: Section, among: list[tuple[Section, float, float]], y_min: float, gap: float
    ) -> None:
        xs = sorted({MARGIN} | {px + q.width + gap for q, px, _ in among})
        ys = sorted(
            {y_min}
            | {py + q.depth + gap for q, _, py in among if py + q.depth + gap > y_min}
        )
        for y in ys:
            for x in xs:
                if fits(x, y, s.width, s.depth, among, gap):
                    placed.append((s, x, y))
                    among.append((s, x, y))
                    return
        raise GenError(
            f"plate 1: no room left on the bed for {s.test} ({s.width:g} x {s.depth:g} mm)"
        )

    front: list[tuple[Section, float, float]] = []
    for s in (s for s in sections if not s.sequential):
        place(s, front, MARGIN, GAP)
    back: list[tuple[Section, float, float]] = []
    y_back = max((py + s.depth for s, _, py in front), default=MARGIN) + radius
    for s in (s for s in sections if s.sequential):
        place(s, back, y_back, radius)
    return placed


def _pa(ctx: PlateContext) -> Section:
    _, lh, _ = COMBO_LABEL
    label_w = pa_label_width(ctx.pa) + 2.0
    n = len(ctx.pa)

    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        yl = y0 + lh / 2
        xs = x0 + label_w
        body = pa_lines(
            x0,
            yl,
            ctx.ex,
            ctx.p.stock_pa,
            ctx.accel,
            values=ctx.pa,
            pitch=PA_PITCH,
            segments=PA_SEGMENTS,
        )
        half = ctx.ex.line_w / 2
        items = tuple(
            Item(
                pa_label(pa),
                (
                    xs,
                    yl + i * PA_PITCH - half,
                    0.0,
                    xs + sum(PA_SEGMENTS),
                    yl + i * PA_PITCH + half,
                    ctx.ex.layer_h,
                ),
                {"pressure_advance": pa},
            )
            for i, pa in enumerate(ctx.pa)
        )
        return (), (
            CustomBlock(
                "pa_lines",
                "pressure_advance",
                tuple(body),
                items,
                {
                    "segments_mm": "/".join(f"{s:g}" for s in PA_SEGMENTS),
                    "speeds_mm_s": "/".join(
                        f"{v:g}"
                        for v in (COMBO_SPEEDS[0], COMBO_SPEEDS[1], COMBO_SPEEDS[0])
                    ),
                    "stock_pa": ctx.p.stock_pa,
                },
                "PA lines, front to back. Each line: slow - fast - slow. The right PA keeps the width even at "
                "both speed changes (too low: thin start, blob after the fast part; too high: blob at the start, "
                "gap after). PA = label / 1000.",
                "3d calibrate record pressure_advance --printer PRINTER --values pressure_advance=VALUE --method pa_lines",
            ),
        )

    return Section(
        "pa", label_w + sum(PA_SEGMENTS), (n - 1) * PA_PITCH + lh, False, build
    )


def _mvs(
    ctx: PlateContext,
    values: tuple[int, ...] = MVS_VALUES,
    lines: int = MVS_LINES,
    labels: bool = True,
) -> Section:
    """A solid strip: first layer slow, then MVS_TEST_LAYERS layers whose speed per segment gives
    `values` mm³/s. Zigzag lines along X; segments keep their speed in both directions. Without
    `labels` the caller labels the segments (Section.depth is then the strip alone)."""
    strip_d = (lines - 1) * MVS_SPACING + MVS_W
    lh = COMBO_LABEL[1]

    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        ex = ctx.ex
        xs = [x0 + k * MVS_SEG_L for k in range(len(values) + 1)]
        ys = [y0 + MVS_W / 2 + j * MVS_SPACING for j in range(lines)]
        speeds = [v / (MVS_W * MVS_H) for v in values]
        out = [
            f"G1 Z{ex.layer_h + ex.lift:.3f} F1200",
            f"G1 X{xs[0]:.3f} Y{ys[0]:.3f} F12000",
            f"G1 Z{ex.layer_h:.3f} F1200",
            f"G1 E{ex.retract:.3f} F{ex.retract_f:.0f}",
        ]
        x, y = xs[0], ys[0]
        layers = [(ex.layer_h, ex.layer_h, False)] + [
            (ex.layer_h + (i + 1) * MVS_H, MVS_H, True) for i in range(MVS_TEST_LAYERS)
        ]
        rows = list(ys)
        for n, (z, h, test) in enumerate(layers):
            epm = ctx.e_per_mm(MVS_W, h)
            if n:
                out.append(f"G1 Z{z:.3f} F1200")
                rows.reverse()
            if test and n == 1:
                out.append("M106 S255")
            for ry in rows:
                if ry != y:
                    out.append(
                        f"G1 Y{ry:.3f} E{abs(ry - y) * epm:.5f} F{MVS_BASE_SPEED * 60:.0f}"
                    )
                    y = ry
                forward = abs(x - xs[0]) < 1e-6
                order = (
                    range(len(values)) if forward else range(len(values) - 1, -1, -1)
                )
                for k in order:
                    nx = xs[k + 1] if forward else xs[k]
                    v = speeds[k] if test else MVS_BASE_SPEED
                    out.append(f"G1 X{nx:.3f} E{abs(nx - x) * epm:.5f} F{v * 60:.0f}")
                    x = nx
        top = layers[-1][0]
        out += [
            "M106 S0",
            f"G1 E-{ex.retract:.3f} F{ex.retract_f:.0f}",
            f"G1 Z{top + ex.lift:.3f} F1200",
        ]
        label_y = y0 + strip_d + MVS_LABEL_GAP
        for k, v in enumerate(values if labels else ()):
            out += label_lines(
                ctx, str(v), xs[k] + MVS_SEG_L / 2 - label_width(str(v)) / 2, label_y
            )
        items = tuple(
            Item(
                str(v),
                (xs[k], y0, 0.0, xs[k + 1], y0 + strip_d, top),
                {"max_volumetric_speed": v, "speed_mm_s": round(speeds[k], 1)},
            )
            for k, v in enumerate(values)
        )
        return (), (
            CustomBlock(
                "mvs_strip",
                "max_volumetric_speed",
                custom_lines(ctx, out, "max volumetric speed strip"),
                items,
                {
                    "line_width_mm": MVS_W,
                    "layer_mm": MVS_H,
                    "test_layers": MVS_TEST_LAYERS,
                },
                f"Strip, left to right; label ({'behind' if labels else 'in front of'} it) = mm³/s. The top stays solid and glossy while the hotend "
                "keeps up; past its limit the lines thin out, gaps and a matte, rough surface appear. "
                "max_volumetric_speed = the last clean segment (use ~90 % of it in profiles).",
                "3d calibrate record max_volumetric_speed --printer PRINTER --values max_volumetric_speed=VALUE "
                "--method mvs_strip",
            ),
        )

    return Section(
        "mvs",
        len(values) * MVS_SEG_L,
        strip_d + (MVS_LABEL_GAP + lh if labels else 0.0),
        False,
        build,
    )


def _flow(ctx: PlateContext) -> Section:
    tx, ty, tz = TILE
    ratio = _first(ctx.fil, "filament_flow_ratio")

    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        objs = []
        for i, step in enumerate(FLOW_STEPS):
            x = x0 + i * (tx + TILE_GAP)
            box = (x, y0 + LABEL_DY, 0.0, x + tx, y0 + LABEL_DY + ty, tz)
            pct = 100 + step
            fr = round(ratio * pct / 100, 4)
            objs.append(
                PlateObject(
                    f"flow_{'m' if step < 0 else 'p'}{abs(step):02d}",
                    "flow_ratio",
                    (box,),
                    enter=(f"M221 S{pct}",),
                    leave=("M221 S100",),
                    params={"m221_pct": pct, "flow_ratio": fr},
                    items=(Item(str(pct), box, {"flow_ratio": fr}),),
                    bed_label=(str(pct), x + tx / 2, y0),
                    read=f"Tile {i + 1} of 5 (label {pct} = {pct} % of the preset flow ratio {ratio:g}). The right "
                    "flow: top lines closed and smooth; too little: gaps; too much: ridges and a rough top.",
                    record="3d calibrate record flow_ratio --printer PRINTER --values flow_ratio=VALUE --method flow_tiles",
                )
            )
        return tuple(objs), ()

    n = len(FLOW_STEPS)
    return Section("flow", n * tx + (n - 1) * TILE_GAP, LABEL_DY + ty, False, build)


def _bridge(gaps: tuple[int, ...] = BRIDGE_GAPS, prefix: str = "bridge") -> Section:
    px, py, pz = BRIDGE_PILLAR

    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        objs = []
        x, yb = x0, y0 + LABEL_DY
        for g in gaps:
            w = 2 * px + g
            boxes = (
                (x, yb, 0.0, x + px, yb + py, pz),
                (x + px + g, yb, 0.0, x + w, yb + py, pz),
                (x, yb, pz, x + w, yb + py, pz + BRIDGE_SLAB),
            )
            objs.append(
                PlateObject(
                    f"{prefix}_{g}",
                    "bridge",
                    boxes,
                    params={"bridge_mm": g},
                    items=(
                        Item(
                            str(g),
                            (x + px, yb, pz, x + px + g, yb + py, pz + BRIDGE_SLAB),
                            {"bridge_mm": g},
                        ),
                    ),
                    bed_label=(str(g), x + w / 2, y0),
                    read=f"{g} mm bridge: look at its underside from the front/below — straight, tight lines are good; "
                    "sagging or broken lines mean bridge flow/speed/fan need work.",
                )
            )
            x += w + OBJ_GAP
        return tuple(objs), ()

    width = sum(2 * px + g for g in gaps) + (len(gaps) - 1) * OBJ_GAP
    return Section(prefix, width, LABEL_DY + py, False, build)


def _reach(a: float) -> float:
    return OVERHANG_H * math.tan(math.radians(a))


def _overhang(angles: tuple[int, ...] = OVERHANG_ANGLES) -> Section:
    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        objs = []
        x, yb = x0, y0 + LABEL_DY
        for a in angles:
            reach = _reach(a)
            profile = [
                (x, 0.0),
                (x + OVERHANG_BASE, 0.0),
                (x + OVERHANG_BASE + reach, OVERHANG_H),
                (x, OVERHANG_H),
            ]
            bbox = (
                x,
                yb,
                0.0,
                x + OVERHANG_BASE + reach,
                yb + OVERHANG_DEPTH,
                OVERHANG_H,
            )
            objs.append(
                PlateObject(
                    f"overhang_{a}",
                    "overhang",
                    stl=prism_stl(profile, yb, yb + OVERHANG_DEPTH),
                    stl_bbox=bbox,
                    params={"overhang_deg": a},
                    items=(Item(str(a), bbox, {"overhang_deg": a}),),
                    bed_label=(str(a), x + OVERHANG_BASE / 2, y0),
                    read=f"{a}° overhang (right face, from vertical): the steepest angle whose face stays smooth "
                    "without curled or drooping edges is the filament's overhang limit with these settings.",
                )
            )
            x += OVERHANG_BASE + reach + OBJ_GAP
        return tuple(objs), ()

    width = sum(OVERHANG_BASE + _reach(a) for a in angles) + (len(angles) - 1) * OBJ_GAP
    return Section("overhang", width, LABEL_DY + OVERHANG_DEPTH, False, build)


def _block_bands(
    shape: TowerShape,
    n: int,
    gcodes: list[tuple[str, ...]],
    params: list[dict[str, float | str]],
    transforms: list[Callable[[str], str]] | None = None,
) -> tuple[Band, ...]:
    return tuple(
        Band(
            shape.block_floor(i),
            shape.block_floor(i + 1) if i < n - 1 else shape.block_floor(n) + 1.0,
            gcodes[i],
            params[i],
            transforms[i] if transforms else None,
        )
        for i in range(n)
    )


def _block_items(
    boxes: list[Box],
    shape: TowerShape,
    labels: list[str],
    params: list[dict[str, float | str]],
) -> tuple[Item, ...]:
    """One item per tower block: the tower's footprint between the block's floor and the next."""
    x0, y0, _, x1, y1, _ = _union(boxes)
    return tuple(
        Item(
            label,
            (x0, y0, shape.block_floor(i), x1, y1, shape.block_floor(i + 1)),
            params[i],
        )
        for i, label in enumerate(labels)
    )


def _retraction(ctx: PlateContext) -> Section:
    bx, by, _ = RETRACT_SHAPE.base

    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        steps = [
            round(length * 10) for length in RETRACT_LENGTHS
        ]  # labels: tenths of a mm
        boxes = temp_tower_boxes(steps, x0 + bx / 2, y0 + by / 2, RETRACT_SHAPE)
        base = ctx.retract
        bands = _block_bands(
            RETRACT_SHAPE,
            len(steps),
            [()] * len(steps),
            [{"retraction_length": length} for length in RETRACT_LENGTHS],
            [scale_retractions(length / base) for length in RETRACT_LENGTHS],
        )
        return (
            PlateObject(
                "retraction_tower",
                "retraction",
                tuple(boxes),
                bands=bands,
                params={
                    "preset_retraction_mm": base,
                    "block_mm": RETRACT_SHAPE.block_h,
                },
                items=_block_items(
                    boxes,
                    RETRACT_SHAPE,
                    [str(s) for s in steps],
                    [{"retraction_length": length} for length in RETRACT_LENGTHS],
                ),
                read="Retraction tower, blocks bottom to top; the label on each block's floor is the retraction "
                f"length in tenths of a mm ({steps[0]} = {RETRACT_LENGTHS[0]} mm … {steps[-1]} = "
                f"{RETRACT_LENGTHS[-1]} mm). Strings between the pillars shrink as retraction grows; take the "
                "shortest length without strings (longer only adds wear and blobs).",
                record="3d calibrate record retraction --printer PRINTER --values retraction_length=VALUE "
                "--method retraction_tower",
            ),
        ), ()

    return Section("retraction", bx, by, False, build)


def _temp(ctx: PlateContext) -> Section:
    bx, by, _ = TEMP_SHAPE.base

    def build(
        x0: float, y0: float
    ) -> tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]:
        steps = temp_steps(ctx.material)
        boxes = temp_tower_boxes(steps, x0 + bx / 2, y0 + by / 2, TEMP_SHAPE)
        bands = _block_bands(
            TEMP_SHAPE,
            len(steps),
            [(f"M104 S{t}",) for t in steps],
            [{"nozzle_temp": t} for t in steps],
        )
        return (
            PlateObject(
                "temp_tower",
                "temp_tower",
                tuple(boxes),
                phase="sequential",
                bands=bands,
                before=(f"M109 S{steps[0]}",),
                after=(f"M109 S{ctx.base_temp}",),
                params={
                    "from_c": steps[0],
                    "to_c": steps[-1],
                    "step_c": steps[0] - steps[1],
                },
                items=_block_items(
                    boxes,
                    TEMP_SHAPE,
                    [str(t) for t in steps],
                    [{"nozzle_temp": t} for t in steps],
                ),
                read="Temperature tower (printed last, alone), blocks bottom to top, label = °C on each block's "
                "floor. min = coldest block that still bonds (try to snap it) and bridges cleanly; max = hottest "
                "without strings or droop; best = the cleanest surface and bridge.",
                record="3d calibrate record temp_tower --filament ID --values nozzle_temp_min=MIN,"
                "nozzle_temp_best=BEST,nozzle_temp_max=MAX --method temp_tower",
            ),
        ), ()

    return Section("temp", bx, by, True, build)


# The tests Orca slices by layer: the plate needs at least one, the hand-written lines (pa, mvs)
# and the sequential tower (temp) are spliced into that slice.
LAYER_TESTS = ("flow", "bridge", "overhang", "retraction")


def build(ctx: PlateContext) -> Plate:
    if not any(ctx.wants(t) for t in LAYER_TESTS):
        raise GenError(
            f"{PLATE_ID}: --only/--skip must keep one of {', '.join(LAYER_TESTS)} (the plate is sliced "
            "around them); alone, PA lines are `gen combo` and the temperature tower `gen temp`"
        )
    makers = {
        "pa": _pa,
        "mvs": _mvs,
        "flow": _flow,
        "bridge": lambda _c: _bridge(),
        "overhang": lambda _c: _overhang(),
        "retraction": _retraction,
        "temp": _temp,
    }
    sections = [makers[t](ctx) for t in TESTS if ctx.wants(t)]
    objects: list[PlateObject] = []
    custom: list[CustomBlock] = []
    for s, x, y in pack(sections, ctx.p.bed, ctx.clearance()[1]):
        objs, blocks = s.build(x, y)
        objects += objs
        custom += blocks
    return Plate(
        PLATE_ID,
        TITLE,
        tuple(objects),
        custom=tuple(custom),
        layer_process={
            "top_shell_layers": 4,
            "bottom_shell_layers": 3,
            "sparse_infill_density": "15%",
            "brim_type": "no_brim",
        },
        seq_process={
            "sparse_infill_density": "15%",
            "brim_type": "no_brim",
            "layer_height": SEQ_LAYER_H,
        },
        restore=(
            "M221 S100",
            "M220 S100",
            f"SET_PRESSURE_ADVANCE ADVANCE={ctx.p.stock_pa:g}",
            f"SET_VELOCITY_LIMIT ACCEL={ctx.accel:g} ACCEL_TO_DECEL={ctx.accel / 2:g}",
        ),
        notes=(
            "Tests: "
            + ", ".join(t for t in TESTS if ctx.wants(t))
            + ". Order: hand-written lines and labels "
            "(first layer) → objects by layer → the temperature tower alone, last.",
            "Photograph the plate on the bed, top-down, light on, then the towers from the front.",
        ),
    )
