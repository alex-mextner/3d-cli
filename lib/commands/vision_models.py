"""3d vision-models — vision model weights: status and explicit, verified, idempotent pull.

WHAT: lists the weights the photo->3D vision stages need (SAM2.1-tiny, OWLv2, CLIP, DINOv2, u2net) with
  sizes and licences, and fetches them on request. Existing local copies are imported (hard link) instead of
  downloaded; every file is sha256-verified; downloads are atomic.

WHY: the vision stages never download at request time (offline-first). A missing model exits 127 naming
  the exact `3d vision-models pull <name>` to run. Implemented in `threed_vision.models`.

Examples:
  3d vision-models status
  3d vision-models pull sam2-tiny owlv2-base
"""
from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d vision-models status [--verify] [--json]
3d vision-models pull [name ...] [--json]
  status   per-model install state, size, licence (--verify hashes every file now; cached afterwards)
  pull     make models present: import a local copy when one exists, otherwise download + verify (explicit, idempotent, atomic)
  Models: sam2-tiny, owlv2-base, clip-b32, dinov2-s, u2net. Cache dir: $THREED_VISION_MODELS_DIR (default ~/.cache/3d-cli/vision/models).

Examples:
  3d vision-models status
  3d vision-models status --verify --json
  3d vision-models pull                       # everything missing
  3d vision-models pull sam2-tiny owlv2-base --json"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("vision-models", argv, heavy=False)


COMMAND = Command(
    name="vision-models",
    group="ENVIRONMENT",
    summary="vision model weights: status / explicit verified pull (threed-vision)",
    usage=USAGE,
    run=run,
)
