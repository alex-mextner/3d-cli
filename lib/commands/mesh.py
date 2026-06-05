"""3d mesh — watertight / manifold / self-intersection / volume report (lib/mesh_check.py)."""
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
