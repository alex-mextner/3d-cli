"""3d visible-faces — the visible side of a decorative part must print with no support and no sag.

WHAT: the user declares the visible side (a half-space of the print frame such as `y<4.8`,
  optionally minus hidden (u, z) polygons, with openings whose short bridges are allowed).
  Mesh pass: slice the union of the part's volumes every layer and list every piece on the
  visible side that the layer below does not carry (steeper than 45° or a bridge). G-code
  pass: list Support extrusion above the raft on the visible side and every layer island
  that steps more than 0.35 mm out past the layer below on that side.

WHY: a global overhang fraction cannot tell a hidden back overhang from a sag on the face
  everyone looks at; a dropped wall sliver under a relief only shows in the actual slice.

The heavy work (manifold3d slicing, scipy raster) runs in lib/visible_faces.py via
`cli.pyrun`; this module stays stdlib-only.
"""
from __future__ import annotations

import os

from cli.pyrun import exec_tool, run_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

DEPS = "numpy,scipy,trimesh,manifold3d"

USAGE = """3d visible-faces PART.stl[,VOLUME2.stl...]|PLATE.3mf [...] --visible SPEC [options]
3d visible-faces --gcode PLATE.gcode --visible [OBJ:]SPEC [options]
  The visible side of a part must print clean: no support, no sag.
  Mesh pass (STL/3MF): every layer, a piece on the visible side not carried by the layer
  below grown by layer x tan45 + 0.1 mm, over --min-area and wider than 0.1 mm, FAILS.
  G-code pass: Support extrusion above the raft on the visible side FAILS; a layer island
  more than 0.35 mm outside the layer below that reaches the outer 0.6 mm skin FAILS.
  Inside a declared opening a bridge with both ends carried and span <= --bar-span passes.

Options:
  --visible [OBJ:]SPEC     visible side: x<V, x>V, y<V, y>V (mm, print/bed frame) or all;
                           repeatable. OBJ (glob) binds it to a 3MF object / G-code object
                           label (also matched without Orca's _id_N_copy_M suffix); a bare
                           SPEC applies to every part/object without its own
  --hidden [OBJ:]FILE      JSON [[[u, z], ...], ...]: polygons hidden from view (u = the
                           other bed axis: x for y<V/all, y for x<V)
  --openings [OBJ:]FILE    JSON [[u0, u1, z0, z1], ...]: openings (windows) where short
                           bridges are allowed
  --gcode PLATE.gcode      also check the sliced plate (objects from EXCLUDE_OBJECT_START or
                           Bambu `; start printing object` labels; a file without them is one
                           object named `plate`)
  --layer MM               layer height (default 0.12)
  --min-area MM2           smallest piece / island reported (default 0.5)
  --bar-span MM            longest bridge allowed in an opening (default 10)
  --json                   JSON report on stdout instead of text

Comma-join the colour volumes of one part (a.stl,b.stl): they are unioned, so the
interfaces between colours are not surfaces. A 3MF is checked per build item in bed
coordinates, its components unioned.

Exit: 0 PASS, 1 FAIL, 2 usage / load error.

Examples:
  3d visible-faces wall_red.stl,wall_white.stl --visible 'y<4.8'
  3d visible-faces wall.stl --visible 'y<4.8' --openings windows.json --hidden covered.json
  3d visible-faces plate.3mf --visible 'WALL*:y<104.8' --visible 'ROOF:all'
  3d visible-faces --gcode plate_1.gcode --visible 'WALL1:y<104.8' --json | jq .status"""

_VALUE_FLAGS = ("--visible", "--hidden", "--openings", "--gcode", "--layer", "--min-area", "--bar-span")


def _input_paths(argv: list[str]) -> list[str]:
    """part files (comma groups split) and the G-code named on the command line"""
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--gcode" and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
        elif tok.startswith("--gcode="):
            out.append(tok.split("=", 1)[1])
            i += 1
        elif tok in _VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            out += [p for p in tok.split(",") if p]
            i += 1
    return out


def _require_existing(paths: list[str]) -> None:
    for p in paths:
        if not os.path.exists(p):
            raise InputNotFound(p, command="visible-faces")


def check(args: list[str]) -> int:
    """Callable gate for other commands (print preflight, hooks): run the check with the
    `3d visible-faces` arguments in a subprocess with its heavy deps; report on stdout.
    Returns 0 PASS, 1 FAIL, 2 usage / load error; raises InputNotFound when a named part or
    G-code file does not exist. Add `--json` for a JSON report."""
    _require_existing(_input_paths(args))
    return run_tool(DEPS, "visible_faces.py", args)


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not any(a == "--visible" or a.startswith("--visible=") for a in argv):
        raise UsageError("visible-faces needs --visible SPEC (e.g. 'y<4.8', 'WALL:x>2', 'all')",
                         command="visible-faces")
    paths = _input_paths(argv)
    if not paths:
        raise UsageError("visible-faces needs a part (STL/3MF) or --gcode PLATE.gcode", command="visible-faces")
    _require_existing(paths)
    return exec_tool(DEPS, "visible_faces.py", argv)


COMMAND = Command(
    name="visible-faces",
    group="QA & GATES",
    summary="visible side of a part prints clean: no unsupported layer piece or support (mesh + G-code)",
    usage=USAGE,
    run=run,
)
