"""calibration_plates/dense.py — the dense plate: filament and mechanics tests on one 220x220 bed.

WHAT (tests, ids for --only/--skip):
  filament (makers from filament.py): mvs strip, bridges 11/14 and 22 mm, overhangs 50–60°,
    retraction tower (= the stringing test), temperature tower
    (sequential, last)
  mechanics:
    ringing     square 2-line tube, ACCEL 2000→12000 by height at a fixed wall speed
    vfa         round 2-line tube, wall speed 50→300 mm/s by height at a fixed ACCEL
    dimension   50x50 frame (X/Y/Z size, skew from the diagonals)
    fit         pins printed in place in holes, radial gaps 0.1–0.5 (shaft/hole)
    hinge       print-in-place hinges, gaps 0.3/0.4
    screw       M3/M4 holes (tap and clearance sizes), slots for a 2 mm insert + the insert
    holes       small holes Ø1–4
    thin_wall   thin walls 0.3–1.0 mm, along Y and along X
    first_layer one-layer patches at the corners and in the middle
    top_surface top skin over sparse infill
  Item labels are one-layer digits on the bed in front of each row (hand-written, like the
  objects' bed labels): raised digits on the parts would add hundreds of tiny islands and travels.
  Ringing and VFA print by layer with everything else: their band transforms set the wall speed
  and ACCEL only on their own lines (calibration_plate hooks restore the slicer's state after
  each visit), so ringing (corners, ACCEL bands) and VFA (speed bands, no corners) stay
  separable. The layout is fixed for the K1 SE bed: every test has its own place, a skipped
  test leaves an empty spot.
WHY: one ≤45 min print instead of separate filament and mechanics plates (express calibration
  of a K1 SE spool and of its motion system).
"""

from __future__ import annotations

from functools import partial
import math
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from slicing.calibration_gen import Box, GenError
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
)
from slicing.calibration_plate_underlay import declare
from slicing.calibration_plates import filament
from slicing.calibration_underlay import UnderlayDecl

PLATE_ID = "dense"
TITLE = "dense plate: filament + mechanics"
TESTS = {
    "mvs": "max volumetric speed strip",
    "bridge": "bridges 11/14 mm",
    "long_bridge": "22 mm bridge",
    "overhang": "overhangs 50–60°",
    "retraction": "retraction tower (stringing)",
    "temp": "temperature tower (sequential, last)",
    "ringing": "ringing tower: ACCEL by height",
    "vfa": "VFA tube: wall speed by height",
    "dimension": "50x50 frame: size and skew",
    "fit": "pins in holes, radial gaps 0.1–0.5",
    "hinge": "print-in-place hinges",
    "screw": "M3/M4 holes, slots for a 2 mm insert",
    "holes": "small holes Ø1–4",
    "thin_wall": "thin walls 0.3–1.0 mm",
    "first_layer": "first-layer patches (--no-underlay only: the bare underlay is the map)",
    "top_surface": "top surface patch",
}
# Where each test stands on the shared underlay (#320): `on` = on the sheet (a foot under its base),
# `cutout` = on the bare bed in a hole of the sheet (print-in-place or loose parts, through holes).
UNDERLAY = {
    "mvs": UnderlayDecl("on", "the strip prints on the underlay; its step is read from above"),
    "bridge": UnderlayDecl("on", "pillars on a foot, the span bridges over the sheet"),
    "long_bridge": UnderlayDecl("on", "pillars on a foot, the span bridges over the sheet"),
    "overhang": UnderlayDecl("on", "the small base of the wedge gets a foot instead of a brim"),
    "retraction": UnderlayDecl("on", "the tower stands on a foot; strings are read from the side"),
    "temp": UnderlayDecl("on", "the sequential tower is lifted onto the finished sheet"),
    "ringing": UnderlayDecl("on", "the tube's thin walls stand on a foot; ripples are read on its faces"),
    "vfa": UnderlayDecl("on", "the round tube stands on a foot; the bands are read on its wall"),
    "dimension": UnderlayDecl("on", "the frame's outer edges are measured; its foot is part of the sheet"),
    "fit": UnderlayDecl("cutout", "pins are printed in place in their holes and must come out: the sheet would weld them", movable=True),
    "hinge": UnderlayDecl("cutout", "print-in-place hinges must turn: the sheet would weld the knuckles", movable=True),
    "screw": UnderlayDecl("cutout", "M3/M4 holes are through holes tried with a screw, the insert is a loose part", movable=True),
    "holes": UnderlayDecl("cutout", "through holes are measured with pins and drill shanks: a foot would close them"),
    "thin_wall": UnderlayDecl("on", "walls 0.3-1.0 mm stand on a foot; a lost wall is read from above"),
    "first_layer": UnderlayDecl("on", "replaced by the underlay: its bare cells are the first-layer map"),
    "top_surface": UnderlayDecl("on", "the tile's top skin is read; its foot is part of the sheet"),
}
BED = (220.0, 220.0)
LABEL_DY = filament.LABEL_DY  # a row's bed labels sit this far in front of its objects

# Ringing: a square 2-line tube; each band steps 0.2 mm inwards (a visible line at the band edge).
RING_SIDE = 25.0
RING_WALL = 0.84
RING_BASE = 1.0
RING_BAND = 2.0
RING_ACCELS = (2000, 4000, 6000, 8000, 10000, 12000)
RING_SPEED = 150.0  # mm/s: ripple spacing = speed / frequency (2.2 mm at 69 Hz)
RING_SCV = 5.0  # the printer's own square_corner_velocity
STEP_IN = 0.2
# VFA: a round 2-line tube. ACCEL is high enough for the slicer's ~1.4 mm chords at 300 mm/s
# (Klipper's centripetal limit), SCV keeps the chord junctions from slowing it down.
VFA_R = 20.0
VFA_SIDES = 360
VFA_SPEEDS = (50, 100, 150, 200, 250, 300)
VFA_ACCEL = 6000.0
VFA_SCV = 20.0
# 50x50 frame
DIM_SIDE, DIM_WALL, DIM_H = 50.0, 3.0, 3.0
# fits: one pin printed in place in each hole
FIT_GAPS = (0.1, 0.2, 0.3, 0.4, 0.5)
FIT_PITCH, FIT_PIN_D, FIT_DEPTH, FIT_H = 7.0, 3.5, 8.0, 3.0
# hinges: knuckle radius, pin radius, length (X), leaf width each side (Y), leaf thickness
HINGE_GAPS = (0.3, 0.4)  # 0.2 fuses on PLA at 0.2 mm layers
HINGE_R, HINGE_PIN_R, HINGE_L, HINGE_LEAF, HINGE_T = 2.0, 0.8, 10.0, 5.0, 1.0
# M3/M4: tap and clearance holes; slots for a 2.0 mm insert printed flat next to them
SCREW_HOLES = (2.5, 3.2, 3.4, 3.3, 4.3, 4.5)  # M3: tap, clearance, loose; M4 the same
SCREW_PITCH, SCREW_DEPTH, SCREW_H = 6.0, 8.0, 3.0
SLOT_WIDTHS = (2.1, 2.2, 2.3, 2.4)
SLOT_PITCH, SLOT_LEN, SLOT_H, SLOT_DEPTH = 5.5, 6.0, 4.0, 3.0
INSERT = (10.0, 6.0, 2.0)
SMALL_D = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
SMALL_PITCH, SMALL_DEPTH, SMALL_H = 7.0, 8.0, 2.0  # pitch fits a "2.5" label
THIN = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0)
THIN_L, THIN_H, THIN_PITCH = 8.0, 3.0, 6.0
PATCH = 18.0
TOP = (18.0, 18.0, 2.0)  # 2 bottom + 5 sparse + 3 top layers at 0.2 mm
BRIDGES = (11, 14)
LONG_BRIDGE = (22,)
OVERHANGS = (50, 55, 60)  # 45° prints on any PLA; the limit sits above it
# The temperature tower prints alone: 5 s per layer (Orca's PLA preset: 8) still cools its 5 mm
# pillars with the fan at 100 % and saves ~2 min.
SEQ_MIN_LAYER_S = 5

# Place of each test (lower-left corner, mm; a row's bed labels start at its y). Front: strip,
# bridges, frame (thin walls and holes inside it), fits; middle: tubes and the retraction tower;
# the temperature tower alone in the back-right corner with the head clearance around it (only
# one-layer patches there).
AT: dict[str, tuple[float, float]] = {
    "patch_fl": (6.0, 6.0),
    "mvs": (30.0, 6.0),
    "bridge": (126.0, 6.0),
    "patch_fr": (196.0, 6.0),
    "dimension": (6.0, 30.0),
    "thin_wall": (14.0, 35.0),
    "holes": (10.0, 54.0),
    "fit": (64.0, 28.0),
    "screw": (64.0, 46.0),
    "slots": (64.0, 64.0),
    "insert": (94.0, 69.0),
    "overhang": (110.0, 28.0),
    "patch_m1": (110.0, 44.0),
    "patch_m2": (130.0, 62.0),
    "top_1": (160.0, 30.0),
    "long_bridge": (160.0, 54.0),
    "hinge": (150.0, 70.0),
    "patch_r": (196.0, 36.0),
    "thin_wall_x": (6.0, 90.0),
    "patch_c": (20.0, 112.0),
    "vfa": (70.0, 100.0),
    "ringing": (120.0, 104.0),
    "retraction": (156.0, 112.0),
    "patch_bl": (6.0, 196.0),
    "patch_bc": (100.0, 196.0),
    "patch_br": (196.0, 170.0),
    "temp": (190.0, 202.0),
}
PATCHES = (
    "patch_fl",
    "patch_fr",
    "patch_r",
    "patch_m1",
    "patch_m2",
    "patch_c",
    "patch_bl",
    "patch_bc",
    "patch_br",
)

Objs = tuple[tuple[PlateObject, ...], tuple[CustomBlock, ...]]


# ---- geometry --------------------------------------------------------------------------------


def _tri(
    out: list[str], a: tuple[float, ...], b: tuple[float, ...], c: tuple[float, ...]
) -> None:
    out.append("facet normal 0 0 0\nouter loop")
    out += [f"vertex {p[0]:.4f} {p[1]:.4f} {p[2]:.4f}" for p in (a, b, c)]
    out.append("endloop\nendfacet")


def tubes_stl(
    cx: float, cy: float, rings: list[tuple[float, float, float, float]], n: int
) -> str:
    """One ASCII STL of stacked round tubes (r_out, r_in, z0, z1), outward winding, n sides."""
    out = ["solid tube"]
    for ro, ri, z0, z1 in rings:
        ang = [2 * math.pi * k / n for k in range(n)]
        o = [(cx + ro * math.cos(a), cy + ro * math.sin(a)) for a in ang]
        i = [(cx + ri * math.cos(a), cy + ri * math.sin(a)) for a in ang]
        for k in range(n):
            j = (k + 1) % n
            _tri(out, (*o[k], z0), (*o[j], z0), (*o[j], z1))  # outer wall
            _tri(out, (*o[k], z0), (*o[j], z1), (*o[k], z1))
            _tri(
                out, (*i[k], z0), (*i[j], z1), (*i[j], z0)
            )  # inner wall (faces the axis)
            _tri(out, (*i[k], z0), (*i[k], z1), (*i[j], z1))
            _tri(out, (*o[k], z1), (*o[j], z1), (*i[j], z1))  # top ring
            _tri(out, (*o[k], z1), (*i[j], z1), (*i[k], z1))
            _tri(out, (*o[k], z0), (*i[j], z0), (*o[j], z0))  # bottom ring
            _tri(out, (*o[k], z0), (*i[k], z0), (*i[j], z0))
    out.append("endsolid tube\n")
    return "\n".join(out)


def square_tube_boxes(
    x0: float, y0: float, side: float, wall: float, z0: float, z1: float
) -> list[Box]:
    x1, y1 = x0 + side, y0 + side
    return [
        (x0, y0, z0, x1, y0 + wall, z1),
        (x0, y1 - wall, z0, x1, y1, z1),
        (x0, y0 + wall, z0, x0 + wall, y1 - wall, z1),
        (x1 - wall, y0 + wall, z0, x1, y1 - wall, z1),
    ]


def _bands_z(n: int) -> list[tuple[float, float]]:
    """[z0, z1) of each band; the first starts above the base, the last runs past the top."""
    zs = [RING_BASE + k * RING_BAND for k in range(n + 1)]
    return [(zs[k], zs[k + 1] if k < n - 1 else zs[n] + 1.0) for k in range(n)]


def openscad_stl(source: str) -> str:
    """Render OpenSCAD source to an ASCII STL (CGAL/Manifold, whatever this build uses)."""
    exe = shutil.which("openscad")
    if exe is None:
        raise GenError(
            "the dense plate needs OpenSCAD on PATH for its fit/hinge/hole objects"
        )
    with tempfile.TemporaryDirectory(prefix="3dcalscad.") as tmp:
        scad, stl = Path(tmp) / "m.scad", Path(tmp) / "m.stl"
        scad.write_text(source)
        r = subprocess.run(
            [exe, "--export-format", "asciistl", "-o", str(stl), str(scad)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode != 0 or not stl.is_file():
            raise GenError(
                f"OpenSCAD failed (exit {r.returncode}):\n{r.stderr[-1500:]}"
            )
        return stl.read_text()


def _row_labels(
    ctx: PlateContext, name: str, texts: list[str], xs: list[float], y: float
) -> CustomBlock:
    """One-layer bed digits, each centred on its x, lower edge at y (in front of the row)."""
    body: list[str] = []
    for text, x in zip(texts, xs, strict=False):
        body += label_lines(ctx, text, x - label_width(text) / 2, y)
    return CustomBlock(name, "labels", custom_lines(ctx, body, f"{name}"))


def _tenths(v: float) -> str:
    return f"{round(v * 10)}"


def _mm(v: float) -> str:
    return f"{v:g}"


# ---- motion transforms -------------------------------------------------------------------------

_VLIMIT = re.compile(r"^SET_VELOCITY_LIMIT\b")


def velocity_line(accel: float, scv: float) -> str:
    return f"SET_VELOCITY_LIMIT ACCEL={accel:g} ACCEL_TO_DECEL={accel / 2:g} SQUARE_CORNER_VELOCITY={scv:g}"


def motion(speed: float, accel: float, scv: float) -> Callable[[str], str]:
    """Band transform: every extruding XY move at `speed` mm/s, every SET_VELOCITY_LIMIT of the
    slicer replaced by (accel, scv). Travels and retractions keep the slicer's feedrates."""
    vel = velocity_line(accel, scv)
    feed = f"F{speed * 60:.0f}"

    def tf(line: str) -> str:
        if _VLIMIT.match(line):
            return vel
        if not line.startswith("G1 "):
            return line
        code, sep, comment = line.partition(";")
        words = code.split()
        e = next((float(w[1:]) for w in words[1:] if w[0] == "E"), 0.0)
        if e <= 0 or not any(w[0] in "XY" for w in words[1:]):
            return line
        trail = code[len(code.rstrip()) :]
        return (
            " ".join([w for w in words if w[0] != "F"] + [feed]) + trail + sep + comment
        )

    return tf


# ---- tests -----------------------------------------------------------------------------------


def _ringing(ctx: PlateContext, x0: float, y0: float) -> Objs:
    boxes: list[Box] = []
    zb = _bands_z(len(RING_ACCELS))
    for k in range(len(RING_ACCELS)):
        z0 = 0.0 if k == 0 else zb[k][0]
        inset = k * STEP_IN
        boxes += square_tube_boxes(
            x0 + inset,
            y0 + LABEL_DY + inset,
            RING_SIDE - 2 * inset,
            RING_WALL,
            z0,
            zb[k][0] + RING_BAND,
        )
    bands = tuple(
        Band(
            z0,
            z1,
            (velocity_line(a, RING_SCV),),
            {"accel": a},
            motion(RING_SPEED, a, RING_SCV),
        )
        for a, (z0, z1) in zip(RING_ACCELS, zb, strict=False)
    )
    bb = _union(boxes)
    labels = ", ".join(
        f"{z0:g}–{z0 + RING_BAND:g} mm: {a}"
        for a, (z0, _) in zip(RING_ACCELS, zb, strict=False)
    )
    return (
        PlateObject(
            "ringing_tower",
            "ringing",
            tuple(boxes),
            bands=bands,
            params={
                "wall_speed_mm_s": RING_SPEED,
                "square_corner_velocity": RING_SCV,
                "band_mm": RING_BAND,
            },
            items=tuple(
                Item(
                    str(a),
                    (bb[0], bb[1], z0, bb[3], bb[4], z0 + RING_BAND),
                    {"accel": a},
                )
                for a, (z0, _) in zip(RING_ACCELS, zb, strict=False)
            ),
            read=f"Ringing tower, ACCEL by height ({labels}); each band starts at a small inward step. "
            "Ripples right after a corner (in print direction) that fade along the face = ringing; "
            "front/back faces show Y, left/right faces X. Spacing s between ripple crests (count N "
            f"crests over a length L: s = L/N) gives the frequency f = {RING_SPEED:g} / s Hz. The "
            "highest band without visible ripples is the usable wall ACCEL.",
            record="ripple frequency and the last clean ACCEL → ~/.local/share/3d/printer-history/<printer>.jsonl "
            "(printops.settings_change/1, kind input_shaper)",
        ),
    ), ()


def _vfa(ctx: PlateContext, x0: float, y0: float) -> Objs:
    cx, cy = x0 + VFA_R, y0 + LABEL_DY + VFA_R
    zb = _bands_z(len(VFA_SPEEDS))
    rings = []
    for k in range(len(VFA_SPEEDS)):
        ro = VFA_R - k * STEP_IN
        rings.append(
            (ro, ro - RING_WALL, 0.0 if k == 0 else zb[k][0], zb[k][0] + RING_BAND)
        )
    top = zb[-1][0] + RING_BAND
    bbox = (cx - VFA_R, cy - VFA_R, 0.0, cx + VFA_R, cy + VFA_R, top)
    bands = tuple(
        Band(
            z0,
            z1,
            (velocity_line(VFA_ACCEL, VFA_SCV),),
            {"speed_mm_s": v},
            motion(v, VFA_ACCEL, VFA_SCV),
        )
        for v, (z0, z1) in zip(VFA_SPEEDS, zb, strict=False)
    )
    labels = ", ".join(
        f"{z0:g}–{z0 + RING_BAND:g} mm: {v}"
        for v, (z0, _) in zip(VFA_SPEEDS, zb, strict=False)
    )
    flow = (
        0.42 - 0.2 * (1 - math.pi / 4)
    ) * 0.2  # mm² of the slicer's 0.42 x 0.2 mm wall line
    return (
        PlateObject(
            "vfa_tube",
            "vfa",
            stl=tubes_stl(cx, cy, rings, VFA_SIDES),
            stl_bbox=bbox,
            bands=bands,
            params={
                "accel": VFA_ACCEL,
                "square_corner_velocity": VFA_SCV,
                "band_mm": RING_BAND,
                "volumetric_at_300_mm3_s": round(flow * 300, 1),
            },
            items=tuple(
                Item(
                    str(v),
                    (bbox[0], bbox[1], z0, bbox[3], bbox[4], z0 + RING_BAND),
                    {"speed_mm_s": v},
                )
                for v, (z0, _) in zip(VFA_SPEEDS, zb, strict=False)
            ),
            read=f"VFA tube, wall speed by height ({labels} mm/s), ACCEL {VFA_ACCEL:g} throughout. VFA = fine "
            "vertical lines evenly spaced all round (no corners here, so it is not ringing) that come and go "
            "with the speed band. Spacing ≈ 2 mm → belt teeth; ≈ 0.2 mm → motor steps; lines only on the "
            "45° diagonals → one belt/motor (CoreXY). Avoid the bands where it shows for outer walls. "
            f"At 300 mm/s the wall line needs {flow * 300:.1f} mm³/s: if that band looks thin or matte, compare "
            "with the MVS strip.",
            record="speeds with VFA → printer history note; outer_wall_speed outside them",
        ),
    ), ()


def _dimension(ctx: PlateContext, x0: float, y0: float) -> Objs:
    s, w, h = DIM_SIDE, DIM_WALL, DIM_H
    boxes = square_tube_boxes(x0, y0, s, w, 0.0, h)
    diag = s * math.sqrt(2)
    return (
        PlateObject(
            "dimension_frame",
            "dimension",
            tuple(boxes),
            params={
                "outer_mm": s,
                "inner_mm": s - 2 * w,
                "height_mm": h,
                "diagonal_mm": round(diag, 3),
            },
            bed_label=("1", x0 + 1.5, y0 - LABEL_DY),  # in front of corner A
            read=f"Frame {s:g}x{s:g} mm (inside {s - 2 * w:g}), {h:g} mm high. Corner A is the one behind the "
            "bed label 1 (front-left), B back-left, C back-right, D front-right. Measure X along AD and Y along "
            f"AB (outside {s:g}), Z ({h:g}), and the outside diagonals AC and BD (both {diag:.2f}). "
            "Skew angle = asin((AC² − BD²) / (4·AB·AD)); above ±0.1° → SET_SKEW XY=AC,BD,AD "
            "(needs [skew_correction]) or square the gantry.",
            record="sizes and AC/BD/AD → printer history (kind dimension); skew → [skew_correction]",
        ),
    ), ()


def _thin_walls(x0: float, y0: float, along_x: bool) -> Objs:
    objs = []
    for k, t in enumerate(THIN):
        if along_x:  # walls along X, stacked left to right with their label in front
            bx = x0 + k * (THIN_L + 3.0)
            box: Box = (bx, y0 + LABEL_DY, 0.0, bx + THIN_L, y0 + LABEL_DY + t, THIN_H)
            label = (_tenths(t), bx + THIN_L / 2, y0)
        else:  # walls along Y
            bx = x0 + k * THIN_PITCH
            box = (bx, y0 + LABEL_DY, 0.0, bx + t, y0 + LABEL_DY + THIN_L, THIN_H)
            label = (_tenths(t), bx + t / 2, y0)
        axis = "x" if along_x else "y"
        objs.append(
            PlateObject(
                f"thin_{axis}_{round(t * 10):02d}",
                "thin_wall",
                (box,),
                params={"wall_mm": t, "along": axis.upper()},
                bed_label=label,
                read=f"{t:g} mm wall along {axis.upper()} (label = tenths of a mm): missing or broken → the slicer "
                "dropped it (detect_thin_wall/arachne); measure the printed thickness.",
            )
        )
    return tuple(objs), ()


def _holes(ctx: PlateContext, x0: float, y0: float) -> Objs:
    xs = [x0 + SMALL_PITCH * (k + 0.5) for k in range(len(SMALL_D))]
    w, yb = len(SMALL_D) * SMALL_PITCH, y0 + LABEL_DY
    yc = yb + SMALL_DEPTH / 2
    cuts = "\n".join(
        f"translate([{x},{yc},-1]) cylinder(d={v},h={SMALL_H + 2},$fn=48);"
        for x, v in zip(xs, SMALL_D, strict=False)
    )
    src = f"difference() {{ translate([{x0},{yb},0]) cube([{w},{SMALL_DEPTH},{SMALL_H}]);\n{cuts}\n}}"
    return (
        PlateObject(
            "small_holes",
            "holes",
            stl=openscad_stl(src),
            stl_bbox=(x0, yb, 0.0, x0 + w, yb + SMALL_DEPTH, SMALL_H),
            params={"diameters_mm": ",".join(f"{v:g}" for v in SMALL_D)},
            items=tuple(
                Item(
                    _mm(v),
                    (x - v / 2, yc - v / 2, 0.0, x + v / 2, yc + v / 2, SMALL_H),
                    {"diameter_mm": v},
                )
                for x, v in zip(xs, SMALL_D, strict=False)
            ),
            read="Small vertical holes, label = Ø in mm. Check with drill shanks or pins: holes "
            "usually come out 0.1–0.3 mm small; the smallest open hole is the resolution limit.",
        ),
    ), (_row_labels(ctx, "small_holes_labels", [_mm(v) for v in SMALL_D], xs, y0),)


def _fit(ctx: PlateContext, x0: float, y0: float) -> Objs:
    xs = [x0 + FIT_PITCH * (k + 0.5) for k in range(len(FIT_GAPS))]
    w, yb = len(FIT_GAPS) * FIT_PITCH, y0 + LABEL_DY
    yc = yb + FIT_DEPTH / 2
    holes = "\n".join(
        f"translate([{x},{yc},-1]) cylinder(d={FIT_PIN_D + 2 * g},h={FIT_H + 2},$fn=64);"
        for x, g in zip(xs, FIT_GAPS, strict=False)
    )
    pins = "\n".join(
        f"translate([{x},{yc},0]) cylinder(d={FIT_PIN_D},h={FIT_H},$fn=64);" for x in xs
    )
    src = f"difference() {{ translate([{x0},{yb},0]) cube([{w},{FIT_DEPTH},{FIT_H}]);\n{holes}\n}}\n{pins}"
    return (
        PlateObject(
            "fits",
            "fit",
            stl=openscad_stl(src),
            stl_bbox=(x0, yb, 0.0, x0 + w, yb + FIT_DEPTH, FIT_H),
            params={
                "gaps_mm": ",".join(f"{g:g}" for g in FIT_GAPS),
                "pin_d_mm": FIT_PIN_D,
            },
            items=tuple(
                Item(
                    _tenths(g),
                    (
                        x - FIT_PITCH / 2,
                        yb,
                        0.0,
                        x + FIT_PITCH / 2,
                        yb + FIT_DEPTH,
                        FIT_H,
                    ),
                    {"gap_mm": g},
                )
                for x, g in zip(xs, FIT_GAPS, strict=False)
            ),
            read=f"Ø{FIT_PIN_D:g} mm pins printed in place in holes, label = radial gap in tenths of a mm. Push "
            "each pin out: the smallest gap that frees by hand is the clearance for moving shaft/hole fits; one "
            "step less is a press fit.",
        ),
    ), (_row_labels(ctx, "fits_labels", [_tenths(g) for g in FIT_GAPS], xs, y0),)


def hinge_scad(hx: float, ay: float, g: float) -> str:
    """OpenSCAD source of one print-in-place hinge: left end at x = hx, axis along X at y = ay,
    radial gap g. Leaf A (outer knuckles + pin) in front, leaf B (middle knuckle) behind."""
    r, rp, length, leaf, t = HINGE_R, HINGE_PIN_R, HINGE_L, HINGE_LEAF, HINGE_T
    k1, k2 = hx + length / 3, hx + 2 * length / 3  # B's knuckle is the middle third

    def knuckle(xa: float, xb: float) -> str:
        return (
            f"translate([{xa},{ay},{r}]) rotate([0,90,0]) cylinder(r={r},h={xb - xa},$fn=48);"
            f"translate([{xa},{ay - r},0]) cube([{xb - xa},{2 * r},{r}]);"
        )

    hole = (
        f"translate([{hx - 1},{ay},{r}]) rotate([0,90,0]) rotate([0,0,45]) "
        f"{{ cylinder(r={rp + g},h={length + 2},$fn=48); cube([{rp + g},{rp + g},{length + 2}]); }}"
    )  # teardrop: the roof point is up (+Z after the turn), no overhang flatter than 45°
    part_a = (
        f"union() {{ {knuckle(hx, k1 - g / 2)} {knuckle(k2 + g / 2, hx + length)}"
        f" translate([{hx},{ay - leaf},0]) cube([{length},{leaf - r - g},{t}]);"
        f" translate([{hx},{ay - r - g},0]) cube([{k1 - g / 2 - hx},{r + g},{t}]);"
        f" translate([{k2 + g / 2},{ay - r - g},0]) cube([{hx + length - k2 - g / 2},{r + g},{t}]);"
        f" translate([{hx},{ay},{r}]) rotate([0,90,0]) cylinder(r={rp},h={length},$fn=32); }}"
    )
    part_b = (
        f"difference() {{ union() {{ {knuckle(k1 + g / 2, k2 - g / 2)}"
        f" translate([{hx},{ay + r + g},0]) cube([{length},{leaf - r - g},{t}]);"
        f" translate([{k1 + g / 2},{ay},0]) cube([{k2 - k1 - g},{r + g},{t}]); }} {hole} }}"
    )
    return part_a + "\n" + part_b


def _hinges(x0: float, y0: float) -> Objs:
    objs = []
    length, leaf = HINGE_L, HINGE_LEAF
    for k, g in enumerate(HINGE_GAPS):
        hx = x0 + k * (length + 4.0)
        ay = y0 + LABEL_DY + leaf  # axis y
        bbox = (hx, ay - leaf, 0.0, hx + length, ay + leaf, 2 * HINGE_R)
        objs.append(
            PlateObject(
                f"hinge_{round(g * 10):02d}",
                "hinge",
                stl=openscad_stl(hinge_scad(hx, ay, g)),
                stl_bbox=bbox,
                params={"gap_mm": g, "pin_d_mm": 2 * HINGE_PIN_R},
                bed_label=(_tenths(g), hx + length / 2, y0),
                read=f"Print-in-place hinge, gap {g:g} mm (label in tenths): fold it; the smallest gap that "
                "turns freely after a firm first twist is the print-in-place clearance.",
            )
        )
    return tuple(objs), ()


def _screw(ctx: PlateContext, x0: float, y0: float) -> Objs:
    xs = [x0 + SCREW_PITCH * (k + 0.5) for k in range(len(SCREW_HOLES))]
    w, yb = len(SCREW_HOLES) * SCREW_PITCH, y0 + LABEL_DY
    yc = yb + SCREW_DEPTH / 2
    cuts = "\n".join(
        f"translate([{x},{yc},-1]) cylinder(d={v},h={SCREW_H + 2},$fn=48);"
        for x, v in zip(xs, SCREW_HOLES, strict=False)
    )
    src = f"difference() {{ translate([{x0},{yb},0]) cube([{w},{SCREW_DEPTH},{SCREW_H}]);\n{cuts}\n}}"
    return (
        PlateObject(
            "screw_holes",
            "fit",
            stl=openscad_stl(src),
            stl_bbox=(x0, yb, 0.0, x0 + w, yb + SCREW_DEPTH, SCREW_H),
            params={"holes_mm": ",".join(f"{v:g}" for v in SCREW_HOLES)},
            items=tuple(
                Item(
                    f"{v:g}",
                    (x - v / 2, yc - v / 2, 0.0, x + v / 2, yc + v / 2, SCREW_H),
                    {"diameter_mm": v},
                )
                for x, v in zip(xs, SCREW_HOLES, strict=False)
            ),
            read="Label 3 = the M3 group (left to right Ø2.5 tap, 3.2 clearance, 3.4 loose), label 4 = M4 (3.3, 4.3, "
            "4.5). Drive a screw into each: the tap hole should hold the thread, the clearance hole should let "
            "the screw slide without play.",
        ),
    ), (_row_labels(ctx, "screw_labels", ["3", "4"], [xs[1], xs[4]], y0),)


def _slots(ctx: PlateContext, x0: float, y0: float) -> Objs:
    xs = [x0 + 1.0 + SLOT_PITCH * (k + 0.5) for k in range(len(SLOT_WIDTHS))]
    w, yb, d = len(SLOT_WIDTHS) * SLOT_PITCH + 2.0, y0 + LABEL_DY, SLOT_LEN + 4.0
    cuts = "\n".join(
        f"translate([{x - v / 2},{yb + 2.0},{SLOT_H - SLOT_DEPTH}]) cube([{v},{SLOT_LEN},{SLOT_DEPTH + 1}]);"
        for x, v in zip(xs, SLOT_WIDTHS, strict=False)
    )
    src = f"difference() {{ translate([{x0},{yb},0]) cube([{w},{d},{SLOT_H}]);\n{cuts}\n}}"
    return (
        PlateObject(
            "insert_slots",
            "fit",
            stl=openscad_stl(src),
            stl_bbox=(x0, yb, 0.0, x0 + w, yb + d, SLOT_H),
            params={
                "slot_widths_mm": ",".join(f"{v:g}" for v in SLOT_WIDTHS),
                "insert_mm": INSERT[2],
            },
            items=tuple(
                Item(
                    f"{v:g}",
                    (
                        x - v / 2,
                        yb + 2.0,
                        SLOT_H - SLOT_DEPTH,
                        x + v / 2,
                        yb + 2.0 + SLOT_LEN,
                        SLOT_H,
                    ),
                    {"slot_mm": v},
                )
                for x, v in zip(xs, SLOT_WIDTHS, strict=False)
            ),
            read="Slots for a 2.0 mm insert, label 1–4 = 2.1–2.4 mm wide: push the printed 2 mm tab (or a 2 mm "
            "sheet) in; the narrowest slot it enters by hand is the sliding fit for inserts.",
        ),
    ), (
        _row_labels(
            ctx, "slots_labels", [str(k + 1) for k in range(len(SLOT_WIDTHS))], xs, y0
        ),
    )


def _insert(x0: float, y0: float) -> Objs:
    ix, iy, iz = INSERT
    return (
        PlateObject(
            "insert_tab",
            "fit",
            ((x0, y0, 0.0, x0 + ix, y0 + iy, iz),),
            params={"thickness_mm": iz},
            read="The 2.0 mm tab for the insert slots (printed flat: its thickness is a Z size).",
        ),
    ), ()


def _patch(name: str, x0: float, y0: float) -> Objs:
    return (
        PlateObject(
            name,
            "first_layer",
            ((x0, y0, 0.0, x0 + PATCH, y0 + PATCH, 0.2),),
            params={"layer_mm": 0.2},
            read="One-layer patch: even, closed lines with no gaps (too high) or ridges and a rough, "
            "transparent-thin surface (too low); compare the patches across the bed for leveling.",
        ),
    ), ()


def _top(name: str, x0: float, y0: float) -> Objs:
    tx, ty, tz = TOP
    return (
        PlateObject(
            name,
            "top_surface",
            ((x0, y0, 0.0, x0 + tx, y0 + ty, tz),),
            params={"top_layers": 3, "sparse_infill": "15%"},
            read="Top skin over 15 % sparse infill: smooth, closed lines are good; pillowing (bumps over "
            "the infill) or gaps mean more top layers, more fan or more flow.",
        ),
    ), ()


def build(ctx: PlateContext) -> Plate:
    if tuple(ctx.p.bed) != BED:
        raise GenError(
            f"the dense plate is laid out for a {BED[0]:g}x{BED[1]:g} bed, not {ctx.p.bed}"
        )
    makers: dict[str, list[tuple[str, Callable[[float, float], Objs]]]] = {
        "mvs": [("mvs", lambda x, y: filament._mvs(ctx).build(x, y))],
        "bridge": [("bridge", lambda x, y: filament._bridge(BRIDGES).build(x, y))],
        "long_bridge": [
            (
                "long_bridge",
                lambda x, y: filament._bridge(LONG_BRIDGE, "long_bridge").build(x, y),
            )
        ],
        "overhang": [
            ("overhang", lambda x, y: filament._overhang(angles=OVERHANGS).build(x, y))
        ],
        "retraction": [
            ("retraction", lambda x, y: filament._retraction(ctx).build(x, y))
        ],
        "temp": [("temp", lambda x, y: filament._temp(ctx).build(x, y))],
        "ringing": [("ringing", lambda x, y: _ringing(ctx, x, y))],
        "vfa": [("vfa", lambda x, y: _vfa(ctx, x, y))],
        "dimension": [("dimension", lambda x, y: _dimension(ctx, x, y))],
        "fit": [("fit", lambda x, y: _fit(ctx, x, y))],
        "hinge": [("hinge", _hinges)],
        "screw": [
            ("screw", lambda x, y: _screw(ctx, x, y)),
            ("slots", lambda x, y: _slots(ctx, x, y)),
            ("insert", _insert),
        ],
        "holes": [("holes", lambda x, y: _holes(ctx, x, y))],
        "thin_wall": [
            ("thin_wall", lambda x, y: _thin_walls(x, y, False)),
            ("thin_wall_x", lambda x, y: _thin_walls(x, y, True)),
        ],
        # the underlay's bare cells are the first-layer map: no patches on an underlay plate
        "first_layer": [] if ctx.underlay_layers is not None else [(p, partial(_patch, p)) for p in PATCHES],
        "top_surface": [("top_1", lambda x, y: _top("top_1", x, y))],
    }
    if ctx.underlay_layers is not None and not any(
        ctx.wants(t) for t in TESTS if t != "first_layer"
    ):
        raise GenError(
            f"{PLATE_ID}: with the underlay the first layer is read from its bare cells, there are no "
            "patches; keep another test too, or use --no-underlay for the patches"
        )
    objects: list[PlateObject] = []
    custom: list[CustomBlock] = []
    for test in TESTS:
        if not ctx.wants(test):
            continue
        for spot, make in makers[test]:
            objs, blocks = make(*AT[spot])
            objects += declare(objs, test, UNDERLAY)
            custom += blocks
    _check_layout(objects)
    return Plate(
        PLATE_ID,
        TITLE,
        tuple(objects),
        custom=tuple(custom),
        layer_process={
            "top_shell_layers": 3,
            "bottom_shell_layers": 2,
            "sparse_infill_density": "15%",
            "brim_type": "no_brim",
        },
        seq_process={
            "sparse_infill_density": "15%",
            "brim_type": "no_brim",
            "layer_height": filament.SEQ_LAYER_H,
        },
        seq_filament={"slow_down_layer_time": SEQ_MIN_LAYER_S},
        restore=(
            "M221 S100",
            "M220 S100",
            f"SET_PRESSURE_ADVANCE ADVANCE={ctx.p.stock_pa:g}",
            f"SET_VELOCITY_LIMIT ACCEL={ctx.accel:g} ACCEL_TO_DECEL={ctx.accel / 2:g}",
        ),
        notes=(
            "Tests: "
            + ", ".join(t for t in TESTS if ctx.wants(t))
            + ". Order: hand-written strip and labels "
            "(first layer) → every object by layer → the temperature tower alone, last.",
            "Photograph the plate top-down with light from one side (raking light shows ringing, VFA and "
            "first-layer lines), then the tubes and towers from the front.",
        ),
    )


def _check_layout(objects: list[PlateObject]) -> None:
    """Objects must not touch (≥ 1 mm apart) unless one sits inside the dimension frame's hole."""
    boxes = [(o.name, o.bbox()) for o in objects]
    frame = next((b for n, b in boxes if n == "dimension_frame"), None)

    def inside(b: Box) -> bool:
        return (
            frame is not None
            and frame[0] + DIM_WALL < b[0]
            and b[3] < frame[3] - DIM_WALL
            and (frame[1] + DIM_WALL < b[1] and b[4] < frame[4] - DIM_WALL)
        )

    for i, (na, a) in enumerate(boxes):
        if not (a[0] >= 0 and a[3] <= BED[0] and a[1] >= 0 and a[4] <= BED[1]):
            raise GenError(f"dense plate: {na} leaves the bed ({a})")
        for nb, b in boxes[i + 1 :]:
            if {na, nb} & {"dimension_frame"} and (inside(a) or inside(b)):
                continue
            if (
                a[0] < b[3] + 1
                and b[0] < a[3] + 1
                and a[1] < b[4] + 1
                and b[1] < a[4] + 1
            ):
                raise GenError(f"dense plate: {na} and {nb} overlap")
