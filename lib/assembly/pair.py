"""pair.py — `3d assembly pair A B`: do two parts mate? Interpenetration, minimum clearance, per-feature radial clearance.

WHAT: two meshes (STL / 3MF / OBJ / PLY), each with an optional scale and pose, are put in the assembled frame and judged:
    interpenetration  exact overlap volume, regions (volume, depth, xyz) - `overlap.pair_regions`;
    clearance         exact minimum gap between the solids - `overlap.min_gap` (0 when they overlap);
    features          per coaxial feature (thread, rib, key, bore), the RADIAL clearance read from surface profiles
                      (`helical`): at the posed phase and at the best phase of screwing / clocking, the free play, crest
                      and root clearances, handedness and pitch coherence;
    as printed        with `--gcode-a/--gcode-b` the same feature clearance from the printed outer-wall paths
                      (`printed`), the model-to-print deviation per layer, and the registration residual.
  `--screw-to-best` turns part B about the axis to the best phase of the first feature before everything else is
  measured: the pose of a screwed fit is not unique, the phase is the free parameter (the turn keeps the axial position,
  so a seat face stays seated; the equivalent axial advance of a thread is reported next to it).
WHY: `3d assembly check` needs an assembly spec of declared mates; the question "do these two parts screw together on
  this plastic" is a pair question with a feature in it. A volume and a minimum gap cannot say which flank binds nor
  whether the printed walls (compensation, line width) close the gap the model has.
INVARIANTS: nothing is repaired or simplified; a feature that is not measured (empty bins, wrong pitch / handedness)
  FAILS rather than passing; the verdicts (`pass | warn | fail`) fold into `ok`; poses and windows are in the assembled
  frame, the axis is a line (point + direction) in it, feature z windows are the coordinate along that axis from the
  axis point's plane through the world origin (for the default +Z axis: world z).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from assembly import helical, printed
from assembly.overlap import min_gap, pair_regions, sampled_gap
from assembly.scene import (
    Box,
    box_intersection,
    load_mesh_file,
    pose_matrix,
    solid_box,
    solid_from_mesh,
    translation_matrix,
)
from assembly.spec import SCHEMA, Pose
from errors import InputNotFound, InvalidArgument

CMD = "assembly"
DEFAULT_MIN_RADIAL_GAP_MM = 0.1
SEARCH_MM = 5.0
EPS_MM3 = 0.01
MIN_COVERAGE_FAIL = 0.5
MIN_COVERAGE_WARN = 0.9
AXIS_OFFSET_WARN_MM = 0.1
SLICE_STEP_MM = 0.12

Array = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]
Range = tuple[float, float]


@dataclass(frozen=True)
class FeatureSide:
    """Which surface of a part a feature is: `inner` = the wall of a bore (material outside), `outer` = a boss."""

    side: str
    z: Range | None = None
    r: Range | None = None


@dataclass(frozen=True)
class Feature:
    name: str
    a: FeatureSide
    b: FeatureSide
    pitch_mm: float = 0.0
    hand: str = "auto"
    min_gap_mm: float | None = None


@dataclass
class PartInput:
    path: Path
    scale: float = 1.0
    pose: Pose = ()
    print_pose: Pose = ()
    gcode: Path | None = None
    gcode_object: str | None = None
    object_id: int | None = None  # which object of a 3MF (`plate.3mf#3`)


@dataclass
class PairOptions:
    a: PartInput
    b: PartInput
    features: list[Feature] = field(default_factory=list)
    axis_point: tuple[float, float, float] | None = None
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    min_radial_gap_mm: float | None = None
    min_clearance_mm: float | None = None
    max_overlap_mm3: float = EPS_MM3
    seat_tol_mm: float = 0.02
    zone: Box | None = None
    screw_to_best: bool = False
    skip_below_mm: float = 0.5
    max_faces: int = 400_000
    fit_resolution: dict[str, Any] | None = None


def parse_range(text: str, where: str) -> Range:
    """`LO:HI` -> (lo, hi)."""
    try:
        lo, hi = (float(x) for x in text.split(":"))
    except ValueError as exc:
        raise InvalidArgument(
            where, text, ["LO:HI, e.g. 1.0:9.5"], command=CMD
        ) from exc
    if hi < lo:
        raise InvalidArgument(where, text, ["LO <= HI"], command=CMD)
    return (lo, hi)


def _unit(v: tuple[float, float, float]) -> Array:
    a = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(a))
    if n == 0.0:
        raise InvalidArgument("--axis", str(v), ["a non-zero direction"], command=CMD)
    return a / n


def split_part_path(text: str) -> tuple[Path, int | None]:
    """`plate.3mf#3` -> (plate.3mf, 3): one object of a 3MF by its id; any other text is a plain path."""
    base, sep, tail = text.rpartition("#")
    if sep and tail.isdigit() and base.lower().endswith(".3mf"):
        return Path(base), int(tail)
    return Path(text), None


def _read_mesh(part: PartInput, label: str) -> Any:
    """STL / OBJ / PLY through trimesh; a 3MF through the stdlib reader (no lxml): its single object, or `#ID`'s."""
    if part.path.suffix.lower() != ".3mf":
        return load_mesh_file(part.path, label)
    import zipfile
    from xml.etree import ElementTree as ET

    from assembly import threemf

    try:
        objects = threemf.object_meshes(part.path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise InvalidArgument(
            f"{label}.mesh", str(part.path), ["a readable 3MF (3D/3dmodel.model)"], command=CMD, extra=str(exc)
        ) from exc
    if part.object_id is not None:
        if part.object_id not in objects:
            raise InvalidArgument(
                f"{label}.mesh",
                f"{part.path}#{part.object_id}",
                [f"an object id of the file: {', '.join(str(i) for i in sorted(objects))}"],
                command=CMD,
            )
        return objects[part.object_id]
    if len(objects) != 1:
        listing = [
            f"#{oid}: {len(m.faces)} faces, box {[round(float(v), 2) for v in m.extents]} mm"
            for oid, m in sorted(objects.items())
        ]
        raise InvalidArgument(
            f"{label}.mesh",
            f"{part.path} holds {len(objects)} objects",
            [f"pick one with `{part.path}#ID`", *listing],
            command=CMD,
        )
    return next(iter(objects.values()))


def _load(part: PartInput, label: str, max_faces: int) -> tuple[Any, Matrix]:
    """(mesh scaled, matrix mesh-frame -> assembled frame)."""
    if not part.path.is_file():
        raise InputNotFound(str(part.path), command=CMD)
    mesh = _read_mesh(part, label)
    if len(mesh.faces) > max_faces:
        raise InvalidArgument(
            f"{label}.mesh",
            f"{len(mesh.faces)} faces",
            [f"at most {max_faces} faces (--max-faces)"],
            command=CMD,
        )
    if part.scale != 1.0:
        mesh.apply_scale(part.scale)
    return mesh, pose_matrix(part.pose)


def _bbox(mesh: Any) -> dict[str, list[float]]:
    return {
        "min": [round(float(v), 4) for v in mesh.bounds[0]],
        "max": [round(float(v), 4) for v in mesh.bounds[1]],
    }


def interpenetration(solid_a: Any, solid_b: Any, max_mm3: float, seat_tol_mm: float) -> dict[str, Any]:
    """Overlap regions; a region thinner than `seat_tol_mm` is a CONTACT (a seat face, #140), not interpenetration."""
    regions = pair_regions("a", solid_a, "b", solid_b, eps_mm3=EPS_MM3, keep_mesh=False)
    real = [r for r in regions if r.depth_mm >= seat_tol_mm]
    contact = [r for r in regions if r.depth_mm < seat_tol_mm]
    total = sum(r.volume_mm3 for r in real)
    return {
        "status": "pass" if total <= max_mm3 else "fail",
        "volume_mm3": round(total, 4),
        "allowed_mm3": max_mm3,
        "count": len(real),
        "regions": [r.to_dict() for r in real[:10]],
        "contacts": {
            "count": len(contact),
            "volume_mm3": round(sum(r.volume_mm3 for r in contact), 4),
            "max_depth_mm": round(max((r.depth_mm for r in contact), default=0.0), 4),
            "seat_tol_mm": seat_tol_mm,
        },
    }


def clearance(
    solid_a: Any, solid_b: Any, mesh_a: Any, mesh_b: Any, required: float | None, zone: Box | None
) -> dict[str, Any]:
    window = box_intersection(solid_box(solid_a), solid_box(solid_b), SEARCH_MM)
    gap = min_gap(solid_a, solid_b, SEARCH_MM, window)
    out: dict[str, Any] = {
        "min_gap_mm": round(gap, 4),
        "capped_at_mm": SEARCH_MM if gap >= SEARCH_MM else None,
        "required_mm": required,
    }
    status = "pass"
    if zone is not None:
        zgap, spacing = sampled_gap(mesh_a, mesh_b, zone, SEARCH_MM)
        out["zone"] = {
            "box": [round(float(v), 4) for v in zone],
            "min_gap_mm": round(zgap, 4),
            "sample_spacing_mm": round(spacing, 4),
        }
        gap = zgap
    if required is not None and gap < required:
        status = "fail"
    out["status"] = status
    return out


@dataclass
class Axis:
    origin: Array
    direction: Array
    matrix: Matrix  # assembled -> axis frame


def make_axis(opts: PairOptions, mesh_a: Any) -> Axis:
    d = _unit(opts.axis)
    if opts.axis_point is not None:
        o = np.asarray(opts.axis_point, dtype=float)
        o = o - float(o @ d) * d
    else:
        c = np.asarray(mesh_a.bounds, dtype=float).mean(axis=0)
        o = c - float(c @ d) * d
    return Axis(o, d, helical.axis_frame(o, d))


@dataclass
class PrintedPart:
    walls: Any
    bed_to_assembly: Matrix
    info: dict[str, Any]
    bed_mesh: Any = field(repr=False, default=None)  # the model on the bed, before the registration shift
    shift: tuple[float, float] = (0.0, 0.0)  # added to the wall points to lay them on `bed_mesh`
    skip_below_mm: float = 0.5


def _drop_matrix(mesh: Any, matrix: Matrix) -> Matrix:
    """`matrix` followed by the translation that puts the mesh's lowest point on the bed (z = 0)."""
    corners = np.array(
        [
            [x, y, z]
            for x in mesh.bounds[:, 0]
            for y in mesh.bounds[:, 1]
            for z in mesh.bounds[:, 2]
        ]
    )
    pts = helical.apply(matrix, corners)
    return translation_matrix([0.0, 0.0, -float(pts[:, 2].min())]) @ matrix


def load_printed(
    part: PartInput, label: str, mesh: Any, pose: Matrix, skip_below: float
) -> PrintedPart:
    from gcode_walls import read_walls

    assert part.gcode is not None
    if not part.gcode.is_file():
        raise InputNotFound(str(part.gcode), command=CMD)
    try:
        walls = read_walls(part.gcode, part.gcode_object)
    except (KeyError, ValueError) as exc:
        raise InvalidArgument(
            f"--object-{label}",
            str(part.gcode_object),
            [str(exc.args[0])],
            command=CMD,
        ) from exc
    explicit = any(kind == "translate" for kind, _ in part.print_pose)
    base = pose_matrix(part.print_pose)
    p0 = base if explicit else _drop_matrix(mesh, base)
    if not explicit and walls.base_z > 0.05:  # a raft: the object's z = 0 sits above the bed
        p0 = translation_matrix([0.0, 0.0, walls.base_z]) @ p0
    bed_mesh = mesh.copy()
    bed_mesh.apply_transform(p0)
    shift = (0.0, 0.0)
    resid = math.nan
    if not explicit:
        shift, resid = printed.register_xy(bed_mesh, walls, skip_below)
    dev = printed.deviation(bed_mesh, printed.layers_in_window(walls, np.eye(4), None), skip_below, shift)
    placed = translation_matrix([-shift[0], -shift[1], 0.0]) @ p0
    info = {
        "gcode": str(part.gcode),
        "object": part.gcode_object,
        "objects_in_file": walls.objects,
        "layers": len(walls.layers),
        "wall_mm": round(walls.wall_mm, 1),
        "overhang_wall_left_out_mm": round(walls.overhang_mm, 1),
        "z_lift_mm": round(walls.base_z, 3),
        "placement": {
            "registered": not explicit,
            "shift_applied_mm": [round(-shift[0], 4), round(-shift[1], 4)],
            "residual_rms_mm": None if math.isnan(resid) else round(resid, 4),
        },
        "deviation": dev,
    }
    return PrintedPart(walls, pose @ np.linalg.inv(placed), info, bed_mesh, shift, skip_below)


@dataclass
class SideProfile:
    profile: helical.Profile
    hand_info: dict[str, Any]
    axis_offset: tuple[float, float, float]
    points: int


def _model_points(mesh_axis: Any, side: FeatureSide) -> Array:
    zr = side.z or (float(mesh_axis.bounds[0][2]), float(mesh_axis.bounds[1][2]))
    return helical.mesh_wall_points(mesh_axis, zr, side.r, SLICE_STEP_MM, 0.1)


def _printed_points(
    pp: PrintedPart, axis: Axis, side: FeatureSide
) -> tuple[Array, Array]:
    from assembly.printed import surface_offsets, to_assembly

    pts, width = to_assembly(pp.walls, axis.matrix @ pp.bed_to_assembly)
    keep = np.ones(len(pts), dtype=bool)
    if side.z is not None:
        keep &= (pts[:, 2] >= side.z[0]) & (pts[:, 2] <= side.z[1])
    if side.r is not None:
        r = np.hypot(pts[:, 0], pts[:, 1])
        keep &= (r >= side.r[0]) & (r <= side.r[1])
    return pts[keep], surface_offsets(width[keep], side.side)


def _side_profile(
    pts: Array, offset: Array | None, feature: Feature, hand: str, side: str
) -> SideProfile:
    if len(pts) < helical.MIN_POINTS_PER_BIN * 10:
        raise InvalidArgument(
            f"features.{feature.name}",
            f"{len(pts)} surface points in the window",
            ["a z / r window that holds the feature's surface"],
            command=CMD,
            extra="Check the windows (axis frame: z along the axis, r from it) and the axis point.",
        )
    prof, info = helical.pick_handedness(pts, feature.pitch_mm, hand, offset, side)
    return SideProfile(prof, info, helical.fit_axis_offset(pts), len(pts))


def _common_hand(
    pa: SideProfile, pb: SideProfile, requested: str
) -> tuple[str, str | None]:
    """The handedness both parts are judged under, and a mismatch message (both decided and different)."""
    if requested in ("right", "left"):
        return requested, None
    ha, hb = pa.hand_info, pb.hand_info
    if ha["hand"] == "none":
        return "auto", None
    if ha["decided"] and hb["decided"] and ha["hand"] != hb["hand"]:
        return ha["hand"], (
            f"handedness mismatch: part A is {ha['hand']}-handed, part B {hb['hand']}-handed - they cannot screw together"
        )
    if ha["decided"]:
        return ha["hand"], None
    if hb["decided"]:
        return hb["hand"], None
    return ha["best_fit"], None


def _profile_summary(p: SideProfile, side: FeatureSide) -> dict[str, Any]:
    prof = p.profile
    return {
        "side": side.side,
        "points": p.points,
        "r_min_mm": round(prof.r_min, 4),
        "r_max_mm": round(prof.r_max, 4),
        "coverage": round(prof.coverage, 3),
        "coherence_mm": round(prof.coherence_mm, 4),
        "handedness": p.hand_info,
        "axis_offset_mm": [round(p.axis_offset[0], 4), round(p.axis_offset[1], 4)],
        "axis_fit_rms_mm": round(p.axis_offset[2], 4),
    }


def _female_male(
    feature: Feature, pa: SideProfile, pb: SideProfile
) -> tuple[SideProfile, SideProfile, bool]:
    """(female, male, a_is_female). A feature is a bore against a boss; two bores or two bosses cannot mate."""
    if feature.a.side == feature.b.side:
        raise InvalidArgument(
            f"features.{feature.name}",
            f"both sides are '{feature.a.side}'",
            ["one 'inner' (bore) and one 'outer' (boss) side"],
            command=CMD,
        )
    a_female = feature.a.side == "inner"
    return (pa, pb, True) if a_female else (pb, pa, False)


def _clearance_dict(
    feature: Feature,
    cl: helical.Clearance,
    hand: int,
    a_is_female: bool,
) -> dict[str, Any]:
    # `best_shift_bins` rolls the MALE profile: the part to move is whichever of A / B is male
    mover = "b" if a_is_female else "a"
    motion = helical.shift_to_motion(cl.best_shift_bins, feature.pitch_mm, hand)
    if mover == "a":  # moving A by +x is moving B by -x for the same relative phase
        motion = {k: -v for k, v in motion.items()}
    play, unit = cl.free_play
    return {
        "at_pose_min_gap_mm": _r(cl.at_pose.min_mm),
        "at_pose_mean_gap_mm": _r(cl.at_pose.mean_mm),
        "best_phase_min_gap_mm": _r(cl.best_gap_mm),
        "best_phase_move_b": motion,
        "free_play": {
            "value": _r(play, 3),
            "unit": unit,
            "fraction_of_turn": _r(cl.free_fraction, 3),
        },
        "male_crest_to_female_root_mm": _r(cl.crest_male_to_root_female_mm),
        "female_crest_to_male_root_mm": _r(cl.crest_female_to_root_male_mm),
    }


def _r(v: float, nd: int = 4) -> float | None:
    return None if math.isnan(v) else round(float(v), nd)


def _side_findings(key: str, side: dict[str, Any], pitch_mm: float) -> tuple[list[str], list[str]]:
    """(fails, warns) of one part's measured feature surface: coverage, handedness decisiveness, concentricity."""
    who = key.upper()
    fails: list[str] = []
    warns: list[str] = []
    if side["coverage"] < MIN_COVERAGE_FAIL:
        fails.append(f"{who} feature surface only {side['coverage']:.0%} measured (empty phase bins)")
    elif side["coverage"] < MIN_COVERAGE_WARN:
        warns.append(f"{who} feature surface {side['coverage']:.0%} measured")
    if pitch_mm > 0 and not side["handedness"]["decided"]:
        warns.append(
            f"{who} handedness is not decisive (coherence {side['handedness']['coherence_mm']}): "
            "check the pitch and the windows"
        )
    off = math.hypot(*side["axis_offset_mm"])
    # a circle fit only means something for a thread; keys and ribs are not circles (the number is still reported)
    if pitch_mm > 0 and off > AXIS_OFFSET_WARN_MM:
        warns.append(f"{who} feature axis is {off:.3f} mm off the declared axis")
    return fails, warns


def _clearance_findings(cl: dict[str, Any], required: float) -> tuple[list[str], list[str]]:
    best, pose = cl["best_phase_min_gap_mm"], cl["at_pose_min_gap_mm"]
    fails: list[str] = []
    warns: list[str] = []
    if best is None:
        fails.append("no phase with both profiles measured")
    elif best < required:
        fails.append(f"best-phase radial gap {best:.3f} mm < required {required:.3f} mm")
    if pose is not None and pose < 0 and (best is None or best >= required):
        warns.append(
            f"the posed phase interferes (radial gap {pose:.3f} mm) though a free phase exists: "
            f"move B by {cl['best_phase_move_b']}"
        )
    return fails, warns


def _status(
    feature: Feature, required: float, res: dict[str, Any]
) -> tuple[str, list[str]]:
    fails: list[str] = []
    warns: list[str] = []
    for key in ("a", "b"):
        f, w = _side_findings(key, res["profiles"][key], feature.pitch_mm)
        fails += f
        warns += w
    if res.get("mismatch"):
        fails.append(res["mismatch"])
    if res.get("clearance") is not None:
        f, w = _clearance_findings(res["clearance"], required)
        fails += f
        warns += w
    status = "fail" if fails else "warn" if warns else "pass"
    return status, fails + warns


@dataclass
class _Ctx:
    axis: Axis
    mesh_a_axis: Any
    mesh_b_axis: Any
    printed_a: PrintedPart | None
    printed_b: PrintedPart | None
    default_required: float


def _points_for(
    ctx: _Ctx, label: str, side: FeatureSide, source: str
) -> tuple[Array, Array | None]:
    pp = ctx.printed_a if label == "a" else ctx.printed_b
    if source == "as_printed" and pp is not None:
        return _printed_points(pp, ctx.axis, side)
    mesh = ctx.mesh_a_axis if label == "a" else ctx.mesh_b_axis
    return _model_points(mesh, side), None


def analyse_feature(feature: Feature, ctx: _Ctx, source: str) -> dict[str, Any]:
    pts_a, off_a = _points_for(ctx, "a", feature.a, source)
    pts_b, off_b = _points_for(ctx, "b", feature.b, source)
    pa = _side_profile(pts_a, off_a, feature, feature.hand, feature.a.side)
    pb = _side_profile(pts_b, off_b, feature, feature.hand, feature.b.side)
    hand, mismatch = _common_hand(pa, pb, feature.hand)
    if feature.pitch_mm > 0 and hand in ("right", "left") and mismatch is None:
        pa = _side_profile(pts_a, off_a, feature, hand, feature.a.side)
        pb = _side_profile(pts_b, off_b, feature, hand, feature.b.side)
    female, male, a_female = _female_male(feature, pa, pb)
    res: dict[str, Any] = {
        "source": source,
        "profiles": {
            "a": _profile_summary(pa, feature.a),
            "b": _profile_summary(pb, feature.b),
        },
        "mismatch": mismatch,
    }
    if mismatch is None:
        cl = helical.clearance(female.profile, male.profile)
        res["clearance"] = _clearance_dict(
            feature, cl, female.profile.hand or male.profile.hand, a_female
        )
    required = (
        feature.min_gap_mm if feature.min_gap_mm is not None else ctx.default_required
    )
    res["required_gap_mm"] = required
    res["status"], res["reasons"] = _status(feature, required, res)
    return res


def _printed_vs_model(feature: Feature, ctx: _Ctx) -> dict[str, Any]:
    """How far the printed surface of each printed part sits from the model's, on this feature's profile."""
    out: dict[str, Any] = {}
    for label, side, pp in (
        ("a", feature.a, ctx.printed_a),
        ("b", feature.b, ctx.printed_b),
    ):
        if pp is None:
            continue
        model = _side_profile(
            _model_points(ctx.mesh_a_axis if label == "a" else ctx.mesh_b_axis, side),
            None,
            feature,
            feature.hand,
            side.side,
        )
        pts, off = _printed_points(pp, ctx.axis, side)
        prin = _side_profile(pts, off, feature, feature.hand, side.side)
        diff = (
            prin.profile.radius - model.profile.radius
            if model.profile.hand == prin.profile.hand
            else None
        )
        window_layers = printed.layers_in_window(
            pp.walls, ctx.axis.matrix @ pp.bed_to_assembly, side.z
        )
        out[label] = {
            "wall_vs_model_in_window": printed.deviation(
                pp.bed_mesh, window_layers, pp.skip_below_mm, pp.shift
            ),
            "printed_r_min_mm": round(prin.profile.r_min, 4),
            "printed_r_max_mm": round(prin.profile.r_max, 4),
            "model_r_min_mm": round(model.profile.r_min, 4),
            "model_r_max_mm": round(model.profile.r_max, 4),
            "radius_printed_minus_model_mm": None
            if diff is None
            else {
                "mean": _r(float(np.nanmean(diff))),
                "min": _r(float(np.nanmin(diff))),
                "max": _r(float(np.nanmax(diff))),
            },
        }
    return out


REACH_MM = 3.0  # a bore surface farther than this beyond the boss's largest radius is not part of the fit


def resolve_windows(feature: Feature, ctx: _Ctx) -> Feature:
    """Fill an omitted radius window of the BORE side: from the axis out to the boss's largest radius + REACH_MM.

    Without it a tube's own outer wall would be read as part of its bore. An omitted window of the boss, or an omitted z
    window, means the whole part."""
    bore_label = "a" if feature.a.side == "inner" else "b"
    bore = feature.a if bore_label == "a" else feature.b
    boss_label = "b" if bore_label == "a" else "a"
    boss = feature.b if bore_label == "a" else feature.a
    if bore.r is not None or feature.a.side == feature.b.side:
        return feature
    mesh = ctx.mesh_a_axis if boss_label == "a" else ctx.mesh_b_axis
    pts = _model_points(mesh, boss)
    if len(pts) == 0:
        return feature
    limit = float(np.hypot(pts[:, 0], pts[:, 1]).max()) + REACH_MM
    bore = replace(bore, r=(0.0, limit))
    return replace(feature, a=bore) if bore_label == "a" else replace(feature, b=bore)


def feature_report(feature: Feature, ctx: _Ctx) -> dict[str, Any]:
    feature = resolve_windows(feature, ctx)
    sources = ["model"]
    if ctx.printed_a is not None or ctx.printed_b is not None:
        sources.append("as_printed")
    doc: dict[str, Any] = {
        "name": feature.name,
        "kind": "thread" if feature.pitch_mm > 0 else "ribs",
        "pitch_mm": feature.pitch_mm,
        "windows": {
            "a": {"side": feature.a.side, "z": feature.a.z, "r": feature.a.r},
            "b": {"side": feature.b.side, "z": feature.b.z, "r": feature.b.r},
        },
    }
    worst = "pass"
    for src in sources:
        doc[src] = analyse_feature(feature, ctx, src)
        worst = _worse(worst, doc[src]["status"])
    if len(sources) > 1:
        doc["printed_vs_model"] = _printed_vs_model(feature, ctx)
        doc["as_printed_parts"] = [
            k for k, v in (("a", ctx.printed_a), ("b", ctx.printed_b)) if v is not None
        ]
    doc["status"] = worst
    return doc


def _worse(a: str, b: str) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return a if order[a] >= order[b] else b


def _screw_matrix(axis: Axis, motion: dict[str, float]) -> Matrix:
    """The pose change of part B that realises `best_phase_move_b`: a turn about the axis (the axial position, e.g. a
    seat face, stays)."""
    from assembly.scene import axis_rotation

    return axis_rotation(axis.direction, motion["rotate_deg"], axis.origin)


def fit_requirement(
    fit: str,
    shape: str,
    printer: str | None,
    material: str | None,
    nozzle: float | None,
    layer: float | None,
) -> dict[str, Any]:
    """The radial gap the `3d fits` table asks of this fit on this printer and plastic: clearance per face - tolerance."""
    import fits_sources

    res = fits_sources.resolve(fit, shape, printer, material, nozzle, layer, command=CMD)
    return {
        "fit_type": res.fit_type,
        "shape": shape,
        "clearance_mm": round(res.clearance_mm, 4),
        "tol_mm": round(res.tol_mm, 4),
        "interference_mm": round(res.interference_mm, 4),
        "required_gap_mm": round(res.clearance_mm - res.tol_mm, 4),
        "fallback": res.fallback,
        "source": "fit table (`3d fits resolve`)",
        "provenance": [dict(p) for p in res.provenance],
    }


def _fit_required(opts: PairOptions) -> float:
    if opts.min_radial_gap_mm is not None:
        return opts.min_radial_gap_mm
    if opts.fit_resolution is not None:
        return float(opts.fit_resolution["required_gap_mm"])
    return DEFAULT_MIN_RADIAL_GAP_MM


@dataclass
class _Setup:
    """Everything `run_pair` measures: the posed meshes, the axis, the printed parts, the screw that was applied."""

    posed_a: Any
    posed_b: Any
    axis: Axis
    printed_a: PrintedPart | None
    printed_b: PrintedPart | None
    required: float
    screw: dict[str, Any] | None


def _prepare(opts: PairOptions) -> _Setup:
    mesh_a, qa = _load(opts.a, "a", opts.max_faces)
    mesh_b, qb = _load(opts.b, "b", opts.max_faces)
    posed_a, posed_b = mesh_a.copy(), mesh_b.copy()
    posed_a.apply_transform(qa)
    posed_b.apply_transform(qb)
    axis = make_axis(opts, posed_a)
    printed_a = (
        load_printed(opts.a, "a", mesh_a, qa, opts.skip_below_mm) if opts.a.gcode else None
    )
    printed_b = (
        load_printed(opts.b, "b", mesh_b, qb, opts.skip_below_mm) if opts.b.gcode else None
    )
    required = _fit_required(opts)
    screw = None
    if opts.screw_to_best and opts.features:
        screw, qb, posed_b = _apply_screw(opts, axis, posed_a, posed_b, qb, required)
        if printed_b is not None:
            printed_b = replace(
                printed_b, bed_to_assembly=screw["matrix"] @ printed_b.bed_to_assembly
            )
    return _Setup(posed_a, posed_b, axis, printed_a, printed_b, required, screw)


def _head(opts: PairOptions, su: _Setup) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "pair",
        "parts": {
            "a": _part_doc(opts.a, su.posed_a),
            "b": _part_doc(opts.b, su.posed_b),
        },
        "axis": {
            "point": [round(float(v), 4) for v in su.axis.origin],
            "direction": [round(float(v), 6) for v in su.axis.direction],
        },
    }
    if su.screw is not None:
        doc["screw_to_best"] = {k: v for k, v in su.screw.items() if k != "matrix"}
    return doc


def _verdict(doc: dict[str, Any]) -> None:
    """`ok` and the names of the failed / warned checks, from the statuses already in `doc`."""
    named = _named(doc)
    doc["ok"] = all(s != "fail" for _, s in named)
    doc["summary"] = {
        "fail": [n for n, s in named if s == "fail"],
        "warn": [n for n, s in named if s == "warn"],
    }


def run_pair(opts: PairOptions) -> dict[str, Any]:
    """Judge two parts; the returned document is the `--json` payload (`kind: pair`)."""
    su = _prepare(opts)
    ctx = _Ctx(
        su.axis,
        _to_axis(su.posed_a, su.axis),
        _to_axis(su.posed_b, su.axis),
        su.printed_a,
        su.printed_b,
        su.required,
    )
    doc = _head(opts, su)
    solid_a = solid_from_mesh(su.posed_a, "a", max_faces=opts.max_faces)
    solid_b = solid_from_mesh(su.posed_b, "b", max_faces=opts.max_faces)
    doc["interpenetration"] = interpenetration(
        solid_a, solid_b, opts.max_overlap_mm3, opts.seat_tol_mm
    )
    doc["clearance"] = clearance(
        solid_a, solid_b, su.posed_a, su.posed_b, opts.min_clearance_mm, opts.zone
    )
    doc["features"] = [feature_report(f, ctx) for f in opts.features]
    printed_doc = {
        k: v.info for k, v in (("a", su.printed_a), ("b", su.printed_b)) if v is not None
    }
    if printed_doc:
        doc["printed"] = printed_doc
    if opts.fit_resolution is not None:
        doc["fit"] = opts.fit_resolution
    _verdict(doc)
    return doc


def _named(doc: dict[str, Any]) -> list[tuple[str, str]]:
    rows = [
        ("interpenetration", doc["interpenetration"]["status"]),
        ("clearance", doc["clearance"]["status"]),
    ]
    rows.extend((f"feature:{f['name']}", f["status"]) for f in doc["features"])
    return rows


def _to_axis(mesh: Any, axis: Axis) -> Any:
    out = mesh.copy()
    out.apply_transform(axis.matrix)
    return out


def _part_doc(part: PartInput, posed: Any) -> dict[str, Any]:
    return {
        "mesh": str(part.path),
        "scale": part.scale,
        "bbox_assembled": _bbox(posed),
        "faces": len(posed.faces),
        "gcode": str(part.gcode) if part.gcode else None,
    }


def _apply_screw(
    opts: PairOptions,
    axis: Axis,
    posed_a: Any,
    posed_b: Any,
    qb: Matrix,
    required: float,
) -> tuple[dict[str, Any], Matrix, Any]:
    """Probe the first feature on the MODEL, then move B by the motion that brings it to its best phase."""
    probe = _Ctx(
        axis, _to_axis(posed_a, axis), _to_axis(posed_b, axis), None, None, required
    )
    feature = resolve_windows(opts.features[0], probe)
    res = analyse_feature(feature, probe, "model")
    cl = res.get("clearance")
    if cl is None:
        raise InvalidArgument(
            "--screw-to-best",
            "no common phase",
            [res.get("mismatch") or "a measurable feature"],
            command=CMD,
        )
    motion = cl["best_phase_move_b"]
    m = _screw_matrix(axis, motion)
    moved = posed_b.copy()
    moved.apply_transform(m)
    return (
        {"feature": feature.name, "applied_to_b": motion, "matrix": m},
        m @ qb,
        moved,
    )


def pose_from_text(text: str | None, where: str) -> Pose:
    """A pose written inline (`{translate: [0, 0, 5], rotate: [0, 0, 90]}` or a list of them) -> spec pose ops."""
    if not text:
        return ()
    import yaml

    from assembly.spec import parse_pose

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InvalidArgument(
            where, text, ["a pose mapping, e.g. '{translate: [0, 0, 5]}'"], command=CMD
        ) from exc
    return parse_pose(value, where)


def features_from_file(path: Path) -> list[Feature]:
    """`features:` list of a YAML / JSON file; see docs/commands/assembly.md."""
    import yaml

    if not path.is_file():
        raise InputNotFound(str(path), command=CMD)
    data = yaml.safe_load(path.read_text())
    rows = data.get("features") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise InvalidArgument(
            "--features",
            str(path),
            ["a `features:` list with at least one entry"],
            command=CMD,
        )
    return [_feature_from_mapping(r, f"{path.name}[{i}]") for i, r in enumerate(rows)]


def _side_from_mapping(raw: Any, where: str) -> FeatureSide:
    if not isinstance(raw, dict) or raw.get("side") not in ("inner", "outer"):
        raise InvalidArgument(
            where,
            str(raw),
            ["{side: inner|outer, z: [lo, hi], r: [lo, hi]}"],
            command=CMD,
        )

    def rng(key: str) -> Range | None:
        v = raw.get(key)
        if v is None:
            return None
        return parse_range(f"{v[0]}:{v[1]}", f"{where}.{key}")

    return FeatureSide(raw["side"], rng("z"), rng("r"))


def _feature_from_mapping(raw: Any, where: str) -> Feature:
    if not isinstance(raw, dict):
        raise InvalidArgument(where, str(raw), ["a mapping"], command=CMD)
    unknown = set(raw) - {"name", "pitch", "hand", "a", "b", "min_gap_mm"}
    if unknown:
        raise InvalidArgument(
            where,
            ", ".join(sorted(unknown)),
            ["name, pitch, hand, a, b, min_gap_mm"],
            command=CMD,
        )
    hand = str(raw.get("hand", "auto"))
    if hand not in ("auto", "right", "left"):
        raise InvalidArgument(
            f"{where}.hand", hand, ["auto", "right", "left"], command=CMD
        )
    pitch = float(raw.get("pitch", 0.0))
    if pitch < 0:
        raise InvalidArgument(
            f"{where}.pitch", str(pitch), ["0 (ribs) or a positive pitch"], command=CMD
        )
    gap = raw.get("min_gap_mm")
    return Feature(
        str(raw.get("name", "feature")),
        _side_from_mapping(raw.get("a"), f"{where}.a"),
        _side_from_mapping(raw.get("b"), f"{where}.b"),
        pitch,
        hand,
        None if gap is None else float(gap),
    )
