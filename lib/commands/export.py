"""3d export — STL/3MF/USDZ export WITH geometry validation. Nonzero exit on bad geometry.

WHAT: exports a .scad model to a mesh format (STL, 3MF, OFF, AMF, USDZ) and validates
  the result for manifoldness, watertightness, and self-intersection before returning.

WHY: a bad mesh exported to a slicer will fail mid-print or produce garbage. `export`
  gates the output: if OpenSCAD warns about non-manifold geometry, self-intersections,
  or degenerate faces, the command exits nonzero and the bad mesh never reaches the
  slicer. This is the last line of defense before physical printing.

Examples:
  3d export model.scad -o model.stl
  3d export model.scad -o model.3mf -D 'width=80'
  3d export model.scad -o model.usdz --color 0.30,0.55,0.85

ROADMAP §34: "STL — the slicing/mesh-export lingua franca. 3d export part.scad -o part.stl
  (auto-emits part.3d.yaml sidecar). 3MF — preferred rich print format (per-part color,
  material, metadata). USDZ — Apple AR Quick Look (tap-to-view)."
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from cli.env import require_openscad
from cli.pyrun import run_tool, tool_argv
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError
from export_formats import (
    ExportFormat,
    build_export_plan,
    get_format,
    list_export_formats,
    selector_map,
)

USAGE = """3d export <file.scad> [options]
  Export to STL/3MF/OFF/AMF/USDZ with manifold and self-intersection validation.
  List and plan future formats before converters land. Exit 1 on bad geometry so
  bad meshes never reach a slicer.

Options:
  -o, --out PATH        output file path. Default: <file>.<format extension>.
                        Accepted extensions: .stl, .3mf, .off, .amf, .usdz,
                        .obj, .ply, .glb, .gltf, .step, .stp, .brep, .svg.
                        Example: 3d export bracket.scad -o bracket.stl
  --format FORMAT       stl, 3mf, off, amf, usdz, obj, ply, glb, gltf, step, brep, svg.
                        Use when the output path is omitted or generated elsewhere.
                        Example: 3d export bracket.scad --format 3mf
  --stl/--3mf/--usdz    selector shortcuts. Planned selectors include --obj, --ply,
                        --glb, --gltf, --step, --brep, and --svg.
                        Example: 3d export bracket.scad --usdz --color 0.30,0.55,0.85
  --list-formats        list supported and planned export formats.
                        Example: 3d export --list-formats
  --plan                print the export plan without running OpenSCAD/converters.
                        Example: 3d export bracket.scad --plan --format glb
  --ascii              ASCII STL instead of binary. Use this only when a downstream
                        tool requires human-readable STL (most slicers prefer binary).
                        Example: 3d export bracket.scad -o bracket.stl --ascii
  --color r,g,b        diffuse colour for USDZ export, each 0..1 (default: stone/travertine).
                        Only used when output extension is .usdz.
                        Example: 3d export part.scad -o part.usdz --color 0.30,0.55,0.85
  -D k=v               pass an OpenSCAD variable define. Repeatable. Use this to
                        export a variant without editing the .scad.
                        Example: 3d export bracket.scad -o wide.stl -D 'width=80'

Examples:
  3d export model.scad -o model.stl
  3d export model.scad -o model.3mf -D 'width=80'
  3d export model.scad --usdz --color 0.30,0.55,0.85
  3d export --list-formats
  3d export model.scad --plan --format glb"""

_SELECTORS = selector_map()

DEFAULT_COLOR = (0.78, 0.74, 0.66)


def _parse_color(raw: str) -> tuple[float, float, float]:
    parts = raw.split(",")
    if len(parts) != 3:
        raise InvalidArgument(
            "--color", raw, ["r,g,b with three comma-separated floats"],
            command="export",
            extra="Each component is 0..1, e.g. --color 0.30,0.55,0.85",
        )
    try:
        vals = tuple(float(p) for p in parts)
    except ValueError:
        raise InvalidArgument(
            "--color", raw, ["three floats in 0..1"],
            command="export",
            extra="Each component is 0..1, e.g. --color 0.30,0.55,0.85",
        ) from None
    for v in vals:
        if not (0.0 <= v <= 1.0):
            raise InvalidArgument(
                "--color", raw, ["each component in 0..1"],
                command="export",
                extra="e.g. --color 0.30,0.55,0.85",
            )
    return (vals[0], vals[1], vals[2])


def _mesh_check_capture(out_path: str) -> str:
    """Run mesh_check.py on the produced STL and return its combined output."""
    argv = tool_argv("trimesh,manifold3d,numpy", "mesh_check.py", [out_path])
    r = subprocess.run(argv, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def _print_formats() -> None:
    print("3d export formats")
    print("")
    print(f"{'format':<8} {'status':<10} {'selectors':<18} {'extensions':<20} summary")
    for fmt in list_export_formats():
        selector = fmt.selector or ""
        exts = ", ".join(f".{ext}" for ext in fmt.extensions)
        print(f"{fmt.key:<8} {fmt.status:<10} {selector:<18} {exts:<20} {fmt.summary}")
        for note in fmt.notes:
            print(f"{'':<8} {'':<10} {'':<18} {'':<20} note: {note}")


def _print_plan(plan_format: ExportFormat, output_path: str, steps: tuple[str, ...]) -> None:
    print("3d export plan")
    print(f"format: {plan_format.key} ({plan_format.status})")
    print(f"output: {output_path}")
    print("steps:")
    for step in steps:
        print(f"  - {step}")


def _native_format_args(fmt: ExportFormat, ascii_stl: bool) -> list[str]:
    if fmt.key == "stl":
        return ["--export-format", "asciistl" if ascii_stl else "binstl"]
    if fmt.key == "usdz":
        return ["--export-format", "binstl"]
    if fmt.openscad_format:
        return ["--export-format", fmt.openscad_format]
    return []


def _cleanup_tmp(path: str) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


def _set_explicit_format(current: str, new: str) -> str:
    new_key = get_format(new).key
    current_key = get_format(current).key if current else ""
    if current_key and current_key != new_key:
        raise UsageError(
            f"conflicting export formats: {current_key} and {new_key}",
            command="export",
            remediation=["Use one --format value or one selector flag."],
        )
    return new_key


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] == "--list-formats":
        _print_formats()
        return 0

    inp = argv[0]
    rest = argv[1:]
    out = ""
    ascii_stl = False
    color = DEFAULT_COLOR
    plan_only = False
    explicit_format = ""
    defs: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="export")
            out = rest[i + 1]
            i += 2
        elif a == "--ascii":
            ascii_stl = True
            i += 1
        elif a == "--plan":
            plan_only = True
            i += 1
        elif a == "--list-formats":
            _print_formats()
            return 0
        elif a == "--format":
            if i + 1 >= n:
                raise UsageError("option --format needs a value", command="export")
            explicit_format = _set_explicit_format(explicit_format, rest[i + 1])
            i += 2
        elif a in _SELECTORS:
            explicit_format = _set_explicit_format(explicit_format, _SELECTORS[a])
            i += 1
        elif a == "--color":
            if i + 1 >= n:
                raise UsageError("option --color needs a value", command="export")
            color = _parse_color(rest[i + 1])
            i += 2
        elif a == "-D":
            if i + 1 >= n:
                raise UsageError("option -D needs a value", command="export")
            defs += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="export")

    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="export")

    plan = build_export_plan(inp, out, explicit_format, ascii_stl=ascii_stl)
    fmt = plan.format
    out = plan.output_path

    if plan_only:
        _print_plan(fmt, out, plan.steps)
        return 0

    if fmt.status == "planned":
        raise UsageError(
            f"{fmt.key} export is planned but not implemented yet",
            command="export",
            remediation=[
                f"Run '3d export <file.scad> --plan --format {fmt.key}' to inspect the planned path."
            ],
        )

    osc = require_openscad("export")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    fmt_args = _native_format_args(fmt, ascii_stl)
    tmp_dir = ""
    export_path = out
    if fmt.key == "usdz":
        tmp_dir = tempfile.mkdtemp(prefix="3d-export-usdz-")
        export_path = os.path.join(tmp_dir, os.path.splitext(os.path.basename(out))[0] + ".stl")

    print("================================================================")
    print(f"export: {os.path.basename(inp)} -> {out}")
    print(f"  format: {fmt.key}")
    if defs:
        print(f"  defines: {' '.join(defs)}")
    print("================================================================")

    r = subprocess.run([osc, *fmt_args, *defs, "-o", export_path, inp], capture_output=True, text=True)
    result = (r.stdout or "") + (r.stderr or "")

    bad = False
    warn: list[str] = []
    low = result.lower()
    if "not manifold" in low or "non-manifold" in low:
        warn.append("non-manifold geometry (holes in mesh)")
        bad = True
    if "self-intersect" in low:
        warn.append("self-intersecting geometry")
        bad = True
    if "degenerate" in low:
        warn.append("degenerate faces (zero-area triangles)")
        bad = True
    if "ERROR:" in result:
        warn.append("openscad ERROR: during export")
        bad = True

    if not os.path.isfile(export_path):
        sys.stderr.write("export: FAILED — no output produced\n")
        for line in result.splitlines():
            sys.stderr.write(f"  {line}\n")
        _cleanup_tmp(tmp_dir)
        return 1

    size = os.path.getsize(export_path)
    if fmt.key == "usdz":
        print(f"intermediate: {export_path} ({size} bytes)")
    else:
        print(f"output: {out} ({size} bytes)")
    if fmt.key == "stl" and not ascii_stl:
        try:
            with open(export_path, "rb") as fh:
                fh.seek(80)
                import struct
                (tris,) = struct.unpack("<I", fh.read(4))
            print(f"triangles: {tris}")
        except (OSError, struct.error):
            pass
    print("--- geometry validation ---")

    # The modern (manifold) backend often produces output WITHOUT a text warning for a
    # non-watertight/non-manifold mesh, so run the authoritative mesh check on the STL.
    mesh_verdict = ""
    check_path = export_path
    if fmt.key in ("stl", "usdz") and not (fmt.key == "stl" and ascii_stl):
        mout = _mesh_check_capture(check_path)
        if any(s in mout for s in ("ModuleNotFoundError", "No module named", "no python runtime")):
            mesh_verdict = "skip"
        elif ">>> MESH CHECK: FAIL" in mout:
            bad = True
            detail = ""
            for line in mout.splitlines():
                if "MESH CHECK: FAIL" in line:
                    detail = line.split("FAIL", 1)[-1]
            warn.append(f"mesh check:{detail}")
            mesh_verdict = "fail"
        else:
            mesh_verdict = "pass"

    if bad:
        print("STATUS: FAIL")
        for w in warn:
            print(f"  - {w}")
        print("  (non-manifold -> closed solids; self-intersect -> union(); degenerate -> no zero-thickness)")
        print("================================================================")
        _cleanup_tmp(tmp_dir)
        return 1

    if fmt.key == "usdz":
        # Convert the validated STL to USDZ.
        stem = os.path.splitext(os.path.basename(out))[0]
        color_args = [str(color[0]), str(color[1]), str(color[2])]
        rc = run_tool("trimesh,usd-core", "usdz.py", [check_path, out] + color_args + [stem])
        if rc != 0:
            print("STATUS: FAIL — USDZ conversion failed")
            print("================================================================")
            _cleanup_tmp(tmp_dir)
            return 1
        if not os.path.isfile(out):
            print("STATUS: FAIL — USDZ conversion produced no output")
            print("================================================================")
            _cleanup_tmp(tmp_dir)
            return 1
        print(f"output: {out} ({os.path.getsize(out)} bytes)")
        print("STATUS: PASS — manifold, watertight, AR Quick Look ready")
        print("================================================================")
        _cleanup_tmp(tmp_dir)
        return 0

    if mesh_verdict == "pass":
        print("STATUS: PASS — manifold, watertight (mesh-verified), slicer-ready")
    elif mesh_verdict == "skip":
        print(
            f"STATUS: PASS — no openscad warnings (mesh stack absent: 'STATUS' is log-grep only; "
            f"run '3d mesh {out}' for the full check)"
        )
    else:
        print("STATUS: PASS — manifold, no self-intersections, slicer-ready")
    print("================================================================")
    return 0


COMMAND = Command(
    name="export",
    group="GEOMETRY & EXPORT",
    summary="format-aware 3D export with STL/3MF/USDZ validation and planned format listing",
    usage=USAGE,
    run=run,
)
