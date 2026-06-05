"""3d mesh — watertight / manifold / self-intersection / volume report.

WHAT: runs a geometric health check on a mesh or .scad file, reporting whether it is
  watertight (closed), manifold (no holes), self-intersection-free, and its volume.

WHY: every slicer and every downstream gate (printability, collision, pack) requires a
  clean manifold mesh. `mesh` is the surgical primitive that exposes the raw geometry
  facts before the umbrella gates (`3d check`) bundle them into a verdict.

Examples:
  3d mesh part.stl                    # full mesh report on an STL
  3d mesh model.scad -D 'width=80'    # export + check at a specific parameter
  3d check part.scad --mesh           # same gate, run through the umbrella command

ROADMAP §3: "mesh — watertight/manifold/self-intersect/volume report.
  Engine tiers: trimesh+open3d (full) -> trimesh+manifold3d -> openscad warning grep.
  Exit 0 = PASS, 1 = FAIL (geometric defect), 2 = load/usage error."
"""
from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command

USAGE = """3d mesh <file.stl|.3mf|.scad> [-D k=v ...]
  Report watertight / manifold / self-intersection / volume.
  Engine tiers: trimesh+open3d (full) -> trimesh+manifold3d -> openscad warning grep.
  Exit 0 = PASS, 1 = FAIL (geometric defect), 2 = load/usage error.

Example:
  3d mesh part.stl
  3d mesh model.scad -D 'width=80'"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return exec_tool("trimesh,manifold3d,numpy,scipy,rtree", "mesh_check.py", argv)


COMMAND = Command(
    name="mesh",
    group="QA & GATES",
    summary="watertight/manifold/self-intersect/volume (trimesh/manifold3d/open3d)",
    usage=USAGE,
    run=run,
)
