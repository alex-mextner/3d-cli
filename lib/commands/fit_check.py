"""3d fit-check — does the socket still fit an insert that is already printed?

WHAT: given an insert mesh (several are unioned), the socket part(s) it goes into — both in
  the assembly frame — its printed size and the designed gap per side, checks that the
  insert's bounding box still equals the printed size, and, slicing every --step mm up the
  insert, that it never overlaps the socket and the clearance stays at the gap.

WHY: parts of one assembly get printed days apart, on different printers, while the
  generator keeps being rebuilt; a rebuild that moves or resizes the socket of an insert
  already in plastic only shows at assembly — after the big mating part was printed.

The heavy work (trimesh/manifold3d slicing, shapely distances) runs in lib/fit_check.py via
`cli.pyrun`; this module stays stdlib-only.
"""
from __future__ import annotations

import glob
import os

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d fit-check --insert A.stl[,B.stl] --socket S1.stl[,S2.stl] --gap MM [options]
3d fit-check --insert A.stl --socket S.stl --fit snap [--shape round] [--printer k1se --material PETG] [options]
3d fit-check --spec fits.yaml [--json]
  Check an insert that is already printed against its socket, both in the assembly frame:
    size    the insert's bounding box equals --printed-size within --size-tol
    socket  slicing every --step mm from the insert's bottom to its top: no overlap with
            the socket (the solids are intersected too, so nothing hides between
            layers), and wherever socket material lies within 2*gap+1 mm of the
            insert the clearance stays at --gap +- --gap-tol
    depth   reported; FAIL below --min-depth
    open    the insert can go in: moved out along its insertion direction (--open
            above, default; below) it meets no socket material — a roof over the
            socket or a shoulder over a wider foot (undercut) FAILs; --open none for an
            insert captured between parts
  No socket around the insert = FAIL. Comma-join, repeat or glob the parts of one side.

Options:
  --insert A[,B]        insert mesh(es), unioned (STL/3MF/OBJ; globs ok; repeatable)
  --socket S1[,S2]      socket part(s), unioned (globs ok, quote them; repeatable)
  --gap MM              designed clearance per side (tightest side); with --fit it overrides the number
                        taken from the fit table
  --fit TYPE            fit type of the shared fit table (`3d fits list`): snap, sliding, tight, glue or an
                        alias (free_running, snug, normal, loose). The gap and --gap-tol come from
                        `3d fits resolve` instead of literals; `press` (interference) needs an explicit --gap
  --shape SHAPE         feature shape for --fit: planar (default), round, slot, pin, hinge
  --printer NAME        printer whose correction rows apply to --fit, e.g. k1se (unknown name = table default)
  --material NAME       material whose correction rows apply to --fit, e.g. PETG
  --nozzle MM           nozzle diameter for the correction rows, e.g. 0.4
  --layer MM            layer height for the correction rows, e.g. 0.2
  --printed-size X,Y,Z  size of the printed insert, mm; omitted = size not checked
  --any-turn            accept the printed size in any axis order (a turned insert)
  --name NAME           insert name in the report (default: insert file names)
  --size-tol MM         printed-size tolerance (default 0.02)
  --gap-tol MM          clearance tolerance (default 0.03; with --fit the tolerance of the fit type)
  --step MM             slice step up the socket (default 0.25)
  --min-depth MM        FAIL when the socket is shallower (default: not checked)
  --open DIR            insertion direction the socket must be open to: above
                        (default), below, or none (captured insert, not checked)
  --spec FILE           YAML/JSON with many fits (see docs/commands/fit-check.md);
                        paths relative to the spec file
  --json                JSON report on stdout instead of the text lines

Exit: 0 PASS, 1 FAIL, 2 usage / spec / load error.

Examples:
  3d fit-check --insert out/assembly/PFL__DOOR.stl --socket 'out/assembly/BASE2__*.stl' --gap 0.2 --printed-size 46.275,75.446,15.311
  3d fit-check --insert KEY1.stl --socket BASE1.stl,BASE2.stl --gap 0.15 --printed-size 28,14,4.3 --any-turn --name KEY1
  3d fit-check --insert plug.stl --socket plate.stl --gap 0.2 --gap-tol 0.05 --size-tol 0.05 --step 0.1 --min-depth 3.5
  3d fit-check --insert magnet.stl --socket 'HALF_*.stl' --gap 0.1 --open none
  3d fit-check --insert foot.stl --socket base.stl --gap 0.2 --open below
  3d fit-check --insert plug.stl --socket plate.stl --fit sliding --shape round --printer k1se --material PETG
  3d fit-check --insert lid.stl --socket box.stl --fit snap --gap-tol 0.05
  3d fit-check --spec fits.yaml
  3d fit-check --spec fits.yaml --json | jq '.fits[] | {insert, status, gap_mm, socket_depth_mm}'"""

DEPS = "numpy,trimesh,manifold3d,shapely,pyyaml"
_PATH_FLAGS = ("--insert", "--socket", "--spec")


def _path_values(argv: list[str]) -> dict[str, list[str]]:
    """values of the path flags, `--flag value` and `--flag=value` alike"""
    out: dict[str, list[str]] = {}
    for i, tok in enumerate(argv):
        flag, eq, value = tok.partition("=")
        if flag not in _PATH_FLAGS:
            continue
        if not eq:
            if i + 1 >= len(argv):
                continue                     # argparse reports the missing value
            value = argv[i + 1]
        out.setdefault(flag, []).append(value)
    return out


def _require_existing(values: dict[str, list[str]]) -> None:
    """literal (non-glob) paths named on the command line must exist"""
    for flag, specs in values.items():
        for spec in specs:
            for p in [spec] if flag == "--spec" else spec.split(","):
                p = p.strip()
                if p and not glob.has_magic(p) and not os.path.exists(p):
                    raise InputNotFound(p, command="fit-check")


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    values = _path_values(argv)
    if "--spec" not in values and not ("--insert" in values and "--socket" in values):
        raise UsageError("fit-check needs --insert A.stl --socket S.stl --gap MM (or --fit TYPE), or --spec fits.yaml",
                         command="fit-check")
    _require_existing(values)
    return exec_tool(DEPS, "fit_check.py", argv)


COMMAND = Command(
    name="fit-check",
    group="QA & GATES",
    summary="printed insert vs its socket: locked printed size, no overlap, gap per side all the way up",
    usage=USAGE,
    run=run,
)
