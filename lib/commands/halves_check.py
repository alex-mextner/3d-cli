"""3d halves-check — do the two halves of a part split for printing still fit together?

WHAT: for every pair of halves marked in a 3MF (object metadata `halves_pair`,
  `halves_side`, `halves_to_assembly`, `halves_cut`, …) or given as STL files, brings both
  halves into the assembly frame and compares their cut faces: outline deviation and IoU,
  dowel holes (count, centres, diameters), face size, unmatched islands, and whether each
  half stays on its own side of the cut. Optional PNG overlay of both faces per pair.

WHY: halves generated independently (one half's own projection as its cut face, islands
  dropped per half, a mirrored pose missed) print fine one by one and only show the
  mismatch when glued. A per-part mesh gate cannot see it; this gate can, before printing.

The heavy work (numpy/scipy raster comparison, 3MF/STL loading) runs in lib/halves_check.py
via `cli.pyrun`; this module stays stdlib-only.
"""
from __future__ import annotations

import os

from cli.pyrun import exec_tool, run_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d halves-check <file.3mf|dir> [...] [options]
3d halves-check --pair A.stl[,A2.stl] B.stl[,B2.stl] [--plane auto|y=V|'nx ny nz d'] [options]
  Check paired halves of a split part: bring both halves into the assembly frame and
  compare their cut faces. 3MF pairs are marked per object (Orca model_settings.config
  <object> metadata, or <metadata name=…> in the 3dmodel.model object):
    halves_pair         pair id shared by both halves (e.g. TA); none = not a half
    halves_side         1 (material on n·p >= d) or 2 (n·p <= d)
    halves_to_assembly  16 numbers, row-major 4x4: object LOCAL coords -> assembly (mm)
    halves_local_min    'x y z' local bbox min when written (undoes slicer re-centring)
    halves_cut          'nx ny nz d': the cut plane in the assembly frame
    halves_dowels       optional: dowel holes each face must have
  STL pairs must share the assembly frame; comma-join colour parts of one half.
  Objects named X1 + X2 without metadata are reported as unmarked halves (WARN).

Options:
  --pair A B          an STL pair (repeatable); A = side 1
  --plane SPEC        cut plane for --pair: auto (largest shared flat face, default),
                      x=V / y=V / z=V, or 'nx ny nz d'
  --tol MM            max outline deviation, dowel offset/diameter, size (default 0.2)
  --res MM            raster pixel for the face comparison (default 0.05)
  --min-iou F         minimum cut-face IoU (default 0.9)
  --png DIR           write <file>__<pair>.png overlays (grey both, red half 1 only,
                      blue half 2 only)
  --json              JSON report on stdout instead of the table
  --strict-names      unmarked X1/X2 objects FAIL instead of WARN
  --require-pairs     FAIL when no marked pair is found

Exit: 0 PASS, 1 FAIL, 2 usage / load / metadata error.

Examples:
  3d halves-check print/u1_trees.3mf
  3d halves-check print/ --png out/halves/
  3d halves-check --pair out/assembly/TA1__TREE_L.stl,out/assembly/TA1__TRUNK.stl out/assembly/TA2__TREE_L.stl,out/assembly/TA2__TRUNK.stl --plane y=146.3
  3d halves-check plate.3mf --json | jq '.pairs[] | {pair, status, max_dev_mm}'"""

_VALUE_FLAGS = ("--plane", "--tol", "--res", "--min-iou", "--png")


def _paths(argv: list[str]) -> list[str]:
    """every input path named on the command line (positional files/dirs and --pair members)"""
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--pair":
            out += [p for spec in argv[i + 1:i + 3] for p in spec.split(",")]
            i += 3
        elif tok in _VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            out.append(tok)
            i += 1
    return out


def _require_existing(paths: list[str]) -> None:
    for p in paths:
        if not os.path.exists(p):
            raise InputNotFound(p, command="halves-check")


def check(paths: list[str], *extra_args: str) -> int:
    """Callable gate for other commands (print preflight, hooks): run the check on `paths`
    (.3mf files / directories) in a subprocess with its heavy deps, table on stdout.
    Returns 0 PASS, 1 FAIL, 2 load/metadata error. Use `--json` in extra_args for JSON."""
    _require_existing(paths)
    return run_tool("numpy,scipy,trimesh", "halves_check.py", [*paths, *extra_args])


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    paths = _paths(argv)
    if not paths:
        raise UsageError("halves-check needs a .3mf file, a directory, or --pair A.stl B.stl", command="halves-check")
    _require_existing(paths)
    return exec_tool("numpy,scipy,trimesh", "halves_check.py", argv)


COMMAND = Command(
    name="halves-check",
    group="QA & GATES",
    summary="paired halves of a split part: cut faces, dowels, size, islands match (3MF metadata or STL pair)",
    usage=USAGE,
    run=run,
)
