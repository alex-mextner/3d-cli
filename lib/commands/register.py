"""3d register — register two photos of one scene: homography, relative pose, mask warping, repeatability.

WHAT: SIFT features + RANSAC give the plane-induced homography A -> B; it is decomposed into the relative camera
  rotation/translation (`baseline_ok` says whether photo B adds any parallax), masks of A are warped into B and, when B
  masks are given, scored per instance against the best-overlapping B mask (repeatability IoU; unstable segmentations
  show up here). Numpy + OpenCV only.

WHY: a second photo is the held-out view of the multi-view check, and a photo at the SAME viewpoint (rotation < 3 degrees,
  no translation) must not be mistaken for one. The algorithm lives in packages/threed-vision
  (`threed_vision.register.register`); this command is a thin launcher.

Examples:
  3d register a.jpg b.jpg --json
  3d register a.jpg b.jpg --mask-a segA/instances.json --mask-b segB/instances.json -o reg/
"""

from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d register <a.jpg> <b.jpg> [--mask-a MASKS] [--mask-b MASKS] [options]
  SIFT + RANSAC homography A -> B, decomposed relative pose, mask warp and repeatability IoU (numpy + OpenCV, no model).
  --json: {"ok", "matches", "inliers", "median_reproj_px", "H_a_to_b", "rel_R", "rel_rot_deg", "t_over_depth",
           "translation_ratio", "baseline_ok", "quality", "repeatability": [{"index","iou","best_b_index","unstable"}], ...}

Options:
  --mask-a MASKS       masks in A to warp into B: instances.json (from `3d segment`), a directory of *.png, or one PNG
  --mask-b MASKS       masks segmented in B (same formats): each warped A mask is scored by IoU with its best B mask
  --f-px-a N, --f-px-b N   focal length of each photo in px (default 0.375 * image diagonal)
  --plane-depth-mm D   distance of the plane from camera A, scales t_over_depth to t_mm
  --flag-below IOU     repeatability IoU below this marks a mask `unstable` (default 0.75)
  -o, --out DIR        write DIR/registration.json and DIR/warped/NNN.png (A masks in B)
  --json               print exactly one JSON object

baseline_ok is false when the relative rotation is < 3 degrees AND the translation is < 5 % of the plane depth: B is then
(nearly) the same viewpoint (measured on the carpet pair: 1853 matches, rotation 0.94 degrees) and adds no parallax.
The homography is exact only for one plane; tall objects are approximate.

Exit: 0 ok, 2 bad input, 3 photos could not be registered.

Examples:
  3d register a.jpg b.jpg --json
  3d register a.jpg b.jpg --mask-a segA/instances.json --mask-b segB/instances.json -o reg/"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("register", argv)


COMMAND = Command(
    name="register",
    group="REFERENCE-MATCH PIPELINE",
    summary="register two photos (SIFT + RANSAC): relative pose, baseline_ok, mask warp A->B, repeatability IoU",
    usage=USAGE,
    run=run,
)
