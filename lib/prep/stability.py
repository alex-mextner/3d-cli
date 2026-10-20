"""stability.py — will the printed part stand on a table: mass, centre of mass, tip angle, tap/pull tests.

WHAT: `analyse` builds a voxel mass model of the part (wall zone at 100 %, the rest at the infill
  fraction), its centre of mass, the support polygon (convex hull of the bed footprint) and, per
  scenario ("part alone" plus one scenario per `ExternalLoad`, e.g. a phone), the CoM margin to the
  polygon, the tip angle and planar tap/pull tip tests with the friction each needs.
WHY: the Chekhov cat stand carries a phone high above a small base; whether it tips under a tap or
  slides under a pull decided the design, and hours were lost to hand-computing it per phone.
MODEL:
  * Mass: `voxelize` at `pitch` (0.5 mm). A cell at depth d from the surface (Euclidean distance to the
    nearest outside cell minus one pitch) is dense for the fraction `clip((perimeters * line_width - d) / pitch, 0, 1)`
    and infill for the rest, so a perimeter zone that is not a multiple of the pitch is not rounded to whole cells.
    `density_g_cm3` is the filament density (PLA 1.24); the slicer's own number replaces this estimate later.
  * Support polygon: convex hull of the corners of the lowest occupied voxel layer (the bed footprint at the
    first layer, resolution = pitch; a rounded bottom over-states its contact by up to one layer).
  * Tip angle about a polygon edge = atan2(CoM distance inside that edge, CoM height); the part tips at the
    minimum over edges. Heights are measured from the mesh's lowest point, which is the bed plane.
  * Loads are alternatives, each combined with the part on its own (the three reference phones of the
    Chekhov study); pass one summed `ExternalLoad` for simultaneous loads.
  * Tap/pull tests are 3-D torque balances about each support edge: a tap pushes along `-tap_normal`
    (into a screen whose outward normal is `tap_normal`), a pull is horizontal along the horizontal part
    of `+tap_normal`. Safety factor = restoring torque (weight x CoM distance inside the edge) / overturning
    torque of the applied force about that edge; the governing edge is the one with the smallest factor,
    which is the edge the push direction faces. `mu_slide` = horizontal force / (weight + downward force),
    compared with `mu_bed`.
VERDICT per scenario (the report takes the worst): FAIL if the CoM is outside the polygon, the tip angle is
  below `tip_min_deg` or a test's safety factor is below `sf_fail`; WARN if the tip angle is below
  `tip_warn_deg`, a safety factor is below `sf_warn`, or a test needs more friction than `mu_bed`.
ASSUMPTIONS: the mesh is closed (run repair first) and already in the print frame; load coordinates are in
  the same frame. mu_bed 0.30 is PLA on a varnished table [INFERENCE: typical literature value].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy import ndimage
from scipy.spatial import ConvexHull

from errors import InvalidArgument
from prep.contracts import Verdict, worst
from prep.voxel import Grid, voxelize

_G = (
    9.81  # m/s2; masses are in grams, forces in newtons, lengths in mm, torques in N*mm
)


@dataclass(frozen=True)
class ExternalLoad:
    """A mass resting on the part (a phone). `tap_point_mm`/`tap_normal` enable the tap and pull tests."""

    name: str
    mass_g: float
    com_mm: tuple[float, float, float]
    tap_point_mm: tuple[float, float, float] | None = None
    tap_normal: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.mass_g <= 0.0:
            raise InvalidArgument(
                "load mass_g",
                str(self.mass_g),
                ["a positive mass in grams"],
                command="prep",
            )
        if (self.tap_point_mm is None) != (self.tap_normal is None):
            raise InvalidArgument(
                "load tap",
                self.name,
                ["tap_point_mm and tap_normal together, or neither"],
                command="prep",
            )
        if (
            self.tap_normal is not None
            and float(np.linalg.norm(self.tap_normal)) < 1e-9
        ):
            raise InvalidArgument(
                "load tap_normal",
                str(self.tap_normal),
                ["a non-zero vector"],
                command="prep",
            )


@dataclass
class MassModel:
    """Voxel mass estimate of the part alone."""

    volume_mm3: float
    wall_zone_mm3: float
    infill_zone_mm3: float
    mass_g: float
    com_mm: NDArray[np.float64]
    #: occupancy of the lowest layer; the footprint polygon is built from it
    footprint_corners: NDArray[np.float64]
    footprint_cells: int

    def mass_at_infill_g(self, infill: float, density_g_cm3: float) -> float:
        return (
            density_g_cm3 * 1e-3 * (self.wall_zone_mm3 + infill * self.infill_zone_mm3)
        )


@dataclass
class TapTest:
    """One planar tip/slide test of one load at one force."""

    kind: str  # "tap" | "pull"
    load: str
    force_n: float
    #: None when the force cannot overturn the part about any edge
    safety_factor: float | None
    restoring_nmm: float
    overturning_nmm: float
    #: None when the force lifts the part off the bed
    mu_slide: float | None
    edge_index: int
    edge_normal: list[float]
    verdict: Verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "load": self.load,
            "force_n": self.force_n,
            "safety_factor": self.safety_factor,
            "restoring_nmm": self.restoring_nmm,
            "overturning_nmm": self.overturning_nmm,
            "mu_slide": self.mu_slide,
            "edge_index": self.edge_index,
            "edge_normal": self.edge_normal,
            "verdict": self.verdict,
        }


@dataclass
class Scenario:
    """The part alone, or the part plus one load."""

    name: str
    mass_g: float
    com_mm: list[float]
    com_height_mm: float
    min_margin_mm: float
    tip_angle_deg: float
    tip_edge_normal: list[float]
    tests: list[TapTest]
    verdict: Verdict
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mass_g": self.mass_g,
            "com_mm": self.com_mm,
            "com_height_mm": self.com_height_mm,
            "min_margin_mm": self.min_margin_mm,
            "tip_angle_deg": self.tip_angle_deg,
            "tip_edge_normal": self.tip_edge_normal,
            "tests": [t.to_dict() for t in self.tests],
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }


@dataclass
class StabilityReport:
    """Stability result (a `StepResult`); `scenarios[0]` is always "part alone"."""

    verdict: Verdict
    reasons: list[str]
    mass_g: float
    volume_mm3: float
    wall_zone_mm3: float
    infill_zone_mm3: float
    com_mm: list[float]
    support_polygon_mm: list[list[float]]
    support_area_mm2: float
    contact_area_mm2: float
    mass_vs_infill_g: dict[str, float]
    scenarios: list[Scenario] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "mass_g": self.mass_g,
            "volume_mm3": self.volume_mm3,
            "wall_zone_mm3": self.wall_zone_mm3,
            "infill_zone_mm3": self.infill_zone_mm3,
            "com_mm": self.com_mm,
            "support_polygon_mm": self.support_polygon_mm,
            "support_area_mm2": self.support_area_mm2,
            "contact_area_mm2": self.contact_area_mm2,
            "mass_vs_infill_g": self.mass_vs_infill_g,
            "scenarios": [s.to_dict() for s in self.scenarios],
        }

    def summary(self) -> str:
        if not self.scenarios:
            return f"stability {self.verdict}: {self.reasons[0]}"
        tightest = min(self.scenarios, key=lambda s: s.tip_angle_deg)
        return (
            f"{self.mass_g:.0f} g, CoM z {self.com_mm[2]:.1f} mm, support {self.support_area_mm2:.0f} mm2; "
            f"tightest tip angle {tightest.tip_angle_deg:.1f} deg ({tightest.name})"
        )


def mass_model(
    mesh: trimesh.Trimesh,
    *,
    pitch: float = 0.5,
    infill: float = 0.15,
    perimeters: int = 4,
    line_width: float = 0.42,
    density_g_cm3: float = 1.24,
) -> MassModel:
    """Voxel mass, wall/infill zone volumes, centre of mass and bed-footprint corners of `mesh`."""
    grid = voxelize(mesh, pitch)
    occ = grid.occ
    if not occ.any():
        raise InvalidArgument(
            "mesh", "no solid voxels", ["a closed mesh with volume"], command="prep"
        )
    edt = ndimage.distance_transform_edt(np.pad(occ, 1), sampling=pitch)[
        1:-1, 1:-1, 1:-1
    ]
    # edt is the distance to the nearest outside cell centre = depth of the cell's near face + one pitch
    dense = np.where(
        occ, np.clip((perimeters * line_width - (edt - pitch)) / pitch, 0.0, 1.0), 0.0
    )
    cell = pitch**3
    weights = np.where(occ, dense + (1.0 - dense) * infill, 0.0) * (
        density_g_cm3 * 1e-3 * cell
    )
    mass = float(weights.sum())
    com = (
        np.array(
            [
                float((weights.sum(axis=(1, 2)) * grid.centres(0)).sum()),
                float((weights.sum(axis=(0, 2)) * grid.centres(1)).sum()),
                float((weights.sum(axis=(0, 1)) * grid.centres(2)).sum()),
            ]
        )
        / mass
    )
    corners, cells = _footprint_corners(grid)
    return MassModel(
        volume_mm3=float(occ.sum()) * cell,
        wall_zone_mm3=float(dense.sum()) * cell,
        infill_zone_mm3=float((np.where(occ, 1.0 - dense, 0.0)).sum()) * cell,
        mass_g=mass,
        com_mm=com,
        footprint_corners=corners,
        footprint_cells=cells,
    )


def analyse(
    mesh: trimesh.Trimesh,
    *,
    loads: tuple[ExternalLoad, ...] = (),
    infill: float = 0.15,
    perimeters: int = 4,
    line_width: float = 0.42,
    density_g_cm3: float = 1.24,
    mu_bed: float = 0.30,
    forces_n: tuple[float, ...] = (2.0, 5.0),
    tip_min_deg: float = 10.0,
    tip_warn_deg: float = 15.0,
    sf_fail: float = 1.0,
    sf_warn: float = 1.5,
    pitch: float = 0.5,
    infill_sweep: tuple[float, ...] = (0.15, 0.25, 0.30, 0.40),
) -> StabilityReport:
    """Stability of `mesh` alone and with each of `loads`; thresholds and models are documented in the header."""
    _validate(infill, perimeters, line_width, density_g_cm3, mu_bed, forces_n, pitch)
    try:
        model = mass_model(
            mesh,
            pitch=pitch,
            infill=infill,
            perimeters=perimeters,
            line_width=line_width,
            density_g_cm3=density_g_cm3,
        )
    except InvalidArgument:
        return StabilityReport(
            verdict="FAIL",
            reasons=[
                "voxelization found no solid: the mesh is empty or not closed (repair it first)"
            ],
            mass_g=0.0,
            volume_mm3=0.0,
            wall_zone_mm3=0.0,
            infill_zone_mm3=0.0,
            com_mm=[0.0, 0.0, 0.0],
            support_polygon_mm=[],
            support_area_mm2=0.0,
            contact_area_mm2=0.0,
            mass_vs_infill_g={},
        )
    hull = ConvexHull(model.footprint_corners)
    poly = model.footprint_corners[hull.vertices]  # counter-clockwise
    bed_z = float(mesh.bounds[0][2])
    scenarios = [
        _scenario(
            "part alone",
            model.mass_g,
            model.com_mm,
            None,
            poly,
            bed_z,
            (),
            mu_bed,
            tip_min_deg,
            tip_warn_deg,
            sf_fail,
            sf_warn,
        )
    ]
    for load in loads:
        total = model.mass_g + load.mass_g
        com = (
            model.com_mm * model.mass_g
            + np.asarray(load.com_mm, dtype=np.float64) * load.mass_g
        ) / total
        scenarios.append(
            _scenario(
                f"with {load.name}",
                total,
                com,
                load,
                poly,
                bed_z,
                forces_n,
                mu_bed,
                tip_min_deg,
                tip_warn_deg,
                sf_fail,
                sf_warn,
            )
        )
    reasons = [
        f"{s.name}: {r}" for s in scenarios for r in s.reasons if s.verdict != "PASS"
    ]
    return StabilityReport(
        verdict=worst([s.verdict for s in scenarios]),
        reasons=reasons or ["stable in every scenario"],
        mass_g=model.mass_g,
        volume_mm3=model.volume_mm3,
        wall_zone_mm3=model.wall_zone_mm3,
        infill_zone_mm3=model.infill_zone_mm3,
        com_mm=[float(v) for v in model.com_mm],
        support_polygon_mm=[[float(a), float(b)] for a, b in poly],
        support_area_mm2=float(hull.volume),
        contact_area_mm2=model.footprint_cells * pitch**2,
        mass_vs_infill_g={
            f"{f:g}": model.mass_at_infill_g(f, density_g_cm3) for f in infill_sweep
        },
        scenarios=scenarios,
    )


def _validate(
    infill: float,
    perimeters: int,
    line_width: float,
    density: float,
    mu_bed: float,
    forces: tuple[float, ...],
    pitch: float,
) -> None:
    if not 0.0 <= infill <= 1.0:
        raise InvalidArgument(
            "infill", str(infill), ["a fraction in [0, 1]"], command="prep"
        )
    if (
        perimeters < 0
        or line_width <= 0.0
        or density <= 0.0
        or pitch <= 0.0
        or mu_bed < 0.0
    ):
        raise InvalidArgument(
            "perimeters/line_width/density/pitch/mu_bed",
            f"{perimeters}/{line_width}/{density}/{pitch}/{mu_bed}",
            ["perimeters >= 0", "line_width, density, pitch > 0", "mu_bed >= 0"],
            command="prep",
        )
    if any(f <= 0.0 for f in forces):
        raise InvalidArgument(
            "forces_n", str(forces), ["positive forces in newtons"], command="prep"
        )


def _footprint_corners(grid: Grid) -> tuple[NDArray[np.float64], int]:
    """Corner points (x, y) of every occupied cell in the lowest occupied layer, and the cell count."""
    layers = np.flatnonzero(grid.occ.any(axis=(0, 1)))
    cells = np.argwhere(grid.occ[:, :, layers[0]])
    half = grid.pitch / 2.0
    cx = grid.centres(0)[cells[:, 0]]
    cy = grid.centres(1)[cells[:, 1]]
    corners = np.concatenate(
        [
            np.column_stack([cx + sx * half, cy + sy * half])
            for sx in (-1, 1)
            for sy in (-1, 1)
        ]
    )
    return np.unique(np.round(corners, 9), axis=0), int(len(cells))


def _edges(
    poly: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Start points and outward unit normals of the edges of a counter-clockwise polygon."""
    edge = np.roll(poly, -1, axis=0) - poly
    outward = (
        np.column_stack([edge[:, 1], -edge[:, 0]])
        / np.linalg.norm(edge, axis=1)[:, None]
    )
    return poly, outward


def _scenario(
    name: str,
    mass_g: float,
    com: NDArray[np.float64],
    load: ExternalLoad | None,
    poly: NDArray[np.float64],
    bed_z: float,
    forces_n: tuple[float, ...],
    mu_bed: float,
    tip_min_deg: float,
    tip_warn_deg: float,
    sf_fail: float,
    sf_warn: float,
) -> Scenario:
    starts, outward = _edges(poly)
    margins = -np.einsum(
        "ij,ij->i", com[:2] - starts, outward
    )  # distance inside each edge
    com_h = float(com[2]) - bed_z
    tips = np.degrees(np.arctan2(margins, max(com_h, 1e-12)))
    worst_edge = int(np.argmin(margins))
    tip = float(tips[worst_edge])
    weight_n = mass_g / 1000.0 * _G
    tests = (
        _tap_tests(
            load,
            starts,
            outward,
            margins,
            weight_n,
            bed_z,
            forces_n,
            mu_bed,
            sf_fail,
            sf_warn,
        )
        if load
        else []
    )
    reasons: list[str] = []
    verdicts: list[Verdict] = []
    if float(margins[worst_edge]) < 0.0:
        verdicts.append("FAIL")
        reasons.append(
            f"centre of mass is {-margins[worst_edge]:.1f} mm outside the support polygon"
        )
    elif tip < tip_min_deg:
        verdicts.append("FAIL")
        reasons.append(f"tip angle {tip:.1f} deg is below {tip_min_deg:g} deg")
    elif tip < tip_warn_deg:
        verdicts.append("WARN")
        reasons.append(f"tip angle {tip:.1f} deg is below {tip_warn_deg:g} deg")
    for t in tests:
        verdicts.append(t.verdict)
        if t.verdict != "PASS":
            sf = (
                "no overturning"
                if t.safety_factor is None
                else f"safety factor {t.safety_factor:.2f}"
            )
            reasons.append(
                f"{t.kind} {t.force_n:g} N on {t.load}: {sf}, needs mu {t.mu_slide:.2f} (bed {mu_bed:g})"
            )
    return Scenario(
        name=name,
        mass_g=float(mass_g),
        com_mm=[float(v) for v in com],
        com_height_mm=com_h,
        min_margin_mm=float(margins[worst_edge]),
        tip_angle_deg=tip,
        tip_edge_normal=[float(v) for v in outward[worst_edge]],
        tests=tests,
        verdict=worst(verdicts) if verdicts else "PASS",
        reasons=reasons,
    )


def _tap_tests(
    load: ExternalLoad,
    starts: NDArray[np.float64],
    outward: NDArray[np.float64],
    margins: NDArray[np.float64],
    weight_n: float,
    bed_z: float,
    forces_n: tuple[float, ...],
    mu_bed: float,
    sf_fail: float,
    sf_warn: float,
) -> list[TapTest]:
    if load.tap_point_mm is None or load.tap_normal is None:
        return []
    point = np.asarray(load.tap_point_mm, dtype=np.float64)
    normal = np.asarray(load.tap_normal, dtype=np.float64)
    normal = normal / np.linalg.norm(normal)
    horizontal = np.array([normal[0], normal[1], 0.0])
    rows: list[TapTest] = []
    for kind, direction in (("tap", -normal), ("pull", horizontal)):
        if kind == "pull" and float(np.linalg.norm(horizontal)) < 1e-9:
            continue  # a screen facing straight up has no horizontal pull direction
        for force in forces_n:
            vec = (
                force
                * direction
                / (np.linalg.norm(direction) if kind == "pull" else 1.0)
            )
            rows.append(
                _one_test(
                    kind,
                    load.name,
                    force,
                    vec,
                    point,
                    starts,
                    outward,
                    margins,
                    weight_n,
                    bed_z,
                    mu_bed,
                    sf_fail,
                    sf_warn,
                )
            )
    return rows


def _one_test(
    kind: str,
    load: str,
    force: float,
    vec: NDArray[np.float64],
    point: NDArray[np.float64],
    starts: NDArray[np.float64],
    outward: NDArray[np.float64],
    margins: NDArray[np.float64],
    weight_n: float,
    bed_z: float,
    mu_bed: float,
    sf_fail: float,
    sf_warn: float,
) -> TapTest:
    # torque of the applied force about each edge axis (z x outward), positive = tipping outward over that edge
    axis = np.column_stack([-outward[:, 1], outward[:, 0], np.zeros(len(outward))])
    anchor = np.column_stack([starts, np.full(len(starts), bed_z)])
    overturn = np.einsum("ij,ij->i", np.cross(point - anchor, vec), axis)
    restoring = weight_n * margins
    with np.errstate(divide="ignore", invalid="ignore"):
        factor = np.where(overturn > 1e-12, restoring / overturn, np.inf)
    edge = (
        int(np.argmin(factor))
        if np.isfinite(factor).any()
        else int(np.argmax(overturn))
    )
    sf = float(factor[edge]) if np.isfinite(factor[edge]) else None
    normal_force = weight_n - float(
        vec[2]
    )  # a downward tap (vec z < 0) adds to the bed reaction
    mu = (
        float(np.hypot(vec[0], vec[1]) / normal_force)
        if normal_force > 0.0
        else float("inf")
    )
    verdict: Verdict = "PASS"
    if sf is not None and sf < sf_fail:
        verdict = "FAIL"
    elif (sf is not None and sf < sf_warn) or mu > mu_bed:
        verdict = "WARN"
    return TapTest(
        kind=kind,
        load=load,
        force_n=float(force),
        safety_factor=sf,
        restoring_nmm=float(restoring[edge]),
        overturning_nmm=float(max(overturn[edge], 0.0)),
        mu_slide=mu,
        edge_index=edge,
        edge_normal=[float(v) for v in outward[edge]],
        verdict=verdict,
    )
