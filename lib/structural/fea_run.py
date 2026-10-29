"""The FEA_LINEAR path of `3d strength`: mesh every body, assemble contacts, solve every load case, report.

Split from run.py only so the closed-form paths stay free of numpy-heavy imports until they need them.
"""

from __future__ import annotations

import pathlib
import time
from typing import TYPE_CHECKING, Any

import numpy as np
from strength import LABELS, SCHEMA, UNVERIFIED, StrengthRequest, verdict_for
from structural import fdm, fea, orientation
from structural import mesh as meshing
from structural.model import Model

if TYPE_CHECKING:
    from structural.fea import BodyField, BodyModel, CaseSolution, Problem

DEFAULT_TETS_P2 = 6_000
DEFAULT_TETS_P1 = 40_000
HOTSPOTS = 5


def _shell_scales(
    surface: Any, tet: meshing.TetMesh, infill: float, walls: int, nozzle_mm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Per-element (stiffness, strength) scale: 1 inside the printed shell, `infill` in the core."""
    import trimesh
    from scipy.spatial import cKDTree

    shell = walls * nozzle_mm
    centroids = tet.points[tet.tets].mean(axis=1)
    count = int(min(max(surface.area / (0.5 * shell) ** 2, 4000), 200_000))
    samples, _ = trimesh.sample.sample_surface(surface, count, seed=11)
    distance, _ = cKDTree(samples).query(centroids)
    scale = np.where(distance > shell, infill, 1.0)
    return scale, scale.copy()


def _case_record(
    sol: CaseSolution, problem: Problem, required: float, cards: dict[str, fdm.FdmCard]
) -> tuple[dict[str, Any], float]:
    """Summarise one solved case. Returns (record, governing safety factor)."""
    fi_p = max(float(f.failure["fi_inplane"].max()) for f in sol.fields)
    fi_z = max(float(f.failure["fi_layer"].max()) for f in sol.fields)
    sf_in_plane = 1.0 / fi_p if fi_p > 0 else float("inf")
    sf_layer = 1.0 / fi_z if fi_z > 0 else float("inf")
    sf_stress = min(sf_in_plane, sf_layer)
    sf_strain = min(float(f.strain_sf.min()) for f in sol.fields)
    vm_max = max(float(f.failure["von_mises"].max()) for f in sol.fields)
    max_strain = max(float(f.principal_strain.max()) for f in sol.fields)

    if sol.force_controlled:
        sf, governing = sf_stress, ("in_plane" if fi_p >= fi_z else "across_layers")
        control = "force"
    else:
        sf, governing = sf_strain, "permissible strain"
        control = "displacement"

    disp_best, disp_at = 0.0, [0.0, 0.0, 0.0]
    per_body: list[dict[str, Any]] = []
    for f in sol.fields:
        norms = np.linalg.norm(f.u, axis=1)
        k = int(np.argmax(norms))
        if norms[k] > disp_best:
            disp_best, disp_at = float(norms[k]), f.body.mesh.p[:, k].tolist()
        body_fi = max(
            float(f.failure["fi_inplane"].max()), float(f.failure["fi_layer"].max())
        )
        per_body.append(
            {
                "name": f.body.name,
                "max_von_mises_mpa": float(f.failure["von_mises"].max()),
                "safety_factor": (1.0 / body_fi) if body_fi > 0 else None,
                "max_displacement_mm": float(norms.max()),
            }
        )
    record: dict[str, Any] = {
        "name": sol.name,
        "control": control,
        "verdict": verdict_for(sf, required),
        "safety_factor": sf,
        "governing": governing,
        "safety_factors": {
            "in_plane": sf_in_plane,
            "across_layers": sf_layer,
            "permissible strain": sf_strain,
            **({"away_from_patches": away} if (away := _safety_away_from_patches(sol)) is not None else {}),
        },
        "max_von_mises_mpa": vm_max,
        "max_principal_strain": max_strain,
        "max_displacement_mm": disp_best,
        "max_displacement_at_mm": disp_at,
        "allowable_mpa": {name: card.allowables() for name, card in cards.items()},
        "reaction_n": sol.reaction_n.tolist(),
        "applied_n": sol.applied_n.tolist(),
        "per_body": per_body,
        "contact": {
            "iterations": sol.contact_iterations,
            "active_nodes": sol.active_contacts,
        },
        "hotspots": _hotspots(sol, force_controlled=sol.force_controlled),
        "solve_seconds": round(sol.solve_seconds, 3),
        "detail": list(sol.notes),
    }
    return record, sf


def _safety_away_from_patches(sol: CaseSolution) -> float | None:
    """Lowest safety factor at element vertices farther than two element lengths from every load / support patch.

    Stress at a prescribed displacement, a point load or a clamp edge is a modelling singularity; this is the number
    to read next to the (conservative) global one. None when every vertex is near a patch or there is no patch.
    """
    if not sol.constraint_points.size:
        return None
    from scipy.spatial import cKDTree

    tree = cKDTree(sol.constraint_points)
    best = float("inf")
    for f in sol.fields:
        sf = f.failure["sf"] if sol.force_controlled else f.strain_sf
        vertices = f.body.mesh.p[:, f.body.mesh.t].transpose(2, 1, 0).reshape(-1, 3)
        distance, _ = tree.query(vertices)
        far = distance.reshape(sf.shape) > 2.0 * f.body.tet.edge_length_mm
        if np.any(far):
            best = min(best, float(sf[far].min()))
    return best if np.isfinite(best) else None


def _hotspots(sol: CaseSolution, *, force_controlled: bool) -> list[dict[str, Any]]:
    """Distinct worst locations: lowest safety-factor element vertices, at least 3 element lengths apart."""
    candidates: list[tuple[float, int, int, int]] = []
    for bi, f in enumerate(sol.fields):
        sf = f.failure["sf"] if force_controlled else f.strain_sf
        flat = np.argsort(sf, axis=None)[:60]
        for idx in flat:
            e, v = divmod(int(idx), 4)
            candidates.append((float(sf[e, v]), bi, e, v))
    candidates.sort()
    picked: list[dict[str, Any]] = []
    positions: list[np.ndarray] = []
    for value, bi, e, v in candidates:
        fld: BodyField = sol.fields[bi]
        xyz = fld.body.mesh.p[:, fld.body.mesh.t[v, e]]
        spacing = 3.0 * fld.body.tet.edge_length_mm
        if any(np.linalg.norm(xyz - q) < spacing for q in positions):
            continue
        positions.append(xyz)
        fi_p = float(fld.failure["fi_inplane"][e, v])
        fi_z = float(fld.failure["fi_layer"][e, v])
        near = bool(
            sol.constraint_points.size
            and float(np.min(np.linalg.norm(sol.constraint_points - xyz, axis=1))) <= 2.0 * fld.body.tet.edge_length_mm
        )
        near_contact = bool(
            sol.contact_points.size
            and float(np.min(np.linalg.norm(sol.contact_points - xyz, axis=1))) <= 1.0 * fld.body.tet.edge_length_mm
        )
        picked.append(
            {
                "body": fld.body.name,
                "xyz": [float(c) for c in xyz],
                "von_mises_mpa": float(fld.failure["von_mises"][e, v]),
                "safety_factor": value,
                "mode": ("in_plane" if fi_p >= fi_z else "across_layers")
                if force_controlled
                else "permissible strain",
                "near_constraint": near,
                "near_contact": near_contact,
            }
        )
        if len(picked) >= HOTSPOTS:
            break
    return picked


def fea_result(
    req: StrengthRequest,
    mdl: Model,
    orients: dict[str, orientation.Orientation],
    cards: dict[str, fdm.FdmCard],
) -> dict[str, Any]:
    from structural import run

    clock = time.perf_counter()
    order = req.order
    budget = req.max_elements or (DEFAULT_TETS_P2 if order == 2 else DEFAULT_TETS_P1)
    per_wall = 1.5 if order == 2 else 3.0
    volumes = [abs(float(b.surface.volume)) for b in mdl.bodies]
    warnings: list[str] = []
    assumptions: list[str] = [
        "static, small-strain, linear-elastic analysis (FEA_LINEAR): no plasticity, creep, fatigue, buckling or large deformation",
        f"P{order} {'quadratic' if order == 2 else 'linear'} tetrahedra from fTetWild (pytetwild); stresses are evaluated at the "
        "element vertices without smoothing, so a stress peak can rise with mesh refinement at a load / support edge "
        "(a singularity); hotspots on a load or support patch are flagged `near_constraint`",
        "a force load is spread uniformly over the surface facets selected by its anchor (radius_mm / anchor area), not applied at a point",
        *mdl.assumptions,
        *run._material_assumptions(cards),
    ]
    bodies: list[BodyModel] = []
    mesh_records: list[dict[str, Any]] = []
    for body, volume in zip(mdl.bodies, volumes, strict=True):
        target = max(500, int(budget * volume / sum(volumes)))
        edge, thin, mesh_warn = meshing.choose_edge_length(
            body.surface,
            target_tets=target,
            explicit_mm=req.mesh_size_mm,
            elements_per_wall=per_wall,
        )
        warnings.extend(f"{body.name}: {w}" for w in mesh_warn)
        tet = meshing.tetrahedralize(body.surface, edge, min_thickness_mm=thin)
        stiff = strength = None
        if body.infill is not None and body.infill < 1.0:
            stiff, strength = _shell_scales(
                body.surface, tet, body.infill, req.walls, req.nozzle_mm
            )
            assumptions.append(
                f"{body.name}: infill {body.infill:.0%} behind a {req.walls}-perimeter x {req.nozzle_mm:g} mm shell; "
                "core modulus and strength scale linearly with the infill fraction (stretch-dominated pattern)"
            )
        card = cards[str(body.material)]
        body_model = fea.build_body(
            body.name,
            tet,
            card,
            np.array(orients[body.name].build_axis),
            order=order,
            stiff_scale=stiff,
            strength_scale=strength,
        )
        bodies.append(body_model)
        mesh_records.append(
            {
                "name": body.name,
                "tets": int(len(tet.tets)),
                "nodes": int(len(tet.points)),
                "dofs": body_model.ndof,
                "edge_length_mm": edge,
                "thinnest_wall_mm": thin,
            }
        )
    if all(b.infill in (None, 1.0) for b in mdl.bodies):
        assumptions.append(
            "solid (100 %) infill: the mesh is the part as modelled; pass --infill N for a sparse core"
        )
    t_mesh = time.perf_counter() - clock

    problem = fea.assemble_problem(bodies, list(mdl.pairs))
    warnings.extend(problem.warnings)
    if problem.contacts:
        assumptions.append(
            "contact: node-to-surface at element corner nodes with a penalty (tied = bonded, frictionless = unilateral "
            "normal contact with sliding); the contact surface of a body is the one it shares with its mate within tolerance"
        )

    solve_clock = time.perf_counter()
    records: list[dict[str, Any]] = []
    worst: tuple[float, CaseSolution] | None = None
    for case in mdl.cases:
        sol = fea.solve_case(problem, case, repeated_use=req.repeated_use)
        record, sf = _case_record(sol, problem, req.safety_factor, cards)
        records.append(record)
        if worst is None or sf < worst[0]:
            worst = (sf, sol)
    assert worst is not None
    sf_total, worst_sol = worst
    warnings.extend(w for w in problem.warnings if w not in warnings)

    worst_record = next(r for r in records if r["name"] == worst_sol.name)
    verdict = verdict_for(sf_total, req.safety_factor)
    artifacts: dict[str, Any] = {}
    if req.viz is not None:
        from structural import viz

        out = _viz_path(req)
        viz.render_stress_png(
            worst_sol.fields,
            out,
            title=f"{verdict}  SF {sf_total:.3g} (need {req.safety_factor:g})  case {worst_sol.name}",
            subtitle=f"{LABELS['fea']}  max von Mises {worst_record['max_von_mises_mpa']:.3g} MPa  "
            f"max displacement {worst_record['max_displacement_mm']:.3g} mm",
            hotspots=worst_record["hotspots"],
        )
        artifacts["stress_png"] = str(out)

    contacts = [
        {
            "a": d.pair.a,
            "b": d.pair.b,
            "kind": d.pair.kind,
            "origin": d.pair.origin,
            "fit_type": d.pair.fit_type,
            "slave": d.slave,
            "master": d.master,
            "nodes": d.count,
        }
        for d in problem.contacts
    ]
    return {
        "schema": SCHEMA,
        "status": "SOLVED",
        "verdict": verdict,
        "method": "fea",
        "label": LABELS["fea"],
        "safety_factor": sf_total,
        "safety_factor_required": req.safety_factor,
        "max_von_mises_mpa": max(r["max_von_mises_mpa"] for r in records),
        "max_displacement_mm": max(r["max_displacement_mm"] for r in records),
        "governing": worst_record["governing"],
        "input": run._input_record(req, mdl, orients),
        "materials": [run.material_record(c) for c in cards.values()],
        "cases": records,
        "contacts": contacts,
        "mesh": {
            "order": order,
            "total_tets": sum(m["tets"] for m in mesh_records),
            "total_dofs": problem.ndof,
            "bodies": mesh_records,
        },
        "assumptions": assumptions,
        "unverified": list(UNVERIFIED),
        "warnings": warnings,
        "artifacts": artifacts,
        "timing_s": {
            "mesh": round(t_mesh, 3),
            "solve": round(time.perf_counter() - solve_clock, 3),
        },
    }


def _viz_path(req: StrengthRequest) -> pathlib.Path:
    if req.viz is not None and str(req.viz) not in ("", "."):
        return req.viz
    base = req.path if req.path is not None else pathlib.Path("strength")
    if base.is_dir():
        return base / "strength.png"
    return base.with_suffix(".strength.png")
