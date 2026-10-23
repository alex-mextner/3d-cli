#!/usr/bin/env python3
"""mesh_deform.py — `3d mesh deform` / `3d mesh fit-silhouette`: deterministic spec-driven mesh sculpting.

ACCESSED VIA: `3d mesh deform <mesh> <spec.yaml> [-o OUT]` and `3d mesh fit-silhouette <mesh> <spec.yaml>`
(lib/commands/mesh.py forwards these subcommands here through cli.pyrun, so the command module stays
stdlib-only and this file carries numpy / scipy / trimesh).

WHAT: loads the mesh, applies the spec's ordered ops (see geometry/meshfit/spec.py), enforces the keep-out
  zones, writes the result (.stl / .3mf byte-reproducible), a JSON report and optionally a before / after
  collage. Exit 0 = PASS, 1 = gate FAIL (keep-out moved, non-determinism), 2 = usage / input error.

WHY: what a person does in Blender sculpt mode must be repeatable from a text file, fast, and provably
  unable to touch the functional zones. Same input + same spec + same seed => the same SHA-256.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# Threaded BLAS changes the bits of large dense products (research note §5); pin it before numpy loads.
# Sparse solves, stacked 3x3 SVDs and np.bincount reductions are single-thread deterministic anyway.
for _var in (
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
):
    os.environ[_var] = "1"

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from errors import GateFailure, ThreeDError, UsageError  # noqa: E402  (lib/ must be on sys.path first)

PROG = "3d mesh"


def _parser(sub: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{PROG} {sub}", add_help=False)
    p.add_argument("mesh")
    p.add_argument("spec")
    p.add_argument("-o", "--out")
    p.add_argument("--report")
    p.add_argument("--collage")
    p.add_argument("--seed", type=int)
    p.add_argument("--verify-determinism", action="store_true")
    p.add_argument("--no-self-intersections", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def run_deform(argv: list[str], sub: str = "deform") -> int:
    from geometry.meshfit import engine, meshio, spec as spec_mod

    try:
        args = _parser(sub).parse_args(argv)
    except SystemExit as exc:
        raise UsageError(
            f"{PROG} {sub}: bad arguments; usage: {PROG} {sub} <mesh> <spec.yaml> [-o OUT] [--report FILE]",
            command="mesh",
        ) from exc
    spec = spec_mod.load_spec(args.spec)
    if args.seed is not None:
        spec["seed"] = args.seed
    if sub == "fit-silhouette" and not any(
        op["op"] == "fit_silhouette" for op in spec["ops"]
    ):
        raise UsageError(
            "fit-silhouette: the spec has no `fit_silhouette` op",
            command="mesh",
            remediation=[
                "Add an op `fit_silhouette` with views and handles, see docs/commands/mesh.md."
            ],
        )
    out = args.out or os.path.splitext(args.mesh)[0] + ".deformed.stl"

    loaded = time.perf_counter()
    V, F = meshio.load_mesh(args.mesh)
    load_s = time.perf_counter() - loaded
    V1, F1, report = engine.run_spec(
        V, F, spec, self_intersections=not args.no_self_intersections
    )
    report["timing_s"] = {"load": round(load_s, 4), "run": report["timing_s"]}
    report["input"]["path"] = args.mesh
    report["input"]["sha256"] = meshio.file_sha256(args.mesh)
    report["spec"] = os.path.abspath(args.spec)

    determinism: dict[str, Any] = {
        "mesh_sha256": report["mesh_sha256"],
        "verified": False,
    }
    if args.verify_determinism:
        _, _, again = engine.run_spec(
            *meshio.load_mesh(args.mesh), spec, self_intersections=False
        )
        determinism["verified"] = True
        determinism["second_run_sha256"] = again["mesh_sha256"]
        determinism["identical"] = again["mesh_sha256"] == report["mesh_sha256"]
        if not determinism["identical"]:
            report["status"] = "FAIL"
    report["determinism"] = determinism

    if report["status"] == "PASS":
        report["output"]["path"] = out
        report["output"]["sha256"] = meshio.save_mesh(out, V1, F1)
        if args.collage:
            from geometry.meshfit import collage

            collage.render_collage(args.collage, V, F, V1, F1)
            report["collage"] = args.collage
    report_path = args.report or (out + ".report.json")
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_summary(report, out, report_path, sub)
    if report["status"] != "PASS":
        reasons = []
        if report["keepout"]["gate"] != "PASS":
            reasons.append(
                f"keep-out zone moved by {report['keepout']['max_displacement_mm']} mm (tolerance {report['keepout']['tolerance_mm']} mm)"
            )
        if determinism.get("identical") is False:
            reasons.append("two runs produced different meshes")
        raise GateFailure(
            ">>> DEFORM: FAIL — " + "; ".join(reasons), command="mesh", silent=False
        )
    return 0


def _print_summary(
    report: dict[str, Any], out: str, report_path: str, sub: str
) -> None:
    inp, outp = report["input"], report["output"]
    print(
        f"{PROG} {sub}: {inp['path']} ({inp['vertices']} verts, {inp['faces']} faces) -> {out if report['status'] == 'PASS' else '(not written)'}"
    )
    print(f"  {'#':>2}  {'op':<15} {'moved':>9} {'max mm':>9} {'sec':>8}")
    for rec in report["ops"]:
        moved = rec.get(
            "vertices_moved",
            f"{rec.get('vertices_before')}→{rec.get('vertices_after')}",
        )
        print(
            f"  {rec['index']:>2}  {rec['op']:<15} {moved!s:>9} {rec.get('max_displacement_mm', ''):>9} {rec['seconds']:>8}"
        )
        if rec["op"] == "fit_silhouette":
            from geometry.meshfit import fit

            print(
                "\n".join(
                    "      " + line for line in fit.format_table(rec).splitlines()
                )
            )
    k = report["keepout"]
    print(
        f"  keep-out: {k['locked_vertices']} locked vertices, {k['fade_vertices']} in the fade band, max displacement {k['max_displacement_mm']} mm (tolerance {k['tolerance_mm']}) -> {k['gate']}"
    )
    print(
        f"  watertight {report['watertight_before']} -> {report['watertight_after']}, volume {report['volume_before_mm3']} -> {report['volume_after_mm3']} mm3, faces {inp['faces']} -> {outp['faces']}"
    )
    si = report.get("self_intersections")
    if si:
        before, after = si["before"], si["after"]
        print(
            f"  self-intersections ({after['engine']}, {after['unit']}): before {before['count']}, after {after['count']}"
        )
    det = report["determinism"]
    if det["verified"]:
        print(
            f"  determinism: {'identical' if det.get('identical') else 'DIFFERENT'} over two runs ({det['mesh_sha256'][:16]}…)"
        )
    if "sha256" in outp:
        print(f"  sha256 {outp['sha256']}")
    print(f"  report {report_path}")
    print(f">>> DEFORM: {report['status']}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("deform", "fit-silhouette"):
        print(
            "usage: mesh_deform.py deform|fit-silhouette <mesh> <spec> ...",
            file=sys.stderr,
        )
        return 2
    try:
        return run_deform(argv[1:], argv[0])
    except ThreeDError as exc:
        text = exc.render()
        if text:
            print(text, file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
