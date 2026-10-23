"""`calibrate-grid`: camera calibration + wall-plane homography from a photo of the HSW honeycomb panel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .. import io
from ..calibrate import DEFAULT_GRID_PITCH_MM, detect_grid, grid_mm_to_px
from ..errors import LowConfidence

NAME = "calibrate-grid"
SUMMARY = "detect the honeycomb grid in a wall photo: focal length, camera pose, photo-px -> wall-mm homography (numpy + OpenCV, no model)"


def _pitch(text: str) -> tuple[float, float]:
    try:
        a, b = (float(v) for v in text.lower().replace("x", ",").split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected COLxROW in mm, e.g. 20.4382x23.6"
        ) from exc
    if a <= 0 or b <= 0:
        raise argparse.ArgumentTypeError("pitches must be positive")
    return a, b


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "image", help="photo (JPEG/PNG/HEIC) showing the honeycomb panel"
    )
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="output directory: calibration.json + overlay.jpg",
    )
    parser.add_argument(
        "--grid-pitch",
        type=_pitch,
        default=DEFAULT_GRID_PITCH_MM,
        metavar="COLxROW",
        help="column x row pitch in mm (default 20.4382x23.6, the HSW panel)",
    )
    f = parser.add_mutually_exclusive_group()
    f.add_argument(
        "--f-px",
        type=float,
        default=None,
        help="known focal length in pixels (R and t only are solved)",
    )
    f.add_argument(
        "--f-ratio",
        type=float,
        default=None,
        help="known focal length / image diagonal (e.g. 0.375)",
    )
    parser.add_argument(
        "--time-budget",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="wall-clock budget; a photo without a grid fails with reason time_budget instead of searching on (default 8)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print exactly one JSON object (the calibration) to stdout",
    )


def _overlay(rgb: np.ndarray, res: dict[str, Any]) -> np.ndarray:
    import cv2

    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    pc, pr = res["grid_pitch_mm"]
    lab = np.asarray(res["inlier_cells"])
    H = np.asarray(res["homography_grid_mm_to_px"])
    c0, r0 = (int(lab[:, 0].min()) - 2, int(lab[:, 1].min()) - 2)
    for col in range(max(0, c0), int(lab[:, 0].max()) + 3):
        for row in range(max(0, r0), int(lab[:, 1].max()) + 3):
            mm = np.array([[(col + 0.5) * pc, (row + 0.5 + 0.5 * (col & 1)) * pr]])
            x, y = grid_mm_to_px(H, mm)[0]
            cv2.drawMarker(
                img, (round(x), round(y)), (0, 0, 255), cv2.MARKER_CROSS, 7, 1
            )
    for x, y in np.asarray(res["inlier_cells_px"]):
        cv2.circle(img, (round(float(x)), round(float(y))), 2, (0, 255, 0), -1)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def run(args: argparse.Namespace) -> int:
    rgb = io.load_rgb(args.image)
    res = detect_grid(
        rgb,
        grid_pitch_mm=args.grid_pitch,
        f_px=args.f_px,
        f_ratio=args.f_ratio,
        time_budget_s=args.time_budget,
    )
    doc = io.to_jsonable(
        {
            "image": {"path": str(args.image), "w": rgb.shape[1], "h": rgb.shape[0]},
            **res,
        }
    )
    if args.out:
        out = Path(args.out)
        io.write_json(out / "calibration.json", doc)
        if res["ok"]:
            from PIL import Image

            Image.fromarray(_overlay(rgb, res)).save(out / "overlay.jpg", quality=90)
    if args.json:
        print(json.dumps(doc))
    elif res["ok"]:
        print(
            f"grid found: {res['n_inliers']} cells (of {res['n_cells']} candidates), focal {res['f_px']:.1f} px ({res['f_source']}), "
            f"reprojection median {res['reproj_median_px']:.2f} px / p95 {res['reproj_p95_px']:.2f} px, "
            f"{res['mm_per_px_centre']:.3f} mm/px, tilt {res['tilt_deg']:.1f} deg, flags: {', '.join(res['quality']) or 'none'}"
        )
    if not res["ok"]:
        raise LowConfidence(
            f"no honeycomb grid found in {args.image}: {res['reason']}",
            "photograph the panel head-on-ish and in focus, with at least ~12 full cells visible, "
            "or use `3d photo-scale --ref/--points` instead",
        )
    return 0
