"""3d fit-silhouette — fit lathe/flat/box params + pose to mask views with known cameras.

Thin launcher: the algorithm is `threed_vision.fit` (packages/threed-vision), run through `cli.vision_env`.
Closed form first; the torch soft-silhouette refiner (and CMA-ES) only run when the closed-form model does not explain the views.
"""
from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d fit-silhouette --view MASK:CAM.json [--view ...] [--heldout MASK:CAM.json] [options] -o DIR [--json]
  Fit a lathe / flat loft / box to calibrated silhouettes. CAM.json = {"K":[[..]],"R":[[..]],"t":[..]} (world->camera,
  mm, OpenCV) or {"K":[[..]],"pose":"identity"}. Writes DIR/fit.json, overlay-N.png and, with --emit, model.<ext>.
Options:
  --template auto|lathe|flat|box   auto = by closed-form kind (box p=8, cylinder/lathe/ring p=2, flat loft p=4)
  --planar --known-depth MM        top-down object on a plane parallel to the image / depth for a single view
  --fixed-length-mm / --fixed-width-mm / --fixed-thickness-mm   hard overrides (exact in the result)
  --heldout MASK:CAM.json          scored, never fitted -> confidence.heldout_iou
  --iou-accept 0.9  --min-confidence 0.8  --max-tries 3  --stations 8  --seed 0  --no-refine  --no-cma
  --device auto|cuda|cpu           refiner device (auto = GPU with CPU fallback)
  --emit glb|stl|obj|scad  --mesh-frame object|world
Exit: 0 ok / single_view, 3 low confidence, 2 bad input, 127 missing torch/cma/trimesh.
Docs: docs/commands/fit-silhouette.md"""


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    return run_stage("fit-silhouette", argv)


COMMAND = Command(name="fit-silhouette", group="REFERENCE-MATCH PIPELINE", summary="fit lathe/flat/box params + pose to calibrated mask views", usage=USAGE, run=run)
