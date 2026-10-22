"""3d segment — multi-object instance masks from one photo (SAM2.1-tiny + OWLv2 fill + CLIP background rejection).

WHAT: splits a photo of many objects (tools on a carpet) into per-object masks: SAM2 automatic masks,
  OWLv2 boxes to fill what SAM2 missed, CLIP to drop carpet/floor patches. Writes instances.json,
  labels.png (pixel value = instance id), masks/NNN.png and overlay.jpg.

WHY: every downstream photo->3D stage (recognise, scale, fit) starts from per-object masks. The algorithm
  lives in packages/threed-vision (`threed_vision.segment.Segmenter`); this command is a thin launcher.

Examples:
  3d segment tools.jpg -o seg/ --json
  3d segment one-tool.jpg -o seg/ --method u2net
  3d segment tools.heic -o seg/ --device cpu --max-side 1024
"""
from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d segment <image> -o DIR [options]
  Instance masks for every object in a photo. Needs the vision venv and models (`3d vision-models pull`).
  Writes DIR/instances.json, labels.png (uint16, value = instance id), masks/NNN.png, overlay.jpg.

Options:
  -o, --out DIR            output directory (required)
  --method sam2|sam2-dense|u2net   sam2 = AMG grid (default), sam2-dense = 64x64 grid, u2net = one salient object
  --fill owlv2|none        OWLv2 boxes + SAM2 box prompts fill what the AMG missed (default owlv2)
  --queries "a,b,c"        OWLv2 queries (default: hand tool, power tool, box, tube of glue, tape roll, pliers, screwdriver)
  --reject-bg | --no-reject-bg   CLIP background rejection (default on)
  --points-per-side N      AMG grid per side (default 32 on GPU; on CPU 0 = OWLv2 boxes only, or 16 with --fill none)
  --max-side PX            downscale the longest side before SAM2, 0 = never (default none on GPU, 1024 on CPU)
  --min-area PX            drop masks below this many pixels (default 150)
  --max-area-frac F        drop masks above this image fraction (default 0.03)
  --device auto|cuda|cpu   auto = cuda only with enough free VRAM (default auto)
  --json                   print exactly one JSON object (the instances.json content)

Exit: 0 ok, 2 bad input, 127 missing model/dependency (names the `3d vision-models pull` command).

Examples:
  3d segment tools.jpg -o seg/ --json
  3d segment tools.jpg -o seg/ --fill none --no-reject-bg
  3d segment one-tool.jpg -o seg/ --method u2net
  3d segment tools.heic -o seg/ --device cpu --max-side 1024
  3d segment tools.jpg -o seg/ --queries "a hand tool,a box" --points-per-side 24
  3d segment tools.jpg -o seg/ --min-area 400 --max-area-frac 0.05   # drop specks, allow larger objects
  3d segment tools.jpg -o seg/ --reject-bg --device cuda             # explicit GPU (errors instead of falling back)"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("segment", argv, heavy=True)


COMMAND = Command(
    name="segment",
    group="REFERENCE-MATCH PIPELINE",
    summary="split a photo into object instance masks (SAM2 + OWLv2 + CLIP; threed-vision)",
    usage=USAGE,
    run=run,
)
