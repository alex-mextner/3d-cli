"""3d fit-profile — closed-form length/width profile + shape kind of a mask (no optimiser).

Thin launcher: the algorithm is `threed_vision.fit_profile` (packages/threed-vision), run through `cli.vision_env`.
"""
from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d fit-profile <mask.png|instances.json#ID> --mm-per-px X [--k 12] [--nbins 96] [--axis-deg A] [--json]
  Closed-form oriented rectangle + width profile of one silhouette (about 1 ms, no optimiser):
  length, max width, per-station lateral extents, a shape kind (box / cylinder_capsule / lathe_profile / loft_flat /
  ring_lathe) and the IoU of the profile against the mask.
  Example: 3d fit-profile masks/046.png --mm-per-px 1.263 --json | jq '.kind, .length_mm'
  Docs: docs/commands/fit-profile.md"""


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    return run_stage("fit-profile", argv)


COMMAND = Command(name="fit-profile", group="REFERENCE-MATCH PIPELINE", summary="closed-form length/width profile + kind of a mask", usage=USAGE, run=run)
