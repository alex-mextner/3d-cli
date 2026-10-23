"""`detect`: open-vocabulary boxes for text queries (OWLv2)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .. import io
from ..detect import OwlDetector
from ..errors import BadInput

NAME = "detect"
SUMMARY = "open-vocabulary object boxes for comma-separated text queries (OWLv2); prints {boxes, timing_s}"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("image", help="photo (JPEG/PNG/HEIC)")
    parser.add_argument(
        "--queries",
        required=True,
        help='comma-separated queries, e.g. "a hand tool,a box"',
    )
    parser.add_argument(
        "--backend",
        choices=("owlv2",),
        default="owlv2",
        help="detector backend (only owlv2)",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.15, help="minimum score (default 0.15)"
    )
    parser.add_argument(
        "--nms",
        type=float,
        default=0.5,
        help="IoU above which the lower-scored box is dropped (default 0.5)",
    )
    parser.add_argument(
        "--max-area-frac",
        type=float,
        default=0.05,
        help="drop boxes larger than this image fraction (default 0.05)",
    )
    parser.add_argument(
        "--masks",
        metavar="DIR",
        help="also write one SAM2 box-prompted mask per box: DIR/instances.json (the `segment` schema, plus label/score/box_xyxy), "
        "masks/NNN.png, labels.png, overlay.jpg",
    )
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object to stdout"
    )


def run(args: argparse.Namespace) -> int:
    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    if not queries:
        raise BadInput("--queries is empty", 'pass e.g. --queries "a hand tool,a box"')
    rgb = io.load_rgb(args.image)
    det = OwlDetector(args.device)
    try:
        res = det.detect(
            rgb,
            queries,
            threshold=args.threshold,
            nms=args.nms,
            max_area_frac=args.max_area_frac,
        )
    finally:
        det.unload()
    res["device"] = det.device
    if args.masks:
        res["masks"] = _write_masks(Path(args.masks), args, rgb, res)
    res["image"] = {"path": args.image, "w": int(rgb.shape[1]), "h": int(rgb.shape[0])}
    if args.json:
        print(json.dumps(io.to_jsonable(res)))
    else:
        for b in res["boxes"]:
            print(f"{b['score']:.3f}  {b['label']:<20s} {b['xyxy']}")
        print(f"{len(res['boxes'])} boxes in {res['timing_s']} s on {det.device}")
        if "masks" in res:
            print(f"{res['masks']['n']} masks -> {args.masks}")
    return 0


def _write_masks(
    out_dir: Path, args: argparse.Namespace, rgb: Any, det_res: dict[str, Any]
) -> dict[str, Any]:
    """SAM2 box prompts for the detected boxes -> `segment`-compatible output directory; returns a small summary."""
    from ..segment import Segmenter
    from ..torchutil import peak_mb, reset_peak
    from ..outputs import write_result

    seg = Segmenter(method="sam2", fill="none", reject_bg=False, device=args.device)
    t0 = time.perf_counter()
    try:
        # no box, no SAM2: an empty result must not need the weights (they may not be pulled) nor a CUDA context
        if det_res["boxes"]:
            seg.load()
            reset_peak(seg.device)
        instances = seg.masks_for_boxes(rgb, det_res["boxes"])
        elapsed = round(time.perf_counter() - t0, 2)
        doc = write_result(
            out_dir,
            args.image,
            rgb,
            {
                "instances": instances,
                "method": "owlv2+sam2-box",
                "device": seg.device,
                "timing_s": {"masks": elapsed, "total": elapsed},
                "vram_mb": peak_mb(seg.device),
            },
        )
    finally:
        seg.unload()
    if not args.json:
        print(
            f"  [masks] {len(instances)}/{len(det_res['boxes'])} boxes in {elapsed} s",
            file=sys.stderr,
        )
    return {
        "dir": str(out_dir),
        "instances": "instances.json",
        "n": len(doc["instances"]),
    }
