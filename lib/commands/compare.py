"""3d compare — reliable model<->reference comparison (segmented IoU + SSIM/DSSIM).

WHAT: segments a reference photo into a clean subject mask, fits the camera against
  that mask, then scores and collages the render against the masked subject — producing
  IoU, SSIM, DSSIM, and diagnostic artifacts.

WHY: raw thresholding of a cluttered photo (sky, ground, adjacent buildings) produces
  a garbage mask and meaningless IoU (0.7 reported while the building clearly does
  not match). `compare` uses grabCut segmentation first, rejects degenerate camera fits,
  then scores — so the number actually means something.

Examples:
  3d compare model.scad photo.jpg -o match/
  3d compare render.png photo.jpg          # already-rendered image, no camera fit
  3d compare model.scad photo.jpg --rand 8 --refine 3   # quick smoke

ROADMAP §7 / §13.2: "fit-camera — silhouette-IoU camera pose fitting (bbox-derived
  bounds), saves camera.json ... match/fit-camera → silhouette IoU, overlay-diff
  (AE / blend / canny)."

Thin CLI wrapper over lib/refmatch.py. The heavy lifting (segmentation, camera
fit/fallback, ImageMagick metrics, collage) lives there and is import-light;
this module only parses argv, prints machine-parseable KEY=VALUE lines, and maps
failures to structured errors.
"""
from __future__ import annotations

import os

from cli.env import require_magick
from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

USAGE = """3d compare <model.scad|render.png> <reference.jpg> [-o outdir] [options]
  Reliable model<->reference comparison.

WHY:
  The old score flow thresholded the RAW reference photo into a mask -- for a
  cluttered photo (sky, ground, adjacent buildings) that mask is garbage and the
  IoU is meaningless (0.7 reported while the building clearly doesn't match).
  `compare` SEGMENTS the reference into a clean subject mask first (OpenCV
  grabCut), fits the camera against THAT mask (rejecting a degenerate fit), then
  scores and collages against the masked subject -- so the number means something.

  Prints IoU / SSIM / DSSIM as KEY=VALUE lines (machine-parseable) and writes
  mask.png, matched_render.png, diff.png and a render|diff|reference collage.png.
  If IoU < 0.50 it warns that the comparison is unreliable and names what to check.

Options:
  -o, --out DIR         output dir for artifacts (default: /tmp/3dcompare)
  --rand N              fit-camera random-search samples (default 80)
  --refine N            fit-camera coordinate-descent refine steps (default 40)

Examples:
  3d compare model.scad photo.jpg -o match/
  3d compare render.png photo.jpg          # already-rendered image, no camera fit
  3d compare model.scad photo.jpg --rand 8 --refine 3   # quick smoke"""


def _int_opt(flag: str, value: str) -> int:
    """Parse an integer option value, raising a structured error (not a traceback) on garbage."""
    try:
        return int(value)
    except ValueError:
        raise InvalidArgument(flag, value, ["a positive integer"], command="compare") from None


def run(argv: list[str]) -> int:  # noqa: C901  (flat arg parse + report)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    if len(argv) < 2:
        print(USAGE)
        return 1

    model, reference = argv[0], argv[1]
    rest = argv[2:]
    outdir = "/tmp/3dcompare"
    fit_rand = 80
    fit_refine = 40

    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="compare")
            outdir = rest[i + 1]
            i += 2
        elif a == "--rand":
            if i + 1 >= n:
                raise UsageError("option --rand needs a value", command="compare")
            fit_rand = _int_opt("--rand", rest[i + 1])
            i += 2
        elif a == "--refine":
            if i + 1 >= n:
                raise UsageError("option --refine needs a value", command="compare")
            fit_refine = _int_opt("--refine", rest[i + 1])
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="compare")

    for f in (model, reference):
        if not os.path.isfile(f):
            raise InputNotFound(f, command="compare")
    require_magick("compare")  # IoU/SSIM/diff/collage all need ImageMagick.

    # Lazy import: refmatch is import-light but still — keep the module top clean.
    import refmatch

    try:
        res = refmatch.compare_pipeline(
            model, reference, outdir, fit_rand=fit_rand, fit_refine=fit_refine
        )
    except (RuntimeError, refmatch.MagickError) as exc:
        raise GateFailure(f"compare: {exc}", command="compare")

    print(f"IoU={res.iou:.4f}")
    print(f"SSIM={res.ssim:.4f}")
    print(f"DSSIM={res.dssim:.4f}")
    print(f"MASK={res.mask_png}")
    print(f"MATCHED_RENDER={res.matched_render_png}")
    print(f"DIFF={res.diff_png}")
    print(f"COLLAGE={res.collage_png}")
    print(f"FALLBACK={'1' if res.used_fallback else '0'}")
    if res.used_fallback and res.fallback_reason:
        print(f"FALLBACK_REASON={res.fallback_reason}")

    if not res.reliable:
        print()
        print(
            f"WARNING: IoU={res.iou:.2f} < {refmatch.UNRELIABLE_IOU:.2f} -- this comparison "
            "is UNRELIABLE and the tool is likely misapplied."
        )
        print("  Check, in order:")
        print(f"    1. The subject mask: open {res.mask_png} -- did grabCut actually "
              "isolate the subject, or grab the sky/ground/neighbours?")
        if res.used_fallback:
            print(f"    2. Camera fit was rejected as degenerate ({res.fallback_reason}); "
                  "the fallback whole-model view may not match the reference angle.")
        else:
            print("    2. The camera fit: open the collage -- is the render at the same "
                  "angle/zoom as the reference subject?")
        print("    3. Whether the model genuinely resembles the reference at all "
              "(a low IoU can simply mean the geometry is wrong).")

    return 0


COMMAND = Command(
    name="compare",
    group="REFERENCE-MATCH PIPELINE",
    summary="reliable model<->reference comparison (segmented IoU + SSIM/DSSIM + collage)",
    usage=USAGE,
    run=run,
)
