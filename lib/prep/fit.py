"""fit.py — does the printed holder hold every item of a dimension table, in every case?

WHAT: `load_items` reads an items table (the Chekhov `phones.json` schema: `cases`, `phones` or `items`,
  optional `non_goals`); `check_fit(mesh, table, spec)` returns one row per item x case with a verdict
  (PASS / RISK / FAIL), the resting pose, the Coulomb friction the pose needs and a reason, plus a
  `matrix()` (items x cases) of those verdicts. Two modes, chosen by `HolderSpec.mode`:
  * "lean": a rigid slab (width W along X, height H, thickness T) stands with its back-bottom corner B
    on a ledge and leans back (top toward +Y in the solver frame, screen facing -Y) until its back face
    meets the model. Rest = the lean where the slab's centre of mass stops going down (`rest_pinned`); the
    friction that pose needs is the smallest mu for which the planar force balance has a solution
    (an LP per contact cluster, `min_friction`). Ported from the Chekhov `phonefit.py` / `phone_fit.py`.
  * "pocket": the item box (+ case margins) must fit inside an axis-aligned pocket box of the model's free
    space: the box must be smaller than the pocket and must find a voxel-empty position inside it.
WHY: checking 28 phones x 4 cases against the stand by hand-slicing took hours; as a table it takes seconds
  and is repeatable when the model changes.
SOLVER FRAME: model print frame (Z up, bed at Z = 0, the desk below Z = 0 is solid). The item's width is
  along X centred at `x_centre_mm`; `front_sign` says which Y direction the screen faces (-1 = Chekhov: the
  slab leans toward +Y). With front_sign +1 the profile is mirrored, solved, and poses mapped back.
HOW (lean): the model is voxelised once at `pitch`; for each slab width the occupancy is swept along X
  into a (Y, Z) profile. A pose is collision-free iff the slab's rotated rectangle covers no profile cell;
  per scan angle the collision test at the seat column is exact row-interval arithmetic on cumulative sums
  (the original FFT of the whole (Y, Z) plane is not needed because only the seat columns are used).
VERDICT per row: FAIL when no pose rests on the ledge, no ledge pose needs less than `risk_mu`, or the rest
  lean is outside `lean_range_deg`; RISK when the rest pose needs more than `pass_mu`; else PASS. The slab is
  set against the back-most seat and slides forward to the first seat that needs <= `risk_mu`, as the
  Chekhov study did; seats beyond the rest seat are not solved. The report verdict folds the rows:
  RISK -> WARN; non-goal items never count; cases outside `required_cases` count at most as WARN.
ASSUMPTIONS: the slab is a rigid box (no camera bump); friction is Coulomb with one mu for every contact;
  the 1 mm contact windows and the 0.5 mm pitch limit the resolution of the contact clusters. mu thresholds
  0.30 / 0.40 are dry PLA against glass or silicone [INFERENCE: typical 0.35..0.6].
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy import ndimage
from scipy.optimize import linprog

from errors import InputNotFound, InvalidArgument
from prep.contracts import Verdict, worst
from prep.voxel import Grid, voxelize

RowVerdict = Literal["PASS", "RISK", "FAIL"]
_ROW_TO_STEP: dict[str, Verdict] = {"PASS": "PASS", "RISK": "WARN", "FAIL": "FAIL"}
_G = 9.81
#: friction above which the planar balance is declared infeasible (a slab that needs more than this never holds)
_MU_MAX = 3.0

Box6 = tuple[float, float, float, float, float, float]


# ---------------------------------------------------------------- items table


@dataclass(frozen=True)
class Item:
    """One thing to hold: outer dimensions in mm, optional mass in grams."""

    id: str
    name: str
    height_mm: float
    width_mm: float
    thick_mm: float
    mass_g: float | None = None
    non_goal: bool = False


@dataclass(frozen=True)
class Case:
    """Margins added to an item (a protective case): per side in width, total in height and thickness."""

    name: str
    d_side_mm: float
    d_height_mm: float
    d_thick_mm: float
    mass_g: float | None = None


@dataclass(frozen=True)
class ItemTable:
    items: tuple[Item, ...]
    cases: tuple[Case, ...]


def load_items(path: str | Path) -> ItemTable:
    """Read an items table: `{"cases": {name: {d_side_mm, d_height_mm, d_thick_mm}}, "phones"|"items": [...],
    "non_goals": [...]}`. Non-goal items are checked and reported but never decide the verdict."""
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(str(p), command="prep")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise InvalidArgument(
            "items table",
            str(p),
            [f"valid JSON ({exc.msg} at line {exc.lineno})"],
            command="prep",
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidArgument(
            "items table",
            str(p),
            ["a JSON object with 'cases' and 'items' (or 'phones')"],
            command="prep",
        )
    if "items" in raw and "phones" in raw:
        raise InvalidArgument(
            "items table",
            str(p),
            ["either 'items' or 'phones', not both"],
            command="prep",
        )
    listed = raw.get("items", raw.get("phones"))
    if not isinstance(listed, list) or not listed:
        raise InvalidArgument(
            "items table",
            str(p),
            ["a non-empty 'items' (or 'phones') list"],
            command="prep",
        )
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, dict) or not raw_cases:
        raise InvalidArgument(
            "items table", str(p), ["a non-empty 'cases' object"], command="prep"
        )
    where = str(p)
    cases = tuple(_case(name, body, where) for name, body in raw_cases.items())
    items = tuple(_item(entry, where, non_goal=False) for entry in listed)
    items += tuple(
        _item(entry, where, non_goal=True) for entry in raw.get("non_goals", [])
    )
    return ItemTable(items=items, cases=cases)


def _number(
    body: Mapping[str, Any], key: str, what: str, where: str, *, required: bool = True
) -> float | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidArgument(
            f"{what}.{key}", repr(value), [f"a number in {where}"], command="prep"
        )
    return float(value)


def _case(name: str, body: Any, where: str) -> Case:
    if not isinstance(body, dict):
        raise InvalidArgument(
            f"case {name}",
            repr(body),
            [f"an object with d_side_mm, d_height_mm, d_thick_mm in {where}"],
            command="prep",
        )
    side, height, thick = (
        float(_number(body, k, f"case {name}", where) or 0.0)
        for k in ("d_side_mm", "d_height_mm", "d_thick_mm")
    )
    return Case(
        name,
        side,
        height,
        thick,
        _number(body, "mass_g", f"case {name}", where, required=False),
    )


def _item(entry: Any, where: str, *, non_goal: bool) -> Item:
    if not isinstance(entry, dict) or "id" not in entry:
        raise InvalidArgument(
            "item",
            repr(entry)[:80],
            [f"an object with id, height_mm, width_mm, thick_mm in {where}"],
            command="prep",
        )
    label = f"item {entry['id']}"
    dims = [
        float(_number(entry, k, label, where) or 0.0)
        for k in ("height_mm", "width_mm", "thick_mm")
    ]
    if min(dims) <= 0.0:
        raise InvalidArgument(
            label, str(dims), ["positive height_mm, width_mm, thick_mm"], command="prep"
        )
    return Item(
        id=str(entry["id"]),
        name=str(entry.get("name", entry["id"])) + (" (non-goal)" if non_goal else ""),
        height_mm=dims[0],
        width_mm=dims[1],
        thick_mm=dims[2],
        mass_g=_number(entry, "mass_g", label, where, required=False),
        non_goal=non_goal,
    )


# ---------------------------------------------------------------- holder spec


@dataclass(frozen=True)
class HolderSpec:
    """Every holder-specific constant of the check. Defaults are the Chekhov cat stand's."""

    mode: Literal["lean", "pocket"] = "lean"
    # --- lean mode
    #: accepted rest lean (degrees from vertical) of a PASS/RISK pose
    lean_range_deg: tuple[float, float] = (8.0, 36.0)
    #: back-bottom corner positions along Y on the ledge, in model coordinates (mm)
    seat_y0_mm: tuple[float, ...] = (-43.0, -42.0, -41.0, -40.0, -39.0)
    #: +1 when the screen faces +Y (the slab leans toward -Y), -1 when it faces -Y (Chekhov)
    front_sign: Literal[-1, 1] = -1
    #: height of the ledge the corner rests on; a pose that settles elsewhere is not "on the ledge". None disables
    #: (the default: the Chekhov stand's ledge steps up between 12.5 and 22 mm and the reference study never checked it).
    ledge_z_mm: float | None = None
    ledge_tol_mm: float = 1.5
    #: PASS if the rest pose needs at most this friction coefficient, RISK up to `risk_mu`, FAIL above
    pass_mu: float = 0.30
    risk_mu: float = 0.40
    #: extra clearance on each side of the slab width (mm) on top of the case margin
    slab_width_margin_mm: float = 0.0
    #: X coordinate the slab is centred on
    x_centre_mm: float = 0.0
    #: lean angles scanned (degrees from vertical) and their step
    scan_deg: tuple[float, float] = (3.0, 45.0)
    scan_step_deg: float = 1.0
    #: back-face contacts closer to B than this are the ledge itself, farther ones are "top" contacts
    contact_split_mm: float = 12.0
    #: half-thickness of the band around the back face (mm) that counts as touching it
    contact_tol_mm: float = 0.9
    #: contacts closer than this along the back face are one cluster
    cluster_gap_mm: float = 4.0
    #: a model cell touching the slab's front face within this height of B is a front stop (lip)
    lip_height_mm: float = 25.0
    #: mass used when an item has none, and the extra mass per case name (case `mass_g` in the table wins)
    default_mass_g: float = 190.0
    case_mass_g: Mapping[str, float] = field(
        default_factory=lambda: {
            "none": 0.0,
            "typical": 25.0,
            "bulky": 40.0,
            "rugged": 60.0,
        }
    )
    # --- pocket mode
    #: (xmin, ymin, zmin, xmax, ymax, zmax) of the pocket in model coordinates
    pocket: Box6 | None = None
    #: model axis (0 = X, 1 = Y, 2 = Z) along which the item's (width, thickness, height) is inserted
    item_axes: tuple[int, int, int] = (0, 1, 2)
    #: slack (pocket - item, mm) below this is a RISK
    pocket_min_slack_mm: float = 0.5
    # --- verdict folding
    #: only these cases decide the report verdict (others count at most as WARN); None = every case decides
    required_cases: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("lean", "pocket"):
            raise InvalidArgument(
                "mode", str(self.mode), ["lean", "pocket"], command="prep"
            )
        if self.mode == "pocket":
            if self.pocket is None or not all(
                self.pocket[i] < self.pocket[i + 3] for i in range(3)
            ):
                raise InvalidArgument(
                    "pocket",
                    str(self.pocket),
                    ["(xmin, ymin, zmin, xmax, ymax, zmax) with min < max"],
                    command="prep",
                )
            if sorted(self.item_axes) != [0, 1, 2]:
                raise InvalidArgument(
                    "item_axes",
                    str(self.item_axes),
                    ["a permutation of (0, 1, 2)"],
                    command="prep",
                )
        else:
            if not self.seat_y0_mm:
                raise InvalidArgument(
                    "seat_y0_mm", "()", ["at least one seat position"], command="prep"
                )
            if self.front_sign not in (-1, 1):
                raise InvalidArgument(
                    "front_sign", str(self.front_sign), ["-1", "1"], command="prep"
                )
            if not 0.0 < self.lean_range_deg[0] < self.lean_range_deg[1] < 90.0:
                raise InvalidArgument(
                    "lean_range_deg",
                    str(self.lean_range_deg),
                    ["0 < low < high < 90"],
                    command="prep",
                )
            if not (0.0 <= self.pass_mu <= self.risk_mu):
                raise InvalidArgument(
                    "pass_mu/risk_mu",
                    f"{self.pass_mu}/{self.risk_mu}",
                    ["0 <= pass_mu <= risk_mu"],
                    command="prep",
                )
            if (
                self.scan_step_deg <= 0.0
                or not 0.0 < self.scan_deg[0] < self.scan_deg[1] < 90.0
            ):
                raise InvalidArgument(
                    "scan_deg/scan_step_deg",
                    f"{self.scan_deg}/{self.scan_step_deg}",
                    ["0 < lo < hi < 90 and a positive step"],
                    command="prep",
                )


# ---------------------------------------------------------------- report


@dataclass
class FitRow:
    """One item x case."""

    item_id: str
    item: str
    case: str
    non_goal: bool
    width_mm: float
    height_mm: float
    thick_mm: float
    mass_g: float
    verdict: RowVerdict
    reason: str
    # lean mode
    phi_deg: float | None = None
    y0_mm: float | None = None
    z0_mm: float | None = None
    mu_req: float | None = None
    #: (smallest, largest) friction over the seats tried, back-most seat first, up to and including the rest seat
    mu_window: list[float] | None = None
    front_stop: bool | None = None
    contact_s_mm: list[float] | None = None
    # pocket mode
    position_mm: list[float] | None = None
    slack_mm: float | None = None
    clearance_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FitReport:
    """Fit table result (a `StepResult`)."""

    verdict: Verdict
    mode: str
    rows: list[FitRow]
    items: list[str]
    cases: list[str]
    spec: dict[str, Any]
    reasons: list[str] = field(default_factory=list)

    def matrix(self) -> dict[str, dict[str, RowVerdict]]:
        """Item id -> case name -> row verdict, in table order."""
        out: dict[str, dict[str, RowVerdict]] = {i: {} for i in self.items}
        for r in self.rows:
            out[r.item_id][r.case] = r.verdict
        return out

    def counts(self) -> dict[str, int]:
        counts = {"PASS": 0, "RISK": 0, "FAIL": 0}
        for r in self.rows:
            counts[r.verdict] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "counts": self.counts(),
            "cases": list(self.cases),
            "items": list(self.items),
            "matrix": self.matrix(),
            "spec": self.spec,
            "rows": [r.to_dict() for r in self.rows],
        }

    def summary(self) -> str:
        c = self.counts()
        return f"{len(self.items)} items x {len(self.cases)} cases ({self.mode}): {c['PASS']} PASS, {c['RISK']} RISK, {c['FAIL']} FAIL"


def check_fit(
    mesh: trimesh.Trimesh, table: ItemTable, spec: HolderSpec, *, pitch: float = 0.5
) -> FitReport:
    """Check every item x case of `table` against `mesh` (print frame) as described in the module header."""
    if pitch <= 0.0:
        raise InvalidArgument(
            "pitch", str(pitch), ["a positive length in mm"], command="prep"
        )
    grid = voxelize(mesh, pitch)
    rows = (
        _lean_rows(grid, table, spec)
        if spec.mode == "lean"
        else _pocket_rows(grid, table, spec)
    )
    required = None if spec.required_cases is None else set(spec.required_cases)
    folded: list[Verdict] = []
    for r in rows:
        if r.non_goal:
            continue
        v = _ROW_TO_STEP[r.verdict]
        folded.append(
            v if required is None or r.case in required or v == "PASS" else "WARN"
        )
    worst_rows = [
        r
        for r in rows
        if not r.non_goal
        and r.verdict == "FAIL"
        and (required is None or r.case in required)
    ]
    reasons = [f"{r.item} / {r.case}: {r.reason}" for r in worst_rows[:10]]
    return FitReport(
        verdict=worst(folded),
        mode=spec.mode,
        rows=rows,
        items=[i.id for i in table.items],
        cases=[c.name for c in table.cases],
        spec=_jsonable(asdict(spec)),
        reasons=reasons,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _row_base(
    item: Item, case: Case, spec: HolderSpec
) -> tuple[float, float, float, float]:
    """Effective (width, height, thickness, mass) of the item in its case."""
    width = item.width_mm + 2.0 * (case.d_side_mm + spec.slab_width_margin_mm)
    height = item.height_mm + case.d_height_mm
    thick = item.thick_mm + case.d_thick_mm
    extra = (
        case.mass_g if case.mass_g is not None else spec.case_mass_g.get(case.name, 0.0)
    )
    return (
        width,
        height,
        thick,
        (item.mass_g if item.mass_g is not None else spec.default_mass_g) + extra,
    )


# ---------------------------------------------------------------- friction LP


def min_friction(
    phi_deg: float,
    height: float,
    thick: float,
    s_tops: list[float],
    mass_kg: float,
    *,
    lip: bool = False,
    tol: float = 1e-3,
) -> float:
    """Smallest Coulomb mu with a planar equilibrium of a slab leaning at `phi_deg` from vertical.

    Bottom: the back-bottom corner B on the ledge (normal +Z, friction along Y). Top: one normal + friction
    pair per entry of `s_tops` (distance from B along the back face; normal perpendicular to the face,
    friction along it). With `lip` a horizontal reaction >= 0 at the front-bottom corner also helps.
    Returns 0.0 if no friction is needed and `inf` if mu > 3 would not suffice. The result is the upper end
    of a bisection, so it is at most `tol` above the true minimum."""
    ph = math.radians(phi_deg)
    u = np.array([math.sin(ph), math.cos(ph)])
    n = np.array([-math.cos(ph), math.sin(ph)])

    def cross(a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
        return float(a[0] * b[1] - a[1] * b[0])

    weight = mass_kg * _G
    centre = (height / 2.0) * u + (thick / 2.0) * n
    k = len(s_tops)
    cols_y = [0.0, 1.0] + [v for _ in s_tops for v in (n[0], u[0])] + [1.0]
    cols_z = [1.0, 0.0] + [v for _ in s_tops for v in (n[1], u[1])] + [0.0]
    cols_m = (
        [0.0, 0.0]
        + [v for s in s_tops for v in (cross(s * u, n), cross(s * u, u))]
        + [cross(thick * n, np.array([1.0, 0.0]))]
    )
    a_eq = np.array([cols_y, cols_z, cols_m])
    b_eq = np.array([0.0, weight, -cross(centre, np.array([0.0, -weight]))])
    nv = 2 + 2 * k + 1
    bounds: list[tuple[float | None, float | None]] = (
        [(0, None), (None, None)]
        + [(0, None), (None, None)] * k
        + [(0, None if lip else 0)]
    )
    cone = [(0, 1, +1.0), (0, 1, -1.0)] + [
        (2 + 2 * i, 3 + 2 * i, sg) for i in range(k) for sg in (+1.0, -1.0)
    ]

    def feasible(mu: float) -> bool:
        a_ub = np.zeros((len(cone), nv))
        for r, (normal_col, fric_col, sign) in enumerate(cone):
            a_ub[r, normal_col] = -mu
            a_ub[r, fric_col] = sign
        res = linprog(
            np.zeros(nv),
            A_ub=a_ub,
            b_ub=np.zeros(len(cone)),
            A_eq=a_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
        return bool(res.status == 0)

    if not feasible(_MU_MAX):
        return math.inf
    if feasible(0.0):
        return 0.0
    lo, hi = 0.0, _MU_MAX
    while hi - lo > tol:
        mid = (lo + hi) / 2.0
        if feasible(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _clusters(values: NDArray[np.float64], gap: float) -> list[float]:
    """Medians of runs of sorted `values` whose neighbours are closer than `gap`."""
    if len(values) == 0:
        return []
    vals = np.sort(values)
    out: list[float] = []
    run = [float(vals[0])]
    for v in vals[1:]:
        if v - run[-1] > gap:
            out.append(float(np.median(run)))
            run = [float(v)]
        else:
            run.append(float(v))
    out.append(float(np.median(run)))
    return out


# ---------------------------------------------------------------- lean mode


class _Mask(NamedTuple):
    """A rotated slab rectangle as per-row z intervals, relative to the corner index B = (iy, kb)."""

    row_off: NDArray[np.intp]  # y-row offsets that contain cells
    z_lo: NDArray[np.intp]  # first covered z-cell offset in that row
    z_hi: NDArray[np.intp]  # last covered z-cell offset (inclusive)


@dataclass
class _Profile:
    """(Y, Z) occupancy of the model swept over one slab width, in the solver frame (front = -Y)."""

    occ: NDArray[np.bool_]  # (ny, ncol): desk (z < 0) True in the first `zpad` columns
    cum: NDArray[np.int32]  # cumulative sums along Z with a leading zero column
    top_cell: NDArray[
        np.intp
    ]  # per y row, index of the highest occupied cell (the desk guarantees one)
    y_lo: float
    zpad: int
    cells_yz: NDArray[np.float64]  # centres (y, z) of the model's occupied cells
    pitch: float

    def seat_row(self, y0: float) -> int:
        return int(round((y0 - self.y_lo) / self.pitch))


class _Pose(NamedTuple):
    phi: float
    z0: float


def _rect_mask(phi: float, height: float, thick: float, h: float) -> _Mask:
    """Cells whose centres lie in the slab rectangle with B at the origin (back face along u, thickness along n).

    Per y row the rectangle is the intersection of two z intervals (s in [0, H] and t in [0, T]), so the
    covered cells are those with centres in it; valid for 0 < phi < 90 deg."""
    su, cu = math.sin(phi), math.cos(phi)
    corner_y = [a * su - b * cu for a in (0.0, height) for b in (0.0, thick)]
    rows = np.arange(math.floor(min(corner_y) / h), math.ceil(max(corner_y) / h) + 1)
    y = (rows + 0.5) * h
    z_lo = np.maximum(-y * su / cu, y * cu / su)
    z_hi = np.minimum((height - y * su) / cu, (thick + y * cu) / su)
    first = np.ceil(z_lo / h - 0.5).astype(np.intp)
    last = np.floor(z_hi / h - 0.5).astype(np.intp)
    keep = first <= last
    return _Mask(rows[keep].astype(np.intp), first[keep], last[keep])


def _scan_masks(
    phis: NDArray[np.float64], height: float, thick: float, h: float
) -> list[tuple[float, _Mask]]:
    """(lean in degrees, slab mask) for every scan angle past the hump; shared by all seats of one item x case."""
    # beyond this tilt the centre of mass is behind the pivot corner
    hump = math.degrees(math.atan(thick / height)) + 1.5
    return [
        (float(pd), _rect_mask(math.radians(float(pd)), height, thick, h))
        for pd in phis
        if pd >= hump
    ]


def _top_collision(prof: _Profile, iy: int, mask: _Mask) -> int | None:
    """Highest corner height kb (cells) at which the slab with its corner in row `iy` still collides with the
    profile; None when the corner row is outside the profile or the collision reaches the top of the profile.

    A row collides for every kb whose cell interval holds an occupied cell, so its highest colliding kb is set by
    the row's topmost occupied cell; the slab's highest collision is the maximum over its rows."""
    rows = iy + mask.row_off
    ok = (rows >= 0) & (
        rows < prof.top_cell.shape[0]
    )  # rows outside the profile are air
    if not ok.any():
        return None
    kb_top = int((prof.top_cell[rows[ok]] - prof.zpad - mask.z_lo[ok]).max())
    kb_limit = prof.occ.shape[1] - 1 - prof.zpad - int(mask.z_hi.max())
    return None if kb_top >= kb_limit - 1 else kb_top


def _collides(prof: _Profile, iy: int, kb: int, mask: _Mask) -> bool:
    """Whether the slab with its corner at (row `iy`, height `kb` cells) covers an occupied cell."""
    rows = iy + mask.row_off
    ok = (rows >= 0) & (rows < prof.top_cell.shape[0])
    base = prof.zpad + kb
    counts = (
        prof.cum[rows[ok], base + mask.z_hi[ok] + 1]
        - prof.cum[rows[ok], base + mask.z_lo[ok]]
    )
    return bool((counts > 0).any())


def _rest_pinned(
    prof: _Profile,
    iy: int,
    scan: list[tuple[float, _Mask]],
    height: float,
    thick: float,
) -> _Pose | None:
    """Slab with B held at row `iy` (friction): drop it at each lean and pivot it back while its centre keeps
    going down. None when the column is blocked all the way up or the slab never gets going."""
    h = prof.pitch
    prev: tuple[float, float, float] | None = None  # (centre height, z0, phi)
    for pd, mask in scan:
        kb_top = _top_collision(prof, iy, mask)
        if kb_top is None:
            return None
        z0 = (kb_top + 1) * h
        phi = math.radians(pd)
        e = z0 + (height / 2.0) * math.cos(phi) + (thick / 2.0) * math.sin(phi)
        if prev is not None and e > prev[0] + 1e-6:
            return _Pose(
                _touch_angle(
                    prof, iy, int(round(prev[1] / h)), prev[2], pd, height, thick
                ),
                prev[1],
            )
        prev = (e, z0, pd)
    return None if prev is None else _Pose(prev[2], prev[1])


#: bisection steps between two scan angles: 1 degree / 2**6; without it the back face of a tall slab can sit
#: up to s * sin(step) (about 1 mm at s = 60 mm) short of the surface it leans on and miss it in the contact band
_REFINE_STEPS = 6


def _touch_angle(
    prof: _Profile,
    iy: int,
    kb: int,
    phi_free: float,
    phi_blocked: float,
    height: float,
    thick: float,
) -> float:
    """Largest lean in [phi_free, phi_blocked] at which the slab, held with its corner at row `iy`, height `kb`,
    is still collision-free: the lean at which its back face first touches the model."""
    lo, hi = phi_free, phi_blocked
    for _ in range(_REFINE_STEPS):
        mid = (lo + hi) / 2.0
        if _collides(
            prof, iy, kb, _rect_mask(math.radians(mid), height, thick, prof.pitch)
        ):
            hi = mid
        else:
            lo = mid
    return lo


def _slab_coords(
    prof: _Profile, y0: float, z0: float, phi_deg: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """(s, t) of every model cell in the slab frame: s along the back face from B, t along the face normal."""
    phi = math.radians(phi_deg)
    d = prof.cells_yz - np.array([y0, z0])
    return d @ np.array([math.sin(phi), math.cos(phi)]), d @ np.array(
        [-math.cos(phi), math.sin(phi)]
    )


def _lean_rows(grid: Grid, table: ItemTable, spec: HolderSpec) -> list[FitRow]:
    h = grid.pitch
    mirror = spec.front_sign == 1
    sizes = [_row_base(i, c, spec) for i in table.items for c in table.cases]
    zpad = int(math.ceil(max(s[1] + s[2] for s in sizes) / h)) + 8
    y_all = [
        float(grid.origin[1]),
        float(grid.origin[1]) + grid.shape[1] * h,
        *spec.seat_y0_mm,
    ]
    y_lo = math.floor((min(y_all) - 2.0) / h) * h
    ny = int(round((math.ceil((max(y_all) + 2.0) / h) * h - y_lo) / h))
    nz_model = max(int(math.ceil((float(grid.origin[2]) + grid.shape[2] * h) / h)), 1)
    ncol = zpad + nz_model + zpad
    phis = np.arange(spec.scan_deg[0], spec.scan_deg[1] + 1e-9, spec.scan_step_deg)
    seats = [(-y if mirror else y) for y in spec.seat_y0_mm]
    profiles: dict[float, _Profile] = {}
    rows: list[FitRow] = []
    for item in table.items:
        for case in table.cases:
            width, height, thick, mass = _row_base(item, case, spec)
            key = round(width, 6)
            if key not in profiles:
                profiles[key] = _profile(
                    grid,
                    spec.x_centre_mm - width / 2.0,
                    spec.x_centre_mm + width / 2.0,
                    y_lo,
                    ny,
                    zpad,
                    ncol,
                    mirror,
                )
            rows.append(
                _lean_row(
                    profiles[key],
                    item,
                    case,
                    spec,
                    (width, height, thick, mass),
                    phis,
                    seats,
                    mirror,
                )
            )
    return rows


def _profile(
    grid: Grid,
    x_lo: float,
    x_hi: float,
    y_lo: float,
    ny: int,
    zpad: int,
    ncol: int,
    mirror: bool,
) -> _Profile:
    h = grid.pitch
    xs = grid.centres(0)
    sel = (xs >= x_lo) & (xs <= x_hi)
    if not sel.any():  # a slab narrower than one cell still sees the nearest cell
        sel = np.zeros_like(sel)
        sel[int(np.argmin(np.abs(xs - (x_lo + x_hi) / 2.0)))] = True
    swept = np.asarray(grid.occ[sel].any(axis=0))
    iy = grid.index(y_lo + (np.arange(ny) + 0.5) * h, 1)
    iz = grid.index((np.arange(ncol - zpad) + 0.5) * h, 2)
    ok_y, ok_z = (iy >= 0) & (iy < swept.shape[0]), (iz >= 0) & (iz < swept.shape[1])
    model = np.zeros((ny, ncol - zpad), dtype=bool)
    model[np.ix_(ok_y, ok_z)] = swept[np.ix_(iy[ok_y], iz[ok_z])]
    occ = np.concatenate([np.ones((ny, zpad), dtype=bool), model], axis=1)
    if mirror:
        occ = occ[::-1].copy()
        model = model[::-1]
        y_lo = -(y_lo + ny * h)
    cum = np.zeros((ny, ncol + 1), dtype=np.int32)
    np.cumsum(occ, axis=1, out=cum[:, 1:])
    top_cell = (ncol - 1 - occ[:, ::-1].argmax(axis=1)).astype(np.intp)
    cells = np.argwhere(model)
    centres = np.column_stack([y_lo + (cells[:, 0] + 0.5) * h, (cells[:, 1] + 0.5) * h])
    return _Profile(
        occ=occ,
        cum=cum,
        top_cell=top_cell,
        y_lo=y_lo,
        zpad=zpad,
        cells_yz=centres,
        pitch=h,
    )


def _lean_row(
    prof: _Profile,
    item: Item,
    case: Case,
    spec: HolderSpec,
    size: tuple[float, float, float, float],
    phis: NDArray[np.float64],
    seats: list[float],
    mirror: bool,
) -> FitRow:
    width, height, thick, mass = size
    row = FitRow(
        item.id,
        item.name,
        case.name,
        item.non_goal,
        round(width, 2),
        round(height, 2),
        round(thick, 2),
        mass,
        "FAIL",
        "",
    )
    scan = _scan_masks(phis, height, thick, prof.pitch)
    poses: list[dict[str, Any]] = []
    rest: dict[str, Any] | None = None
    rested = 0
    # set down against the back-most seat; it slides toward the front to the first seat that needs <= risk_mu,
    # so the seats beyond that one are never solved (each costs a dozen LPs)
    for y0 in sorted(seats, reverse=True):
        iy = prof.seat_row(y0)
        y0g = prof.y_lo + iy * prof.pitch
        pose = _rest_pinned(prof, iy, scan, height, thick)
        if pose is None:
            continue
        if (
            spec.ledge_z_mm is not None
            and abs(pose.z0 - spec.ledge_z_mm) > spec.ledge_tol_mm
        ):
            continue  # it settled on something other than the ledge (a lip, the desk)
        rested += 1
        s, t = _slab_coords(prof, y0g, pose.z0, pose.phi)
        back = (np.abs(t) < spec.contact_tol_mm) & (s > -0.6) & (s < height)
        tops = _clusters(s[back & (s > spec.contact_split_mm)], spec.cluster_gap_mm)
        if not tops:
            continue
        front = bool(
            (
                (np.abs(t - thick) < spec.contact_tol_mm)
                & (s > 0.5)
                & (s < spec.lip_height_mm)
            ).any()
        )
        mu = min_friction(pose.phi, height, thick, tops, mass / 1000.0, lip=front)
        found: dict[str, Any] = {
            "y0": -y0g if mirror else y0g,
            "phi": pose.phi,
            "z0": pose.z0,
            "tops": tops,
            "front": front,
            "mu": mu if math.isfinite(mu) else None,
        }
        poses.append(found)
        if mu <= spec.risk_mu:
            rest = found
            break
    if not poses:
        row.reason = (
            "no resting pose on the ledge (the slab is blocked or settles off the ledge)"
            if rested == 0
            else "the slab rests on the ledge but touches nothing above it, so it falls to the scan limit"
        )
        return row
    mus = [p["mu"] for p in poses if p["mu"] is not None]
    row.mu_window = [round(min(mus), 3), round(max(mus), 3)] if mus else None
    if rest is None:
        row.reason = f"no pose on the ledge needs less than mu {spec.risk_mu:g}"
        return row
    row.phi_deg, row.y0_mm, row.z0_mm = round(rest["phi"], 2), rest["y0"], rest["z0"]
    row.mu_req, row.front_stop = round(rest["mu"], 3), rest["front"]
    row.contact_s_mm = [round(v, 1) for v in rest["tops"]]
    lo, hi = spec.lean_range_deg
    if not lo <= rest["phi"] <= hi:
        row.reason = f"lean {rest['phi']:.1f} deg is outside {lo:g}..{hi:g} deg"
    elif rest["mu"] > spec.pass_mu:
        row.verdict, row.reason = "RISK", f"rest pose needs friction {rest['mu']:.2f}"
    else:
        row.verdict = "PASS"
        row.reason = f"rest pose needs friction {rest['mu']:.2f}" + (
            " (front stop)" if rest["front"] else ""
        )
    return row


# ---------------------------------------------------------------- pocket mode


#: how far around the item box (mm) material is searched for the reported clearance; farther counts as this
_CLEARANCE_CAP_MM = 10.0


def _crop(
    grid: Grid, lo_cell: NDArray[np.intp], hi_cell: NDArray[np.intp]
) -> NDArray[np.bool_]:
    """Occupancy of the cell range [lo_cell, hi_cell); cells outside the voxel grid are air."""
    out = np.zeros(tuple(int(v) for v in hi_cell - lo_cell), dtype=bool)
    src_lo = np.maximum(lo_cell, 0)
    src_hi = np.minimum(hi_cell, np.array(grid.shape))
    if np.all(src_hi > src_lo):
        dst = tuple(
            slice(int(s - c), int(e - c))
            for s, e, c in zip(src_lo, src_hi, lo_cell, strict=True)
        )
        out[dst] = grid.occ[
            tuple(slice(int(s), int(e)) for s, e in zip(src_lo, src_hi, strict=True))
        ]
    return out


def _side_clearance_mm(
    grid: Grid, box_lo: NDArray[np.intp], need: NDArray[np.intp]
) -> float:
    """Distance from the placed item box to the nearest material beside or above it (the floor it rests on is
    ignored), measured on the voxel grid and capped at `_CLEARANCE_CAP_MM`."""
    h = grid.pitch
    pad = int(math.ceil(_CLEARANCE_CAP_MM / h))
    region_lo = box_lo - pad
    occ = _crop(grid, region_lo, box_lo + need + pad)
    occ[:, :, :pad] = False  # everything below the box bottom
    if not occ.any():
        return _CLEARANCE_CAP_MM
    dist = ndimage.distance_transform_edt(~occ, sampling=h)
    inside = dist[
        pad : pad + int(need[0]), pad : pad + int(need[1]), pad : pad + int(need[2])
    ]
    return float(min(max(0.0, float(inside.min()) - h), _CLEARANCE_CAP_MM))


def _pocket_rows(grid: Grid, table: ItemTable, spec: HolderSpec) -> list[FitRow]:
    assert spec.pocket is not None  # HolderSpec guarantees it in pocket mode
    h = grid.pitch
    lo = np.array(spec.pocket[:3])
    hi = np.array(spec.pocket[3:])
    lo_cell = np.ceil((lo - grid.origin) / h - 1e-6).astype(
        np.intp
    )  # first cell fully inside the pocket
    hi_cell = np.floor((hi - grid.origin) / h + 1e-6).astype(
        np.intp
    )  # one past the last
    shape = np.array(hi_cell - lo_cell)
    occupied = _crop(grid, lo_cell, hi_cell)
    cum = np.zeros(tuple(int(s) + 1 for s in shape), dtype=np.int32)
    cum[1:, 1:, 1:] = occupied.astype(np.int32).cumsum(0).cumsum(1).cumsum(2)
    pocket_dims = hi - lo
    rows: list[FitRow] = []
    for item in table.items:
        for case in table.cases:
            width, height, thick, mass = _row_base(item, case, spec)
            dims = np.zeros(3)
            for value, axis in zip((width, thick, height), spec.item_axes, strict=True):
                dims[axis] = value
            row = FitRow(
                item.id,
                item.name,
                case.name,
                item.non_goal,
                round(width, 2),
                round(height, 2),
                round(thick, 2),
                mass,
                "FAIL",
                "",
            )
            slack = pocket_dims - dims
            row.slack_mm = round(float(slack.min()), 3)
            if (slack < 0.0).any():
                along = "XYZ"[int(slack.argmin())]
                row.reason = f"item is {-float(slack.min()):.1f} mm too large for the pocket along {along}"
                rows.append(row)
                continue
            # item size in cells, never more than the pocket (alignment of the pocket to the grid must not invent a failure)
            need = np.minimum(np.ceil(dims / h - 1e-6).astype(np.intp), shape)
            valid = np.argwhere(_box_counts(cum, need) == 0)
            if len(valid) == 0:
                row.reason = (
                    "model material inside the pocket blocks every position of the item"
                )
                rows.append(row)
                continue
            centre = (shape - need) / 2.0
            # lowest position first (the item rests on the pocket floor), then the most central one
            key = (
                valid[:, 2] * 1e6
                + (valid[:, 0] - centre[0]) ** 2
                + (valid[:, 1] - centre[1]) ** 2
            )
            best = valid[int(np.argmin(key))]
            box_lo = best + lo_cell
            row.position_mm = [round(float(v), 3) for v in grid.origin + box_lo * h]
            row.clearance_mm = round(_side_clearance_mm(grid, box_lo, need), 3)
            if float(slack.min()) < spec.pocket_min_slack_mm:
                row.verdict = "RISK"
                row.reason = f"fits with only {float(slack.min()):.2f} mm to spare (< {spec.pocket_min_slack_mm:g} mm)"
            else:
                row.verdict = "PASS"
                row.reason = f"fits with {float(slack.min()):.1f} mm to spare"
            rows.append(row)
    return rows


def _box_counts(cum: NDArray[np.int32], need: NDArray[np.intp]) -> NDArray[np.int32]:
    """Occupied-cell count of the `need`-sized box at every position, by inclusion-exclusion on 3-D prefix sums."""
    nx, ny, nz = (int(v) for v in need)
    sx, sy, sz = (
        cum.shape[0] - 1 - nx + 1,
        cum.shape[1] - 1 - ny + 1,
        cum.shape[2] - 1 - nz + 1,
    )
    total = np.zeros((sx, sy, sz), dtype=np.int32)
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                sign = -1 if (dx + dy + dz) % 2 == 0 else 1
                total += (
                    sign
                    * cum[
                        dx * nx : dx * nx + sx,
                        dy * ny : dy * ny + sy,
                        dz * nz : dz * nz + sz,
                    ]
                )
    return total
