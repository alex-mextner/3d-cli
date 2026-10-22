"""3d view-check — does a fit explain other views of the object?

Thin launcher: the algorithm is `threed_vision.view_check` (packages/threed-vision), run through `cli.vision_env`.
"""
from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d view-check <fit.json> [--view MASK:CAM.json ...] [--flag-below 0.75] [--overlay out.png] [--json]
  Render the fit with the independent cv2 rasteriser into every extra view; IoU per view, flag below the threshold,
  and `needs_views` (object-frame azimuth/elevation of useful missing photos). Without --view: single-photo sanity
  mode (canonical synthetic cameras, template priors) - status `unverified`; it cannot prove the hidden 3-D shape.
  Exit: 0 ok / unverified, 3 flagged, 2 bad input.
  Docs: docs/commands/view-check.md"""


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    return run_stage("view-check", argv)


COMMAND = Command(name="view-check", group="REFERENCE-MATCH PIPELINE", summary="IoU of a fit against extra views; flags what it does not explain", usage=USAGE, run=run)
