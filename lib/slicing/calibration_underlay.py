"""calibration_underlay.py — the shared one-piece underlay of every calibration plate (#320).

WHAT: every plate prints all its tests on ONE underlay: 1 solid layer on a single-head printer, 1–2
  on a multi-head one (`calibrations.underlay_layers` of fleet/policy.yaml, default 1), under the
  whole test area. This module is the pure part: the outline, the cut-outs and the sheet as
  rectangles, the layer count and height, the contrast pick of the underlay head, and the
  `underlay` block of the plate manifest `calib-plate/1`. The plate engine applies it
  (calibration_plate_underlay.apply_underlay).
GEOMETRY (all rectilinear, one coordinate-compressed grid, no polygon library):
    outline  union of every footprint (test bases, hand-written blocks, labels), each grown by
             MARGIN, joined into one piece (a bridging strip where two groups lie further apart)
    cut-outs a test that must stand on the bare bed (`cutout`: print-in-place parts, loose parts,
             elephant-foot blocks, through holes measured with a pin) gets a hole of its footprint
             grown by CUT_MARGIN; the outline minus the cut-outs must stay ONE connected piece
    sheet    the piece minus the bases of the tests that stand on it (`on`): each of those tests
             carries its own foot of the underlay's height, overlapping the sheet by SEAM
EVERY TEST DECLARES `on` or `cutout` with a reason (UnderlayDecl, in the plate module's UNDERLAY);
  a test that moves (`movable`) must be `cutout`, so no movable part's footprint touches the sheet.
WHY: tg#22330 — the plate comes off as one labelled sample of the spool and the printer, small
  tests stop tipping and peeling, and the bare underlay is a free first-layer and flatness map.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from slicing.calibration_gen import Box, GenError

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 on the bed
Mode = Literal["on", "cutout"]

SHEET_NAME = "underlay"  # the sheet is an object of the plate (and its EXCLUDE_OBJECT) of this name
MARGIN = 3.0  # mm of underlay around the union of the footprints
CUT_MARGIN = 1.0  # mm of bare bed around a cut-out test
SEAM = 0.05  # a test's foot overlaps the sheet: perimeter spacing, not a double line
LAYERS = (1, 2)
MIN_CONTRAST_DE = 40.0  # CIEDE2000 between the underlay and the tests (plan §8.3.5: 41 is a clear read)
_SNAP = 4  # coordinates are compared at 0.1 µm


@dataclass(frozen=True)
class UnderlayDecl:
    """What the catalog says about one test: stands on the underlay or cuts it, and why."""

    mode: Mode
    reason: str
    movable: bool = (
        False  # the test has a part that moves or comes loose: it MUST be a cutout
    )

    def __post_init__(self) -> None:
        if self.mode not in ("on", "cutout"):
            raise GenError(f"underlay mode must be on or cutout, got {self.mode!r}")
        if not self.reason.strip():
            raise GenError("every test's underlay declaration needs a reason")
        if self.movable and self.mode != "cutout":
            raise GenError(
                "a test with a movable part must be a cutout: the underlay would glue it to the plate"
            )


def check_declarations(tests: Iterable[str], decls: Mapping[str, UnderlayDecl]) -> None:
    """Every test id of a plate has a declaration, and none is declared for an unknown test."""
    ids = set(tests)
    missing = sorted(ids - set(decls))
    extra = sorted(set(decls) - ids)
    if missing or extra:
        raise GenError(
            "underlay declarations do not match the tests: "
            + (f"missing {', '.join(missing)}" if missing else "")
            + ("; " if missing and extra else "")
            + (f"unknown {', '.join(extra)}" if extra else "")
        )


def validate_layers(n: object) -> int:
    """`calibrations.underlay_layers`: an integer 1 or 2."""
    if isinstance(n, bool) or not isinstance(n, int) or n not in LAYERS:
        raise GenError(
            f"underlay layers must be 1 or 2, got {n!r} (calibrations.underlay_layers)"
        )
    return n


def layers_for(heads: int, policy_layers: int) -> int:
    """Single-head: exactly one layer; multi-head: the policy's 1–2."""
    validate_layers(policy_layers)
    return 1 if heads <= 1 else policy_layers


def underlay_height(first_h: float, layer_h: float, layers: int) -> float:
    """Top of the underlay above the bed: the first layer plus the further layers."""
    return round(first_h + (validate_layers(layers) - 1) * layer_h, 6)


# ---- lifting meshes and G-code by the underlay's height ------------------------------------------

_VERTEX = re.compile(r"(vertex\s+\S+\s+\S+\s+)(\S+)")
_ZWORD = re.compile(r"(?<![A-Za-z])Z(-?\d*\.?\d+)")
_ZMARK = re.compile(r"^;Z:([\d.]+)\s*$")


def shift_stl(stl: str, dz: float) -> str:
    """An ASCII STL with every vertex raised by dz."""
    return _VERTEX.sub(lambda m: f"{m.group(1)}{float(m.group(2)) + dz:.7g}", stl)


def merge_stl(*solids: str) -> str:
    """ASCII STL solids as one solid (their facets side by side; the slicer unions the shells)."""
    facets: list[str] = []
    for s in solids:
        lines = s.strip().splitlines()
        if not lines or not lines[0].startswith("solid"):
            raise GenError("underlay: an object's mesh is not an ASCII STL")
        facets += lines[1:-1] if lines[-1].startswith("endsolid") else lines[1:]
    return "solid merged\n" + "\n".join(facets) + "\nendsolid merged\n"


def shift_z(lines: list[str], dz: float) -> list[str]:
    """Hand-written or sliced G-code lifted by dz: every Z word of a G0/G1 move and every `;Z:`
    layer mark."""
    out: list[str] = []
    for line in lines:
        if m := _ZMARK.match(line):
            out.append(f";Z:{float(m.group(1)) + dz:.3f}")
        elif line.startswith(("G1 ", "G0 ")):
            code, sep, comment = line.partition(";")
            code = _ZWORD.sub(lambda m: f"Z{float(m.group(1)) + dz:.3f}", code)
            out.append(code + sep + comment)
        else:
            out.append(line)
    return out


# ---- the grid ----------------------------------------------------------------------------------


def _key(v: float) -> float:
    return round(v, _SNAP)


class _Mask:
    """A set of grid cells: one int per row, bit i = column i. Union, difference and emptiness are
    integer operations on rows, so a plate's few hundred rectangles cost milliseconds and no
    numeric library is needed (`3d calibrate gen` runs on the stdlib alone)."""

    __slots__ = ("rows", "width")

    def __init__(self, rows: list[int], width: int) -> None:
        self.rows, self.width = rows, width

    def __and__(self, other: _Mask) -> _Mask:
        return _Mask(
            [a & b for a, b in zip(self.rows, other.rows, strict=True)], self.width
        )

    def __or__(self, other: _Mask) -> _Mask:
        return _Mask(
            [a | b for a, b in zip(self.rows, other.rows, strict=True)], self.width
        )

    def __invert__(self) -> _Mask:
        full = (1 << self.width) - 1
        return _Mask([r ^ full for r in self.rows], self.width)

    def any(self) -> bool:
        return any(self.rows)

    def cells(self) -> Iterator[tuple[int, int]]:
        for j, row in enumerate(self.rows):
            while row:
                low = row & -row
                yield j, low.bit_length() - 1
                row ^= low


class _Grid:
    """The unique x and y edges of every rectangle of a computation, so a rectangle is a block of
    cells and a union/difference is a mask operation."""

    def __init__(self, *groups: Iterable[Rect]) -> None:
        xs: set[float] = set()
        ys: set[float] = set()
        for group in groups:
            for x0, y0, x1, y1 in group:
                xs.update((_key(x0), _key(x1)))
                ys.update((_key(y0), _key(y1)))
        self.xs = sorted(xs)
        self.ys = sorted(ys)

    def mask(self, rects: Iterable[Rect]) -> _Mask:
        width = max(len(self.xs) - 1, 0)
        rows = [0] * max(len(self.ys) - 1, 0)
        for x0, y0, x1, y1 in rects:
            if x1 <= x0 or y1 <= y0:
                continue
            i0, i1 = bisect_left(self.xs, _key(x0)), bisect_left(self.xs, _key(x1))
            j0, j1 = bisect_left(self.ys, _key(y0)), bisect_left(self.ys, _key(y1))
            bits = ((1 << (i1 - i0)) - 1) << i0
            for j in range(j0, j1):
                rows[j] |= bits
        return _Mask(rows, width)

    def rects(self, mask: _Mask) -> list[Rect]:
        """The mask as few rectangles: runs along x, equal runs of consecutive rows merged."""
        done: list[Rect] = []
        open_runs: dict[
            tuple[int, int], int
        ] = {}  # (i0, i1) -> first row of the open run
        for j in range(len(mask.rows) + 1):
            runs: set[tuple[int, int]] = set()
            row = mask.rows[j] if j < len(mask.rows) else 0
            i = 0
            while row >> i:
                if (row >> i) & 1:
                    k = i
                    while (row >> (k + 1)) & 1:
                        k += 1
                    runs.add((i, k + 1))
                    i = k + 1
                else:
                    i += 1
            for run, j0 in list(open_runs.items()):
                if run not in runs:
                    del open_runs[run]
                    done.append(self._rect(run, j0, j))
            for run in runs:
                open_runs.setdefault(run, j)
        return sorted(done, key=lambda r: (r[1], r[0]))

    def _rect(self, run: tuple[int, int], j0: int, j1: int) -> Rect:
        return (self.xs[run[0]], self.ys[j0], self.xs[run[1]], self.ys[j1])


def grow(r: Rect, d: float) -> Rect:
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _pieces(mask: _Mask) -> int:
    """Connected pieces (edge contact joins cells; a corner does not)."""
    todo = set(mask.cells())
    count = 0
    while todo:
        count += 1
        stack = [todo.pop()]
        while stack:
            j, i = stack.pop()
            for nb in ((j + 1, i), (j - 1, i), (j, i + 1), (j, i - 1)):
                if nb in todo:
                    todo.remove(nb)
                    stack.append(nb)
    return count


def _gap(a: Rect, b: Rect) -> tuple[float, float]:
    return (max(a[0] - b[2], b[0] - a[2], 0.0), max(a[1] - b[3], b[1] - a[3], 0.0))


def _groups(rects: Sequence[Rect]) -> list[list[int]]:
    """Indices of the rectangles grouped by contact along an edge or an area — union-find."""
    parent = list(range(len(rects)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    eps = 1e-9
    for i, a in enumerate(rects):
        for j in range(i + 1, len(rects)):
            b = rects[j]
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox >= -eps and oy >= -eps and (ox > eps or oy > eps):
                parent[find(i)] = find(j)
    out: dict[int, list[int]] = {}
    for i in range(len(rects)):
        out.setdefault(find(i), []).append(i)
    return list(out.values())


def _bridge(a: Rect, b: Rect) -> list[Rect]:
    """Strips that join two separate rectangles: one strip over the shared span when they face
    each other, an L of two strips when they meet only diagonally."""
    right = b[0] >= a[2]
    above = b[1] >= a[3]
    gap_x = (a[2], b[0]) if right else (b[2], a[0])
    gap_y = (a[3], b[1]) if above else (b[3], a[1])
    dx, dy = _gap(a, b)
    y_lo, y_hi = max(a[1], b[1]), min(a[3], b[3])
    x_lo, x_hi = max(a[0], b[0]), min(a[2], b[2])
    if dx > 0 and y_hi > y_lo:
        return [(gap_x[0], y_lo, gap_x[1], y_hi)]
    if dy > 0 and x_hi > x_lo:
        return [(x_lo, gap_y[0], x_hi, gap_y[1])]
    strip_h = (a[2], a[1], b[2], a[3]) if right else (b[0], a[1], a[0], a[3])
    return [strip_h, (b[0], gap_y[0], b[2], gap_y[1])]


def joined(rects: Sequence[Rect]) -> list[Rect]:
    """`rects` plus the strips that make their union ONE connected piece: the groups that touch are
    one; the others are joined shortest gap first (Kruskal), one bridge per join."""
    out = list(rects)
    groups = _groups(out)
    if len(groups) <= 1:
        return out
    parent = list(range(len(groups)))
    of = {i: g for g, members in enumerate(groups) for i in members}

    def find(g: int) -> int:
        while parent[g] != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g

    pairs = sorted(
        (math.hypot(*_gap(out[i], out[j])), i, j)
        for i in range(len(out))
        for j in range(i + 1, len(out))
        if of[i] != of[j]
    )
    left = len(groups) - 1
    for _, i, j in pairs:
        gi, gj = find(of[i]), find(of[j])
        if gi != gj:
            out.extend(_bridge(out[i], out[j]))
            parent[gi] = gj
            left -= 1
            if not left:
                break
    return out


def rect_difference(outer: Rect, holes: Sequence[Rect]) -> list[Rect]:
    """`outer` minus `holes` as rectangles (the bare sheet between the tests)."""
    g = _Grid([outer], holes)
    return g.rects(g.mask([outer]) & ~g.mask(holes))


def piece_count(rects: Sequence[Rect]) -> int:
    """Connected pieces of the union of `rects` (edge contact joins, a corner does not)."""
    g = _Grid(rects)
    return _pieces(g.mask(rects))


# ---- the underlay ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Underlay:
    """The plate's underlay: where it is, how thick, what is cut out."""

    layers: int
    height: float  # top above the bed, mm
    first_h: float
    layer_h: float
    outline: tuple[Rect, ...]  # the union of the footprints grown by MARGIN, one piece
    cutouts: tuple[Rect, ...]  # holes for the tests that stand on the bare bed
    piece: tuple[Rect, ...]  # outline minus cutouts: the one connected polygon
    sheet: tuple[
        Rect, ...
    ]  # piece minus the bases of the tests that stand on it (the mesh)
    bases: tuple[Rect, ...]  # bases of those tests (each carries its own foot)
    bare: tuple[Rect, ...] = ()  # the sheet where nothing stands: blocks, towers, spans excluded
    seam: float = (
        SEAM  # the sheet's holes are this much smaller than the feet: they overlap
    )

    @property
    def pieces(self) -> int:
        """Connected pieces of outline minus cutouts (1 on every valid plate)."""
        g = _Grid(self.outline, self.cutouts)
        return _pieces(g.mask(self.outline) & ~g.mask(self.cutouts))

    @property
    def area_mm2(self) -> float:
        g = _Grid(self.piece)
        return sum(
            (g.xs[i + 1] - g.xs[i]) * (g.ys[j + 1] - g.ys[j])
            for j, i in g.mask(self.piece).cells()
        )

    def bbox(self) -> Rect:
        return (
            min(r[0] for r in self.outline),
            min(r[1] for r in self.outline),
            max(r[2] for r in self.outline),
            max(r[3] for r in self.outline),
        )

    def sheet_boxes(self) -> list[Box]:
        return [(r[0], r[1], 0.0, r[2], r[3], self.height) for r in self.sheet]

    def foot_boxes(self) -> list[Box]:
        """The feet of the tests that stand on the sheet: the base of each, as high as the sheet."""
        return [(r[0], r[1], 0.0, r[2], r[3], self.height) for r in self.bases]

    def covers(self, r: Rect) -> bool:
        """The rectangle lies inside the outline (and so is under the sheet or a foot)."""
        g = _Grid(self.outline, [r])
        return not (g.mask([r]) & ~g.mask(self.outline)).any()

    def overlaps_sheet(self, r: Rect) -> bool:
        """The rectangle shares area with the sheet or a foot: bare-bed parts must not."""
        g = _Grid(self.piece, [r])
        return bool((g.mask([r]) & g.mask(self.piece)).any())

    def manifest(
        self, *, head: str | None, light: str, tool_note: str = ""
    ) -> dict[str, Any]:
        """The `underlay` block of the plate manifest `calib-plate/1`."""
        x0, y0, x1, y1 = self.bbox()
        return {
            "layers": self.layers,
            "top_mm": round(self.height, 3),
            "first_layer_mm": round(self.first_h, 3),
            "bbox": [round(v, 3) for v in (x0, y0, x1, y1)],
            "margin_mm": MARGIN,
            "cutout_margin_mm": CUT_MARGIN,
            "seam_overlap_mm": self.seam,
            "area_mm2": round(self.area_mm2, 1),
            "pieces": self.pieces,
            "cutouts": [[round(v, 3) for v in r] for r in self.cutouts],
            "bare": [[round(v, 3) for v in r] for r in self.bare],
            "head": head,
            "brim": "none: the underlay holds the tests",
            "read": {
                "first_layer_map": "the bare underlay between the tests (`bare` rectangles) is the "
                "first-layer map: vision scores its line coverage, gaps and ridges per cell",
                "uniformity": "the spread of the map over the plate is the bed-flatness and z-offset "
                "hint: a gradient is a tilt, a dull or ridged zone is too low, a gappy one too high",
                "light": light,
                "note": tool_note,
            },
        }


def build_underlay(
    *,
    first_h: float,
    layer_h: float,
    layers: int,
    footprints: Sequence[Rect],
    blocks: Sequence[Rect] = (),
    cutouts: Sequence[Rect] = (),
    bases: Sequence[Rect] = (),
    covered: Sequence[Rect] = (),
    bed: Rect | None = None,
) -> Underlay:
    """The underlay of a plate.

    footprints  bases of every test (on or cutout) and of every sequential tower
    blocks      hand-written blocks and labels: they print ON the sheet, so nothing is cut there
    cutouts     bases of the tests that stand on the bare bed
    bases       bases of the tests with a foot (the sheet has a hole under each)
    covered     what stands on the sheet without a hole under it (hand-written blocks, towers,
                bridge spans and wedges over it): excluded from `bare`, the first-layer map
    bed         usable area: the outline must lie inside it
    """
    if not footprints and not blocks:
        raise GenError("an underlay needs at least one footprint")
    outline = joined([grow(r, MARGIN) for r in (*footprints, *blocks)])
    shrunk = [grow(b, -SEAM) for b in bases]
    g = _Grid(
        covered,
        outline, cutouts, blocks, bases, shrunk, [grow(r, CUT_MARGIN) for r in cutouts]
    )
    for c in cutouts:
        for b in blocks:
            if (g.mask([c]) & g.mask([b])).any():
                raise GenError(
                    f"a cut-out test at {_fmt(c)} overlaps the hand-written block at {_fmt(b)}: "
                    "the block would print in the air"
                )
    out_mask = g.mask(outline)
    if bed is not None:
        bx0, by0, bx1, by1 = bed
        for r in outline:
            if (
                r[0] < bx0 - 1e-6
                or r[1] < by0 - 1e-6
                or r[2] > bx1 + 1e-6
                or r[3] > by1 + 1e-6
            ):
                raise GenError(
                    f"the underlay outline {_fmt(r)} leaves the usable bed {_fmt(bed)}: "
                    f"keep the tests {MARGIN:g} mm from its edge"
                )
    hole = (
        g.mask([grow(c, CUT_MARGIN) for c in cutouts])
        & ~g.mask(blocks)
        & ~g.mask(bases)
    )
    hole |= g.mask(cutouts)
    piece = out_mask & ~hole
    if _pieces(piece) != 1:
        raise GenError(
            f"the underlay falls into {_pieces(piece)} pieces after the cut-outs: "
            "move the cut-out tests apart or away from the plate's edge"
        )
    sheet = piece & ~g.mask(shrunk)  # the sheet reaches SEAM under each foot
    bare = sheet & ~g.mask(covered)
    return Underlay(
        layers=validate_layers(layers),
        height=underlay_height(first_h, layer_h, layers),
        first_h=first_h,
        layer_h=layer_h,
        outline=tuple(g.rects(out_mask)),
        cutouts=tuple(g.rects(hole)),
        piece=tuple(g.rects(piece)),
        sheet=tuple(g.rects(sheet)),
        bare=tuple(g.rects(bare)),
        bases=tuple(bases),
    )


def _fmt(r: Rect) -> str:
    return f"({r[0]:g}, {r[1]:g})-({r[2]:g}, {r[3]:g})"


# ---- the head of the underlay on a multi-head printer -----------------------------------------


def contrast(a: str, b: str) -> float:
    """CIEDE2000 between two #RRGGBB colours."""
    from printops.color import hex_delta_e

    return hex_delta_e(a, b)


def pick_underlay_tool(
    colours: Mapping[str, str],
    test_tool: str,
    override: str | None = None,
    minimum: float = MIN_CONTRAST_DE,
) -> tuple[str, float]:
    """(tool, ΔE): the head whose filament contrasts most with the tests' (light under dark),
    or `override` (`--underlay-tool`). The contrast check of #263: a pair under `minimum`
    cannot be read by a camera or by eye, so it is refused — also for an override."""
    if test_tool not in colours:
        raise GenError(
            f"the tests' tool {test_tool} is not loaded (loaded: {', '.join(colours)})"
        )
    if override is not None:
        if override not in colours:
            raise GenError(
                f"--underlay-tool {override}: no such head (loaded: {', '.join(colours)})"
            )
        if override == test_tool:
            raise GenError(
                f"--underlay-tool {override} is the tests' own head: the underlay needs its own filament"
            )
        candidates = [override]
    else:
        candidates = [t for t in colours if t != test_tool]
    if not candidates:
        raise GenError("a multi-head underlay needs a second loaded head")
    scored = sorted(
        ((contrast(colours[t], colours[test_tool]), t) for t in candidates),
        reverse=True,
    )
    de, tool = scored[0]
    if de < minimum:
        raise GenError(
            f"underlay contrast too low: {tool} {colours[tool]} vs tests {test_tool} "
            f"{colours[test_tool]} is ΔE {de:.0f} (< {minimum:g}); load a lighter or darker spool "
            "on another head"
        )
    return tool, de


def plan_heads(
    heads: Sequence[tuple[str, int]],
    tests_label: str,
    colours: Mapping[str, str],
    override: str | None = None,
) -> tuple[str, str, float]:
    """(underlay head label, its G-code tool, ΔE) of a multi-head printer. `heads` are the fleet's
    (label, 0-based tool index); `colours` what is loaded on each head (label → #RRGGBB, at least
    the tests' head and the candidates). The head is the most contrasting one, or `override`
    (`--underlay-tool`); both go through the contrast check (pick_underlay_tool)."""
    labels = dict(heads)
    if tests_label not in labels:
        raise GenError(
            f"--head {tests_label}: no such head (heads: {', '.join(labels)})"
        )
    known = {k: v for k, v in colours.items() if k in labels}
    for bad in sorted(set(colours) - set(labels)):
        raise GenError(
            f"--head-colours {bad}: no such head (heads: {', '.join(labels)})"
        )
    if override is not None and override not in labels:
        raise GenError(
            f"--underlay-tool {override}: no such head (heads: {', '.join(labels)})"
        )
    if tests_label not in known:
        raise GenError(
            f"the tests' head {tests_label} has no colour: pass --color #RRGGBB or --head-colours"
        )
    if override is not None and override not in known:
        raise GenError(
            f"--underlay-tool {override} needs its colour for the contrast check: "
            f"--head-colours {override}=#RRGGBB"
        )
    if override is None and len(known) < 2:
        raise GenError(
            "a multi-head plate needs the colours loaded on the heads to pick the underlay's: "
            "--head-colours T1=#000000,T4=#FFFFFF (or name the head with --underlay-tool and its colour)"
        )
    label, delta_e = pick_underlay_tool(known, tests_label, override)
    return label, f"T{labels[label]}", delta_e
