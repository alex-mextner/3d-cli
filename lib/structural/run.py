"""Orchestration of `3d strength`: request -> model -> closed-form screen or FEA -> `3d.strength/2` result dict.

No printing and no argv here: lib/strength_run.py is the thin tool that prints and sets the exit code,
tests call `analyze` directly.
"""

from __future__ import annotations

import math
import pathlib
import time
from typing import TYPE_CHECKING, Any

import numpy as np
from errors import InvalidArgument, MissingDependency, UsageError
from strength import (
    LABELS,
    SCHEMA,
    UNVERIFIED,
    StrengthRequest,
    verdict_for,
)
from structural import beams, fdm, hooks, model, orientation, section

if TYPE_CHECKING:
    from structural.fea import ContactPair, LoadCase

DEFAULT_TETS_P2 = 8_000
DEFAULT_TETS_P1 = 40_000


# ---- material / orientation -------------------------------------------------------------------
def material_card(name: str, start: pathlib.Path | None) -> tuple[fdm.FdmCard, Any]:
    from materials import load_materials

    materials = load_materials(start=start)
    found = materials.get(name)
    if found is None:
        raise InvalidArgument(
            "--material",
            name,
            sorted(materials),
            command="strength",
            extra="Run `3d materials list` to see available material names.",
        )
    return fdm.FdmCard.from_material(found), found


def material_record(card: fdm.FdmCard) -> dict[str, Any]:
    return {
        "name": card.name,
        "e_in_plane_mpa": card.e_p,
        "e_across_layers_mpa": card.e_z,
        "poisson": card.nu_p,
        "allowables": card.allowables(),
        "strain_limit_in_plane_pct": 100.0 * card.strain_limit_p,
        "strain_limit_across_layers_pct": 100.0 * card.strain_limit_z,
        "layer_adhesion": card.layer_adhesion,
        "defaulted_fields": list(card.defaulted),
    }


def _material_assumptions(cards: dict[str, fdm.FdmCard]) -> list[str]:
    out = [
        "FDM material is transversely isotropic about the print (build) axis; the layer plane is isotropic",
        "allowable stress = yield_mpa in-plane, yield_mpa x layer_adhesion across layers (materials.yaml); "
        "failure index separates in-plane von Mises from across-layer tension + shear (Hashin-type)",
    ]
    out.extend(
        f"material {card.name}: {', '.join(card.defaulted)} not set on the card, generic FDM default used"
        for card in cards.values()
        if card.defaulted
    )
    return out


# ---- model construction -----------------------------------------------------------------------
def _project_for(req: StrengthRequest) -> tuple[Any | None, pathlib.Path | None]:
    from project import load_project

    source = req.loads_path
    if req.path is not None and _is_project_path(req.path):
        source = req.path
    if source is None:
        return None, None
    return load_project(source, command="strength"), source


def _is_project_path(path: pathlib.Path) -> bool:
    return path.is_dir() or path.suffix.lower() in (".yaml", ".yml")


def build_model(req: StrengthRequest) -> tuple[model.Model, list[str]]:
    """Load bodies, mates and load cases. Returns (model, warnings)."""
    import trimesh

    warnings: list[str] = []
    assumptions: list[str] = []
    project, project_path = _project_for(req)
    bodies: list[model.Body] = []
    input_3mf: pathlib.Path | None = None
    default_material = req.material or (
        project.material if project is not None else None
    )

    if project is not None and req.path is not None and _is_project_path(req.path):
        if project.units != "mm":
            raise InvalidArgument(
                "project.units",
                str(project.units),
                ["mm"],
                command="strength",
                extra="`3d strength` works in millimetres only; convert the project first.",
            )
        if not project.parts:
            raise UsageError(f"{project.path} has no `parts:`", command="strength")
        for name, part in project.parts.items():
            suffix = part.path.suffix.lower()
            if suffix == ".scad":
                stl = model.render_scad(part.path, req.defines, module=part.module)
                surface = trimesh.load(stl, force="mesh")
            else:
                surface = trimesh.load(part.path, force="mesh")
            infill = part.infill if part.infill is not None else None
            bodies.append(
                model.Body(
                    name=name,
                    surface=model.clean_surface(
                        model.as_mesh(surface), f"{project.path.name}:{name}", warnings
                    ),
                    material=part.material or project.material or req.material,
                    orientation=part.orientation,
                    infill=model.parse_infill(infill, f"parts.{name}.infill")
                    if infill is not None
                    else None,
                    source=str(part.path),
                )
            )
        assumptions.append(
            "parts are analysed at the pose their file / module defines (the assembled pose)"
        )
    else:
        assert req.path is not None
        if req.path.suffix.lower() == ".3mf":
            input_3mf = req.path
        for name, surface in model.load_surfaces(req.path, req.defines, warnings):
            bodies.append(
                model.Body(
                    name=name,
                    surface=surface,
                    material=req.material,
                    source=str(req.path),
                )
            )

    for body in bodies:
        if body.material is None:
            body.material = default_material
        if body.material is None:
            raise UsageError(
                f"no material for body {body.name!r}",
                command="strength",
                remediation=[
                    "Pass --material NAME, or set `material:` on the part / project in 3d.yaml."
                ],
            )
        if req.infill is not None:
            body.infill = model.parse_infill(req.infill, "--infill")
    if req.crop is not None:
        for body in bodies:
            model.apply_crop(body, req.crop)
        assumptions.append(
            f"sub-model: every body is cut to the box {list(req.crop)} mm"
        )

    names = [b.name for b in bodies]
    if len(set(names)) != len(names):
        raise UsageError("body names are not unique", command="strength")

    pairs: list[ContactPair] = []
    if project is not None and req.path is not None and _is_project_path(req.path):
        # validated even for a single body, so a typo in `mates:` is never silently ignored
        declared, notes = model.pairs_from_project(
            project, names, "tied" if req.contact == "none" else req.contact
        )
        pairs.extend(declared)
        assumptions.extend(notes)
    elif project is not None and project.raw.get("mates"):
        assumptions.append("`mates:` of the --loads project are ignored: the input is a single file, not that project")
    if len(bodies) > 1:
        if req.contact != "none":
            extra = model.auto_pairs(bodies, pairs, req.contact, 0.1)
            pairs.extend(extra)
            if extra:
                assumptions.append(
                    f"bodies that touch without a declared mate ({', '.join(p.a + '<->' + p.b for p in extra)}) "
                    f"get a {req.contact} contact (--contact)"
                )
        elif not pairs:
            warnings.append(
                "--contact none and no declared mates: the bodies are analysed as separate, uncoupled parts"
            )

    fixture_notes: list[str] = []
    cases: list[LoadCase]
    from_fixture = False
    if project is not None and project.loads:
        cases = model.cases_from_project(project)
        if req.load_n is not None:
            warnings.append(
                "--load is ignored: the load cases come from `loads:` in 3d.yaml"
            )
    else:
        if req.load_n is None:
            raise UsageError(
                "no load case: pass --load N (with --fixture) or declare `loads:` in 3d.yaml",
                command="strength",
                remediation=[
                    "Example: 3d strength bracket.scad --material PLA --load 25 --fixture cantilever"
                ],
            )
        from_fixture = True
        cases = model.fixture_case(
            bodies,
            fixture=req.fixture,
            load_n=req.load_n,
            axis=model.parse_axis(req.axis),
            span_axis=req.span_axis,
            notes=fixture_notes,
        )
    assumptions.extend(fixture_notes)
    return (
        model.Model(
            bodies=bodies,
            pairs=pairs,
            cases=cases,
            assumptions=assumptions,
            project_path=project_path,
            input_3mf=input_3mf,
            default_material=default_material,
            from_fixture=from_fixture,
        ),
        warnings,
    )


def _orientations(
    req: StrengthRequest, mdl: model.Model
) -> dict[str, orientation.Orientation]:
    out: dict[str, orientation.Orientation] = {}
    for body in mdl.bodies:
        out[body.name] = orientation.resolve(
            req.orientation,
            extents=body.surface.extents,
            part_orientation=body.orientation,
            input_3mf=mdl.input_3mf,
        )
    return out


def _input_record(
    req: StrengthRequest, mdl: model.Model, orients: dict[str, orientation.Orientation]
) -> dict[str, Any]:
    return {
        "file": str(req.path) if req.path else None,
        "loads_from": str(mdl.project_path) if mdl.project_path else None,
        "bodies": [
            {
                "name": b.name,
                "material": b.material,
                "orientation": orients[b.name].to_dict(),
                "volume_mm3": round(float(abs(b.surface.volume)), 3),
                "infill": b.infill,
                "source": b.source,
            }
            for b in mdl.bodies
        ],
    }


# ---- closed-form paths ------------------------------------------------------------------------
def analyze(req: StrengthRequest) -> dict[str, Any]:
    started = time.perf_counter()
    if req.hook is not None:
        return _hook_result(req, started)
    mdl, warnings = build_model(req)
    method = _choose_method(req, mdl)
    orients = _orientations(req, mdl)
    cards = {
        b.material: material_card(str(b.material), req.path or mdl.project_path)[0]
        for b in mdl.bodies
        if b.material
    }
    if method == "beam":
        result = _beam_result(req, mdl, orients, cards)
    elif req.dry_run:
        result = _plan_result(req, mdl, orients, cards, method)
    else:
        from structural import fea_run

        result = fea_run.fea_result(req, mdl, orients, cards)
    result["warnings"] = [*warnings, *result.get("warnings", [])]
    result["timing_s"] = {
        **result.get("timing_s", {}),
        "total": round(time.perf_counter() - started, 3),
    }
    return result


def _fea_available() -> bool:
    try:
        import pytetwild  # noqa: F401
        import skfem  # noqa: F401
    except ImportError:
        return False
    return True


def _choose_method(req: StrengthRequest, mdl: model.Model) -> str:
    assembly = len(mdl.bodies) > 1
    if req.method == "beam":
        if assembly:
            raise UsageError(
                "the beam method analyses one body; this input is an assembly",
                command="strength",
                remediation=["Use --method fea (tied / frictionless mates), or analyse one part at a time."],
            )
        if not mdl.from_fixture:
            raise UsageError(
                "the beam method supports only --fixture cantilever|simple|compression loads",
                command="strength",
                remediation=["Use --method fea for loads from 3d.yaml (anchors, pressure, displacement, gravity)."],
            )
        return "beam"
    if req.method == "fea":
        return "fea"
    if _fea_available():
        return "fea"
    if mdl.from_fixture and not assembly:
        return "beam"
    raise MissingDependency(
        "scikit-fem / pytetwild",
        install='uv sync --extra fea   # or: uv pip install scikit-fem "pytetwild>=0.2,<0.3"',
        degrades="FEA of assemblies and 3d.yaml load cases (the closed-form beam method still works for one part)",
        command="strength",
    )


def _plan_result(
    req: StrengthRequest,
    mdl: model.Model,
    orients: dict[str, orientation.Orientation],
    cards: dict[str, fdm.FdmCard],
    method: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "PLAN",
        "verdict": None,
        "method": method,
        "label": LABELS[method],
        "safety_factor": None,
        "safety_factor_required": req.safety_factor,
        "input": _input_record(req, mdl, orients),
        "materials": [material_record(c) for c in cards.values()],
        "contacts": [
            {"a": p.a, "b": p.b, "kind": p.kind, "origin": p.origin, "nodes": 0}
            for p in mdl.pairs
        ],
        "cases": [
            {
                "name": c.name,
                "verdict": "PLAN",
                "safety_factor": None,
                "loads": [{"name": ld.name, "kind": ld.kind} for ld in c.loads],
                "supports": [s.name for s in c.supports],
            }
            for c in mdl.cases
        ],
        "assumptions": [
            "dry run: inputs validated and the model built; nothing was meshed or solved",
            *mdl.assumptions,
        ],
        "unverified": list(UNVERIFIED),
        "artifacts": {},
    }


def _span_vectors(
    req: StrengthRequest, body: model.Body
) -> tuple[np.ndarray, np.ndarray]:
    load_dir = np.array(model.parse_axis(req.axis), dtype=float)
    span = model.resolve_span(body.surface, req.fixture, load_dir, req.span_axis)
    return span, load_dir


def _beam_result(
    req: StrengthRequest,
    mdl: model.Model,
    orients: dict[str, orientation.Orientation],
    cards: dict[str, fdm.FdmCard],
) -> dict[str, Any]:
    """ANALYTICAL_SCREEN: variable-section Euler-Bernoulli beam from mesh slices (one body, one fixture)."""
    assert req.load_n is not None
    body = mdl.bodies[0]
    card = cards[str(body.material)]
    build = np.array(orients[body.name].build_axis)
    span, load_dir = _span_vectors(req, body)
    modulus = card.modulus_along(span, build)
    allowable = card.strength_along(span, build)
    if req.fixture == "compression":  # the column axis is the load axis: bend about both lateral axes
        helper = np.array([1.0, 0.0, 0.0]) if abs(span[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        lateral = np.cross(span, helper)
        bend_axes = (lateral, np.cross(span, lateral))
    else:
        bend_axes = (load_dir, np.cross(span, load_dir))
    safety: dict[str, float] = {}
    detail: list[str] = []
    assumptions = [
        "closed-form Euler-Bernoulli beam on cross-sections sliced from the mesh (variable section, shear deformation ignored)",
        "the fixture is idealised: the root is perfectly clamped (cantilever) or the supports are rigid knife edges (simple)",
        "no stress concentrations: fillets, holes, notches and the load / support contact stress are not modelled",
        f"span axis {'XYZ'[int(np.argmax(np.abs(span)))]} ({'--span-axis' if req.span_axis else 'longest extent'}), "
        f"modulus along the span {modulus:.0f} MPa and allowable {allowable:.1f} MPa from the orientation",
        *mdl.assumptions,
        *_material_assumptions(cards),
    ]
    if body.infill is not None and body.infill < 1.0:
        assumptions.append(
            f"infill {body.infill:.0%}: the beam path scales modulus and strength by the infill fraction"
        )
        modulus *= body.infill
        allowable *= body.infill

    case_name = req.fixture
    if req.fixture == "compression":
        prof = section.profile(body.surface, span, bend_axes[0])
        area_min = float(np.min(prof.area[prof.area > 1e-9]))
        sigma = beams.axial_stress(req.load_n, area_min)
        safety["compressive stress"] = allowable / sigma
        i_min = float(np.min(prof.inertia[prof.area > 1e-9]))
        i_other = float(
            np.min(
                section.profile(body.surface, span, bend_axes[1]).inertia[
                    prof.area > 1e-9
                ]
            )
        )
        i_weak = min(i_min, i_other)
        p_cr = beams.euler_buckling_load(modulus, i_weak, prof.length, k_factor=1.0)
        safety["euler buckling"] = p_cr / req.load_n
        detail.append(
            f"axial stress {sigma:.3g} MPa on the smallest section {area_min:.3g} mm^2"
        )
        detail.append(
            f"Euler critical load {p_cr:.3g} N (pinned-pinned, K=1; meaningful only for slender parts)"
        )
        max_vm, deflection = (
            sigma,
            req.load_n * prof.length / (modulus * float(np.mean(prof.area))),
        )
        station = None
        assumptions.append(
            "compression: uniform axial stress on the smallest section; Euler buckling reported as a separate check"
        )
    else:
        prof = section.profile(body.surface, span, bend_axes[0])
        if req.fixture == "cantilever":
            deflection, max_vm, station, tau = section.cantilever(
                prof, req.load_n, modulus
            )
        else:
            deflection, max_vm, station, tau = section.simple_beam(
                prof, req.load_n, modulus
            )
        safety["bending stress"] = allowable / max_vm
        safety["shear"] = card.shear_z / tau
        detail.append(
            f"max bending stress {max_vm:.3g} MPa at {station:.1f} mm from the low end; "
            f"{'tip' if req.fixture == 'cantilever' else 'mid-span'} deflection {deflection:.3g} mm"
        )
        detail.append(
            f"peak shear ~1.5 F/A = {tau:.3g} MPa vs {card.shear_z:.3g} MPa across-layer shear allowable (conservative)"
        )
    sf = min(safety.values())
    governing = min(safety, key=lambda k: safety[k])
    verdict = verdict_for(sf, req.safety_factor)
    case = {
        "name": case_name,
        "verdict": verdict,
        "safety_factor": sf,
        "governing": governing,
        "safety_factors": safety,
        "max_von_mises_mpa": max_vm,
        "max_displacement_mm": deflection,
        "allowable_mpa": allowable,
        "detail": detail,
        "hotspots": [],
    }
    return {
        "schema": SCHEMA,
        "status": "SOLVED",
        "verdict": verdict,
        "method": "beam",
        "label": LABELS["beam"],
        "safety_factor": sf,
        "safety_factor_required": req.safety_factor,
        "max_von_mises_mpa": max_vm,
        "max_displacement_mm": deflection,
        "governing": governing,
        "input": _input_record(req, mdl, orients),
        "materials": [material_record(c) for c in cards.values()],
        "cases": [case],
        "contacts": [],
        "mesh": None,
        "assumptions": assumptions,
        "unverified": list(UNVERIFIED),
        "artifacts": {},
        "timing_s": {},
    }


def _hook_result(req: StrengthRequest, started: float) -> dict[str, Any]:
    assert req.hook is not None
    if req.material is None:
        raise UsageError("--material is required", command="strength")
    card, _ = material_card(req.material, pathlib.Path.cwd())
    spec = req.orientation
    if spec in ("flat", "flat-bottom"):
        raise InvalidArgument(
            "--orientation",
            str(spec),
            ["z-up", "x-up", "y-up", "rotate=RX,RY,RZ", "FILE.3mf"],
            command="strength",
            extra="`flat` needs a mesh; give the build axis explicitly for --hook.",
        )
    orient = orientation.resolve(spec, extents=(1.0, 1.0, 1.0))
    build = np.array(orient.build_axis)
    arm = np.array(
        {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}[req.arm_axis]
    )
    cos = float(arm @ build)
    geometry = hooks.HookGeometry(
        thickness_mm=req.hook["t"],
        length_mm=req.hook["L"],
        width_mm=req.hook["w"],
        deflection_mm=req.hook["y"],
        tip_ratio=req.hook.get("taper", 1.0),
    )
    permissible = hooks.permissible_strain(
        100.0 * card.strain_limit_p,
        card.layer_adhesion,
        cos,
        repeated_use=req.repeated_use,
    )
    modulus = card.modulus_along(arm, build)
    check = hooks.check_hook(
        geometry,
        modulus_along_arm=modulus,
        permissible=permissible,
        lead_angle_deg=req.hook.get("lead", 30.0),
        retention_angle_deg=req.hook.get("retention", 45.0),
        friction=req.hook.get("mu", 0.4),
    )
    root_stress = modulus * check.root_strain
    stress_allow = card.strength_along(arm, build)
    verdict = verdict_for(check.safety_factor, req.safety_factor)
    detail = [
        f"root strain {100 * check.root_strain:.3g} % vs permissible {100 * check.permissible_strain:.3g} %"
        f" ({'repeated use, 0.6x' if req.repeated_use else 'single assembly'})",
        f"largest deflection within the limit: {check.max_deflection_mm:.3g} mm (imposed {geometry.deflection_mm:g} mm)",
        f"deflection force {check.deflection_force_n:.3g} N; insertion force "
        f"{'-' if check.insertion_force_n is None else f'{check.insertion_force_n:.3g}'} N; pull-off "
        f"{'self-locking' if check.pull_off_force_n is None else f'{check.pull_off_force_n:.3g} N'} per hook",
        f"root stress E x eps = {root_stress:.3g} MPa vs {stress_allow:.3g} MPa allowable (informational: a snap hook is strain-limited)",
    ]
    case = {
        "name": "snap hook",
        "verdict": verdict,
        "safety_factor": check.safety_factor,
        "governing": "permissible strain",
        "safety_factors": {
            "permissible strain": check.safety_factor,
            "root stress (informational)": stress_allow / root_stress,
        },
        "max_von_mises_mpa": root_stress,
        "max_displacement_mm": geometry.deflection_mm,
        "root_strain": check.root_strain,
        "permissible_strain": check.permissible_strain,
        "max_deflection_mm": check.max_deflection_mm,
        "deflection_force_n": check.deflection_force_n,
        "insertion_force_n": check.insertion_force_n,
        "pull_off_force_n": check.pull_off_force_n,
        "detail": detail,
        "hotspots": [],
    }
    return {
        "schema": SCHEMA,
        "status": "SOLVED",
        "verdict": verdict,
        "method": "beam",
        "label": LABELS["beam"],
        "safety_factor": check.safety_factor,
        "safety_factor_required": req.safety_factor,
        "max_von_mises_mpa": root_stress,
        "max_displacement_mm": geometry.deflection_mm,
        "governing": "permissible strain",
        "input": {
            "file": None,
            "loads_from": None,
            "bodies": [
                {
                    "name": "hook arm",
                    "material": card.name,
                    "orientation": {**orient.to_dict(), "arm_axis": req.arm_axis},
                }
            ],
            "hook": {**req.hook, "arm_axis": req.arm_axis},
        },
        "materials": [material_record(card)],
        "cases": [case],
        "contacts": [],
        "mesh": None,
        "assumptions": [
            "rectangular cantilever arm, rigid base, tip deflected by the barb: eps = 1.5 t y / L^2 (tapered: Bayer Table 1 derivation)",
            f"the bending fibre runs along the {req.arm_axis} axis, {math.degrees(math.acos(min(1.0, abs(cos)))):.0f} deg from the build axis: "
            "the permissible strain is interpolated between the in-plane limit and the across-layer limit (x layer_adhesion)",
            f"permissible strain {'x0.6 for repeated assembly (Bayer Table 2)' if req.repeated_use else 'is the single-assembly value; use --repeated-use for frequent opening'}",
            "friction coefficient and barb face angles are inputs (--hook mu=, lead=, retention=), not measured",
            *_material_assumptions({card.name: card}),
        ],
        "unverified": list(UNVERIFIED),
        "artifacts": {},
        "warnings": [],
        "timing_s": {"total": round(time.perf_counter() - started, 4)},
    }


def tidy(value: Any) -> Any:
    """Make a result JSON-safe: numpy scalars -> python, inf -> None."""
    if isinstance(value, dict):
        return {k: tidy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tidy(v) for v in value]
    if isinstance(value, np.generic):
        return tidy(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.ndarray):
        return tidy(value.tolist())
    if isinstance(value, pathlib.Path):
        return str(value)
    return value

