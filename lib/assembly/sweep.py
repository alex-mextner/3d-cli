"""sweep.py — move a part along its declared insertion/removal path and report the first collision (#553, #61).

WHAT: `sweep_mate` takes one mate's motion (a straight insertion axis of ANY direction, or a hinge angle range) and
  samples the moving part (plus whatever it `carry`s) against every other part, every `step_mm` (degrees for a
  hinge). It reports
    - the first CONTACT and the first BLOCKING position, refined by bisection to `bisect_mm`;
    - for snap mates the peak interference and the TIP DEFLECTION it implies (the overlap's extent along each hook's
      `deflect` direction) against the hook's flex limit (declared, or from the S4 closed forms) and the force
      estimate hook of `assembly.forces`;
    - the sampling resolution: this is a sampled test, not a continuous-collision proof (a feature thinner than
      the step can be stepped over); the report says so.
  `plan_order` is the multi-step version (#61): a graph search over "which part/sub-assembly can leave along a straight
  line", giving a feasible assembly order, or the stuck state and what blocks each part.

PATH CLASSES: contact (thinner than `seat_tol_mm`, ignored) | flex (a snap barb inside the hook's flex limit) |
  allowed (a declared press interference) | blocking (everything else).
  Zones of a mate describe the CLOSED pose and are ignored while the part is moving; caps (`allow`) and flex limits
  apply anywhere along the path.

INVARIANTS: input meshes are never modified; only the moving solids are transformed (lazily, in manifold3d).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from assembly.forces import flex_limit, hook_model, material_modulus
from assembly.overlap import Region, pair_regions
from assembly.scene import (
    Body,
    Scene,
    axis_rotation,
    box_solid,
    boxes_overlap,
    crop,
    translation_matrix,
)
from assembly.spec import DEPTH_EPS_MM, SCHEMA, Hook, MateSpec
from assembly.vec import Vec3, format_direction
from errors import InvalidArgument

RESOLUTION_NOTE = (
    "sampled collision test: positions are checked every {step:g} {unit}; a feature thinner than the step can be "
    "stepped over, so this is not a continuous-collision guarantee (lower --step to tighten it)"
)


@dataclass
class Motion:
    """A path parameterised by s >= 0: s = 0 is the assembled pose, growing s moves away from it."""

    kind: str  # translate | rotate
    mover: str
    carry: tuple[str, ...]
    axis: Vec3  # translate: the assembly direction (the part moves by -axis * s); rotate: hinge axis
    extent: float  # mm (translate) or degrees (rotate)
    origin: Vec3 = (0.0, 0.0, 0.0)
    lo: float = 0.0  # rotate: minimum angle of the sweep range
    ignore: tuple[str, ...] = ()  # parts that are not obstacles on this path

    @property
    def unit(self) -> str:
        return "mm" if self.kind == "translate" else "deg"

    @property
    def group(self) -> tuple[str, ...]:
        return (self.mover, *self.carry)

    def matrix(self, s: float) -> Any:
        if self.kind == "translate":
            return translation_matrix([-c * s for c in self.axis])
        return axis_rotation(self.axis, s, self.origin)

    def displacement_dir(self) -> Vec3:
        """Unit direction a point of the mover travels while s grows (translate only)."""
        return (-self.axis[0], -self.axis[1], -self.axis[2])


def motion_for(mate: MateSpec, distance_mm: float | None = None) -> Motion:
    if mate.insertion is not None:
        ins = mate.insertion
        return Motion(
            "translate",
            ins.part,
            ins.carry,
            ins.axis,
            distance_mm or ins.distance_mm,
            ignore=ins.ignore,
        )
    if mate.hinge is not None:
        hg = mate.hinge
        return Motion(
            "rotate",
            hg.part,
            hg.carry,
            hg.axis,
            distance_mm or hg.range_deg[1],
            hg.origin,
            hg.range_deg[0],
            ignore=hg.ignore,
        )
    raise InvalidArgument(
        f"mate {mate.name}",
        "no motion",
        [
            "insertion: {part, axis, distance_mm}",
            "hinge: {part, axis, origin, range_deg}",
        ],
        command="assembly",
        extra="A sweep needs the mate to declare how its part moves.",
    )


def _solid_at(body: Body, matrix: Any) -> Any:
    m = np.asarray(matrix, dtype=float)
    return body.solid().transform(
        [list(map(float, m[0])), list(map(float, m[1])), list(map(float, m[2]))]
    )


def _transform_point(matrix: Any, point: Sequence[float]) -> tuple[float, float, float]:
    v = np.asarray(matrix, dtype=float) @ np.array([point[0], point[1], point[2], 1.0])
    return (float(v[0]), float(v[1]), float(v[2]))


def _extent_along(region: Region, direction: Sequence[float] | None) -> float:
    """Width of the overlap region along `direction` (the lateral move a hook tip must make); depth if None."""
    if direction is None or region.mesh is None:
        return region.depth_mm
    d = np.asarray(direction, dtype=float)
    proj = np.asarray(region.mesh.vertices) @ (d / np.linalg.norm(d))
    return float(proj.max() - proj.min())


@dataclass
class PathContext:
    """Everything a path evaluation needs about a scene, resolved once."""

    scene: Scene
    motion: Motion
    obstacles: list[str]
    mates_by_pair: dict[frozenset[str], list[MateSpec]]
    limits: dict[str, tuple[float | None, str]] = field(
        default_factory=dict
    )  # hook key -> (limit, source)
    material: str | None = None
    arms: dict[str, Any] = field(default_factory=dict)  # hook key -> arm solid (cached)

    def hooks_for(self, pair: frozenset[str]) -> list[tuple[MateSpec, Hook]]:
        out: list[tuple[MateSpec, Hook]] = []
        for mate in self.mates_by_pair.get(pair, []):
            if mate.snap is not None:
                out.extend((mate, h) for h in mate.snap.hooks)
        return out


def _hook_key(mate: MateSpec, hook: Hook) -> str:
    return f"{mate.name}.{hook.name}"


def build_context(
    scene: Scene,
    motion: Motion,
    *,
    ignore: Sequence[str] = (),
    material: str | None = None,
) -> PathContext:
    group = set(motion.group)
    skipped = set(ignore) | set(motion.ignore)
    obstacles = [n for n in sorted(scene.bodies) if n not in group and n not in skipped]
    by_pair: dict[frozenset[str], list[MateSpec]] = {}
    for mate in scene.spec.mates:
        by_pair.setdefault(mate.pair, []).append(mate)
    ctx = PathContext(
        scene, motion, obstacles, by_pair, {}, material or scene.spec.context.material
    )
    for mate in scene.spec.mates:
        if mate.snap is not None:
            for hook in mate.snap.hooks:
                ctx.limits[_hook_key(mate, hook)] = flex_limit(
                    mate.snap, hook, ctx.material
                )
    return ctx


def _hook_claims(
    ctx: PathContext, mate: MateSpec, hook: Hook, region: Region, matrix: Any
) -> float | None:
    """Distance of an overlap region to a hook if the hook can be its cause, else None.

    With a declared `region` the overlap must lie inside it (in the carrier's own frame, grown by the barb);
    without one the hook must be within 6 mm.
    """
    carrier_moves = mate.snap is not None and mate.snap.part in ctx.motion.group
    centre: tuple[float, float, float] = region.center
    if carrier_moves:
        centre = _transform_point(np.linalg.inv(np.asarray(matrix, dtype=float)), centre)
    dist = math.dist(hook.point, centre)
    if hook.region is not None:
        return dist if hook.region.contains(centre, 0.5) else None
    return dist if dist <= 6.0 else None


DEFLECTION_RES_MM = 0.002  # bisection resolution of the hook deflection (reported as the uncertainty)


def exact_deflection(
    ctx: PathContext, mate: MateSpec, hook: Hook, matrix: Any, cap_mm: float
) -> float | None:
    """Smallest rigid shift of the hook ARM along `deflect` that frees it from the partner at this path position.

    The arm is the carrier's solid inside the hook `region`; it is moved along `deflect` by bisection until it no
    longer overlaps the partner (beyond the seat tolerance). The returned value is the upper end of a
    `DEFLECTION_RES_MM` bracket; None = it cannot clear within `cap_mm`. A rigid shift of the arm geometry is
    conservative for a cantilever that really swings about its root.
    """
    snap = mate.snap
    assert snap is not None and hook.region is not None and hook.deflect is not None
    cfg = ctx.scene.spec.settings
    key = _hook_key(mate, hook)
    arm = ctx.arms.get(key)
    if arm is None:
        lo, hi = hook.region.bbox(0.0)
        arm = ctx.scene.body(snap.part).solid() ^ box_solid(lo, hi)
        ctx.arms[key] = arm
    if arm.is_empty():
        return None
    partner_name = mate.b if snap.part == mate.a else mate.a
    partner = ctx.scene.body(partner_name)
    carrier_moves = snap.part in ctx.motion.group
    partner_moves = partner_name in ctx.motion.group
    rows = [list(map(float, r)) for r in np.asarray(matrix, dtype=float)[:3]]
    arm_world = arm.transform(rows) if carrier_moves else arm
    box = arm_world.bounding_box()
    window = (
        box[0] - cap_mm - 1.0, box[1] - cap_mm - 1.0, box[2] - cap_mm - 1.0,
        box[3] + cap_mm + 1.0, box[4] + cap_mm + 1.0, box[5] + cap_mm + 1.0,
    )  # fmt: skip
    partner_solid = partner.solid().transform(rows) if partner_moves else partner.solid()
    partner_near = crop(partner_solid, window)  # one crop per call; the bisection then works on a few hundred faces
    if partner_near.is_empty():
        return 0.0
    rot = np.asarray(matrix, dtype=float)[:3, :3] if carrier_moves else np.eye(3)
    direction = rot @ np.asarray(hook.deflect, dtype=float)

    def collides(d: float) -> bool:
        moved = arm_world.translate(tuple(float(c) for c in direction * d))
        found = pair_regions(
            snap.part, moved, partner_name, partner_near,
            eps_mm3=cfg.eps_mm3, pad_mm=cfg.pair_pad_mm, keep_mesh=False,
        )  # fmt: skip
        return any(r.depth_mm > DEPTH_EPS_MM for r in found)

    if not collides(0.0):
        return 0.0
    if collides(cap_mm):
        return None
    lo_d, hi_d = 0.0, cap_mm
    while hi_d - lo_d > DEFLECTION_RES_MM:
        mid = (lo_d + hi_d) / 2.0
        if collides(mid):
            lo_d = mid
        else:
            hi_d = mid
    return hi_d


def classify_path_region(
    ctx: PathContext, mover: str, obstacle: str, region: Region, s: float
) -> dict[str, Any]:
    """Label an overlap met on the path contact | flex | allowed | blocking (see module docstring)."""
    spec = ctx.scene.spec
    out: dict[str, Any] = region.to_dict()
    out["s"] = round(s, 4)
    if region.depth_mm <= spec.settings.seat_tol_mm:
        out.update(
            classification="contact", reason="touching (within the seat tolerance)"
        )
        return out
    pair = frozenset((mover, obstacle))
    matrix = ctx.motion.matrix(s)
    # snap: the nearest hook of this pair that can be the cause decides: its arm must flex clear by `deflection`
    best: tuple[float, MateSpec, Hook] | None = None
    for mate, hook in ctx.hooks_for(pair):
        dist = _hook_claims(ctx, mate, hook, region, matrix)
        if dist is not None and (best is None or dist < best[0]):
            best = (dist, mate, hook)
    if best is not None:
        _, mate, hook = best
        limit, source = ctx.limits[_hook_key(mate, hook)]
        exact = hook.region is not None and hook.deflect is not None
        if exact:
            cap = (limit if limit is not None else 1.0) + 0.3
            needed_opt = exact_deflection(ctx, mate, hook, matrix, cap)
            needed = cap if needed_opt is None else needed_opt
            out["deflection_method"] = "arm shifted along `deflect` until clear"
        else:
            carrier_moves = mate.snap is not None and mate.snap.part in ctx.motion.group
            deflect = None
            if hook.deflect is not None:
                rot = np.asarray(matrix, dtype=float)[:3, :3] if carrier_moves else np.eye(3)
                deflect = tuple(float(c) for c in rot @ np.asarray(hook.deflect))
            needed_opt = needed = _extent_along(region, deflect)
            out["deflection_method"] = "overlap extent (declare hook `region` + `deflect` for the exact value)"
        out.update(
            mate=mate.name,
            hook=hook.name,
            deflection_mm=round(needed, 4),
            flex_limit_mm=limit,
        )
        if needed_opt is None:
            out.update(
                classification="blocking",
                reason=f"snap hook {hook.name} cannot clear by flexing within {needed:.2f} mm",
            )
        elif limit is None:
            out.update(
                classification="flex",
                limit_known=False,
                reason=f"snap hook {hook.name} flexes {needed:.3f} mm; no flex limit known ({source})",
            )
        elif needed - (DEFLECTION_RES_MM if exact else 0.0) > limit + DEPTH_EPS_MM:
            out.update(
                classification="blocking",
                reason=f"snap hook {hook.name} would have to flex {needed:.3f} mm > flex limit {limit:.3f} mm",
            )
        else:
            marginal = needed > limit + DEPTH_EPS_MM
            out.update(
                classification="flex",
                limit_known=True,
                marginal=marginal,
                reason=(
                    f"snap hook {hook.name} flexes {needed:.3f} mm = the flex limit {limit:.3f} mm within the "
                    f"{DEFLECTION_RES_MM:g} mm resolution (no margin)"
                    if marginal
                    else f"snap hook {hook.name} flexes {needed:.3f} mm <= flex limit {limit:.3f} mm"
                ),
            )
        return out
    for mate in ctx.mates_by_pair.get(pair, []):
        allowed_cap = mate.allow.max_depth_mm
        if allowed_cap is None and mate.kind == "press":
            from assembly.check import resolve_fit

            fit = resolve_fit(mate, spec.context)
            allowed_cap = fit.interference_mm + fit.tol_mm
        if allowed_cap is not None and region.depth_mm <= allowed_cap + DEPTH_EPS_MM:
            out.update(
                classification="allowed",
                mate=mate.name,
                reason=f"designed interference of mate {mate.name} (depth {region.depth_mm:.3f} <= {allowed_cap:.3f} mm)",
            )
            return out
    out.update(
        classification="blocking",
        reason=f"{mover} collides with {obstacle} (depth {region.depth_mm:.3f} mm)"
        + ("" if ctx.mates_by_pair.get(pair) else "; no mate between them declares it"),
    )
    return out


def evaluate(ctx: PathContext, s: float) -> list[dict[str, Any]]:
    """Every overlap of the moving group with the obstacles at path parameter `s`, classified."""
    spec = ctx.scene.spec
    matrix = ctx.motion.matrix(s)
    found: list[dict[str, Any]] = []
    for mover_name in ctx.motion.group:
        mover = ctx.scene.body(mover_name)
        moved_box = _moved_box(mover, matrix)
        placed: Any = None
        for name in ctx.obstacles:
            other = ctx.scene.body(name)
            if not boxes_overlap(moved_box, other.box, 0.0):
                continue
            if placed is None:
                placed = _solid_at(mover, matrix)
            for region in pair_regions(
                mover_name,
                placed,
                name,
                other.solid(),
                eps_mm3=spec.settings.eps_mm3,
                pad_mm=spec.settings.pair_pad_mm,
            ):
                found.append(classify_path_region(ctx, mover_name, name, region, s))  # noqa: PERF401 — a formatted row per item reads better than a comprehension
    return found


def _moved_box(body: Body, matrix: Any) -> Any:
    b = body.box
    corners = np.array(
        list(itertools.product((b[0], b[3]), (b[1], b[4]), (b[2], b[5])))
    )
    moved = (np.asarray(matrix)[:3, :3] @ corners.T).T + np.asarray(matrix)[:3, 3]
    lo, hi = moved.min(axis=0), moved.max(axis=0)
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _blocking(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in found if r["classification"] == "blocking"]


def _engaged(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in found if r["classification"] != "contact"]


def _positions(motion: Motion, step: float, direction: str) -> list[float]:
    """Sample parameters: [0, extent] for a slide, [lo, hi] degrees for a hinge."""
    lo = motion.lo if motion.kind == "rotate" else 0.0
    hi = motion.extent
    n = max(1, math.ceil((hi - lo) / step - 1e-9))
    pts = [min(lo + i * step, hi) for i in range(n + 1)]
    if pts[-1] < hi - 1e-9:
        pts.append(hi)
    return pts if direction == "remove" else pts[::-1]


def _bisect(
    ctx: PathContext,
    free: float,
    hit: float,
    predicate: Callable[[list[dict[str, Any]]], bool],
    tol: float,
) -> tuple[float, list[dict[str, Any]]]:
    """Refine the boundary between a free sample and the first sample where `predicate` holds."""
    hit_found = evaluate(ctx, hit)
    while abs(hit - free) > tol:
        mid = (hit + free) / 2.0
        found = evaluate(ctx, mid)
        if predicate(found):
            hit, hit_found = mid, found
        else:
            free = mid
    return hit, hit_found


def sweep_mate(
    scene: Scene,
    mate: MateSpec,
    *,
    direction: str = "insert",
    step: float | None = None,
    distance: float | None = None,
    ignore: Sequence[str] = (),
    material: str | None = None,
) -> dict[str, Any]:
    """Sweep one mate's motion in `insert` or `remove` direction and report the first collision."""
    if direction not in ("insert", "remove"):
        raise InvalidArgument(
            "--direction", direction, ["insert", "remove"], command="assembly"
        )
    cfg = scene.spec.settings
    motion = motion_for(mate, distance)
    ctx = build_context(scene, motion, ignore=ignore, material=material)
    step_v = step if step is not None else cfg.step_mm
    if step_v <= 0:
        raise InvalidArgument("--step", str(step_v), ["a step > 0"], command="assembly")
    positions = _positions(motion, step_v, direction)
    profile: list[dict[str, Any]] = []
    first_contact: dict[str, Any] | None = None
    first_block: dict[str, Any] | None = None
    peak: dict[str, dict[str, Any]] = {}
    prev_s: float | None = None
    prev_engaged = False
    prev_blocked = False
    for s in positions:
        found = evaluate(ctx, s)
        engaged, blocked = _engaged(found), _blocking(found)
        row = {
            "s": round(s, 4),
            "depth_mm": round(max((r["depth_mm"] for r in engaged), default=0.0), 4),
            "state": "blocking" if blocked else ("engaged" if engaged else "free"),
        }
        if engaged or prev_engaged:
            profile.append(row)
        if engaged and first_contact is None:
            if prev_s is None:
                at, at_found = s, found
            else:
                at, at_found = _bisect(
                    ctx, prev_s, s, lambda f: bool(_engaged(f)), cfg.bisect_mm
                )
            first_contact = _event(motion, direction, at, _engaged(at_found))
        if blocked and first_block is None:
            if prev_s is not None and not prev_blocked:
                at, at_found = _bisect(
                    ctx, prev_s, s, lambda f: bool(_blocking(f)), cfg.bisect_mm
                )
            else:
                at, at_found = s, found
            first_block = _event(motion, direction, at, _blocking(at_found))
        for r in engaged:
            if "hook" in r:
                key = f"{r['mate']}.{r['hook']}"
                cur = peak.get(key)
                if cur is None or r["deflection_mm"] > cur["deflection_mm"]:
                    peak[key] = {**r, "at_s": r["s"]}
        prev_s, prev_engaged, prev_blocked = s, bool(engaged), bool(blocked)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "sweep",
        "ok": first_block is None,
        "mate": mate.name,
        "a": mate.a,
        "b": mate.b,
        "mover": motion.mover,
        "carried": list(motion.carry),
        "direction": direction,
        "motion": {
            "type": motion.kind,
            "axis": list(motion.axis),
            "axis_label": format_direction(motion.axis),
            "extent": motion.extent,
            "unit": motion.unit,
            "origin": list(motion.origin) if motion.kind == "rotate" else None,
        },
        "step": step_v,
        "samples": len(positions),
        "resolution_note": RESOLUTION_NOTE.format(step=step_v, unit=motion.unit),
        "obstacles": ctx.obstacles,
        "path_clear": first_block is None,
        "first_contact": first_contact,
        "first_blocking": first_block,
        "profile": profile[:400],
        "profile_truncated": len(profile) > 400,
    }
    if mate.snap is not None:
        report["snap"] = _snap_verdict(ctx, mate, peak, first_block)
        report["ok"] = report["ok"] and report["snap"]["verdict"]["status"] != "fail"
    return report


def _event(
    motion: Motion, direction: str, s: float, regions: list[dict[str, Any]]
) -> dict[str, Any]:
    worst = max(regions, key=lambda r: r["depth_mm"])
    if motion.kind == "rotate":
        travelled = abs(s)
    else:
        travelled = motion.extent - s if direction == "insert" else s
    return {
        "s": round(s, 4),
        "distance_from_assembled": round(s, 4),
        "travelled": round(max(travelled, 0.0), 4),
        "unit": motion.unit,
        "pair": [worst["a"], worst["b"]],
        "depth_mm": worst["depth_mm"],
        "center": worst["center"],
        "classification": worst["classification"],
        "reason": worst["reason"],
        "regions": len(regions),
    }


def _snap_verdict(
    ctx: PathContext,
    mate: MateSpec,
    peak: dict[str, dict[str, Any]],
    first_block: dict[str, Any] | None,
) -> dict[str, Any]:
    snap = mate.snap
    assert snap is not None
    modulus = material_modulus(ctx.material)
    hooks_out: list[dict[str, Any]] = []
    status, reasons = "pass", []
    for hook in snap.hooks:
        key = _hook_key(mate, hook)
        limit, source = ctx.limits[key]
        entry: dict[str, Any] = {
            "hook": hook.name,
            "flex_limit_mm": limit,
            "flex_limit_source": source,
        }
        seen = peak.get(key)
        if seen is None:
            entry.update(
                engaged=False, max_interference_mm=0.0, implied_deflection_mm=0.0
            )
            if first_block is None:
                reasons.append(
                    f"hook {hook.name}: no overlap on this path (does not engage)"
                )
                status = _worse(status, "warn")
        else:
            entry.update(
                engaged=True,
                max_interference_mm=seen["depth_mm"],
                implied_deflection_mm=seen["deflection_mm"],
                at_s=seen["at_s"],
                location=seen["center"],
            )
            entry["forces"] = hook_model(snap, hook, modulus).estimate(
                seen["deflection_mm"]
            )
            if limit is None:
                reasons.append(
                    f"hook {hook.name}: deflection {seen['deflection_mm']:.3f} mm, flex limit unknown ({source})"
                )
                status = _worse(status, "warn")
            elif seen.get("marginal"):
                entry["marginal"] = True
                reasons.append(
                    f"hook {hook.name}: deflection {seen['deflection_mm']:.3f} mm is AT the flex limit {limit:.3f} mm "
                    f"(within {DEFLECTION_RES_MM:g} mm resolution): no margin, strain safety factor ~1"
                )
                status = _worse(status, "warn")
            elif seen["deflection_mm"] > limit + DEPTH_EPS_MM:
                reasons.append(
                    f"hook {hook.name}: deflection {seen['deflection_mm']:.3f} mm exceeds the flex limit {limit:.3f} mm"
                )
                status = _worse(status, "fail")
            else:
                reasons.append(
                    f"hook {hook.name}: deflection {seen['deflection_mm']:.3f} mm within the flex limit {limit:.3f} mm"
                )
        hooks_out.append(entry)
    if first_block is not None:
        status = "fail"
        reasons.append(f"the path is blocked: {first_block['reason']}")
    return {"verdict": {"status": status, "reasons": reasons}, "hooks": hooks_out}


def _worse(a: str, b: str) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return a if order[a] >= order[b] else b


# --------------------------------------------------------------------------- assembly order (#61)
def _candidate_directions(scene: Scene, subset: Sequence[str]) -> list[Vec3]:
    """Removal directions to try: the reverse of each declared insertion axis of the subset, then +-X/Y/Z."""
    dirs: list[Vec3] = []
    for mate in scene.spec.mates:
        ins = mate.insertion
        if ins is not None and (ins.part in subset or set(ins.carry) & set(subset)):
            dirs.append(
                ins.axis
            )  # moving by -axis*s is removal; the removal direction is -axis => axis=assembly dir
    seen: set[tuple[float, float, float]] = set()
    out: list[Vec3] = []
    for d in [
        *dirs,
        (0.0, 0.0, 1.0),
        (0.0, 0.0, -1.0),
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0),
    ]:
        key = (round(d[0], 4), round(d[1], 4), round(d[2], 4))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _removable(
    scene: Scene,
    subset: tuple[str, ...],
    state: frozenset[str],
    step: float,
    max_travel: float,
) -> dict[str, Any]:
    """Can `subset` leave `state` along some straight line? -> {ok, direction, travel, blockers[]}."""
    rest = sorted(state - set(subset))
    blockers: list[dict[str, Any]] = []
    cfg = scene.spec.settings
    for axis in _candidate_directions(scene, subset):
        motion = Motion("translate", subset[0], tuple(subset[1:]), axis, max_travel)
        ctx = build_context(scene, motion)
        ctx.obstacles = rest
        ok, hit = True, None
        passes: list[dict[str, Any]] = []
        s = 0.0
        while s <= max_travel + 1e-9:
            found = evaluate(ctx, s)
            blocked = _blocking(found)
            if blocked:
                ok, hit = False, (s, blocked[0])
                break
            passes.extend(
                r for r in found if r["classification"] in ("flex", "allowed")
            )
            if s > 0 and _clear_of_rest(
                scene, subset, rest, motion, s, cfg.pair_pad_mm
            ):
                return {
                    "ok": True,
                    "direction": axis,
                    "travel_mm": round(s, 3),
                    "passes": _passes_brief(passes),
                }
            s += step
        if not ok and hit is not None:
            blockers.append(
                {
                    "removal_direction": [-c for c in axis],
                    "at_mm": round(hit[0], 3),
                    "blocker": hit[1]["b"],
                    "mover": hit[1]["a"],
                }
            )
    return {"ok": False, "blockers": blockers}


def _passes_brief(passes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    worst: dict[tuple[str, str], dict[str, Any]] = {}
    for r in passes:
        key = (r["a"], r["b"])
        cur = worst.get(key)
        if cur is None or r["depth_mm"] > cur["depth_mm"]:
            worst[key] = r
    return [
        {
            "pair": [k[0], k[1]],
            "class": v["classification"],
            "depth_mm": v["depth_mm"],
            "deflection_mm": v.get("deflection_mm"),
            "mate": v.get("mate"),
        }
        for k, v in sorted(worst.items())
    ]


def _clear_of_rest(
    scene: Scene,
    subset: Sequence[str],
    rest: Sequence[str],
    motion: Motion,
    s: float,
    pad: float,
) -> bool:
    """True once the moved subset's box no longer touches any remaining part's box (it can leave for good)."""
    matrix = motion.matrix(s)
    for name in subset:
        box = _moved_box(scene.body(name), matrix)
        if any(boxes_overlap(box, scene.body(r).box, pad) for r in rest):
            return False
    return True


def plan_order(
    scene: Scene,
    *,
    base: str | None = None,
    step: float | None = None,
    max_travel: float | None = None,
    max_parts: int = 14,
) -> dict[str, Any]:
    """Find an assembly order by disassembly search: repeatedly remove one part/group along a straight line.

    State = the parts still assembled; a move removes a single part or a declared group and needs one straight path
    (the reverse of a mate's insertion axis, or +-X/Y/Z) that is free of blocking overlaps until the moved part
    clears the rest. Depth-first with a memo of dead states; the base part never moves.
    """
    spec = scene.spec
    names = sorted(scene.bodies)
    if len(names) > max_parts:
        raise InvalidArgument(
            "parts",
            f"{len(names)} parts",
            [f"at most {max_parts} for the order search"],
            command="assembly",
            extra="Pass a project subset or declare `groups:` to treat rigid sub-assemblies as one part.",
        )
    printed = [n for n in names if spec.parts[n].printable]
    pool = printed or names
    base_name = base or spec.base or max(pool, key=lambda n: _volume(scene.body(n)))
    if base_name not in scene.bodies:
        raise InvalidArgument("--base", base_name, names, command="assembly")
    step_v = step if step is not None else max(spec.settings.step_mm, 0.25)
    diag = _scene_diag(scene)
    travel = max_travel if max_travel is not None else diag
    subsets: list[tuple[str, ...]] = [(n,) for n in names if n != base_name]
    subsets += [tuple(g) for g in spec.groups.values() if base_name not in g]
    memo: dict[tuple[frozenset[str], tuple[str, ...]], dict[str, Any]] = {}
    dead: set[frozenset[str]] = set()
    deepest: list[Any] = [frozenset(names), {}]

    def search(state: frozenset[str]) -> list[dict[str, Any]] | None:
        if state == frozenset((base_name,)):
            return []
        if state in dead:
            return None
        stuck_info: dict[str, Any] = {}
        # prefer moving big groups first, then parts by name, so the order is deterministic
        for subset in sorted(
            (s for s in subsets if set(s) <= state), key=lambda s: (-len(s), s)
        ):
            key = (state, subset)
            if key not in memo:
                memo[key] = _removable(scene, subset, state, step_v, travel)
            res = memo[key]
            if not res["ok"]:
                stuck_info[",".join(subset)] = res["blockers"]
                continue
            rest = search(state - set(subset))
            if rest is not None:
                move = {
                    "remove": list(subset),
                    "direction": list(res["direction"]),
                    "direction_label": format_direction(res["direction"]),
                    "clear_travel_mm": res["travel_mm"],
                    "passes": res["passes"],
                }
                return [move, *rest]
        dead.add(state)
        if len(state) <= len(deepest[0]):
            deepest[0], deepest[1] = state, stuck_info
        return None

    removal = search(frozenset(names))
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "order",
        "base": base_name,
        "parts": names,
        "step_mm": step_v,
        "max_travel_mm": round(travel, 2),
        "resolution_note": RESOLUTION_NOTE.format(step=step_v, unit="mm"),
        "feasible": removal is not None,
        "ok": removal is not None,
        "states_searched": len(dead) + (len(removal) if removal else 0),
    }
    if removal is not None:
        steps = [
            {
                "step": 1,
                "action": "place",
                "parts": [base_name],
                "note": "base part (fixed)",
            }
        ]
        for i, move in enumerate(reversed(removal), start=2):
            approach = list(move["direction"])  # assembly direction (removal reversed)
            steps.append(
                {
                    "step": i,
                    "action": "assemble",
                    "parts": move["remove"],
                    "move_along": approach,
                    "move_along_label": format_direction(move["direction"]),
                    "clear_travel_mm": move["clear_travel_mm"],
                    "passes": move["passes"],
                }
            )
        report["order"] = steps
        report["disassembly"] = [
            {
                "step": i,
                "parts": m["remove"],
                "move_along": [-c for c in m["direction"]],
                "move_along_label": format_direction(
                    (-m["direction"][0], -m["direction"][1], -m["direction"][2])
                ),
                "clear_travel_mm": m["clear_travel_mm"],
            }
            for i, m in enumerate(removal, start=1)
        ]
    else:
        state, info = deepest
        report["stuck"] = {
            "remaining": sorted(state),
            "blocked": [
                {"part": part, "blockers": blockers[:6]}
                for part, blockers in sorted(info.items())
            ],
        }
    return report


def _volume(body: Body) -> float:
    b = body.box
    return (b[3] - b[0]) * (b[4] - b[1]) * (b[5] - b[2])


def _scene_diag(scene: Scene) -> float:
    lo = np.min([[b.box[0], b.box[1], b.box[2]] for b in scene.bodies.values()], axis=0)
    hi = np.max([[b.box[3], b.box[4], b.box[5]] for b in scene.bodies.values()], axis=0)
    return float(np.linalg.norm(hi - lo)) + 5.0
