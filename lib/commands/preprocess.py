"""3d preprocess — subject mask + proportional depth (SAM2/Depth-Anything else OpenCV)."""
from __future__ import annotations

import os

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound

USAGE = """3d preprocess <reference.jpg> [-o outdir] [options]
  Produce mask.png (subject silhouette) + depth.png (proportional depth).
  Tiers (auto): SAM2/rembg -> grabCut (mask); Depth-Anything-V2 -> pseudo-depth.
  Always writes both outputs (degrades gracefully if heavy models unavailable).

Options:
  -o, --out DIR         output dir (default: alongside the image)
  --force-fallback     skip model tiers, use OpenCV/numpy floor only
  --sam2-checkpoint P   enable SAM2 mask tier (path to a .pt checkpoint)
  --depth-model ID      HF model id (default: depth-anything/Depth-Anything-V2-Small-hf)

Example:
  3d preprocess ref.jpg -o work/
  3d preprocess ref.jpg --force-fallback"""


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

    return exec_tool("opencv-python-headless,numpy,pillow", "preprocess_reference.py", args)


COMMAND = Command(
    name="preprocess",
    group="REFERENCE-MATCH PIPELINE",
    summary="subject mask + proportional depth (SAM2/Depth-Anything, OpenCV fallback)",
    usage=USAGE,
    run=run,
)
