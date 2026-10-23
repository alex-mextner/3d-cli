"""`fit-profile`: closed-form oriented rectangle + width profile of one silhouette (numpy, milliseconds)."""

from __future__ import annotations

import argparse
import json

from ..io import to_jsonable
from ._views import load_mask_spec

NAME = "fit-profile"
SUMMARY = (
    "closed-form length/width profile + shape kind of a mask (no optimiser, ~1 ms)"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "mask",
        help="mask.png (non-zero = object) or instances.json#ID from `3d segment`",
    )
    parser.add_argument(
        "--mm-per-px",
        type=float,
        default=1.0,
        help="scale; lengths in the output are mm (default 1.0 = pixels)",
    )
    parser.add_argument(
        "--k", type=int, default=12, help="number of profile stations (default 12)"
    )
    parser.add_argument(
        "--nbins",
        type=int,
        default=96,
        help="axis bins the profile is measured in (default 96)",
    )
    parser.add_argument(
        "--axis-deg",
        type=float,
        default=None,
        help="force the axis angle (deg, image frame) instead of PCA",
    )
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object"
    )


def run(args: argparse.Namespace) -> int:
    from ..fit_profile import fit_profile

    out = fit_profile(
        load_mask_spec(args.mask),
        args.mm_per_px,
        K=args.k,
        nbins=args.nbins,
        axis_angle_deg=args.axis_deg,
    )
    if args.json:
        print(json.dumps(to_jsonable(out)))
    else:
        print(
            f"kind={out['kind']} length={out['length_mm']:.1f} max_width={out['max_width_mm']:.1f} "
            f"iou_recon={out['iou_recon']:.3f} axis={out['axis_deg']:.1f}deg time={out['time_ms']:.1f}ms"
        )
    return 0
