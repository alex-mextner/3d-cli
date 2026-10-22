"""`photo-scale`: millimetres per pixel, plane depth and intrinsics for a photo (reference object, two points, honeycomb grid, or another calibrated photo)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .. import io
from ..calibrate import (
    DEFAULT_F_RATIO,
    default_intrinsics,
    detect_grid,
    local_mm_per_px,
    scale_from_grid,
    scale_from_points,
    scale_from_reference,
)
from ..errors import BadInput, LowConfidence
from ..register import register
from ._masks import instance_mask

NAME = "photo-scale"
SUMMARY = "mm per pixel + plane depth + K for a photo: --ref OBJECT=MM, --points .. --distance MM, --grid honeycomb, or --homography-to OTHER (numpy + OpenCV, no model)"


def _split_ref(text: str) -> tuple[str, float]:
    src, sep, length = text.rpartition("=")
    if not sep or not src:
        raise argparse.ArgumentTypeError(
            "expected INSTANCE=LENGTH_MM or MASK.png=LENGTH_MM"
        )
    try:
        return src, float(length)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"length must be a number of mm, got {length!r}"
        ) from exc


def _points(text: str) -> tuple[float, float, float, float]:
    try:
        a, b, c, d = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected X1,Y1,X2,Y2 in pixels") from exc
    return a, b, c, d


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("image", help="photo (JPEG/PNG/HEIC)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--ref",
        type=_split_ref,
        metavar="INSTANCE=MM",
        help="reference object of known longest dimension: an instance id of --instances (or a mask PNG path) = its length in mm",
    )
    src.add_argument(
        "--points",
        type=_points,
        metavar="X1,Y1,X2,Y2",
        help="two pixel points a known --distance apart",
    )
    src.add_argument(
        "--grid",
        choices=("honeycomb",),
        help="scale of the wall plane from the HSW honeycomb panel in the photo",
    )
    src.add_argument(
        "--homography-to",
        metavar="OTHER.jpg",
        help="transfer the scale of a second photo of the same plane (needs --other-mm-per-px or --other-grid)",
    )
    parser.add_argument(
        "--instances",
        help="instances.json written by `3d segment` (for --ref INSTANCE=MM)",
    )
    parser.add_argument("--distance", type=float, help="distance in mm for --points")
    parser.add_argument(
        "--calibration",
        help="--grid: reuse a calibrate-grid calibration.json or an HSW photo_calibration.json instead of detecting",
    )
    parser.add_argument(
        "--at",
        type=_points_xy,
        metavar="X,Y",
        help="--grid/--homography-to: pixel where the scale is wanted (default: grid centroid / image centre)",
    )
    parser.add_argument(
        "--other-mm-per-px",
        type=float,
        help="--homography-to: known scale of the other photo (uniform)",
    )
    parser.add_argument(
        "--other-grid",
        choices=("honeycomb",),
        help="--homography-to: calibrate the other photo with its honeycomb grid",
    )
    f = parser.add_mutually_exclusive_group()
    f.add_argument(
        "--f-px",
        type=float,
        default=None,
        help="focal length in pixels (EXIF/calibration)",
    )
    f.add_argument(
        "--f-ratio",
        type=float,
        default=None,
        help="focal length / image diagonal when --f-px is absent (default 0.375, the HSW phone)",
    )
    parser.add_argument(
        "-o", "--out", default=None, help="also write the JSON to this file"
    )
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object to stdout"
    )


def _points_xy(text: str) -> tuple[float, float]:
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected X,Y in pixels") from exc
    return x, y


def _ratio(args: argparse.Namespace) -> float:
    return DEFAULT_F_RATIO if args.f_ratio is None else float(args.f_ratio)


def _reg_summary(reg: dict[str, Any]) -> dict[str, Any]:
    return {
        k: reg[k]
        for k in (
            "matches",
            "inliers",
            "median_reproj_px",
            "rel_rot_deg",
            "translation_ratio",
            "baseline_ok",
        )
    }


def _homography_scale(
    args: argparse.Namespace, rgb: np.ndarray, size: tuple[int, int]
) -> dict[str, Any]:
    if bool(args.other_grid) == (args.other_mm_per_px is not None):
        raise BadInput(
            "--homography-to needs exactly one of --other-mm-per-px X or --other-grid honeycomb"
        )
    other = io.load_rgb(args.homography_to)
    reg = register(rgb, other)
    if not reg["ok"]:
        raise LowConfidence(
            f"could not register {args.image} to {args.homography_to}: {reg['reason']}",
            "use two photos of the same scene with overlapping, textured content",
        )
    H = np.asarray(reg["H_a_to_b"])
    at = np.asarray(args.at if args.at else (size[0] / 2.0, size[1] / 2.0), float)
    if args.other_grid:
        cal = detect_grid(other)
        if not cal["ok"]:
            raise LowConfidence(
                f"no honeycomb grid found in {args.homography_to}: {cal['reason']}"
            )
        H_px_to_mm = np.asarray(cal["homography_px_to_grid_mm"]) @ H
    else:
        if not args.other_mm_per_px > 0:
            raise BadInput("--other-mm-per-px must be positive")
        H_px_to_mm = np.diag([args.other_mm_per_px, args.other_mm_per_px, 1.0]) @ H
    mm_per_px = local_mm_per_px(H_px_to_mm, at)
    K = default_intrinsics(size[0], size[1], args.f_px, _ratio(args))
    quality = [
        "plane_of_overlap_only",
        "fronto_parallel_depth_assumed",
        "f_given" if args.f_px else "f_default_ratio",
    ]
    if not reg["baseline_ok"]:
        quality.append("same_viewpoint")
    return {
        "mm_per_px": mm_per_px,
        "plane_depth_mm": float(K[0, 0]) * mm_per_px,
        "K": K,
        "f_px": float(K[0, 0]),
        "f_source": "given" if args.f_px else "default",
        "source": "homography",
        "at_px": at,
        "image_size": list(size),
        "quality": quality,
        "reg": _reg_summary(reg),
    }


def run(args: argparse.Namespace) -> int:
    rgb = io.load_rgb(args.image)
    size = (rgb.shape[1], rgb.shape[0])
    ratio = _ratio(args)
    if args.ref:
        spec, length = args.ref
        if Path(spec).is_file():
            mask = io.load_mask(spec)
        elif spec.isdigit():
            if not args.instances:
                raise BadInput(
                    f"--ref {spec}=MM names an instance id: pass --instances DIR/instances.json"
                )
            mask = instance_mask(args.instances, int(spec))
        else:
            raise BadInput(
                f"--ref {spec!r} is neither an existing mask PNG nor an instance id"
            )
        res = scale_from_reference(
            mask, length, image_size=size, f_px=args.f_px, f_ratio=ratio
        )
    elif args.points:
        if args.distance is None:
            raise BadInput("--points needs --distance MM")
        x1, y1, x2, y2 = args.points
        res = scale_from_points(
            (x1, y1),
            (x2, y2),
            args.distance,
            image_size=size,
            f_px=args.f_px,
            f_ratio=ratio,
        )
    elif args.grid:
        if args.calibration:
            try:
                cal = json.loads(Path(args.calibration).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BadInput(
                    f"cannot read calibration {args.calibration}: {exc}"
                ) from exc
        else:
            cal = detect_grid(rgb, f_px=args.f_px, f_ratio=args.f_ratio)
            if not cal["ok"]:
                raise LowConfidence(
                    f"no honeycomb grid found in {args.image}: {cal['reason']}",
                    "use --ref/--points, or photograph the panel with >= 12 full cells visible",
                )
        res = scale_from_grid(cal, at_px=args.at)
    else:
        res = _homography_scale(args, rgb, size)
    doc = io.to_jsonable(res)
    if args.out:
        io.write_json(args.out, doc)
    if args.json:
        print(json.dumps(doc))
    else:
        print(
            f"{res['mm_per_px']:.4f} mm/px, plane depth {res['plane_depth_mm']:.0f} mm, f {res['f_px']:.1f} px "
            f"({res['source']}; {', '.join(res['quality'])})"
        )
    return 0
