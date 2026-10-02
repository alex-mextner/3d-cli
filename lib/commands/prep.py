"""3d prep — repair, auto-orient, and print-ready-export a model in one pass.

WHAT: takes a .scad / .stl / .3mf model and prepares it for FDM printing:
  repair the mesh (merge vertices, drop duplicate/degenerate faces, fix winding + normals,
  fill holes), auto-orient it to the lowest-support rest orientation (scored by overhang
  area fraction over the convex-hull rest faces + the 6 axes), then write the repaired +
  oriented STL (and optionally a print-ready single-plate 3MF). Ends with a printability
  summary and a READY / NOT READY verdict.

WHY: nothing else did mesh repair, auto-orientation, or a single print-prep orchestrator.
  A non-manifold model, or one modeled resting on an overhang-heavy face, wastes filament
  and time. `prep` fixes the geometry and picks a low-support orientation before slicing.
  It does NOT generate supports, invoke a slicer, or pack multiple plates — that is
  `3d slice-check` / `3d arrange`.

Examples:
  3d prep bracket.scad
  3d prep part.stl -o part.ready.stl
  3d prep part.3mf --3mf --bed 256
  3d prep part.stl --json
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from cli.env import require_openscad
from cli.pyrun import run_tool
from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

USAGE = """3d prep <file.scad|.stl|.3mf> [options]
  Repair the mesh, auto-orient it for minimum support, and write a print-ready model.
  Reports before/after watertight+manifold state, the chosen orientation and its overhang
  improvement, a printability summary, and a READY / NOT READY verdict (exit 1 if the mesh
  is unrepairable or fails a HARD printability rule).

Options:
  -o, --out PATH        output STL path (default: <input>.prep.stl).
                        Example: 3d prep part.stl -o part.ready.stl
  --3mf                 also write a print-ready single-plate 3MF next to the STL
                        (<out-stem>.3mf). Example: 3d prep part.stl --3mf
  --bed MM              square bed size (mm) recorded in the 3MF (default: 270).
                        Example: 3d prep part.3mf --3mf --bed 256
  --json                emit a machine-readable JSON summary instead of the table.
                        Example: 3d prep part.stl --json
  -D k=v                OpenSCAD variable define (repeatable; only for a .scad input).
                        Example: 3d prep bracket.scad -D 'depth=40'

Examples:
  3d prep bracket.scad
  3d prep part.stl -o part.ready.stl
  3d prep part.3mf --3mf --bed 256
  3d prep part.stl --json"""

_MESH_EXTS = (".stl", ".3mf")
_ACCEPTED_EXTS = (".scad", ".stl", ".3mf")
# trimesh + numpy do the mesh work; networkx backs trimesh's hole filler; scipy/rtree back
# the printability ray-casts; manifold3d backs the final manifold gate (reused from
# printability_mesh / mesh_check). prep_mesh fan-fills holes if networkx is unavailable.
_DEPS = "trimesh,numpy,networkx,scipy,rtree,manifold3d"


def _parse(argv: list[str]) -> tuple[str, str, bool, float, bool, list[str]]:
    """Parse argv into (input, out, want_3mf, bed, emit_json, defines)."""
    inp = argv[0]
    rest = argv[1:]
    out = ""
    want_3mf = False
    bed = 270.0
    emit_json = False
    defs: list[str] = []
    i, n = 0, len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="prep")
            out = rest[i + 1]
            i += 2
        elif a == "--3mf":
            want_3mf = True
            i += 1
        elif a == "--bed":
            if i + 1 >= n:
                raise UsageError("option --bed needs a value", command="prep")
            bed = _parse_bed(rest[i + 1])
            i += 2
        elif a == "--json":
            emit_json = True
            i += 1
        elif a == "-D":
            if i + 1 >= n:
                raise UsageError("option -D needs a value", command="prep")
            defs += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="prep")
    return inp, out, want_3mf, bed, emit_json, defs


def _parse_bed(raw: str) -> float:
    try:
        val = float(raw)
    except ValueError:
        raise InvalidArgument("--bed", raw, ["a positive number in mm"], command="prep") from None
    if val <= 0:
        raise InvalidArgument(
            "--bed", raw, ["a positive number in mm"], command="prep",
            extra="--bed must be greater than zero.",
        )
    return val


def _default_out(inp: str) -> str:
    base = inp[:-len(".scad")] if inp.lower().endswith(".scad") else inp
    return base + ".prep.stl"


def _export_scad(inp: str, defs: list[str], work: str) -> str:
    """Render a .scad to a temp binary STL so the engine works on a real mesh."""
    osc = require_openscad("prep")
    stl = os.path.join(work, os.path.splitext(os.path.basename(inp))[0] + ".stl")
    r = subprocess.run([osc, "--export-format", "binstl", *defs, "-o", stl, inp],
                       capture_output=True, text=True)
    if not (os.path.isfile(stl) and os.path.getsize(stl) > 0):
        detail = (r.stderr or r.stdout or "").strip()
        raise GateFailure(
            f"prep: OpenSCAD produced no STL from {inp}\n  {detail}", command="prep",
        )
    return stl


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    inp, out, want_3mf, bed, emit_json, defs = _parse(argv)
    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="prep")
    ext = os.path.splitext(inp)[1].lower()
    if ext not in _ACCEPTED_EXTS:
        raise InvalidArgument(
            "<input>", inp, list(_ACCEPTED_EXTS), command="prep",
            extra="Pass a .scad, .stl, or .3mf model.",
        )
    if defs and ext != ".scad":
        raise UsageError("-D defines only apply to a .scad input", command="prep")

    out = out or _default_out(inp)
    work = ""
    try:
        mesh_input = _export_scad(inp, defs, work := tempfile.mkdtemp(prefix="3d_prep.")) \
            if ext == ".scad" else inp
        tool_args = [mesh_input, "-o", out, "--bed", repr(bed),
                     "--name", os.path.splitext(os.path.basename(inp))[0]]
        if want_3mf:
            tool_args += ["--3mf", os.path.splitext(out)[0] + ".3mf"]
        if emit_json:
            tool_args.append("--json")
        return run_tool(_DEPS, "prep_mesh.py", tool_args)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


COMMAND = Command(
    name="prep",
    group="GEOMETRY & EXPORT",
    summary="repair + auto-orient + print-ready export a model (STL/3MF) with a printability verdict",
    usage=USAGE,
    run=run,
)
