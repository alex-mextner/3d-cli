"""gcode_walls.py — the printed wall lines of one object, layer by layer, straight from a sliced G-code.

WHAT: streams an Orca / Snapmaker-Orca / Bambu / PrusaSlicer / Marlin G-code once and returns, for the chosen object,
  every layer's perimeter lines (default: the outer wall, `WALL_FEATURES`) as straight pieces with the extrusion
  width and the layer height the slicer wrote for them. Arcs (G2/G3, centre form) are followed as chords. The object is
  named the way the slicer labels it (`gcode_labels`: EXCLUDE_OBJECT, Bambu blocks, M486, PrusaSlicer comments); a name
  that matches several copies or none, an omitted name on a plate with several objects, and a file with no such wall at
  all are refused with the labels the file has.
WHY: the as-printed fit check (`3d assembly pair --gcode-a ...`) needs where the nozzle really put the wall: the
  slicer's compensation (elephant foot, xy_hole_compensation, line width) moves it away from the model, and a thread or
  a rib clearance is a few tenths of a millimetre. Object and layer labels come from `gcode_labels` (the one label
  reader); the move parsing is kept here on purpose: `gcode_overhangs.parse` merges every feature kind of an object
  into one line set, this reader must keep the feature type, the line width and the path height of each piece.
INVARIANTS:
  - Coordinates are the bed frame of the G-code. XY is absolute unless G91; E is relative after M83, absolute after M82
    (G92 E resets it); the position is unknown (NaN) until the first absolute move and again after G28.
  - A layer starts at the layer marker (Orca / Prusa `;LAYER_CHANGE`, Bambu `; CHANGE_LAYER`); its Z is the `;Z:` /
    `; Z_HEIGHT:` value, the TOP of the layer. The first `;HEIGHT:` / `; LAYER_HEIGHT:` after the marker is only the step
    since the previous layer of the whole plate; the object's own layer height is the `;HEIGHT:` in effect when it
    draws, which is what each piece records. So the mid height `z - h/2`, where the slicer cuts the model, comes from the
    object's own first piece of the layer.
  - `base_z` is the bottom of the object's lowest layer (`z - h`): 0 on the bed, the raft height plus its gap on a raft.
  - A piece with no `;WIDTH:` before it takes the footer's line width for its kind (outer or inner wall).
  - Overhanging stretches of the outer loop are tagged `Overhang wall` by Orca / Bambu (inner loops are too, so the tag
    cannot just be added to the kinds): their length is counted in `Walls.overhang_mm` so the caller can say how much
    of the wall was left out; `kinds` can ask for them.
  - A crafted file cannot exhaust memory: arcs with a radius beyond `MAX_ARC_RADIUS_MM`, pieces beyond `MAX_PIECE_MM`
    and more than `MAX_PIECES` pieces per file are refused with `ValueError`; R-form arcs are refused too.
Stdlib + numpy only; imported lazily from the geometry worker.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt

from gcode_labels import (
    LAYER_MARKERS,
    ObjectLabels,
    feature,
    line_width,
    undecorated,
)
from printer_ops.move_bounds import _MOVE, _WORD, Move
from slicing.mix_gcode_faces import DEFAULT_WIDTH_MM, WALL_FEATURES

MAX_WIDTH_MM = (
    3.0  # a configured line width beyond this is a percentage or a typo, not a nozzle
)
MAX_ARC_RADIUS_MM = 500.0
MAX_PIECE_MM = 2000.0
MAX_PIECES = 3_000_000
ARC_CHORD_MM = 0.2
OVERHANG = "Overhang wall"
_BAMBU_BLOCK = "; start printing object, unique label id:"
_CONFIG_WIDTH = re.compile(
    r"^;\s*(outer_wall_line_width|inner_wall_line_width|external_perimeter_extrusion_width"
    r"|perimeter_extrusion_width)\s*=\s*([\d.]+)\s*$"
)
_OUTER_KEYS = ("outer_wall_line_width", "external_perimeter_extrusion_width")
_KLIPPER_LABEL = re.compile(r"_id_(\d+)_copy_(\d+)$")

Points = npt.NDArray[np.float64]
Vector = npt.NDArray[np.float64]
Piece = tuple[
    float, float, float, float, float, float
]  # x0, y0, x1, y1, width, path height (NaN: unknown)


def densify_segments(
    a: Points, b: Points, spacing_mm: float
) -> tuple[Points, npt.NDArray[np.intp]]:
    """Points every ~`spacing_mm` along the segments a[i] -> b[i] (each start included, each end left to the next
    segment) and, per point, the index of the segment it lies on."""
    if len(a) == 0:
        return np.zeros((0, a.shape[1] if a.ndim == 2 else 2)), np.zeros(
            0, dtype=np.intp
        )
    length = np.linalg.norm(b - a, axis=1)
    count = np.maximum(1, np.ceil(length / spacing_mm).astype(int))
    owner = np.repeat(np.arange(len(count)), count)
    first = np.cumsum(count) - count
    t = ((np.arange(len(owner)) - first[owner]) / count[owner])[:, None]
    return a[owner] + (b[owner] - a[owner]) * t, owner


@dataclass
class WallLayer:
    """One layer of one object: the straight pieces `a -> b` of every chosen feature line and each piece's width."""

    z: float  # the top of the layer on the bed
    h: float  # the object's own layer height (the path height of its first piece)
    a: Points = field(default_factory=lambda: np.zeros((0, 2)))
    b: Points = field(default_factory=lambda: np.zeros((0, 2)))
    width: Vector = field(default_factory=lambda: np.zeros(0))

    @property
    def mid_z(self) -> float:
        """Where the slicer cuts the model for this layer: half a layer below its top."""
        return self.z - self.h / 2.0

    def points(self, spacing_mm: float) -> tuple[Points, Vector]:
        """Centre-line points every ~`spacing_mm` along the pieces, with the width of the piece each lies on."""
        pts, owner = densify_segments(self.a, self.b, spacing_mm)
        return pts, self.width[owner]


@dataclass
class Walls:
    layers: list[WallLayer]
    # every object label the file has, as written (id and copy included)
    objects: list[str]
    # bottom of the object's lowest layer on the bed: 0, or the raft height plus its gap
    base_z: float
    # length of the chosen wall lines, and of the `Overhang wall` lines that were left out
    wall_mm: float
    overhang_mm: float


def _norm(label: str) -> str:
    """Case, `_` / space and the Klipper `_id_N_copy_M` spelling made comparable."""
    label = _KLIPPER_LABEL.sub(r" id:\1 copy \2", label)
    return re.sub(r"[\s_]+", " ", label).strip().casefold()


def _bare(label: str) -> str:
    """The object's own name: the id / copy suffix cut, and Bambu's `NAME — NAME` doubling folded to `NAME`."""
    name = undecorated(label)
    parts = [p.strip() for p in name.split("\u2014")]
    return parts[0] if len(parts) > 1 and len(set(parts)) == 1 else name


def _matches(label: str, wanted: str) -> bool:
    """A project object name (or the slicer's full label with its id and copy) against a G-code object label."""
    suffix = label[len(undecorated(label)) :]
    candidates = (label, undecorated(label), _bare(label), _bare(label) + suffix)
    return _norm(wanted) in {_norm(c) for c in candidates}


def _width(text: str) -> float | None:
    """A finite line width in (0, MAX_WIDTH_MM], else None (a percentage, zero, `nan`, a typo)."""
    try:
        v = float(text)
    except ValueError:
        return None
    return v if 0.0 < v <= MAX_WIDTH_MM else None


def _number_after_colon(line: str) -> float | None:
    """`;Z:0.2` / `; LAYER_HEIGHT: 0.200000` -> the number (linear time, no regex on a hostile line)."""
    try:
        v = float(line.partition(":")[2].strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


@dataclass
class _Reader:
    """Mutable state of the pass; one small method per line kind."""

    wanted: str | None
    kinds: tuple[str, ...]
    labels: ObjectLabels = field(default_factory=ObjectLabels)
    layers: list[WallLayer] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    # distinct (label, Bambu block id) that matched and printed a wall
    printed_by: list[tuple[str, int]] = field(default_factory=list)
    config_widths: dict[str, float] = field(default_factory=dict)
    # NaN until the first `;WIDTH:`; the footer's width fills it in at the end
    width: float = math.nan
    path_h: float = math.nan
    kind: str = ""
    block: int = 0
    x: float = math.nan
    y: float = math.nan
    rel_xy: bool = False
    rel_e: bool = False
    e: float = 0.0
    seg: list[Piece] = field(default_factory=list)
    z: float | None = None
    started: bool = False
    pieces: int = 0
    wall_mm: float = 0.0
    overhang_mm: float = 0.0
    # the object printing now is the wanted one (cached when the label changes)
    owner_ok: bool = False

    def __post_init__(self) -> None:
        self.owner_ok = self.wanted is None

    def open_layer(self) -> None:
        self.close_layer()
        self.started = True
        self.z = None
        self.path_h = math.nan
        self.seg = []

    def close_layer(self) -> None:
        if not self.started or not self.seg or self.z is None:
            return
        a = np.asarray(self.seg, dtype=float)
        known = a[~np.isnan(a[:, 5]), 5]
        h = float(known[0]) if len(known) else 0.0
        self.layers.append(WallLayer(self.z, h, a[:, 0:2], a[:, 2:4], a[:, 4]))

    def comment(self, line: str) -> None:
        body = line[1:].lstrip()
        if line.startswith(LAYER_MARKERS):
            self.open_layer()
        elif body.startswith(("Z:", "Z_HEIGHT:")):
            self.z = _number_after_colon(line)
        elif body.startswith(("HEIGHT:", "LAYER_HEIGHT:")):
            self.path_h = _number_after_colon(line) or self.path_h
        elif (tag := feature(line)) is not None:
            self.kind = tag
        elif (w := line_width(line)) is not None:
            self.width = _width(w) or self.width
        elif (m := _CONFIG_WIDTH.match(line)) is not None:
            width = _width(m.group(2))
            if width is not None:
                self.config_widths[m.group(1)] = width

    def modal(self, up: str) -> bool:
        """G28 / G90 / G91 / G92 (homing, positioning mode, extruder reset); True when `up` was one of them."""
        code = up[:3]
        if code not in ("G28", "G90", "G91", "G92") or up[3:4].isdigit():
            return False
        if code == "G28":
            self.x = self.y = math.nan
        elif code == "G92":
            words = dict(_WORD.findall(up[3:]))
            if "E" in words or not words:
                self.e = float(words.get("E", 0.0))
        else:
            self.rel_xy = code == "G91"
        return True

    def extrusion(self, words: dict[str, str]) -> bool:
        """Advance the extruder position by an E word; True when the move extrudes."""
        if "E" not in words:
            return False
        ev = float(words["E"])
        before = self.e
        self.e = before + ev if self.rel_e else ev
        return (self.e - before) > 1e-6

    def target(self, words: dict[str, str]) -> tuple[float, float]:
        nx, ny = self.x, self.y
        if "X" in words:
            nx = float(words["X"]) + (self.x if self.rel_xy else 0.0)
        if "Y" in words:
            ny = float(words["Y"]) + (self.y if self.rel_xy else 0.0)
        return nx, ny

    def moves_in_object(self, g: int, nx: float, ny: float) -> bool:
        """An extruding move of the wanted object that goes somewhere (or is an arc)."""
        return (
            self.started
            and self.owner_ok
            and not math.isnan(self.x + self.y + nx + ny)
            and (nx != self.x or ny != self.y or g in (2, 3))
        )

    def move(self, line: str) -> None:
        up = line.split(";", 1)[0].strip().upper()
        if self.modal(up):
            return
        m = _MOVE.match(up)
        if m is None:
            return
        g = int(m.group(1))
        words = dict(_WORD.findall(up[m.end() :]))
        nx, ny = self.target(words)
        if self.extrusion(words) and self.moves_in_object(g, nx, ny):
            if self.kind in self.kinds:
                self.add(g, words, nx, ny)
            elif self.kind == OVERHANG:
                self.overhang_mm += math.hypot(nx - self.x, ny - self.y)
        self.x, self.y = nx, ny

    def chords(
        self, g: int, words: dict[str, str], nx: float, ny: float
    ) -> list[tuple[float, float, float, float]]:
        """The straight pieces of one move: itself, or the chords of a centre-form arc."""
        if g not in (2, 3):
            return [(self.x, self.y, nx, ny)]
        if "R" in words:
            raise ValueError(
                "an arc with an R word (radius form) is not supported: slice with I/J arcs"
            )
        ci, cj = float(words.get("I", 0.0)), float(words.get("J", 0.0))
        if math.hypot(ci, cj) > MAX_ARC_RADIUS_MM:
            raise ValueError(
                f"an arc of radius {math.hypot(ci, cj):.0f} mm is not a print move (limit {MAX_ARC_RADIUS_MM:.0f})"
            )
        pieces = Move.arc_move(self.x, self.y, nx, ny, ci, cj, g == 2).chords(
            ARC_CHORD_MM
        )
        return [
            (c.x0, c.y0, c.x1, c.y1) for c in pieces or [Move(self.x, self.y, nx, ny)]
        ]

    def add(self, g: int, words: dict[str, str], nx: float, ny: float) -> None:
        cur = self.labels.current
        if cur is not None and (cur, self.block) not in self.printed_by:
            self.printed_by.append((cur, self.block))
        for x0, y0, x1, y1 in self.chords(g, words, nx, ny):
            length = math.hypot(x1 - x0, y1 - y0)
            if length > MAX_PIECE_MM:
                raise ValueError(
                    f"a wall piece of {length:.0f} mm is not a print move (limit {MAX_PIECE_MM:.0f})"
                )
            self.pieces += 1
            if self.pieces > MAX_PIECES:
                raise ValueError(
                    f"more than {MAX_PIECES} wall pieces: not a print G-code"
                )
            self.wall_mm += length
            self.seg.append((x0, y0, x1, y1, self.width, self.path_h))

    def label_changed(self) -> None:
        cur = self.labels.current
        if cur is not None and cur not in self.objects:
            self.objects.append(cur)
        self.owner_ok = self.wanted is None or (
            cur is not None and _matches(cur, self.wanted)
        )

    def line(self, raw: str) -> None:
        line = raw.strip()
        if not line:
            return
        if line.startswith(_BAMBU_BLOCK):
            with contextlib.suppress(ValueError):
                self.block = int(line[len(_BAMBU_BLOCK) :])
        if self.labels.feed(line):
            self.label_changed()
            return
        c0 = line[0]
        if c0 == ";":
            self.comment(line)
        elif c0 in "Mm":
            head = line.split(";", 1)[0].split()[0].upper()
            if head in ("M82", "M83"):
                self.rel_e = head == "M83"
        elif c0 in "Gg":
            self.move(line)

    def fallback_width(self) -> float:
        """The footer's line width for the kind asked for (outer-wall kinds: the outer width), else the default."""
        outer = any(k in WALL_FEATURES for k in self.kinds)
        keys = (
            _OUTER_KEYS
            if outer
            else ("inner_wall_line_width", "perimeter_extrusion_width")
        )
        return next(
            (self.config_widths[k] for k in keys if k in self.config_widths),
            DEFAULT_WIDTH_MM,
        )


def read_walls(
    path: Path | str,
    obj: str | None = None,
    kinds: tuple[str, ...] = tuple(sorted(WALL_FEATURES)),
) -> Walls:
    """The `kinds` lines of every layer of `obj`, in file order.

    `obj` is a project object name (`Plug x2.stl`) or the slicer's full label (`Plug x2.stl id:1 copy 0`); None takes
    everything the plate prints, which is only accepted for a plate with at most one labelled object. Raises
    `KeyError` when no such wall line exists, `ValueError` when the name matches several copies, the plate has several
    objects and none was named, or the file is not a print G-code (limits above) - the messages list the file's labels.
    """
    reader = _Reader(obj, kinds)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            reader.line(raw)
    reader.close_layer()
    listing = ", ".join(reader.objects) or "none labelled"
    if obj is None and len(reader.objects) > 1:
        raise ValueError(
            f"the plate holds several objects and none was named: {listing}"
        )
    if not reader.layers:
        what = f"object '{obj}'" if obj is not None else "the file"
        raise KeyError(
            f"{what} prints no {'/'.join(kinds)} line; objects in the file: {listing}"
        )
    if len(reader.printed_by) > 1:
        shown = ", ".join(
            f"{label} (block {block})" if block else label
            for label, block in reader.printed_by
        )
        raise ValueError(
            f"object '{obj}' matches several copies: {shown}; "
            "name one with its id and copy, e.g. 'name id:1 copy 0' (copies of one name in a Bambu file cannot be told apart: "
            "slice them on separate plates)"
        )
    fallback = reader.fallback_width()
    for layer in reader.layers:
        layer.width = np.where(np.isnan(layer.width), fallback, layer.width)
    base = min(layer.z - layer.h for layer in reader.layers)
    return Walls(
        reader.layers,
        reader.objects,
        max(base, 0.0),
        reader.wall_mm,
        reader.overhang_mm,
    )
