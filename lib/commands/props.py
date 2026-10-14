"""3d props — snap-off support ribs for a part printed standing, and its stability gate.

WHAT: adds sloped snap-off ribs (with tabs and a foot strip) on the hidden back side of a
  tall thin part printed upright, and checks height / (depth at the bed + rib reach)
  against a maximum ratio. Writes the props as an extra part of the same 3MF object (or an
  STL of the props alone).

WHY: a standing wall panel, roof or rim rocks at tool changes and z-hops and can tip over
  hours into a print; the ratio gate finds the unstable part before the print starts.

The heavy work (manifold3d booleans, trimesh STL I/O) runs in lib/standing_props.py via
`cli.pyrun`; this module stays stdlib-only.
"""
from __future__ import annotations

import os

from cli.pyrun import exec_tool, run_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

DEPS = "numpy,trimesh,manifold3d"

USAGE = """3d props <part.stl|part.3mf> --back +y|-y [options]
  Add snap-off support ribs to a part printed standing (z up, thickness along y) on its
  hidden back side, and gate its stability. Ribs every ~--spacing mm along x (at least 2,
  10 mm in from each end) rise from the bed to --height of the part height; each is a
  1.6 mm plate whose inner edge stands --gap behind the hindmost point of the back and whose
  outer edge slopes down at --angle to the bed. Up to three 0.8 mm tabs per rib tie it to
  the back where the back reaches it; a 0.6 mm foot strip on the bed joins the ribs.
  Stability: height / (part depth at the bed + rib reach) <= --max-ratio, else FAIL.

Options:
  --back +y|-y       side the props go on (the hidden back); required
  --object NAME      3MF object to prop (required when the 3MF holds several objects)
  --spacing MM       rib spacing along x (default 50)
  --height F         rib height as a fraction of the part height, 0-1 (default 0.6)
  --angle DEG        slope of the rib's outer edge to the bed, (0, 89] (default 55)
  --gap MM           snap-off gap between the props and the part (default 0.25)
  --max-ratio F      stability limit for height / base depth (default 3)
  -o OUT.3mf         write the input with the props as an extra part of the object
                     (extruder = the object's largest part); other objects are kept
  -o OUT.stl         write the props alone
  --json             JSON report on stdout instead of the table

Nothing is written when the gate fails.
Exit: 0 PASS, 1 FAIL (too tall for its base), 2 usage / load error.

Examples:
  3d props wall.stl --back +y
  3d props wall.3mf --back -y -o wall_props.3mf
  3d props plate.3mf --object roof_L --back +y --json | jq '{ratio, ok}'"""

_VALUE_FLAGS = ("--back", "--object", "--spacing", "--height", "--angle", "--gap", "--max-ratio", "-o", "--output")


def _inputs(argv: list[str]) -> list[str]:
    """positional arguments (the input path)"""
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok in _VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            out.append(tok)
            i += 1
    return out


def check(path: str, back: str, *extra_args: str) -> int:
    """Callable stability gate for other commands (print preflight, hooks): run `3d props` on
    `path` with `--back back` in a subprocess with its heavy deps, report on stdout. Pass
    `--json`, `--object NAME` or `-o OUT` in extra_args. Returns 0 PASS, 1 FAIL, 2 load error."""
    if not os.path.exists(path):
        raise InputNotFound(path, command="props")
    return run_tool(DEPS, "standing_props.py", [path, "--back", back, *extra_args])


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    inputs = _inputs(argv)
    if len(inputs) != 1:
        raise UsageError("props needs exactly one part file (.stl or .3mf)", command="props")
    if "--back" not in argv and not any(a.startswith("--back=") for a in argv):
        raise UsageError("props needs --back +y or --back -y: the hidden side the ribs go on", command="props")
    if not os.path.exists(inputs[0]):
        raise InputNotFound(inputs[0], command="props")
    return exec_tool(DEPS, "standing_props.py", argv)


COMMAND = Command(
    name="props",
    group="GEOMETRY & EXPORT",
    summary="snap-off support ribs for a part printed standing + its height/base stability gate",
    usage=USAGE,
    run=run,
)
