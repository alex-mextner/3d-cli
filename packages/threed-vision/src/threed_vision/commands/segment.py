"""`segment`: multi-object instance masks from one photo (SAM2.1-tiny AMG + OWLv2 fill + CLIP background rejection)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import io
from ..errors import BadInput
from ..outputs import write_result
from ..segment import FILLS, METHODS, QUALITIES, Segmenter, load_prompts

NAME = "segment"
SUMMARY = "segment a photo into object instances (SAM2 + OWLv2 fill + CLIP background rejection); writes instances.json, labels.png, masks/, overlay.jpg"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("image", help="photo (JPEG/PNG/HEIC)")
    parser.add_argument(
        "-o",
        "--out",
        required=True,
        help="output directory (instances.json, labels.png, masks/NNN.png, overlay.jpg)",
    )
    parser.add_argument(
        "--method",
        choices=METHODS,
        default="sam2",
        help="sam2 (grid AMG), sam2-dense (64x64 grid) or u2net (one salient object)",
    )
    parser.add_argument(
        "--quality",
        choices=QUALITIES,
        default=None,
        help="fast: grid masks filtered on SAM2's 256x256 logits (fp16 on GPU), only where OWLv2 sees objects, + batched box prompts; "
        "accurate: the stock SAM2 automatic mask generator (default: accurate on cuda, fast on cpu)",
    )
    parser.add_argument(
        "--fill",
        choices=FILLS,
        default="owlv2",
        help="owlv2: add SAM2 masks for OWLv2 boxes the AMG missed; none: AMG only",
    )
    parser.add_argument(
        "--queries",
        default=None,
        help='OWLv2 queries, comma separated (default "a hand tool,a power tool,a box,...")',
    )
    parser.add_argument(
        "--owl-tiles",
        type=int,
        default=1,
        help="also run OWLv2 on an N x N grid of overlapping tiles (1 = whole image only, max 4; finds small / thin tools)",
    )
    parser.add_argument(
        "--grid-gate",
        type=float,
        default=None,
        help="--quality fast: decode only grid points near a patch with OWLv2 objectness >= F (0 = every point, default 0.1)",
    )
    parser.add_argument(
        "--bg-prompts",
        default=None,
        help="CLIP background prompts: comma list or a file, one per line (default: carpet / parquet / cabinet / shadow / foot / bin / cable); "
        "e.g. --bg-prompts 'a photo of a wooden table,a photo of a shadow'",
    )
    parser.add_argument(
        "--fg-prompts",
        default=None,
        help="CLIP object prompts: comma list or a file, one per line (default: hand tool / power tool / box / screwdriver / pliers / ...)",
    )
    parser.add_argument(
        "--reject-bg",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="drop carpet/floor/cabinet patches with CLIP (default on)",
    )
    parser.add_argument(
        "--points-per-side",
        type=int,
        default=None,
        help="prompt grid per side (default 32; --quality accurate on CPU: 0 = OWLv2 boxes only, or 16 with --fill none)",
    )
    parser.add_argument(
        "--max-side",
        type=int,
        default=None,
        help="downscale the longest image side before SAM2 (0 = never; default none, except 1024 for --quality accurate on CPU)",
    )
    parser.add_argument(
        "--min-area",
        type=int,
        default=150,
        help="drop masks smaller than this many pixels (default 150)",
    )
    parser.add_argument(
        "--max-area-frac",
        type=float,
        default=0.03,
        help="drop masks larger than this fraction of the image (default 0.03)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="auto picks cuda only with enough free VRAM",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print exactly one JSON object (the instances.json content) to stdout",
    )


def run(args: argparse.Namespace) -> int:
    rgb = io.load_rgb(args.image)
    queries = (
        [q.strip() for q in args.queries.split(",") if q.strip()]
        if args.queries
        else None
    )
    if args.queries is not None and not queries:
        raise BadInput("--queries is empty")
    seg = Segmenter(
        method=args.method,
        quality=args.quality,
        fill=args.fill,
        reject_bg=args.reject_bg,
        device=args.device,
        points_per_side=args.points_per_side,
        max_side=args.max_side,
        min_area=args.min_area,
        max_area_frac=args.max_area_frac,
        queries=queries,
        owl_tiles=args.owl_tiles,
        grid_gate=args.grid_gate,
        bg_prompts=load_prompts(args.bg_prompts)
        if args.bg_prompts is not None
        else None,
        fg_prompts=load_prompts(args.fg_prompts)
        if args.fg_prompts is not None
        else None,
    )

    def progress(stage: str, frac: float) -> None:
        if frac == 0.0 and not args.json:
            print(f"  [{stage}] ...", file=sys.stderr)

    try:
        result = seg.run(rgb, progress=progress)
    finally:
        seg.unload()
    doc = write_result(Path(args.out), args.image, rgb, result)
    if args.json:
        print(json.dumps(io.to_jsonable(doc)))
    else:
        t = doc["timing_s"]
        print(
            f"{len(doc['instances'])} instances ({doc['method']}, {doc['device']}, {t['total']} s, vram {doc['vram_mb']} MB) -> {args.out}"
        )
    return 0
