#!/usr/bin/env python3
"""assembly_cli.py — the worker behind `3d assembly` (run through `cli.pyrun` with trimesh/manifold3d/numpy/PIL).

WHAT: argument parsing, report assembly and printing for `check | sweep | order | ergonomics | explode | bom | position |
  show`; the geometry lives in `lib/assembly/`. `commands/assembly.py` is the stdlib-only registry shell that spawns this.

CONVENTIONS: `--json` prints exactly one JSON document (schema `3d.assembly/1`, `kind` names the report); a failed
  verdict exits 1 (`GateFailure`), bad input exits 2, a missing dependency 127 - the `lib/errors.py` contract. Errors
  are rendered here because this runs in a child process of the dispatcher.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from errors import (
    GateFailure,
    InvalidArgument,
    MissingDependency,
    ThreeDError,
    UsageError,
)

CMD = "assembly"
SUBS = ("check", "sweep", "order", "ergonomics", "explode", "bom", "position", "show")


def _peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024.0 if sys.platform != "darwin" else 1024.0 * 1024.0), 1)


def _apply_mem_limit(mb: int | None) -> None:
    if mb is None:
        return
    limit = mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="3d assembly", add_help=False)
    ap.add_argument("sub", choices=SUBS)
    ap.add_argument("target")
    ap.add_argument("--mate", action="append", default=[])
    ap.add_argument("--printer")
    ap.add_argument("--material")
    ap.add_argument("--viz")
    ap.add_argument("--view", default="iso")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mem-limit-mb", type=int)
    ap.add_argument("--direction", default="both")
    ap.add_argument("--step", type=float)
    ap.add_argument("--distance", type=float)
    ap.add_argument("--ignore", action="append", default=[])
    ap.add_argument("--base")
    ap.add_argument("--max-travel", type=float)
    ap.add_argument("--finger", type=float)
    ap.add_argument("--nail", type=float)
    ap.add_argument("--tool", type=float)
    ap.add_argument("--reach", type=float)
    ap.add_argument("--factor", type=float, default=1.0)
    ap.add_argument("--usdz")
    ap.add_argument("--3mf", dest="out_3mf")
    ap.add_argument("--bed", type=float, default=220.0)
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--margin", type=float, default=8.0)
    ap.add_argument("--base-project")
    ap.add_argument("--process")
    ap.add_argument("--filament")
    ap.add_argument("--preset-dir", action="append", default=[], dest="preset_dirs")
    ap.add_argument("--set", action="append", default=[], dest="sets")
    ap.add_argument("--include-non-printable", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--yes", action="store_true")
    return ap


def _fail(message: str, remediation: list[str] | None = None) -> GateFailure:
    return GateFailure(message, command=CMD, silent=True, remediation=remediation)


def load_target(target: str, args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """The scene of a spec file / project dir / positioned 3MF, and extra report fields (drift for a 3MF)."""
    from assembly import scene as scn
    from assembly import spec as sp
    from assembly import threemf

    path = Path(target)
    extra: dict[str, Any] = {}
    if path.suffix.lower() == ".3mf":
        pos = threemf.read_positioned(path)
        spec = pos.spec
        scene = scn.load_scene(spec, meshes=pos.meshes)
        extra = {"drift": pos.drift, "source_3mf": str(path)}
    else:
        spec = sp.load_spec(path)
        scene = scn.load_scene(spec)
    if args.finger or args.nail or args.tool or args.reach:
        from dataclasses import replace

        cfg = spec.settings
        cfg = replace(
            cfg,
            finger_pad_mm=args.finger or cfg.finger_pad_mm,
            finger_nail_mm=args.nail or cfg.finger_nail_mm,
            tool_mm=args.tool or cfg.tool_mm,
            reach_mm=args.reach or cfg.reach_mm,
        )
        scene.spec = replace(spec, settings=cfg)
    return scene, extra


def _emit(doc: dict[str, Any], lines: list[str], as_json: bool) -> None:
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(lines))


def cmd_check(args: argparse.Namespace) -> int:
    from assembly import check, report, viz

    scene, extra = load_target(args.target, args)
    ctx = check.with_context(scene.spec, args.printer, args.material)
    styles = None
    highlights: list[Any] = []
    viz_dir = Path(args.viz) if args.viz else None

    def on_region(sc: Any, rep: dict[str, Any]) -> None:
        nonlocal styles
        region = rep["_region"]
        if viz_dir is None:
            return
        from parts import assign_styles

        styles = styles or assign_styles(sc.spec.part_list())
        highlights.append(region.mesh)
        stem = f"{rep['classification']}_{len(highlights):02d}_{rep['a']}_{rep['b']}"
        out = viz.render_section(
            sc, rep, viz_dir / f"{stem}.png", styles=styles, region_mesh=region.mesh
        )
        rep["image"] = str(out)

    rep = check.run_check(
        scene, ctx=ctx, mate_filter=args.mate, on_region=on_region if viz_dir else None
    )
    rep.update(extra)
    if viz_dir is not None:
        overview = viz.render_view(
            scene,
            viz_dir / "overview.png",
            view=args.view,
            highlights=[h for h in highlights if h is not None],
            title=f"{rep['assembly']['name']}: {rep['summary']['blocking']} blocking (red), {rep['summary']['allowed']} allowed",
        )
        rep["images"] = [
            str(overview),
            *[r["image"] for r in rep["blocking"] + rep["allowed"] if r.get("image")],
        ]
    drift = rep.get("drift") or []
    if drift:
        rep["ok"] = False
    rep["resources"] = {"peak_rss_mb": _peak_mb()}
    _emit(rep, report.check_lines(rep), args.json)
    return 0 if rep["ok"] else _exit_fail("assembly check failed")


def _exit_fail(message: str) -> NoReturn:
    raise _fail(message)


def cmd_sweep(args: argparse.Namespace) -> int:
    from assembly import report, sweep

    if not args.mate:
        raise UsageError(
            "sweep needs --mate NAME",
            command=CMD,
            remediation=[
                "Example: 3d assembly sweep assembly.yaml --mate lid --direction remove"
            ],
        )
    if args.direction not in ("insert", "remove", "both"):
        raise InvalidArgument(
            "--direction", args.direction, ["insert", "remove", "both"], command=CMD
        )
    scene, _ = load_target(args.target, args)
    reports: list[dict[str, Any]] = []
    for name in args.mate:
        mate = scene.spec.mate(name)
        for direction in (
            ("insert", "remove") if args.direction == "both" else (args.direction,)
        ):
            reports.append(  # noqa: PERF401 — a formatted row per item reads better than a comprehension
                sweep.sweep_mate(
                    scene,
                    mate,
                    direction=direction,
                    step=args.step,
                    distance=args.distance,
                    ignore=args.ignore,
                    material=args.material,
                )
            )
    doc = (
        reports[0]
        if len(reports) == 1
        else {
            "schema": reports[0]["schema"],
            "kind": "sweeps",
            "ok": all(r["ok"] for r in reports),
            "sweeps": reports,
        }
    )
    lines = [line for r in reports for line in report.sweep_lines(r)]
    _emit(doc, lines, args.json)
    return 0 if doc["ok"] else _exit_fail("assembly sweep failed")


def cmd_order(args: argparse.Namespace) -> int:
    from assembly import report, sweep

    scene, _ = load_target(args.target, args)
    rep = sweep.plan_order(
        scene, base=args.base, step=args.step, max_travel=args.max_travel
    )
    _emit(rep, report.order_lines(rep), args.json)
    return 0 if rep["ok"] else _exit_fail("no straight-line assembly order")


def cmd_ergonomics(args: argparse.Namespace) -> int:
    from assembly import ergonomics, report

    scene, _ = load_target(args.target, args)
    rep = ergonomics.run_ergonomics(
        scene, mate_filter=args.mate, material=args.material
    )
    _emit(rep, report.ergonomics_lines(rep), args.json)
    return 0 if rep["ok"] else _exit_fail("assembly ergonomics failed")


def cmd_explode(args: argparse.Namespace) -> int:
    from assembly import viz
    from assembly.spec import SCHEMA
    from parts import assign_styles, legend

    scene, _ = load_target(args.target, args)
    parts = scene.spec.part_list()
    styles = assign_styles(parts)
    offsets = viz.explode_offsets(scene, args.factor)
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "explode",
        "ok": True,
        "offsets": {k: [round(c, 4) for c in v] for k, v in offsets.items()},
        "legend": legend(parts),
        "images": [],
    }
    if args.viz:
        doc["images"].append(
            str(
                viz.render_view(
                    scene,
                    args.viz,
                    view=args.view,
                    styles=styles,
                    offsets=offsets,
                    title=f"{scene.spec.name}: exploded x{args.factor:g}",
                )
            )
        )
    if args.usdz:
        doc["usdz"] = _write_usdz(scene, styles, offsets, Path(args.usdz))
    lines = [f"assembly explode: {scene.spec.name}"]
    lines += [f"  {k}: offset {v}" for k, v in doc["offsets"].items() if any(v)]
    lines += [f"  image: {p}" for p in doc["images"]]
    if "usdz" in doc:
        lines.append(f"  usdz: {doc['usdz']['path']} (legend: {doc['usdz']['legend']})")
    _emit(doc, lines, args.json)
    return 0


def _write_usdz(
    scene: Any, styles: dict[str, Any], offsets: dict[str, Any], out: Path
) -> dict[str, Any]:
    import usdz
    from assembly.viz import hex_rgb
    from parts import legend

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="assembly-usdz-") as tmp:
        items: list[Any] = []
        for name, body in sorted(scene.bodies.items()):
            mesh = body.mesh.copy()
            mesh.apply_translation(offsets.get(name, (0.0, 0.0, 0.0)))
            stl = Path(tmp) / f"{name}.stl"
            mesh.export(stl)
            r, g, b = hex_rgb(styles[name].color)
            items.append(usdz.UsdzPart(str(stl), (r / 255, g / 255, b / 255), name))
        usdz.meshes_to_usdz(items, str(out))
    sidecar = out.with_suffix(".legend.json")
    sidecar.write_text(
        json.dumps(legend(scene.spec.part_list()), indent=2) + "\n", encoding="utf-8"
    )
    return {"path": str(out), "legend": str(sidecar), "parts": len(items)}


def cmd_bom(args: argparse.Namespace) -> int:
    from assembly.spec import SCHEMA
    from parts import assign_styles, legend

    scene, _ = load_target(args.target, args)
    rows: list[dict[str, Any]] = []
    for name, part in sorted(scene.spec.parts.items()):
        body = scene.body(name)
        size = body.mesh.extents
        rows.append(
            {
                "part": name,
                "role": part.role,
                "printable": part.printable,
                "size_mm": [round(float(s), 2) for s in size],
                "volume_mm3": round(float(body.mesh.volume), 1)
                if body.mesh.is_watertight
                else None,
                "reference_source": part.part.reference_source.to_dict()
                if part.part.reference_source
                else None,
                "label": "stand-in (invented placeholder)"
                if part.role == "stand_in"
                else None,
            }
        )
    fasteners = [
        {"mate": m.name, "between": [m.a, m.b], "kind": m.kind}
        for m in scene.spec.mates
        if m.kind == "bolt"
    ]
    doc = {
        "schema": SCHEMA,
        "kind": "bom",
        "ok": True,
        "assembly": scene.spec.name,
        "parts": rows,
        "fasteners": fasteners,
        "mates": [
            {"name": m.name, "a": m.a, "b": m.b, "kind": m.kind, "fit": m.fit}
            for m in scene.spec.mates
        ],
        "legend": legend(scene.spec.part_list()),
        "styles": {
            k: v.to_dict() for k, v in assign_styles(scene.spec.part_list()).items()
        },
    }
    lines = [
        f"assembly bom: {scene.spec.name}",
        f"  {'part':<16} {'role':<10} {'size mm':<22} source",
    ]
    for r in rows:
        src = (
            r["reference_source"]["url"]
            if r["reference_source"]
            else (r["label"] or "-")
        )
        lines.append(
            f"  {r['part']:<16} {r['role']:<10} {' x '.join(map(str, r['size_mm'])):<22} {src}"
        )
    lines += [
        f"  fastener {f['mate']}: {f['between'][0]} - {f['between'][1]}"
        for f in fasteners
    ]
    lines.append(f"  {len(scene.spec.mates)} mate(s)")
    _emit(doc, lines, args.json)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from assembly.spec import SCHEMA
    from parts import legend

    scene, _ = load_target(args.target, args)
    spec = scene.spec
    doc = {
        "schema": SCHEMA,
        "kind": "show",
        "ok": True,
        "name": spec.name,
        "spec": str(spec.path) if spec.path else None,
        "root": str(spec.root),
        "context": spec.context.to_dict(),
        "settings": spec.settings.to_dict(),
        "parts": {
            n: {**p.part.to_dict(), "mesh": scene.body(n).source}
            for n, p in spec.parts.items()
        },
        "mates": [m.name for m in spec.mates],
        "loads": [
            {"name": ld.name, "part": ld.part, "force_n": ld.force_n}
            for ld in spec.loads
        ],
        "legend": legend(spec.part_list()),
    }
    lines = [
        f"assembly show: {spec.name} ({len(spec.parts)} parts, {len(spec.mates)} mates, {len(spec.loads)} loads)"
    ]
    from assembly import report

    lines += report.legend_lines(doc["legend"])
    _emit(doc, lines, args.json)
    return 0


def cmd_position(args: argparse.Namespace) -> int:
    from assembly import position, report
    from assembly.spec import SCHEMA

    if not args.out_3mf:
        raise UsageError(
            "position needs --3mf OUT",
            command=CMD,
            remediation=[
                "Example: 3d assembly position assembly.yaml --3mf plates.3mf --yes"
            ],
        )
    if args.target.lower().endswith(".3mf"):
        raise InvalidArgument(
            "target",
            args.target,
            ["an assembly spec (YAML/JSON) or a project directory"],
            command=CMD,
            extra="`position` lays out parts from a spec; a 3MF is already positioned.",
        )
    scene, _ = load_target(args.target, args)
    layout = position.plan_layout(
        scene,
        bed=args.bed,
        gap=args.gap,
        margin=args.margin,
        include_non_printable=args.include_non_printable,
        reason=args.reason,
    )
    out = Path(args.out_3mf)
    if args.printer and not args.base_project:
        raise UsageError(
            "--printer needs a slicer project to retarget",
            command=CMD,
            remediation=[
                "Pass --base-project PROJECT.3mf (an Orca-family project holding the slicer settings), "
                "then the result is moved to the printer with `3d retarget`."
            ],
        )
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "position",
        "ok": True,
        "assembly": {"name": scene.spec.name},
        "out": str(out),
        "written": False,
        "layout": layout.to_dict(),
        "slicer": {
            "note": "geometry-only project (no slicer settings): pass --base-project to carry them"
        },
    }
    if args.base_project:
        doc["slicer"] = {
            "note": f"settings of {args.base_project} carried; per-part print.settings written as object metadata",
            "base_project": args.base_project,
        }
    if args.yes:
        if (
            out.exists()
            and out.is_file()
            and out.resolve() == Path(args.base_project or "/nonexistent").resolve()
        ):
            raise InvalidArgument(
                "--3mf", str(out), ["a path different from --base-project"], command=CMD
            )
        staged = out.with_name(out.stem + ".pre-retarget.3mf") if args.printer else out
        doc["verification"] = position.write_project(
            scene, layout, staged, base_project=args.base_project, printer=args.printer
        )
        if args.printer:
            try:
                doc["retarget"] = _retarget(staged, out, args)
            finally:
                staged.unlink(missing_ok=True)  # the retarget never overwrites its source: stage, then replace
            doc["slicer"]["retargeted_to"] = args.printer
        doc["written"] = True
    _emit(doc, report.position_lines(doc), args.json)
    return 0


def _retarget(src: Path, out: Path, args: argparse.Namespace) -> dict[str, Any]:
    from commands import retarget

    argv = [str(src), "--to", args.printer, "-o", str(out)]
    if args.process:
        argv += ["--process", args.process]
    if args.filament:
        argv += ["--filament", args.filament]
    for item in args.sets:
        argv += ["--set", item]
    for item in args.preset_dirs:
        argv += ["--preset-dir", item]
    rc = retarget.run(argv)
    if rc != 0:
        raise _fail(f"`3d retarget` exited {rc}")
    return {"to": args.printer, "argv": argv}


HANDLERS = {
    "check": cmd_check,
    "sweep": cmd_sweep,
    "order": cmd_order,
    "ergonomics": cmd_ergonomics,
    "explode": cmd_explode,
    "bom": cmd_bom,
    "position": cmd_position,
    "show": cmd_show,
}


def main(argv: list[str]) -> int:
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as exc:
            raise UsageError(
                "bad arguments", command=CMD, remediation=["Run `3d assembly --help`."]
            ) from exc
        _apply_mem_limit(args.mem_limit_mb)
        try:
            return HANDLERS[args.sub](args)
        except MemoryError as exc:
            raise _fail(
                "out of memory while checking the assembly",
                [
                    "Raise --mem-limit-mb or lower settings.max_faces; one pair is built at a time."
                ],
            ) from exc
    except ImportError as exc:
        err: ThreeDError = MissingDependency(
            str(exc.name or exc),
            install="uv sync --extra dev",
            degrades="`3d assembly` cannot run",
            command=CMD,
        )
        sys.stderr.write(err.render() + "\n")
        return err.exit_code
    except ThreeDError as exc:
        text = exc.render()
        if text:
            sys.stderr.write(text + "\n")
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
