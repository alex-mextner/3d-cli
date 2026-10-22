"""`segment`: multi-object instance masks from one photo (SAM2.1-tiny AMG + OWLv2 fill + CLIP background rejection)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import io
from ..errors import BadInput
from ..segment import FILLS, METHODS, Segmenter, instances_to_labelmap

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
        "--reject-bg",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="drop carpet/floor/cabinet patches with CLIP (default on)",
    )
    parser.add_argument(
        "--points-per-side",
        type=int,
        default=None,
        help="AMG prompt grid per side (default 32 on GPU; on CPU 0 = OWLv2 boxes only, or 16 with --fill none)",
    )
    parser.add_argument(
        "--max-side",
        type=int,
        default=None,
        help="downscale the longest image side before SAM2 (0 = never; default none on GPU, 1024 on CPU)",
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


def write_result(
    out_dir: Path, image_path: str, rgb: Any, result: dict[str, Any]
) -> dict[str, Any]:
    """Write instances.json + labels.png + masks/NNN.png + overlay.jpg; returns the instances.json document."""
    import cv2

    h, w = rgb.shape[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    insts = result["instances"]
    records = []
    for inst in insts:
        rel = f"masks/{inst['id']:03d}.png"
        io.save_png(out_dir / rel, inst["mask"])
        records.append(
            {
                "id": inst["id"],
                "source": inst["source"],
                "bbox_xywh": inst["bbox_xywh"],
                "area_px": inst["area_px"],
                "bg_prob": round(inst["bg_prob"], 4),
                "stability": round(inst["stability"], 4),
                "mask": rel,
                "polygon": io.mask_polygon(inst["mask"]),
                **{k: inst[k] for k in ("label", "score", "box_xyxy") if k in inst},
            }
        )
    io.save_png(out_dir / "labels.png", instances_to_labelmap(insts, (h, w)))
    overlay = io.draw_overlay(rgb, [i["mask"] for i in insts], numbers=False)
    cv2.imwrite(
        str(out_dir / "overlay.jpg"),
        cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_JPEG_QUALITY, 90],
    )
    doc = {
        "image": {"path": str(image_path), "w": w, "h": h},
        **{k: v for k, v in result.items() if k != "instances"},
        "instances": records,
    }
    io.write_json(out_dir / "instances.json", doc)
    return doc


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
        fill=args.fill,
        reject_bg=args.reject_bg,
        device=args.device,
        points_per_side=args.points_per_side,
        max_side=args.max_side,
        min_area=args.min_area,
        max_area_frac=args.max_area_frac,
        queries=queries,
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
