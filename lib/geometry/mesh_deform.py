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

import numpy as np  # noqa: E402
from errors import GateFailure, InputNotFound, ThreeDError, UsageError  # noqa: E402  (lib/ must be on sys.path first)
from numpy.typing import NDArray  # noqa: E402

PROG = "3d mesh"


def _parser(sub: str) -> argparse.ArgumentParser:
    from geometry.deform_args import build_parser

    return build_parser(sub)


def _load_labels(path: str, n_faces: int) -> dict[str, NDArray[Any]]:
    """Every array of an .npz; `face_label` is required and has one entry per face of the mesh as loaded.
    Any other array with `n_faces` rows (a face group, a patch id) is per-face data too and is carried alike."""
    if not os.path.isfile(path):
        raise InputNotFound(path, command="mesh")
    with np.load(path, allow_pickle=False) as data:
        arrays = {k: np.asarray(data[k]) for k in data.files}
    if "face_label" not in arrays:
        raise UsageError(
            f"{path}: no `face_label` array (found: {', '.join(arrays) or 'none'})",
            command="mesh",
            remediation=[
                "Save labels as np.savez(path, face_label=<one int per face>, ...)."
            ],
        )
    count = len(arrays["face_label"]) if arrays["face_label"].ndim else 0
    if count != n_faces:
        raise UsageError(
            f"{path}: {count} face labels but the mesh has {n_faces} faces after loading (vertices are welded, degenerate and duplicate faces dropped)",
            command="mesh",
            remediation=[
                "Label the mesh as `3d mesh deform` loads it: same file, same face order."
            ],
        )
    return arrays


def _save_labels(
    path: str,
    arrays: dict[str, NDArray[Any]],
    n_faces: int,
    parents: NDArray[np.int64],
) -> str:
    """Write the arrays for the output faces: per-face arrays (`n_faces` rows) are indexed by each face's parent, the rest copied."""
    out: dict[str, Any] = {
        k: v[parents] if v.ndim and len(v) == n_faces else v for k, v in arrays.items()
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    np.savez_compressed(path, **out)
    return path


def _parse_args(argv: list[str], sub: str) -> argparse.Namespace:
    try:
        return _parser(sub).parse_args(argv)
    except SystemExit as exc:
        raise UsageError(
            f"{PROG} {sub}: bad arguments; usage: {PROG} {sub} <mesh> <spec.yaml> [-o OUT] [--report FILE]",
            command="mesh",
        ) from exc


def _load_checked_spec(args: argparse.Namespace, sub: str) -> dict[str, Any]:
    from geometry.meshfit import spec as spec_mod

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
    return spec


def _require_labels_for_out_labels(args: argparse.Namespace, sub: str) -> None:
    if args.out_labels and not args.labels:
        raise UsageError(
            "--out-labels needs --labels",
            command="mesh",
            remediation=[
                f"{PROG} {sub} part.stl spec.yaml --labels part.labels.npz --out-labels out.labels.npz"
            ],
        )


def _labels_report(
    path: str, face_data: dict[str, NDArray[Any]], n_faces: int
) -> dict[str, Any]:
    return {
        "input": path,
        "faces_in": n_faces,
        "classes": int(len(np.unique(face_data["face_label"]))),
        "per_face_arrays": sorted(
            k for k, v in face_data.items() if v.ndim and len(v) == n_faces
        ),
    }


def _verify_determinism(
    args: argparse.Namespace, spec: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    """Record the first run's hash and, with --verify-determinism, re-run and compare (a mismatch fails the report)."""
    from geometry.meshfit import engine, meshio

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
    return determinism


def _write_passing_outputs(
    args: argparse.Namespace,
    report: dict[str, Any],
    out: str,
    mesh: tuple[NDArray[Any], NDArray[Any], NDArray[Any], NDArray[Any]],
    face_data: dict[str, NDArray[Any]] | None,
    parents: NDArray[np.int64] | None,
) -> None:
    """Write the mesh, the carried labels and the collage of a PASS run, noting each in the report."""
    from geometry.meshfit import meshio

    V, F, V1, F1 = mesh
    report["output"]["path"] = out
    report["output"]["sha256"] = meshio.save_mesh(out, V1, F1)
    if face_data is not None:
        assert parents is not None
        labels_out = args.out_labels or os.path.splitext(out)[0] + ".labels.npz"
        report["labels"]["output"] = _save_labels(
            labels_out, face_data, len(F), parents
        )
        report["labels"]["faces_out"] = int(len(parents))
    if args.collage:
        from geometry.meshfit import collage

        collage.render_collage(args.collage, V, F, V1, F1)
        report["collage"] = args.collage


def _write_report(report: dict[str, Any], report_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _gate_failure(report: dict[str, Any]) -> GateFailure:
    reasons = []
    if report["keepout"]["gate"] != "PASS":
        reasons.append(
            f"keep-out zone moved by {report['keepout']['max_displacement_mm']} mm (tolerance {report['keepout']['tolerance_mm']} mm)"
        )
    if report["determinism"].get("identical") is False:
        reasons.append("two runs produced different meshes")
    return GateFailure(
        ">>> DEFORM: FAIL — " + "; ".join(reasons), command="mesh", silent=False
    )


def run_deform(argv: list[str], sub: str = "deform") -> int:
    from geometry.meshfit import engine, meshio

    args = _parse_args(argv, sub)
    spec = _load_checked_spec(args, sub)
    out = args.out or os.path.splitext(args.mesh)[0] + ".deformed.stl"
    _require_labels_for_out_labels(args, sub)

    loaded = time.perf_counter()
    V, F = meshio.load_mesh(args.mesh)
    load_s = time.perf_counter() - loaded
    face_data = _load_labels(args.labels, len(F)) if args.labels else None
    V1, F1, report, parents = engine.run_spec_with_parents(
        V,
        F,
        spec,
        self_intersections=not args.no_self_intersections,
        carry_faces=face_data is not None,
    )
    if face_data is not None:
        report["labels"] = _labels_report(args.labels, face_data, int(len(F)))
    report["timing_s"] = {"load": round(load_s, 4), "run": report["timing_s"]}
    report["input"]["path"] = args.mesh
    report["input"]["sha256"] = meshio.file_sha256(args.mesh)
    report["spec"] = os.path.abspath(args.spec)
    report["determinism"] = _verify_determinism(args, spec, report)

    if report["status"] == "PASS":
        _write_passing_outputs(args, report, out, (V, F, V1, F1), face_data, parents)
    report_path = args.report or (out + ".report.json")
    _write_report(report, report_path)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_summary(report, out, report_path, sub)
    if report["status"] != "PASS":
        raise _gate_failure(report)
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
    lab = report.get("labels")
    if lab and "output" in lab:
        print(
            f"  labels: {lab['faces_in']} -> {lab['faces_out']} faces ({lab['classes']} classes) {lab['output']}"
        )
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
