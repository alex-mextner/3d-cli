"""fit_coupon.py — the quick fit coupon of `3d calibrate fit`: ladders, geometry, layout, print-time estimate.

WHAT: a coupon is a set of LADDERS, each one fit type on one shape in 7 steps of a stepped gap around the value
  of the fit table (`lib/data/fits.yaml`), plus the SPARES that go into them:

    1 sliding / round   pin Ø4 in holes, radial gap 0.05 … 0.35 mm (0.05 steps)
    2 sliding / slot    2 mm tab in slots, gap per face 0.05 … 0.35
    3 tight   / round   gap 0.00 … 0.30
    4 snap    / slot    gap 0.15 … 0.45 (the tab of a snap hook enters its slot)
    5 press   / round   hole UNDERSIZE: gap -0.15 … 0.00 (0.025 steps; negative = interference)
    6 press   / slot    the same for the slot

  Every step has a raised seven-segment digit (strokes 1.0 mm, 0.6 mm high) on a lip in front of it, and the
  ladder number sits at the left end of the row: ladders read front (1) to back, left to right = step 1…7 =
  smallest to largest gap. The spares (two pins Ø4.00, two tabs 2.00 x 6.00) are printed upright, like the
  holes, so the XY accuracy that fits depend on is what is measured.
WHY: the dense plates' fit pins are read by eye and never recorded; this coupon is small enough (about 10 min of
  printing, `estimate()`) to print for every new spool and its readings go to the fit table (`calibrate_fit`).
INVARIANTS:
  - Stdlib + `slicing.calibration_gen.digit_boxes` only; no OpenSCAD here. `ladder_scad()` returns source text, the
    caller renders it (`slicing.calibration_plates.dense.openscad_stl`).
  - A step's gap is the PHYSICAL design gap per face: hole Ø = pin Ø + 2 * gap, slot width = tab + 2 * gap.
  - The layout is in bed coordinates for a given origin (lower-left of the coupon), so the plate machinery and the
    STL/3MF export place the same geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fits import FitTable

SCHEMA = "3d.calibrate_fit/1"
Bbox = tuple[float, float, float, float, float, float]  # x0, y0, z0, x1, y1, z1

STEPS = 7
CLEARANCE_STEP_MM = 0.05
PRESS_STEP_MM = 0.025
CLEARANCE_BELOW_MM = 0.15  # a clearance ladder starts this far under the table value (never below 0)
PRESS_ABOVE_MM = 0.05  # a press ladder starts this much more interference than the table value

PIN_D = 4.0  # nominal pin diameter (the hole of step k is PIN_D + 2 * gap)
PIN_H = 4.5
TAB_T, TAB_W, TAB_H = 2.0, 6.0, 4.5  # tab thickness (X), width (Y), height
BLOCK_H = 3.0
ROUND_PITCH, ROUND_DEPTH = 6.5, 7.0
SLOT_PITCH, SLOT_DEPTH, SLOT_LEN, SLOT_EDGE = 5.0, 9.0, 7.0, 1.0
LIP_D, LIP_H = 4.0, 1.0  # the label lip in front of a row
DIGIT = (1.6, 2.4, 1.0, 1.0)  # slicing.calibration_gen.digit_boxes: width, height, gap, stroke
DIGIT_EMBOSS = 0.6
ROW_GAP = 3.0
COLUMN_GAP = 6.0
NUMBER_ZONE = 5.0  # left of a row: the ladder number
MARGIN_MM = 5.0  # clearance from the bed edge on every side
SPARE_PITCH = 8.0
SPARE_COUNT = 2

# print-time model (house estimate, K1 SE class printer, PLA, 0.4 mm nozzle, 0.2 mm layers, ~15 % infill):
# deposition at an effective 10 mm³/s for walls this small, 0.6 s of travel/retract per object visit of a layer.
FILL_FACTOR = 0.85  # share of the solid volume that is plastic (walls + skins + 15 % infill)
EFFECTIVE_FLOW_MM3_S = 10.0
VISIT_S = 0.6
LAYER_H = 0.2
PLA_G_PER_MM3 = 0.00124

# the ladders, front to back: (fit type, shape), column 0 = holes of Ø, column 1 = slots
LADDER_SPECS: tuple[tuple[str, str], ...] = (
    ("sliding", "round"),
    ("tight", "round"),
    ("press", "round"),
    ("sliding", "slot"),
    ("snap", "slot"),
    ("press", "slot"),
)

RESULT_WORDS = {
    "round": {
        "free": "the pin goes in and out by hand and turns in the hole without force",
        "press": "the pin goes in only with a push or a twist (or sticks); it stays put when you let go",
        "fused": "the pin will not enter at all (the hole closed up) or the wall cracked",
    },
    "slot": {
        "free": "the tab goes in and out by hand without force",
        "press": "the tab goes in only with a push (or sticks); it stays put when you let go",
        "fused": "the tab will not enter at all (the slot closed up) or the wall cracked",
    },
}
PRESS_WORDS = {
    "round": {
        "free": "the pin is loose: it falls out or turns freely, nothing grips it",
        "press": "the pin needs force to go in and then holds firmly",
        "fused": "the pin will not enter at all, or the wall cracked",
    },
    "slot": {
        "free": "the tab is loose: it falls out or wobbles, nothing grips it",
        "press": "the tab needs force to go in and then holds firmly",
        "fused": "the tab will not enter at all, or the wall cracked",
    },
}


@dataclass(frozen=True)
class Step:
    step: int  # 1-based, smallest gap first
    gap_mm: float  # design gap per face (negative = interference)
    hole_mm: float  # hole diameter / slot width at this step


@dataclass(frozen=True)
class Ladder:
    number: int  # printed on the coupon, row order front to back within its column
    fit_type: str
    shape: str  # round | slot
    table_mm: float  # the table's signed target (clearance - interference)
    steps: tuple[Step, ...]
    mating_mm: float  # pin Ø / tab thickness

    @property
    def id(self) -> str:
        return f"{self.fit_type}_{self.shape}"

    @property
    def gaps(self) -> tuple[float, ...]:
        return tuple(s.gap_mm for s in self.steps)

    @property
    def mating(self) -> str:
        return "pin" if self.shape == "round" else "tab"

    def words(self) -> dict[str, str]:
        return (PRESS_WORDS if self.fit_type == "press" else RESULT_WORDS)[self.shape]


@dataclass(frozen=True)
class Placed:
    """One printed object of the coupon, in bed coordinates."""

    name: str
    kind: str  # ladder | spares
    scad: str
    bbox: Bbox
    volume_mm3: float
    layers: int
    islands: int  # separate pieces visited per layer
    ladder: Ladder | None = None
    step_x: tuple[float, ...] = ()  # x of each step's hole centre (bed coordinates)
    step_y: float = 0.0


@dataclass(frozen=True)
class Layout:
    origin: tuple[float, float]
    objects: tuple[Placed, ...]
    size: tuple[float, float, float]  # footprint x, y and height of the whole coupon
    ladders: tuple[Ladder, ...] = field(default_factory=tuple)

    def bbox(self) -> tuple[float, float, float, float]:
        return (
            self.origin[0],
            self.origin[1],
            self.origin[0] + self.size[0],
            self.origin[1] + self.size[1],
        )


def _r(v: float) -> float:
    return round(v, 4) + 0.0


def gaps_for(base_clearance: float, base_interference: float, fit_type: str) -> tuple[float, ...]:
    """The 7 gaps of a ladder, smallest first: stepped around the table value (module doc)."""
    if fit_type == "press":
        top = base_interference + PRESS_ABOVE_MM
        return tuple(_r(-(top - k * PRESS_STEP_MM)) for k in range(STEPS))
    start = max(0.0, _r(base_clearance - CLEARANCE_BELOW_MM))
    return tuple(_r(start + k * CLEARANCE_STEP_MM) for k in range(STEPS))


def ladders(
    fit_types: tuple[str, ...] | None = None,
    shapes: tuple[str, ...] | None = None,
    table: FitTable | None = None,
) -> tuple[Ladder, ...]:
    """The ladders of the coupon for the chosen fit types / shapes (None = all), numbered 1..n in order."""
    import fits

    tbl = table or fits.load_table()
    out: list[Ladder] = []
    for fit_type, shape in LADDER_SPECS:
        if (fit_types is not None and fit_type not in fit_types) or (
            shapes is not None and shape not in shapes
        ):
            continue
        base = tbl.fit_types[fit_type]
        mating = PIN_D if shape == "round" else TAB_T
        gaps = gaps_for(base.clearance_mm_per_face, base.interference_mm, fit_type)
        out.append(
            Ladder(
                len(out) + 1,
                fit_type,
                shape,
                _r(base.clearance_mm_per_face - base.interference_mm),
                tuple(
                    Step(k + 1, g, _r(mating + 2 * g)) for k, g in enumerate(gaps)
                ),
                mating,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- geometry
def _cube(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> str:
    return f"translate([{x0:g},{y0:g},{z0:g}]) cube([{x1 - x0:g},{y1 - y0:g},{z1 - z0:g}]);"


def _digits(text: str, cx: float, y0: float) -> list[str]:
    """Raised digits `text`, horizontally centred on cx with their lower edge at y0, on the lip."""
    from slicing.calibration_gen import digit_boxes

    w, _h, gap, s = DIGIT
    width = len(text) * (w + gap) - gap
    boxes = digit_boxes(text, cx - width / 2, y0 + s / 2, LIP_H, DIGIT, DIGIT_EMBOSS)
    return [_cube(*b) for b in boxes]


def _ladder_geometry(
    ladder: Ladder, x0: float, y0: float
) -> tuple[str, tuple[float, ...], float, float, float, Bbox]:
    """(scad, hole centres x, hole centre y, row width, row depth incl. lip, bbox of the row)."""
    round_ = ladder.shape == "round"
    pitch = ROUND_PITCH if round_ else SLOT_PITCH
    depth = ROUND_DEPTH if round_ else SLOT_DEPTH
    edge = 0.0 if round_ else SLOT_EDGE
    block_w = len(ladder.steps) * pitch + 2 * edge
    xb = x0 + NUMBER_ZONE  # block lower-left x
    yb = y0 + LIP_D  # block lower-left y (the lip is in front)
    xs = tuple(xb + edge + pitch * (k + 0.5) for k in range(len(ladder.steps)))
    yc = yb + depth / 2
    cuts: list[str] = []
    for x, st in zip(xs, ladder.steps, strict=True):
        if round_:
            cuts.append(
                f"translate([{x:g},{yc:g},-1]) cylinder(d={st.hole_mm:g},h={BLOCK_H + 2:g},$fn=96);"
            )
        else:
            cuts.append(
                _cube(
                    x - st.hole_mm / 2,
                    yc - SLOT_LEN / 2,
                    -1,
                    x + st.hole_mm / 2,
                    yc + SLOT_LEN / 2,
                    BLOCK_H + 1,
                )
            )
    solid = [
        _cube(xb, yb, 0, xb + block_w, yb + depth, BLOCK_H),
        # the lip runs under the ladder number too and reaches 0.2 mm into the block (one body)
        _cube(x0, y0, 0, xb + block_w, yb + 0.2, LIP_H),
    ]
    digit_cubes: list[str] = []
    for x, st in zip(xs, ladder.steps, strict=True):
        digit_cubes += _digits(str(st.step), x, y0 + (LIP_D - DIGIT[1] - DIGIT[3]) / 2)
    digit_cubes += _digits(str(ladder.number), x0 + NUMBER_ZONE / 2 - 0.2, y0 + (LIP_D - DIGIT[1] - DIGIT[3]) / 2)
    scad = (
        "union() {\n  difference() {\n    union() {\n      "
        + "\n      ".join(solid)
        + "\n    }\n    "
        + "\n    ".join(cuts)
        + "\n  }\n  "
        + "\n  ".join(digit_cubes)
        + "\n}\n"
    )
    width = NUMBER_ZONE + block_w
    return scad, xs, yc, width, LIP_D + depth, (x0, y0, 0.0, x0 + width, y0 + LIP_D + depth, BLOCK_H)


def _ladder_volume(ladder: Ladder) -> float:
    round_ = ladder.shape == "round"
    pitch = ROUND_PITCH if round_ else SLOT_PITCH
    depth = ROUND_DEPTH if round_ else SLOT_DEPTH
    edge = 0.0 if round_ else SLOT_EDGE
    block_w = len(ladder.steps) * pitch + 2 * edge
    holes = sum(
        (math.pi * (st.hole_mm / 2) ** 2) if round_ else st.hole_mm * SLOT_LEN
        for st in ladder.steps
    ) * BLOCK_H
    lip = (NUMBER_ZONE + block_w) * LIP_D * LIP_H
    digits = (len(ladder.steps) + 1) * 8.0 * DIGIT_EMBOSS  # ~8 mm² of strokes per digit
    return block_w * depth * BLOCK_H - holes + lip + digits


def _spares(x0: float, y0: float) -> tuple[str, Bbox, float, int]:
    parts: list[str] = []
    x = x0
    for _ in range(SPARE_COUNT):
        parts.append(
            f"translate([{x + PIN_D / 2:g},{y0 + PIN_D / 2:g},0]) cylinder(d={PIN_D:g},h={PIN_H:g},$fn=96);"
        )
        x += SPARE_PITCH
    for _ in range(SPARE_COUNT):
        parts.append(_cube(x, y0, 0, x + TAB_T, y0 + TAB_W, TAB_H))
        x += SPARE_PITCH
    width = x - SPARE_PITCH + max(TAB_T, 0) - x0
    volume = SPARE_COUNT * (math.pi * (PIN_D / 2) ** 2 * PIN_H + TAB_T * TAB_W * TAB_H)
    return "union() {\n  " + "\n  ".join(parts) + "\n}\n", (x0, y0, 0.0, x0 + width, y0 + TAB_W, PIN_H), volume, 2 * SPARE_COUNT


def layout(
    selected: tuple[Ladder, ...], origin: tuple[float, float] = (0.0, 0.0)
) -> Layout:
    """Place the ladders of `selected` and the spares, lower-left corner of the coupon at `origin`.

    Round ladders stack front to back in the left column, slot ladders in the right one; the spares stand
    in front of the right column (or the left one when there are no slots)."""
    ox, oy = origin
    columns: dict[str, list[Ladder]] = {"round": [], "slot": []}
    for ladder in selected:
        columns[ladder.shape].append(ladder)
    objects: list[Placed] = []
    x = ox
    height = BLOCK_H
    size_y = 0.0
    col_x: dict[str, float] = {}
    for shape in ("round", "slot"):
        if not columns[shape]:
            continue
        col_x[shape] = x
        y = oy
        width = 0.0
        for ladder in columns[shape]:
            scad, xs, yc, w, d, bbox = _ladder_geometry(ladder, x, y)
            objects.append(
                Placed(
                    f"ladder_{ladder.number}_{ladder.id}",
                    "ladder",
                    scad,
                    bbox,
                    _ladder_volume(ladder),
                    round(BLOCK_H / LAYER_H),
                    1,
                    ladder,
                    xs,
                    yc,
                )
            )
            width = max(width, w)
            y += d + ROW_GAP
        size_y = max(size_y, y - ROW_GAP - oy)
        x += width + COLUMN_GAP
    # spares: in the column that exists (right-most), behind the ladders
    spare_shape = "slot" if columns["slot"] else "round"
    sy = oy + size_y + ROW_GAP + 1.0
    scad, bbox, volume, islands = _spares(col_x[spare_shape] + NUMBER_ZONE, sy)
    objects.append(
        Placed(
            "spares",
            "spares",
            scad,
            bbox,
            volume,
            round(max(PIN_H, TAB_H) / LAYER_H),
            islands,
        )
    )
    height = max(height, PIN_H)
    width_total = max(b.bbox[3] for b in objects) - ox
    size_y_total = max(b.bbox[4] for b in objects) - oy
    return Layout(
        origin,
        tuple(objects),
        (_r(width_total), _r(size_y_total), height),
        tuple(selected),
    )


def centred(lay_size: tuple[float, float, float], bed: tuple[float, float]) -> tuple[float, float]:
    """The origin that centres a coupon of this footprint on the bed (mm)."""
    return (_r((bed[0] - lay_size[0]) / 2), _r((bed[1] - lay_size[1]) / 2))


def fits_bed(size: tuple[float, float, float], bed: tuple[float, ...]) -> bool:
    """True when the footprint plus MARGIN_MM on every side fits the bed (x, y) and the height the volume."""
    return (
        size[0] + 2 * MARGIN_MM <= bed[0]
        and size[1] + 2 * MARGIN_MM <= bed[1]
        and (len(bed) < 3 or size[2] <= bed[2])
    )


def estimate(objects: tuple[Placed, ...]) -> dict[str, Any]:
    """Print time (minutes, without heating and bed levelling) and plastic (g) of the objects.

    time = plastic volume / EFFECTIVE_FLOW_MM3_S + per-layer object visits * VISIT_S, plastic =
    FILL_FACTOR * solid volume. A house model for a K1 SE class printer; the slicer's own figure
    (`3d calibrate gen fit`, `; estimated printing time`) replaces it once a G-code exists."""
    solid = sum(o.volume_mm3 for o in objects)
    plastic = solid * FILL_FACTOR
    visits = sum(o.layers * o.islands for o in objects)
    seconds = plastic / EFFECTIVE_FLOW_MM3_S + visits * VISIT_S
    return {
        "minutes": round(seconds / 60, 1),
        "grams": round(plastic * PLA_G_PER_MM3, 1),
        "plastic_mm3": round(plastic),
        "basis": (
            f"{FILL_FACTOR:g} x solid volume at {EFFECTIVE_FLOW_MM3_S:g} mm³/s plus {VISIT_S:g} s per object visit "
            f"of a {LAYER_H:g} mm layer ({visits} visits); no heating or bed levelling"
        ),
    }
