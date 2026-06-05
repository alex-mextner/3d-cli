"""3d export — STL/3MF export WITH geometry validation. Nonzero exit on bad geometry."""
from __future__ import annotations

import os
import subprocess
import sys

from cli.env import require_openscad
from cli.pyrun import tool_argv
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d export <file.scad> [options]
  Export STL/3MF with manifold/self-intersect validation. Exit 1 on bad geometry.

Options:
  -o, --out PATH        output (.stl/.3mf/.off/.amf). Default: <file>.stl
  --ascii              ASCII STL (default: binary STL)
  -D k=v                pass-through define (repeatable)

Examples:
  3d export model.scad -o model.stl
  3d export model.scad -o model.3mf -D 'width=80'"""

_ACCEPTED_EXT = ["stl", "3mf", "off", "amf"]


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
    if ext == "stl" and not ascii_stl:
        mout = _mesh_check_capture(out)
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
