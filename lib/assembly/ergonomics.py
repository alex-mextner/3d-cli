"""ergonomics.py — can a hand or a tool assemble and release this? (`3d assembly ergonomics`)

WHAT: three families of checks, each `pass | warn | fail` with the reason:
  ACCESS      a finger / nail / tool probe (a capsule: an axis from the target out to `reach`, radius r) must get to
              each snap hook's release point, each fastener's drive point and each declared press point. The widest
              probe that fits is reported: finger pad (default diameter 12 mm) passes, only the nail (6 mm) or a
              tool warns, nothing fits fails. A prying slot is the same test with a blade-thin probe along the seam.
              The carrier part may touch the probe right at the target (that is where the finger presses); beyond
              that, and for every other part, the probe axis must stay at least `r` from every surface.
  DEFLECTION  a snap hook must be able to flex clear: the arm geometry (carrier mesh inside the hook `region`) is moved
              along the hook's `deflect` direction until it hits a neighbour; the free deflection must cover the
              deflection the path sweeps need (and the declared flex limit must not be below it).
  PATH        the declared removal path of each mate is swept (`assembly.sweep`): disassembly must not be blocked.
  FORCES      push-in / pull-off force per hook from the S4 closed forms when available (`assembly.forces`).

APPROXIMATIONS (stated in every report): the arm flexes by a rigid translation of its geometry (conservative for a
  cantilever that actually rotates about its root); finger and tool are straight capsules, no wrist or wrist angle.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh

from assembly.forces import hook_model, material_modulus
from assembly.scene import Scene, box_solid, translation_matrix
from assembly.spec import SCHEMA, Access, Hook, MateSpec
from assembly.sweep import sweep_mate
from assembly.vec import Vec3, format_direction

APPROXIMATIONS = [
    "hook flex is tested as a rigid translation of the arm geometry along `deflect` (conservative for a cantilever)",
    "finger and tool are straight capsules from the outside to the target; no hand or wrist geometry",
]
_AXES: tuple[Vec3, ...] = (
    (0.0, 0.0, 1.0),
    (0.0, 0.0, -1.0),
    (1.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0),
)


@dataclass(frozen=True)
class Probe:
    name: str
    radius_mm: float


def probes_for(scene: Scene, access: Access) -> list[Probe]:
    """Probes to try for an access entry, widest first; an explicit radius or a tool/nail entry narrows it."""
    cfg = scene.spec.settings
    sizes = {
        "finger": cfg.finger_pad_mm / 2.0,
        "nail": cfg.finger_nail_mm / 2.0,
        "tool": cfg.tool_mm / 2.0,
    }
    if access.radius_mm is not None:
        return [Probe(access.probe, access.radius_mm)]
    order = ["finger", "nail", "tool"]
    return [Probe(n, sizes[n]) for n in order[order.index(access.probe) :]]


def _clearances(mesh: trimesh.Trimesh, pts: Any) -> Any:
    """Signed clearance of points to a mesh surface: distance outside, negative distance inside."""
    _, dist, _ = trimesh.proximity.closest_point(mesh, pts)
    dist = np.asarray(dist, dtype=float)
    if mesh.is_watertight:
        inside = np.asarray(mesh.contains(pts), dtype=bool)
        dist = np.where(inside, -dist, dist)
    return dist


def probe_reach(
    scene: Scene,
    target: Vec3,
    from_dir: Vec3,
    probe: Probe,
    reach_mm: float,
    target_parts: frozenset[str] | str | None,
) -> dict[str, Any]:
    """Slide a capsule axis from the target outward along `from_dir`; report whether it stays clear."""
    r = probe.radius_mm
    spacing = max(0.25, min(r / 2.0, 1.0))
    ts = np.arange(0.0, reach_mm + 1e-9, spacing)
    d = np.asarray(from_dir, dtype=float)
    pts = np.asarray(target, dtype=float) + ts[:, None] * d
    relax = (
        r + 0.5
    )  # near the target the carrier may touch the probe: that is where the finger presses
    if target_parts is None:
        touching: frozenset[str] = frozenset()
    elif isinstance(target_parts, str):
        touching = frozenset((target_parts,))
    else:
        touching = target_parts
    worst: dict[str, Any] = {"clearance_mm": float("inf"), "part": None, "at_mm": None}
    for name, body in scene.bodies.items():
        clear = _clearances(body.mesh, pts)
        mask = ts >= relax if name in touching else np.ones(len(ts), dtype=bool)
        if not mask.any():
            continue
        margin = clear[mask] - r
        i = int(np.argmin(margin))
        if margin[i] + r < worst["clearance_mm"]:
            worst = {
                "clearance_mm": float(margin[i] + r),
                "part": name,
                "at_mm": float(ts[mask][i]),
            }
    ok = worst["clearance_mm"] >= r - 1e-6
    return {
        "clear": ok,
        "probe": probe.name,
        "radius_mm": r,
        "direction": list(from_dir),
        "direction_label": format_direction(from_dir),
        "min_clearance_mm": round(worst["clearance_mm"], 3)
        if math.isfinite(worst["clearance_mm"])
        else None,
        "blocked_by": None if ok else worst["part"],
        "blocked_at_mm": None if ok else round(worst["at_mm"], 2),
    }


def _candidate_accesses(
    hook: Hook | None, declared: Sequence[Access], purpose: str
) -> list[Access]:
    if declared:
        return list(declared)
    out: list[Access] = []
    if hook is not None and hook.deflect is not None:
        d = hook.deflect
        out.append(
            Access(
                "auto-push",
                hook.point,
                (-d[0], -d[1], -d[2]),
                "finger",
                None,
                None,
                purpose,
            )
        )
    if hook is not None:
        out.extend(
            Access(
                f"auto{format_direction(a)}",
                hook.point,
                a,
                "finger",
                None,
                None,
                purpose,
            )
            for a in _AXES
        )
    return out


def _check_access(
    scene: Scene,
    mate: MateSpec,
    hook: Hook | None,
    target_default: Vec3,
    carrier: str | None,
    declared: Sequence[Access],
    label: str,
    purpose: str,
) -> dict[str, Any]:
    cfg = scene.spec.settings
    accesses = _candidate_accesses(hook, declared, purpose)
    tried: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    for acc in accesses:
        target = acc.target or target_default
        dirs = [acc.from_dir] if acc.from_dir is not None else list(_AXES)
        reach = acc.reach_mm or cfg.reach_mm
        for from_dir in dirs:
            for probe in probes_for(scene, acc):
                res = probe_reach(
                    scene, target, from_dir, probe, reach, _part_at(scene, target, carrier)
                )
                res["access"] = acc.name
                res["declared_probe"] = acc.probe
                tried.append(res)
                if res["clear"]:
                    rank = (
                        ("finger", "nail", "tool").index(probe.name)
                        if probe.name in ("finger", "nail", "tool")
                        else 3
                    )
                    if best is None or rank < best["_rank"]:
                        best = {**res, "_rank": rank}
                    break  # a narrower probe on this direction adds nothing once the widest fits
    status, reasons = "fail", []
    if best is None:
        reasons.append(
            f"no {', '.join(sorted({t['probe'] for t in tried}))} probe reaches {label} "
            f"from {len(sorted({t['direction_label'] for t in tried}))} direction(s): "
            + _blockers(tried)
        )
        if not declared and not accesses:
            reasons = [
                f"{label}: no access direction can be derived; declare `access:` entries"
            ]
    else:
        probe = best["probe"]
        if probe == best["declared_probe"]:
            status = "pass"
            noun = {"finger": "finger pad", "nail": "fingernail", "tool": "tool"}[probe]
            reasons.append(
                f"{noun} (d {2 * best['radius_mm']:g} mm) reaches {label} from {best['direction_label']}"
            )
        elif probe == "nail":
            status = "warn"
            reasons.append(
                f"only a fingernail (d {2 * best['radius_mm']:g} mm) fits at {label} (from {best['direction_label']}); the finger pad is blocked"
            )
        else:
            status = "warn"
            reasons.append(
                f"only a tool (d {2 * best['radius_mm']:g} mm) fits at {label} (from {best['direction_label']}); no finger access"
            )
    out: dict[str, Any] = {"status": status, "reasons": reasons, "tried": len(tried)}
    if best is not None:
        best.pop("_rank", None)
        out["best"] = best
    else:
        out["blockers"] = sorted({(t["blocked_by"] or "?") for t in tried})
    return out


def _check_grip(
    scene: Scene,
    mate: MateSpec,
    hook: Hook,
    carrier: str,
    declared: Sequence[Access],
    label: str,
) -> dict[str, Any]:
    """A hook released by pulling the parts apart: every declared grip / prying point must be reachable."""
    grips = [a for a in declared if a.purpose in ("pull", "press", "pry")]
    if not grips:
        return {
            "status": "warn",
            "reasons": [
                f"{label} is released by pulling the parts apart, but no `access:` entry (purpose pull / press / pry) "
                "says where a hand or tool takes hold"
            ],
            "tried": 0,
        }
    worst, reasons, parts = "pass", [], []
    for acc in grips:
        res = _check_access(
            scene, mate, hook, acc.target or hook.point, carrier, [acc],
            f"{acc.purpose} point '{acc.name}'", acc.purpose,
        )  # fmt: skip
        if acc.purpose == "pry" and res["status"] != "fail":
            res = _seam_gap(scene, mate, acc, res)
        worst = _worse_status(worst, res["status"])
        reasons.extend(res["reasons"])
        parts.append({"access": acc.name, "purpose": acc.purpose, "status": res["status"], "best": res.get("best")})
    return {"status": worst, "reasons": reasons, "tried": len(grips), "points": parts}


def _seam_gap(scene: Scene, mate: MateSpec, acc: Access, res: dict[str, Any]) -> dict[str, Any]:
    """A prying point also needs a gap between the two parts that the blade can enter."""
    from assembly.overlap import sampled_gap

    target = acc.target
    if target is None:
        return res
    blade = scene.spec.settings.pry_blade_mm
    r = 2.5
    window = (target[0] - r, target[1] - r, target[2] - r, target[0] + r, target[1] + r, target[2] + r)
    gap, _ = sampled_gap(scene.body(mate.a).mesh, scene.body(mate.b).mesh, window, 5.0)
    out = {**res, "seam_gap_mm": round(gap, 3)}
    if gap + 1e-6 < blade:
        out["status"] = _worse_status(res["status"], "warn")
        out["reasons"] = [
            *res["reasons"],
            f"seam gap at '{acc.name}' is {gap:.2f} mm: no slot admits a {blade:g} mm prying blade "
            "(add a notch or a lip to start the lever)",
        ]
    return out


def _worse_status(a: str, b: str) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return a if order[a] >= order[b] else b


def _part_at(
    scene: Scene, point: Vec3, default: str | None, tol: float = 0.6
) -> frozenset[str]:
    """The parts whose surface the target point lies on (within `tol`), else `default`: the finger may touch them."""
    near: set[str] = set()
    p = np.asarray([point], dtype=float)
    for name, body in scene.bodies.items():
        dist = float(trimesh.proximity.closest_point(body.mesh, p)[1][0])
        if dist <= tol:
            near.add(name)
    if not near and default is not None:
        near.add(default)
    return frozenset(near)


def _blockers(tried: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for t in tried:
        counts[str(t["blocked_by"])] = counts.get(str(t["blocked_by"]), 0) + 1
    return "blocked by " + ", ".join(
        f"{k} ({v}x)" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def _free_deflection(
    scene: Scene, hook: Hook, carrier: str, deflect: Vec3, cap_mm: float, step_mm: float
) -> dict[str, Any]:
    """How far the hook arm can move along `deflect` before it hits a neighbour (not its own carrier)."""
    assert hook.region is not None
    body = scene.body(carrier)
    lo, hi = hook.region.bbox(0.0)
    arm = body.solid() ^ box_solid(lo, hi)
    cfg = scene.spec.settings
    if arm.is_empty():
        return {
            "free_mm": None,
            "note": f"hook region holds no geometry of {carrier}",
            "blocker": None,
        }
    from assembly.overlap import pair_regions

    neighbours = [n for n in scene.bodies if n != carrier]
    # the rest of the carrier (walls behind the arm, a stop) is an obstacle too; the arm's root only touches it
    carrier_rest = scene.body(carrier).solid() - box_solid(lo, hi)
    free, blocker = 0.0, None
    d = 0.0
    while d <= cap_mm + 1e-9:
        moved = arm.transform(translation_matrix([c * d for c in deflect])[:3].tolist())
        hit = None
        for name in [*neighbours, carrier]:
            other_solid = carrier_rest if name == carrier else scene.body(name).solid()
            for region in pair_regions(
                carrier,
                moved,
                name,
                other_solid,
                eps_mm3=cfg.eps_mm3,
                pad_mm=cfg.pair_pad_mm,
                keep_mesh=False,
            ):
                if region.depth_mm > cfg.seat_tol_mm:
                    hit = (name, region)
                    break
            if hit:
                break
        if hit:
            blocker = {
                "part": hit[0],
                "depth_mm": round(hit[1].depth_mm, 3),
                "at_mm": round(d, 3),
            }
            break
        free = d
        d += step_mm
    return {"free_mm": round(free, 3), "blocker": blocker, "capped": blocker is None}


def run_ergonomics(
    scene: Scene, *, mate_filter: Sequence[str] = (), material: str | None = None
) -> dict[str, Any]:
    spec = scene.spec
    mates = [m for m in spec.mates if not mate_filter or m.name in set(mate_filter)]
    checks: list[dict[str, Any]] = []
    modulus = material_modulus(material or spec.context.material)
    for mate in mates:
        part_names = (mate.a, mate.b)
        # --- path (disassembly) --------------------------------------------------------------------------
        sweeps: dict[str, dict[str, Any]] = {}
        if mate.insertion is not None or mate.hinge is not None:
            for direction in ("remove", "insert"):
                sweeps[direction] = sweep_mate(
                    scene, mate, direction=direction, material=material
                )
            rem = sweeps["remove"]
            status = "pass" if rem["ok"] else "fail"
            reason = (
                "removal path is clear"
                if rem["ok"]
                else f"removal is blocked at {rem['first_blocking']['travelled']:g} "
                f"{rem['motion']['unit']}: {rem['first_blocking']['reason']}"
            )
            if rem["ok"] and rem.get("snap"):
                snap_status = rem["snap"]["verdict"]["status"]
                if snap_status != "pass":
                    status, reason = (
                        snap_status,
                        "; ".join(rem["snap"]["verdict"]["reasons"]),
                    )
            checks.append(
                {
                    "id": f"{mate.name}.removal_path",
                    "mate": mate.name,
                    "check": "removal_path",
                    "status": status,
                    "reasons": [reason],
                }
            )
        # --- snap hooks ----------------------------------------------------------------------------------
        if mate.snap is not None:
            snap = mate.snap
            pull = [h for h in snap.hooks if h.release == "pull"]
            if pull:
                # released by pulling the parts apart: one grip / prying check for the whole mate
                grips = [*mate.access, *(a for h in pull for a in h.access)]
                res = _check_grip(
                    scene, mate, pull[0], snap.part, grips,
                    f"snap {mate.name} (hooks {', '.join(h.name for h in pull)})",
                )  # fmt: skip
                checks.append(
                    {
                        "id": f"{mate.name}.grip",
                        "mate": mate.name,
                        "hooks": [h.name for h in pull],
                        "check": "grip_access",
                        **res,
                    }
                )
            for hook in snap.hooks:
                label = f"hook {mate.name}.{hook.name}"
                declared = [*hook.access, *mate.access]
                if hook.release != "pull":
                    res = _check_access(
                        scene, mate, hook, hook.point, snap.part, declared, label, "release"
                    )
                    checks.append(
                        {
                            "id": f"{mate.name}.{hook.name}.access",
                            "mate": mate.name,
                            "hook": hook.name,
                            "check": "finger_access",
                            **res,
                        }
                    )
                need = _needed_deflection(sweeps, mate, hook)
                if hook.region is not None and hook.deflect is not None:
                    cap = max(need * 1.5 if need else 0.0, 0.6)
                    fd = _free_deflection(
                        scene, hook, snap.part, hook.deflect, cap, 0.02
                    )
                    checks.append(_deflection_check(mate, hook, fd, need))
                else:
                    checks.append(
                        {
                            "id": f"{mate.name}.{hook.name}.deflection",
                            "mate": mate.name,
                            "hook": hook.name,
                            "check": "deflection_clearance",
                            "status": "warn",
                            "reasons": [
                                f"{label}: declare `region` and `deflect` to check that the arm can flex clear of its neighbours"
                            ],
                        }
                    )
                deflection = need if need else (hook.barb_mm or 0.0)
                forces = hook_model(snap, hook, modulus).estimate(deflection)
                checks.append(
                    {
                        "id": f"{mate.name}.{hook.name}.forces",
                        "mate": mate.name,
                        "hook": hook.name,
                        "check": "forces",
                        "status": "pass" if forces.get("available") else "warn",
                        "reasons": [_force_line(forces)],
                        "forces": forces,
                    }
                )
        elif mate.access:
            res = _check_access(
                scene,
                mate,
                None,
                _centre(scene, part_names),
                None,
                mate.access,
                f"mate {mate.name}",
                "drive",
            )
            checks.append(
                {
                    "id": f"{mate.name}.access",
                    "mate": mate.name,
                    "check": "tool_access",
                    **res,
                }
            )
        elif mate.kind == "bolt":
            checks.append(
                {
                    "id": f"{mate.name}.access",
                    "mate": mate.name,
                    "check": "tool_access",
                    "status": "warn",
                    "reasons": [
                        f"bolt mate {mate.name} declares no `access:` entry: tool access to the head is not checked"
                    ],
                }
            )
    summary = {
        s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail")
    }
    return {
        "schema": SCHEMA,
        "kind": "ergonomics",
        "ok": summary["fail"] == 0,
        "probes": {
            "finger_pad_mm": spec.settings.finger_pad_mm,
            "finger_nail_mm": spec.settings.finger_nail_mm,
            "tool_mm": spec.settings.tool_mm,
            "reach_mm": spec.settings.reach_mm,
        },
        "approximations": APPROXIMATIONS,
        "checks": checks,
        "summary": summary,
    }


def _centre(scene: Scene, names: Sequence[str]) -> Vec3:
    pts = np.array([scene.body(n).mesh.bounds.mean(axis=0) for n in names])
    c = pts.mean(axis=0)
    return (float(c[0]), float(c[1]), float(c[2]))


def _needed_deflection(
    sweeps: dict[str, dict[str, Any]], mate: MateSpec, hook: Hook
) -> float:
    """The largest tip deflection the hook makes on the insert/remove sweeps (0 when it never engages)."""
    need = 0.0
    for report in sweeps.values():
        snap = report.get("snap")
        if not snap:
            continue
        for h in snap["hooks"]:
            if h["hook"] == hook.name:
                need = max(need, h.get("implied_deflection_mm") or 0.0)
    return need


def _deflection_check(
    mate: MateSpec, hook: Hook, fd: dict[str, Any], need: float
) -> dict[str, Any]:
    base = {
        "id": f"{mate.name}.{hook.name}.deflection",
        "mate": mate.name,
        "hook": hook.name,
        "check": "deflection_clearance",
        "free_deflection_mm": fd["free_mm"],
        "needed_deflection_mm": round(need, 4),
        "blocker": fd["blocker"],
    }
    if fd["free_mm"] is None:
        return {**base, "status": "warn", "reasons": [fd["note"]]}
    if need <= 0:
        need_text = "no deflection is needed on the swept paths"
        status = "pass"
    elif fd["free_mm"] + 1e-6 >= need:
        need_text = (
            f"the arm can flex {fd['free_mm']:.2f} mm, the paths need {need:.3f} mm"
        )
        status = "pass"
    else:
        blk = fd["blocker"]
        need_text = (
            f"the arm can flex only {fd['free_mm']:.2f} mm before it hits {blk['part'] if blk else 'a neighbour'}, "
            f"the paths need {need:.3f} mm"
        )
        status = "fail"
    return {**base, "status": status, "reasons": [f"hook {hook.name}: {need_text}"]}


def _force_line(forces: dict[str, Any]) -> str:
    if not forces.get("available"):
        return f"force estimate unavailable: {forces.get('reason')}"
    return (
        f"flex force {forces['deflection_force_n']} N, push-in {forces['push_in_force_n']} N, "
        f"pull-off {forces['pull_off_force_n']} N at {forces['deflection_mm']} mm (strain {forces['strain']})"
    )


