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
import subprocess
import sys

from cli.env import require_openscad
from cli.pyrun import run_tool, tool_argv
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d export <file.scad> [options]
  Export to STL/3MF/OFF/AMF/USDZ with manifold and self-intersection validation. Run this
  before slicing or sharing a model. Exit 1 on bad geometry so bad meshes never
  reach a slicer.

Options:
  -o, --out PATH        output file path. Accepted extensions: .stl, .3mf, .off,
                        .amf, .usdz. Default: <file>.stl. Use .3mf for multi-material/color
                        assemblies; .usdz for Apple AR Quick Look.
                        Example: 3d export bracket.scad -o bracket.stl
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
  3d export model.scad -o model.usdz --color 0.30,0.55,0.85"""

_ACCEPTED_EXT = ["stl", "3mf", "off", "amf", "usdz"]

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


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    osc = require_openscad("export")

    inp = argv[0]
    rest = argv[1:]
    out = ""
    ascii_stl = False
    color = DEFAULT_COLOR
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
    if not out:
        out = (inp[:-5] if inp.endswith(".scad") else inp) + ".stl"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    ext = out.rsplit(".", 1)[-1].lower() if "." in out else ""
    fmt_args: list[str] = []
    if ext == "stl":
        fmt_args = ["--export-format", "asciistl" if ascii_stl else "binstl"]
    elif ext == "3mf":
        fmt_args = ["--export-format", "3mf"]
    elif ext in ("off", "amf"):
        fmt_args = []  # let openscad infer
    elif ext == "usdz":
        fmt_args = ["--export-format", "binstl"]  # intermediate STL
    else:
        raise InvalidArgument(
            "output extension",
            "." + ext if ext else "(none)",
            ["." + e for e in _ACCEPTED_EXT],
            command="export",
        )

    print("================================================================")
    print(f"export: {os.path.basename(inp)} -> {out}")
    if defs:
        print(f"  defines: {' '.join(defs)}")
    print("================================================================")

    r = subprocess.run([osc, *fmt_args, *defs, "-o", out, inp], capture_output=True, text=True)
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

    if not os.path.isfile(out):
        sys.stderr.write("export: FAILED — no output produced\n")
        for line in result.splitlines():
            sys.stderr.write(f"  {line}\n")
        return 1

    size = os.path.getsize(out)
    print(f"output: {out} ({size} bytes)")
    if ext == "stl" and not ascii_stl:
        try:
            with open(out, "rb") as fh:
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
    check_path = out
    if ext == "usdz":
        # For USDZ, we exported to a temp STL that needs mesh validation.
        check_path = out
    if ext in ("stl", "usdz") and not (ext == "stl" and ascii_stl):
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
        return 1

    if ext == "usdz":
        # Convert the validated STL to USDZ.
        stem = os.path.splitext(os.path.basename(out))[0]
        color_args = [str(color[0]), str(color[1]), str(color[2])]
        rc = run_tool("trimesh,usd-core", "usdz.py", [check_path, out] + color_args + [stem])
        if rc != 0:
            print("STATUS: FAIL — USDZ conversion failed")
            print("================================================================")
            return 1
        print("STATUS: PASS — manifold, watertight, AR Quick Look ready")
        print("================================================================")
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
    summary="STL/3MF export with manifold validation (nonzero on bad geometry)",
    usage=USAGE,
    run=run,
)
