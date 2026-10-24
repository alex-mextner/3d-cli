"""3d preprocess — subject mask + proportional depth (SAM2/Depth-Anything else OpenCV).

WHAT: takes a reference photo and produces two outputs: a clean binary mask (white
  subject, black background) and a proportional depth map (lighter = closer, darker =
  farther), using the best available tier and degrading gracefully.

WHY: the match pipeline and score gate need a clean reference silhouette to compare
  against. A raw photo with sky, ground, or clutter produces a garbage mask. Preprocessing
  isolates the subject first — via SAM2, Depth-Anything, or OpenCV fallback — so every
  downstream metric operates on a real silhouette rather than background noise.

Examples:
  3d preprocess ref.jpg -o work/          # mask + depth into work/
  3d preprocess ref.jpg --force-fallback    # skip model tiers, use OpenCV only
  3d preprocess ref.jpg --sam2-checkpoint sam2.pt   # enable SAM2 tier

ROADMAP §13.2: "Reference silhouette pre-pass: one prompt-click on the reference photo
  → a clean binary subject mask via SAM2 (+ optional per-feature sub-masks), normalised
  to the render frame; falls back to Depth-Anything + GrabCut when SAM2 is unavailable.
  Why: a clean reference silhouette is the foundation of the whole metric."
"""
from __future__ import annotations

import os

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound

USAGE = """3d preprocess <reference.jpg> [-o outdir] [options]
  Produce mask.png (subject silhouette) + depth.png (proportional depth).
  Tiers (auto): SAM2/rembg -> grabCut (mask); Depth-Anything-V2 -> pseudo-depth.
  Always writes both outputs (degrades gracefully if heavy models unavailable).
  Prints mask coverage, bbox_xywh, and centroid_xy so segmentation quality is inspectable.

Options:
  -o, --out DIR         output dir (default: alongside the image)
  --force-fallback     skip model tiers, use OpenCV/numpy floor only
  --sam2-checkpoint P   enable SAM2 mask tier (path to a .pt checkpoint)
  --depth-model ID      HF model id (default: depth-anything/Depth-Anything-V2-Small-hf)
  --backdrop            studio render / product shot on a smooth neutral backdrop: the mask is everything that
                        is not the backdrop (keeps black, white and pink parts that grabCut drops; a floor
                        shadow stays backdrop). Tier "backdrop"; skips SAM2/rembg/grabCut
  --backdrop-saturation S   --backdrop: highest HSV saturation of a backdrop pixel (default 0.14)
  --backdrop-value LO,HI    --backdrop: HSV value range of a backdrop pixel (default 0.38,0.80)

Example:
  3d preprocess ref.jpg -o work/
  3d preprocess ref.jpg --force-fallback
  3d preprocess studio_render.png --backdrop -o work/
  3d preprocess studio_render.png --backdrop --backdrop-saturation 0.2 --backdrop-value 0.3,0.9"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    img = argv[0]
    if not os.path.isfile(img):
        raise InputNotFound(img, command="preprocess")

    # translate -o/--out -> --out-dir (the tool's flag); pass everything else through.
    args: list[str] = [img]
    rest = argv[1:]
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            if i + 1 < n:
                args += ["--out-dir", rest[i + 1]]
                i += 2
            else:
                i += 1
        else:
            args.append(a)
            i += 1

    deps = "opencv-python-headless,numpy,pillow"
    if "--backdrop" in argv:
        deps += ",scipy"  # the backdrop mask uses scipy.ndimage
    return exec_tool(deps, "preprocess_reference.py", args)


COMMAND = Command(
    name="preprocess",
    group="REFERENCE-MATCH PIPELINE",
    summary="subject mask + proportional depth (SAM2/Depth-Anything, OpenCV fallback)",
    usage=USAGE,
    run=run,
)
