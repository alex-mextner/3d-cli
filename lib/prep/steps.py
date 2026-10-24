"""steps.py — the adapters between `Pipeline` and the step libraries.

WHAT: `runners(pipe)` returns one zero-argument callable per step; each calls its library, saves the
  mesh/artifacts the later steps load, and returns a `StepOutput` (JSON-able result, verdict, one line).
WHY: keeps `pipeline.py` about order, caching and blocking, and each library about its algorithm; this
  module is the only place that knows both.
INVARIANTS: heavy libraries are imported inside the runners (a `3d prep --help` must stay instant); no step
  here implements geometry, physics or slicing logic of its own.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import trimesh

from errors import InvalidArgument
from prep import frame
from prep.contracts import Assign, Part, Verdict, worst
from prep.pipeline import (
    DEFAULT_PROCESS_PRESET,
    PRINTER_BED_MM,
    StepOutput,
    default_filaments,
    load_parts,
    result_dict,
    save_mesh,
    save_parts,
)

if TYPE_CHECKING:
    from prep.pipeline import Pipeline


@dataclass(frozen=True)
class SavedLayerPlan:
    """The layer plan as the project builder and the proof need it, rebuilt from the cached JSON."""

    profile_text: str
    first_height: float
    heights: tuple[float, ...]
    z_tops: tuple[float, ...]
    mix_zranges: tuple[tuple[float, float], ...]


def runners(pipe: Pipeline) -> dict[str, Callable[[], StepOutput]]:
    return {
        "diagnose": lambda: _diagnose(pipe),
        "repair": lambda: _repair(pipe),
        "orient": lambda: _orient(pipe),
        "adapt": lambda: _adapt(pipe),
        "overhang": lambda: _overhang(pipe),
        "fit": lambda: _fit(pipe),
        "stability": lambda: _stability(pipe),
        "colors": lambda: _colors(pipe),
        "layers": lambda: _layers(pipe),
        "project": lambda: _project(pipe),
        "slice": lambda: _slice(pipe),
        "gates": lambda: _gates(pipe),
        "proof": lambda: _proof(pipe),
    }


def _diagnose(pipe: Pipeline) -> StepOutput:
    from prep import meshfix

    d = meshfix.diagnose(pipe.model_mesh())
    return StepOutput(result_dict(d), d.verdict, d.summary())


def _repair(pipe: Pipeline) -> StepOutput:
    from prep import meshfix

    r = meshfix.repair(pipe.model_mesh())
    path = pipe.mesh_path("repair")
    save_mesh(path, r.mesh)
    return StepOutput(result_dict(r), r.verdict, r.summary(), [path])


def _orient(pipe: Pipeline) -> StepOutput:
    from prep import orient

    mesh = pipe.mesh_after("repair")
    rep = orient.search_orientations(mesh, angle_deg=pipe.cfg.angle_deg)
    data = result_dict(rep)
    better = rep.best.rank != rep.current.rank
    applied = pipe.cfg.orient == "auto" and better and rep.verdict in ("WARN", "FAIL")
    if applied:
        mesh = orient.apply(mesh, rep.best)
    data["applied"] = applied
    data["chosen"] = rep.best.label if applied else rep.current.label
    path = pipe.mesh_path("orient")
    save_mesh(path, mesh)
    note = (
        f"rotated to '{rep.best.label}'" if applied else "kept the current orientation"
    )
    # "even the best orientation has overhang islands" is the input of the adapt step, not a verdict on the model
    verdict: Verdict = "WARN" if rep.verdict == "FAIL" else rep.verdict
    return StepOutput(data, verdict, f"{note}; {rep.summary()}", [path])


def _zones(pipe: Pipeline) -> list[Any]:
    """`overhang.Zone` list from the config (a relaxed angle / no pillars in a height range)."""
    from prep import overhang

    return [overhang.Zone.from_dict(z) for z in pipe.cfg.adapt_zones]


def _analyse_overhang(pipe: Pipeline, mesh: trimesh.Trimesh) -> Any:
    from prep import overhang

    cfg = pipe.cfg
    zones = _zones(pipe)
    key = f"overhang:{frame.mesh_digest(mesh)}:{cfg.angle_deg}:{cfg.pitch}:{cfg.bridge_max_mm}:{cfg.adapt_zones}"
    return pipe.memo(
        key,
        lambda: overhang.analyse(
            mesh,
            angle_deg=cfg.angle_deg,
            pitch=cfg.pitch,
            bridge_max_mm=cfg.bridge_max_mm,
            zones=zones,
        ),
    )


def _adapt(pipe: Pipeline) -> StepOutput:
    from prep import adapt

    mesh = pipe.mesh_after("orient")
    path = pipe.mesh_path("adapt")
    if not pipe.cfg.adapt:
        save_mesh(path, mesh)
        return StepOutput(
            {"ops": []}, "PASS", "adaptation disabled (--no-adapt)", [path]
        )
    before = _analyse_overhang(pipe, mesh)
    data: dict[str, Any] = {"before": before.to_dict(), "ops": []}
    if before.verdict == "PASS":
        save_mesh(path, mesh)
        return StepOutput(
            data, "PASS", "no overhang steeper than the limit: nothing to adapt", [path]
        )
    results = adapt.auto(mesh, before, zones=_zones(pipe))
    for res in results:
        data["ops"].append(res.to_dict())
    if results and results[-1].verdict != "FAIL":
        mesh = results[-1].mesh
    save_mesh(path, mesh)
    if not results:
        return StepOutput(
            data, "WARN", "overhangs found but no operator clears them", [path]
        )
    verdict = worst([r.verdict for r in results])
    return StepOutput(data, verdict, "; ".join(r.summary() for r in results), [path])


def _overhang(pipe: Pipeline) -> StepOutput:
    rep = _analyse_overhang(pipe, pipe.final_mesh())
    return StepOutput(
        result_dict(rep),
        rep.verdict,
        f"{len(rep.islands)} island(s), {len(rep.regions)} region(s) over {rep.angle_deg:g} deg, {rep.total_area_mm2:.0f} mm2",
    )


_TUPLE_FIELDS = ("lean_range_deg", "seat_y0_mm", "scan_deg", "pocket", "item_axes")


def holder_spec(raw: Mapping[str, Any]) -> Any:
    """`fit.HolderSpec` from a plain mapping (YAML lists become tuples; unknown keys are refused with the accepted names)."""
    from prep import fit

    names = {f.name for f in dataclasses.fields(fit.HolderSpec)}
    unknown = sorted(set(raw) - names)
    if unknown:
        raise InvalidArgument(
            "holder", ", ".join(unknown), sorted(names), command="prep"
        )
    kwargs: dict[str, Any] = {
        k: (tuple(v) if k in _TUPLE_FIELDS and isinstance(v, list) else v)
        for k, v in raw.items()
    }
    return fit.HolderSpec(**kwargs)


def _fit(pipe: Pipeline) -> StepOutput:
    from prep import fit

    cfg = pipe.cfg
    if cfg.items is None:
        return StepOutput(
            {}, "SKIP", "no items table (--fit FILE.json): fit check not run"
        )
    table = fit.load_items(cfg.items)
    rep = fit.check_fit(
        pipe.final_mesh(), table, holder_spec(cfg.holder), pitch=max(cfg.pitch, 0.5)
    )
    return StepOutput(result_dict(rep), rep.verdict, rep.summary())


def _vec3(seq: Sequence[float]) -> tuple[float, float, float]:
    if len(seq) != 3:
        raise InvalidArgument(
            "load vector",
            str(list(seq)),
            ["three numbers [x, y, z] in mm"],
            command="prep",
        )
    return (float(seq[0]), float(seq[1]), float(seq[2]))


def _fit_loads(pipe: Pipeline) -> list[Any]:
    """Stability loads: the configured ones plus, in lean mode, the lightest and heaviest item at their rest pose."""
    from prep import stability

    loads = [
        stability.ExternalLoad(
            name=str(d["name"]),
            mass_g=float(d["mass_g"]),
            com_mm=_vec3(d["com_mm"]),
            tap_point_mm=_vec3(d["tap_point_mm"]) if d.get("tap_point_mm") else None,
            tap_normal=_vec3(d["tap_normal"]) if d.get("tap_normal") else None,
        )
        for d in pipe.cfg.loads
    ]
    fit_data = pipe.record_data("fit")
    if not fit_data or fit_data.get("mode") != "lean":
        return loads
    sign = -int((fit_data.get("spec") or {}).get("front_sign", -1))
    x_c = float((fit_data.get("spec") or {}).get("x_centre_mm", 0.0))
    posed = [
        r
        for r in fit_data["rows"]
        if not r["non_goal"]
        and r["verdict"] in ("PASS", "RISK")
        and r["phi_deg"] is not None
    ]
    for tag, pick in (("light", min), ("heavy", max)):
        if not posed:
            break
        r = pick(posed, key=lambda row: row["mass_g"])
        phi = math.radians(r["phi_deg"])
        h, t, y0, z0 = r["height_mm"], r["thick_mm"], r["y0_mm"], r["z0_mm"]
        com = (
            x_c,
            y0 + sign * ((h / 2) * math.sin(phi) - (t / 2) * math.cos(phi)),
            z0 + (h / 2) * math.cos(phi) + (t / 2) * math.sin(phi),
        )
        tap = (
            x_c,
            y0 + sign * ((h - 10) * math.sin(phi) - t * math.cos(phi)),
            z0 + (h - 10) * math.cos(phi) + t * math.sin(phi),
        )
        normal = (
            0.0,
            -sign * math.cos(phi),
            math.sin(phi),
        )
        loads.append(
            stability.ExternalLoad(
                f"{tag}: {r['item']} / {r['case']}",
                float(r["mass_g"]),
                com,
                tap,
                normal,
            )
        )
    return loads


def _stability(pipe: Pipeline) -> StepOutput:
    from prep import stability

    cfg = pipe.cfg
    # Static tipping decides PASS/FAIL. A tap/pull that would tip the stand (safety factor < 1) is a WARN by default:
    # a 250 g phone on a 90 g desk stand tips at 2-5 N, a finding to read rather than a broken model. Gate on it
    # with `stability: {sf_fail: 1.0}` in the spec.
    opts: dict[str, Any] = {
        "infill": cfg.infill_pct / 100.0,
        "perimeters": cfg.walls,
        "sf_fail": 0.0,
        "sf_warn": 1.0,
    }
    opts.update(cfg.stability)
    rep = stability.analyse(pipe.final_mesh(), loads=tuple(_fit_loads(pipe)), **opts)
    return StepOutput(result_dict(rep), rep.verdict, rep.summary())


def _colors(pipe: Pipeline) -> StepOutput:
    from prep import regions

    cfg = pipe.cfg
    if cfg.colors is None:
        return StepOutput(
            {"mono": True},
            "SKIP",
            "no colour spec (--colors FILE.yaml): single-colour print",
        )
    spec = regions.load_colour_spec(cfg.colors, mix_cap=cfg.mix_cap)
    plan = regions.plan_colours(pipe.final_mesh(), spec)
    path = pipe.mesh_path("parts")
    save_parts(path, plan.parts)
    return StepOutput(result_dict(plan), plan.verdict, plan.summary(), [path])


def _layers(pipe: Pipeline) -> StepOutput:
    from prep import layers, regions

    cfg = pipe.cfg
    if cfg.flat_profile:
        return StepOutput(
            {},
            "SKIP",
            f"{cfg.printer}: one filament at the process preset's fixed layer height, no adaptive plan",
        )
    parts = _parts(pipe)
    zr = layers.mix_zranges_from(parts) if parts else ()
    plan = layers.plan_layers(
        pipe.final_mesh(),
        hmin=cfg.hmin,
        hmax=cfg.hmax,
        mix_zranges=zr,
        mix_cap=cfg.mix_cap,
    )
    data = result_dict(plan)
    verdicts: list[Verdict] = [plan.verdict]
    line = plan.summary()
    if cfg.colors is not None and parts:
        spec = regions.load_colour_spec(cfg.colors, mix_cap=cfg.mix_cap)
        check = regions.check_mixes(
            spec, layers.heights_by_part(plan, parts), mix_cap=cfg.mix_cap
        )
        data["mix_check"] = check.to_dict()
        if not check.ok:
            verdicts.append("FAIL")
            line += f"; mix rule violated in {', '.join(sorted(check.violations_by_region()))}"
    return StepOutput(data, worst(verdicts), line)


def saved_layer_plan(pipe: Pipeline) -> SavedLayerPlan | None:
    d = pipe.record_data("layers")
    if not d or "profile_text" not in d:
        return None
    return SavedLayerPlan(
        d["profile_text"],
        float(d["first_height"]),
        tuple(d["heights"]),
        tuple(d["z_tops"]),
        tuple((float(a), float(b)) for a, b in d.get("mix_zranges", [])),
    )


def _parts(pipe: Pipeline) -> list[Part]:
    rec = pipe.records.get("colors")
    if rec is None or rec.verdict == "SKIP":
        return []
    return load_parts(pipe.mesh_path("parts"))


def _process_overrides(pipe: Pipeline, first_height: float) -> Any:
    """The process settings `3d prep` owns (walls, infill, layer height) plus `--set`, as `ProcessOverrides`."""
    from prep.project3mf import ProcessOverrides

    cfg = pipe.cfg
    return ProcessOverrides(
        wall_loops=cfg.walls,
        sparse_infill_density=f"{cfg.infill_pct}%",
        layer_height=0.2,
        initial_layer_print_height=first_height,
        adaptive_layer_height=False,
        extra=dict(cfg.set_process),
    )


def _project_flat(pipe: Pipeline) -> StepOutput:
    """Single-extruder printers: the placed STL and three flat preset files instead of a 3MF project."""
    import numpy as np

    from prep import flat_profiles

    cfg = pipe.cfg
    flags: dict[str, Any] = dict(_process_overrides(pipe, 0.2).to_config())
    flags.pop("enable_support", None)  # `--supports` decides, in build_profiles
    for key in ("layer_height", "initial_layer_print_height"):
        flags.pop(key, None)  # the process preset's own layer height stands (`--set layer_height=` overrides)
    if cfg.brim_mm:
        flags.update({"brim_type": "outer_only", "brim_width": f"{cfg.brim_mm:g}"})
    else:
        flags["brim_type"] = "no_brim"
    overlay = flat_profiles.load_overlay(cfg.overlay) if cfg.overlay else None
    overlay = (overlay or flat_profiles.Overlay()).merged({}, cfg.set_filament)
    # `--set` outranks the overlay file, which outranks the flags above; to_config() carried the extras, so drop
    # them from the flags and hand them over as the top layer
    for key in cfg.set_process:
        flags.pop(key, None)
    overlay = overlay.merged(cfg.set_process, {})
    profiles = flat_profiles.build_profiles(
        cfg.printer,
        process_preset=cfg.process_name,
        filament_preset=cfg.filament_preset,
        process_flags=flags,
        supports=cfg.supports,
        overlay=overlay,
    )
    bed = PRINTER_BED_MM[cfg.printer]
    mesh = pipe.final_mesh().copy()
    lo, hi = mesh.bounds
    mesh.apply_translation(
        np.array([bed[0] / 2 - (lo[0] + hi[0]) / 2, bed[1] / 2 - (lo[1] + hi[1]) / 2, -lo[2]])
    )
    lo, hi = mesh.bounds
    if lo[0] < 1 or lo[1] < 1 or hi[0] > bed[0] - 1 or hi[1] > bed[1] - 1 or hi[2] > bed[2]:
        raise InvalidArgument(
            "model size",
            f"{(hi - lo).round(1).tolist()} mm",
            [f"inside the {bed[0]:g} x {bed[1]:g} x {bed[2]:g} mm bed of the {cfg.printer}"],
            command="prep",
        )
    stl = pipe.work / f"{cfg.name}.print.stl"
    mesh.export(stl)
    paths = profiles.write(pipe.work / f"{cfg.name}.profiles")
    limits = flat_profiles.speed_limits(profiles.process)
    data = {
        "printer": cfg.printer,
        "stl": stl.name,
        "profiles": {k: str(v.relative_to(pipe.work)) for k, v in paths.items()},
        "preset_names": profiles.names,
        "renamed": profiles.renamed,
        "supports": cfg.supports,
        "layer_height": float(profiles.process.get("layer_height", 0.2)),
        "speed_limits": limits,
        "requested_settings": profiles.requested,
        "bbox": [lo.round(3).tolist(), hi.round(3).tolist()],
    }
    summary = (
        f"{profiles.names['machine']} / {profiles.names['process']} / {profiles.names['filament']}; "
        f"supports {cfg.supports}; model {(hi - lo).round(1).tolist()} mm on the {bed[0]:g} bed"
    )
    return StepOutput(data, "PASS", summary, [stl, *paths.values()])


def _project(pipe: Pipeline) -> StepOutput:
    from prep import regions
    from prep.project3mf import ProjectSpec, build_project

    cfg = pipe.cfg
    if cfg.flat_profile:
        return _project_flat(pipe)
    parts = _parts(pipe)
    filaments = (
        list(regions.load_colour_spec(cfg.colors, mix_cap=cfg.mix_cap).filaments)
        if cfg.colors
        else default_filaments()
    )
    if not parts:
        parts = [Part("model", pipe.final_mesh(), Assign(slot=1))]
    plan = saved_layer_plan(pipe)
    overrides = _process_overrides(pipe, plan.first_height if plan else 0.2)
    spec = ProjectSpec(
        name=cfg.name,
        parts=parts,
        filaments=filaments,
        process_preset=cfg.process_name or DEFAULT_PROCESS_PRESET,
        printer=cfg.printer,
        filament_preset=cfg.filament_preset,
        overrides=overrides,
        layer_plan=plan,
        brim_mm=cfg.brim_mm,
        mix_layer_mm=cfg.mix_cap,
    )
    out = pipe.work / f"{cfg.name}.3mf"
    manifest = build_project(spec, out)
    stl = pipe.work / f"{cfg.name}.print.stl"
    pipe.final_mesh().export(stl)
    return StepOutput(
        {**result_dict(manifest), "project": str(out.name), "stl": stl.name},
        manifest.verdict,
        manifest.summary(),
        [out, Path(manifest.manifest_path), stl],
    )


def _slice(pipe: Pipeline) -> StepOutput:
    from slicing import orca_cli, slice_warnings

    cfg = pipe.cfg
    if not cfg.slice:
        return StepOutput({}, "SKIP", "slicing disabled (--no-slice)")
    if cfg.flat_profile:
        files = pipe.record_data("project")
        outcome = orca_cli.slice_stl_with_profiles(
            pipe.work / files["stl"],
            pipe.work / "slice",
            machine=pipe.work / files["profiles"]["machine"],
            process=pipe.work / files["profiles"]["process"],
            filament=pipe.work / files["profiles"]["filament"],
            heavy=True,
        )
    else:
        project = pipe.work / f"{cfg.name}.3mf"
        outcome = orca_cli.slice_project(project, pipe.work / "slice", heavy=True)
    data = result_dict(outcome)
    artifacts: list[Path] = []
    if outcome.gcode is not None:
        gcode = pipe.copy_out(outcome.gcode, f"{cfg.name}.gcode")
        data["gcode"] = gcode.name
        artifacts.append(gcode)
        # every slicer warning is recorded next to the G-code, bound to its SHA-256 (print-preflight's `slicer warn` gate)
        slice_dir = pipe.work / "slice"
        log = slice_dir / "slice.log"
        sidecar = slice_warnings.record(
            gcode,
            logs=[("slice.log", log.read_text(errors="replace"))]
            if log.is_file()
            else (),
            results=[p for p in (slice_dir / "result.json",) if p.is_file()],
            sliced_3mfs=[p for p in (slice_dir / "sliced.3mf",) if p.is_file()],
        )
        data["warnings_sidecar"] = sidecar.path.name
        artifacts.append(sidecar.path)
    return StepOutput(data, outcome.verdict, outcome.summary(), artifacts)


def _gates(pipe: Pipeline) -> StepOutput:
    from prep import gcode_gates

    cfg = pipe.cfg
    gcode = pipe.work / f"{cfg.name}.gcode"
    if not gcode.is_file():
        return StepOutput(
            {}, "SKIP", "no G-code (slice step did not produce one): gates not run"
        )
    fields: dict[str, Any] = {
        "walls": cfg.walls,
        "infill": cfg.infill_pct / 100.0,
        "hmin": cfg.hmin,
        "hmax": cfg.hmax,
        "mix_cap": cfg.mix_cap,
        "bed_mm": PRINTER_BED_MM[cfg.printer],
        "supports": cfg.supports == "auto",
    }
    if cfg.flat_profile:
        # fixed layer height: every layer sits at the preset's value, which is also the "rare" thin layer
        height = float(pipe.record_data("project").get("layer_height", 0.2))
        fields.update(
            hmin=height,
            hmax=height,
            rare_fraction=1.0,
            speed_limits=pipe.record_data("project").get("speed_limits"),
            expect_settings=pipe.record_data("project").get("requested_settings"),
        )
    spec = gcode_gates.GateSpec(**{**fields, **cfg.gate})
    manifest = pipe.work / f"{cfg.name}.manifest.json"
    expectations = (
        gcode_gates.GateExpectations.from_manifest(manifest)
        if manifest.is_file()
        else None
    )
    rep = gcode_gates.check_gcode(gcode, spec, expectations)
    return StepOutput(result_dict(rep), rep.verdict, rep.summary())


def _proof(pipe: Pipeline) -> StepOutput:
    from prep import proof

    return proof.build(pipe)
