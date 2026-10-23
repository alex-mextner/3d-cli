"""`register`: SIFT + RANSAC homography between two photos, decomposed relative pose, mask warping and repeatability IoU."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .. import io
from ..calibrate import default_intrinsics
from ..errors import LowConfidence
from ..register import register
from ._masks import load_masks

NAME = "register"
SUMMARY = "register two photos of one scene (SIFT + RANSAC homography): relative pose, baseline_ok, mask warp A->B, repeatability IoU (numpy + OpenCV, no model)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("a", help="photo A (JPEG/PNG/HEIC)")
    parser.add_argument("b", help="photo B")
    parser.add_argument(
        "--mask-a",
        help="masks in A to warp into B: instances.json (from `3d segment`), a directory of PNGs or one PNG",
    )
    parser.add_argument(
        "--mask-b",
        help="masks segmented in B (same formats): the best-overlapping one scores each warped A mask (repeatability IoU)",
    )
    parser.add_argument(
        "--f-px-a",
        type=float,
        default=None,
        help="focal length of A in px (default 0.375 * diagonal)",
    )
    parser.add_argument(
        "--f-px-b",
        type=float,
        default=None,
        help="focal length of B in px (default 0.375 * diagonal)",
    )
    parser.add_argument(
        "--plane-depth-mm",
        type=float,
        default=None,
        help="distance of the plane from camera A: scales the translation to mm",
    )
    parser.add_argument(
        "--flag-below",
        type=float,
        default=0.75,
        help="repeatability IoU below this marks a mask unstable (default 0.75)",
    )
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="output directory: registration.json + warped/NNN.png",
    )
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object to stdout"
    )


def _document(res: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    keep = {k: v for k, v in res.items() if k != "warped_masks"}
    return io.to_jsonable({"a": str(args.a), "b": str(args.b), **keep})


def run(args: argparse.Namespace) -> int:
    a, b = io.load_rgb(args.a), io.load_rgb(args.b)
    K_a = (
        default_intrinsics(a.shape[1], a.shape[0], args.f_px_a) if args.f_px_a is not None else None
    )
    K_b = (
        default_intrinsics(b.shape[1], b.shape[0], args.f_px_b) if args.f_px_b is not None else None
    )
    masks_a = load_masks(args.mask_a) if args.mask_a else None
    masks_b = load_masks(args.mask_b) if args.mask_b else None
    res = register(
        a,
        b,
        masks_a,
        K_a,
        K_b,
        masks_b=masks_b,
        plane_depth_mm=args.plane_depth_mm,
        flag_below=args.flag_below,
    )
    doc = _document(res, args)
    if args.out:
        out = Path(args.out)
        io.write_json(out / "registration.json", doc)
        for i, m in enumerate(res["warped_masks"]):
            io.save_png(out / "warped" / f"{i + 1:03d}.png", np.asarray(m))
    if args.json:
        print(json.dumps(doc))
    elif res["ok"]:
        print(
            f"registered: {res['matches']} matches, {res['inliers']} inliers, median reprojection {res['median_reproj_px']:.3f} px, "
            f"relative rotation {res['rel_rot_deg']:.2f} deg, translation/depth {res['translation_ratio']:.3f}, "
            f"baseline_ok={res['baseline_ok']}"
        )
        for r in res["repeatability"]:
            if r["iou"] is not None:
                print(
                    f"  mask {r['index']}: repeatability IoU {r['iou']:.3f}{'  UNSTABLE' if r['unstable'] else ''}"
                )
    if not res["ok"]:
        raise LowConfidence(
            f"could not register {args.a} to {args.b}: {res['reason']}",
            "use two photos of the same scene with overlapping, textured content",
        )
    return 0
