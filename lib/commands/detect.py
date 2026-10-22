"""3d detect — open-vocabulary object boxes for text queries (OWLv2).

WHAT: finds boxes for comma-separated text queries ("a hand tool,a box") in a photo and prints
  {"boxes": [{"xyxy", "score", "label"}], "timing_s"}; `--masks DIR` adds a SAM2 mask per box.

WHY: text-prompted boxes drive `3d segment`'s fill stage and let tools be found by name. The algorithm lives in
  packages/threed-vision (`threed_vision.detect.OwlDetector`); this command is a thin launcher.

Examples:
  3d detect tools.jpg --queries "a hand tool,a box" --json
  3d detect tools.jpg --queries pliers --threshold 0.25 --nms 0.3
"""
from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d detect <image> --queries "q1,q2" [options]
  Open-vocabulary boxes (OWLv2). Needs the vision venv and the owlv2-base model (`3d vision-models pull owlv2-base`).

Options:
  --queries "a,b,c"        comma-separated text queries (required)
  --backend owlv2          detector backend (only owlv2)
  --threshold F            minimum score (default 0.15)
  --nms F                  IoU above which the lower-scored box is dropped (default 0.5)
  --max-area-frac F        drop boxes larger than this image fraction (default 0.05)
  --masks DIR              also write SAM2 box-prompted masks: DIR/instances.json (segment schema + label/score), masks/NNN.png
  --device auto|cuda|cpu   auto = cuda only with enough free VRAM (default auto)
  --json                   print exactly one JSON object

Exit: 0 ok, 2 bad input, 127 missing model/dependency.

Examples:
  3d detect tools.jpg --queries "a hand tool,a box" --json
  3d detect tools.jpg --queries pliers --threshold 0.25 --nms 0.3 --json
  3d detect tools.jpg --queries "a tape roll" --max-area-frac 0.02 --device cpu
  3d detect tools.jpg --queries "a hand tool" --masks out/ --json     # boxes + one mask per box"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("detect", argv, heavy=True)


COMMAND = Command(
    name="detect",
    group="REFERENCE-MATCH PIPELINE",
    summary="open-vocabulary object boxes for text queries (OWLv2; threed-vision)",
    usage=USAGE,
    run=run,
)
