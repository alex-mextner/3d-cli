"""3d collision — generic collision/penetration engine (static / --frame / --viz).

WHAT: checks whether parts in an assembly overlap or penetrate each other, either at
  static poses (every pair at every phase), per-frame over a motion timeline, or as a
  visualization with overlaps highlighted in red.

WHY: an assembly can look correct from the outside but have internal parts clipping
  through each other — especially after parameter changes in a match loop. The collision
  gate catches those penetrations before they become a real mechanical failure.

Examples:
  3d collision project/verify/collision.json     # static gate, all pairs
  3d collision project/verify/collision.json --frame   # per-frame over motion
  3d collision project/verify/collision.json --viz       # render with overlaps red

ROADMAP §3: "collision (static / --frame / --viz). config.json supplies: pair_scad,
  parts, phases, intended contacts, eps_mm3 / touch_tol_mm / contact_max_mm3 thresholds.
  All paths in the config are resolved RELATIVE TO THE CONFIG FILE'S DIRECTORY."
"""
from __future__ import annotations

import os

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d collision <config.json> [--frame] [--viz]
  Run the generic collision/penetration engine over a project config.

  config.json supplies: pair_scad (placement), parts, phases, intended contacts,
  eps_mm3 / touch_tol_mm / contact_max_mm3 thresholds. All paths in the config are
  resolved RELATIVE TO THE CONFIG FILE'S DIRECTORY.

Modes:
  (default)   static gate: every pair at every phase (collision_check.py)
  --frame     per-frame gate over the config's timeline (frame_check.py)
  --viz       render each phase with overlaps highlighted red (collision_viz.py)

Env: PHASES_SEL="0,1" (static) / FRAMES=N (frame) override the config.

Example:
  3d collision project/verify/collision.json
  3d collision project/verify/collision.json --frame"""

_MODES = {
    "static": ("collision_check.py", "trimesh,manifold3d,rtree,numpy,scipy"),
    "frame": ("frame_check.py", "trimesh,manifold3d,rtree,numpy,scipy"),
    "viz": ("collision_viz.py", "trimesh,manifold3d,rtree,numpy,scipy,pyvista"),
}


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cfg = argv[0]
    if not os.path.isfile(cfg):
        raise InputNotFound(cfg, command="collision")

    mode = "static"
    for a in argv[1:]:
        if a == "--frame":
            mode = "frame"
        elif a == "--viz":
            mode = "viz"
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="collision")

    script, deps = _MODES[mode]
    return exec_tool(deps, script, [cfg])


COMMAND = Command(
    name="collision",
    group="QA & GATES",
    summary="collision/penetration engine (static / --frame / --viz)",
    usage=USAGE,
    run=run,
)
