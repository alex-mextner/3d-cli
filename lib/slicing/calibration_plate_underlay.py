"""calibration_plate_underlay.py — puts a plate of the engine (calibration_plate.py) on the shared underlay (#320).

WHAT: apply_underlay(ctx, plate) turns a Plate declared on the bare bed into the same plate on the
  one-piece underlay (calibration_underlay.py gives the rectangles):
    sheet       a new first layer object `underlay`: the outline minus the cut-outs and the feet,
                1 layer (2 on a multi-head printer at `calibrations.underlay_layers` 2) thick
    on          a layer test is lifted by the underlay's height and carries a foot of that height
                under its base (one mesh: Orca drops objects to the bed, not parts), so it starts
                at the underlay's top; its Z bands and items move up with it
    cutout      a layer test stands on the bare bed from the first layer, in a hole of the sheet
    towers      a sequential object (the temperature tower) is sliced alone on the bed and its
                G-code is lifted by the underlay's height (shift_z): it starts on the finished sheet,
                which has no hole under it, and the underlay stays the first thing printed
    hand-written blocks (PA lines, MVS strip, labels) are lifted by the underlay's height and
                printed right after the underlay's own layers (splice, after_z)
    brims       none under an underlay: every covered object gets brim 0; only an object marked
                `off` (outside the underlay) keeps the Plate.brim / brim_width rule
WHY: one sheet to peel off, small tests that do not tip, and the sheet's bare cells as the
  first-layer map (tg#22330, #320).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import replace

from slicing.calibration_gen import Box, GenError, boxes_stl
from slicing.calibration_plate import (
    Band,
    CustomBlock,
    Item,
    Plate,
    PlateContext,
    PlateObject,
    Rect,
    _first,
)
from slicing.calibration_underlay import (
    SHEET_NAME,
    Underlay,
    UnderlayDecl,
    build_underlay,
    merge_stl,
    shift_stl,
    shift_z,
)

MIN_FOOT = (
    2.0  # mm: a foot narrower than this (a thin wall's base) is widened, it must print
)


def foot(r: Rect) -> Rect:
    """The foot under a base rectangle: the rectangle, widened about its centre to MIN_FOOT."""
    dx = max(0.0, (MIN_FOOT - (r[2] - r[0])) / 2)
    dy = max(0.0, (MIN_FOOT - (r[3] - r[1])) / 2)
    return (r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy)


def feet(o: PlateObject) -> list[Rect]:
    return [foot(r) for r in o.base_rects()]


def declare(
    objs: tuple[PlateObject, ...], test: str, decls: Mapping[str, UnderlayDecl]
) -> tuple[PlateObject, ...]:
    """The objects of `test` with the catalog's place on the underlay: `cutout` tests and movable
    objects stand on the bare bed; the others on the underlay."""
    if test not in decls:
        raise GenError(f"test {test!r} has no underlay declaration")
    d = decls[test]
    return tuple(
        replace(
            o,
            test=test,
            movable=o.movable or d.movable,
            underlay="cutout"
            if d.mode == "cutout" or o.movable or d.movable
            else o.underlay,
        )
        for o in objs
    )


def _shift_box(b: Box, dz: float) -> Box:
    return (b[0], b[1], b[2] + dz, b[3], b[4], b[5] + dz)


def _shift_meta(o: PlateObject, dz: float) -> PlateObject:
    """Bands and items of an object that now starts dz higher."""
    return replace(
        o,
        bands=tuple(
            replace(b, z0=b.z0 + dz, z1=b.z1 + dz) if isinstance(b, Band) else b
            for b in o.bands
        ),
        items=tuple(
            Item(it.label, _shift_box(it.bbox, dz), it.params) for it in o.items
        ),
    )


def _lift(o: PlateObject, ul: Underlay) -> PlateObject:
    """A layer test standing on the underlay: its geometry dz higher, a foot under each base."""
    dz = ul.height
    rects = o.base_rects()
    slabs = [(r[0], r[1], 0.0, r[2], r[3], dz) for r in feet(o)]
    if o.stl:
        if o.stl_bbox is None:
            raise GenError(f"{o.name}: an stl object needs stl_bbox")
        x0, y0, _, x1, y1, z1 = o.stl_bbox
        lifted = replace(
            o,
            stl=merge_stl(shift_stl(o.stl, dz), boxes_stl(slabs)),
            stl_bbox=(x0, y0, 0.0, x1, y1, z1 + dz),
        )
    else:
        lifted = replace(o, boxes=(*slabs, *(_shift_box(b, dz) for b in o.boxes)))
    return _shift_meta(replace(lifted, base=rects), dz)


def _shift_block(c: CustomBlock, dz: float) -> CustomBlock:
    return replace(
        c,
        lines=tuple(shift_z(list(c.lines), dz)),
        items=tuple(
            Item(it.label, _shift_box(it.bbox, dz), it.params) for it in c.items
        ),
    )


def _rect(b: Box) -> Rect:
    return (b[0], b[1], b[3], b[4])


RASTER = 0.5  # mm: resolution of a hand-written block's footprint
CLUSTER = 3.0  # mm: marks closer than twice this belong to one footprint (a label, a line group)
_WORD = re.compile(r"([XYE])(-?\d*\.?\d+)")


def block_rects(c: CustomBlock) -> list[Rect]:
    """Where a hand-written block really prints: its extruding moves, grouped into clusters (marks
    closer than 2 x CLUSTER belong together). The bbox of a block of scattered labels covers the
    whole bed and would swallow the cut-outs."""
    x = y = 0.0
    cells: set[tuple[int, int]] = set()
    for line in c.lines:
        if not line.startswith(("G1 ", "G0 ")):
            continue
        w = dict(_WORD.findall(line.partition(";")[0]))
        nx, ny = float(w.get("X", x)), float(w.get("Y", y))
        if float(w.get("E", 0.0)) > 0 and ("X" in w or "Y" in w):
            n = max(1, math.ceil(math.hypot(nx - x, ny - y) / (RASTER / 2)))
            cells.update(
                (
                    round((x + (nx - x) * k / n) / RASTER),
                    round((y + (ny - y) * k / n) / RASTER),
                )
                for k in range(n + 1)
            )
        x, y = nx, ny
    if not cells:
        raise GenError(f"{c.name}: a hand-written block without an extruding move")
    reach = round(CLUSTER / RASTER)
    out: list[Rect] = []
    todo = set(cells)
    while todo:
        stack = [todo.pop()]
        members = list(stack)
        while stack:
            i, j = stack.pop()
            near = [
                (a, b)
                for a in range(i - reach, i + reach + 1)
                for b in range(j - reach, j + reach + 1)
                if (a, b) in todo
            ]
            todo.difference_update(near)
            stack += near
            members += near
        out.append(
            (
                min(i for i, _ in members) * RASTER - RASTER / 2,
                min(j for _, j in members) * RASTER - RASTER / 2,
                max(i for i, _ in members) * RASTER + RASTER / 2,
                max(j for _, j in members) * RASTER + RASTER / 2,
            )
        )
    return out


def _check_feet(objs: list[PlateObject]) -> None:
    """Two tests' feet are one piece of underlay: they must not overlap (double extrusion)."""
    for i, a in enumerate(objs):
        for b in objs[i + 1 :]:
            for ra in feet(a):
                for rb in feet(b):
                    if (
                        min(ra[2], rb[2]) - max(ra[0], rb[0]) > 1e-6
                        and min(ra[3], rb[3]) - max(ra[1], rb[1]) > 1e-6
                    ):
                        raise GenError(
                            f"{a.name} and {b.name}: their feet on the underlay overlap; "
                            f"move them at least {MIN_FOOT:g} mm apart"
                        )


def apply_underlay(
    ctx: PlateContext, plate: Plate, layers: int
) -> tuple[Plate, Underlay]:
    """`plate` on the underlay of `layers` layers (see the module docstring)."""
    first_h = ctx.ex.layer_h
    layer_h = _first(ctx.proc, "layer_height")
    on = [o for o in plate.objects if o.underlay == "on"]
    cut = [o for o in plate.objects if o.underlay == "cutout"]
    towers = [
        o for o in plate.objects if o.phase == "sequential" and o.underlay != "off"
    ]
    for o in towers:
        if o.underlay != "on":
            raise GenError(
                f"{o.name}: a sequential object stands on the underlay (it is sliced alone and "
                "lifted), it cannot be a cutout"
            )
    covered = [o for o in plate.objects if o.underlay in ("on", "cutout")]
    # the outline covers whatever overhangs the base too (a leaning wedge, a bridge's span)
    footprints = [r for o in covered for r in (_rect(o.bbox()), *o.base_rects())]
    covered_objs = [o for o in covered if o.underlay == "on"]
    blocks = [r for c in plate.custom for r in block_rects(c)]
    layer_on = [o for o in on if o.phase == "layer"]
    cutouts = [r for o in cut for r in o.base_rects()]
    bases = [r for o in layer_on for r in feet(o)]
    _check_feet(layer_on)
    ul = build_underlay(
        first_h=first_h,
        layer_h=layer_h,
        layers=layers,
        footprints=footprints,
        blocks=blocks,
        cutouts=cutouts,
        bases=bases,
        covered=[*blocks, *(_rect(o.bbox()) for o in covered_objs)],
        bed=(0.0, 0.0, ctx.p.bed[0], ctx.p.bed[1]),
    )
    objects: list[PlateObject] = []
    for o in plate.objects:
        if o.underlay == "on" and o.phase == "layer":
            o = replace(_lift(o, ul), brim=0.0)
        elif o.underlay == "on":
            o = replace(_shift_meta(o, ul.height), brim=0.0)
        elif o.underlay == "cutout":
            o = replace(o, brim=0.0)
        objects.append(o)
    sheet = PlateObject(
        SHEET_NAME,
        "underlay",
        boxes=tuple(ul.sheet_boxes()),
        phase="layer",
        params={"layers": ul.layers, "top_mm": round(ul.height, 3)},
        read="The one-piece underlay: every test stands on it or in a cut-out of it. Peel it off "
        "in one piece and keep it as the sample of this spool. Its bare cells between the tests "
        "are the first-layer map: even closed lines everywhere, a dull or ridged zone is too low, "
        "gaps are too high.",
        underlay="sheet",
        brim=0.0,
    )
    custom = tuple(_shift_block(c, ul.height) for c in plate.custom)
    return (
        replace(plate, objects=(sheet, *objects), custom=custom),
        ul,
    )
