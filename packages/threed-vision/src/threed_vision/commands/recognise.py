"""`recognise`: class proposals (+ bank matches) for the instances of a segmented photo."""

from __future__ import annotations

import argparse
import json
import time

from .. import bank, io
from ..errors import BadInput
from ..recognise import (
    CALIBRATION_MIN_AREA_FRAC,
    CALIBRATION_POOL,
    Recogniser,
    load_categories,
    load_instances,
)

NAME = "recognise"
SUMMARY = (
    "classify the instances of a `3d segment` run: CLIP text classes (class-mean calibrated) "
    "+ optional bank retrieval inside the proposed classes"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("image", help="the photo that was segmented")
    parser.add_argument(
        "--instances", required=True, help="instances.json written by `3d segment`"
    )
    parser.add_argument(
        "--categories",
        default=None,
        help='categories: a file (one per line, JSON list or objects) or "a,b,c" (default: 22 workshop tools)',
    )
    parser.add_argument(
        "--bank",
        default=None,
        help="bank index directory (or its index.json) from `3d bank index`",
    )
    parser.add_argument(
        "--topk", type=int, default=5, help="text classes per instance (default 5)"
    )
    parser.add_argument(
        "--bank-topk",
        type=int,
        default=None,
        help="bank matches per instance (default = --topk)",
    )
    parser.add_argument(
        "--rerank-classes",
        type=int,
        default=3,
        help="bank search is restricted to the best N text classes (default 3)",
    )
    parser.add_argument(
        "--metric",
        choices=bank.METRICS,
        default="auto",
        help="bank metric (auto: appearance for textured banks, silhouette otherwise)",
    )
    parser.add_argument(
        "--calibrate",
        "--calibrate-prior",
        dest="calibrate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="subtract the per-class mean similarity over the photo's larger instances (default on; needs >= 8 instances)",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=CALIBRATION_POOL,
        help=f"calibrate over the N largest instances (default {CALIBRATION_POOL}, the spike's protocol; 0 = all instances)",
    )
    parser.add_argument(
        "--pool-min-area-frac",
        type=float,
        default=CALIBRATION_MIN_AREA_FRAC,
        help="calibration pool instances must cover at least this image fraction (default 0.00083 = 2500 px of 2000x1500)",
    )
    parser.add_argument(
        "--ids",
        default=None,
        help="only report these instance ids (comma separated); calibration still uses all instances",
    )
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object to stdout"
    )


def _parse_ids(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    try:
        return {int(x) for x in spec.split(",") if x.strip()}
    except ValueError as exc:
        raise BadInput(f"--ids must be comma-separated integers, got '{spec}'") from exc


def run(args: argparse.Namespace) -> int:
    only = _parse_ids(args.ids)
    rgb = io.load_rgb(args.image)
    insts = load_instances(args.instances, (rgb.shape[0], rgb.shape[1]))
    if not insts:
        raise BadInput("the instances file has no instances")
    if only is not None and (unknown := sorted(only - {i["id"] for i in insts})):
        raise BadInput(f"no instance with id {unknown}")
    cats = load_categories(args.categories)
    rec = Recogniser(args.device)
    try:
        t0 = time.perf_counter()
        res = rec.classify(
            rgb,
            insts,
            cats,
            calibrate=args.calibrate,
            topk=args.topk,
            bank=args.bank,
            bank_topk=args.bank_topk,
            rerank_classes=args.rerank_classes,
            metric=args.metric,
            pool_size=args.pool_size,
            pool_min_area_frac=args.pool_min_area_frac,
        )
        ms = (time.perf_counter() - t0) * 1e3 / max(len(res), 1)
        device = rec.device
    finally:
        rec.unload()
    calibrated = bool(res and res[0]["calibrated"])
    if only is not None:
        res = [r for r in res if r["id"] in only]
    doc = {
        "instances": res,
        "timing_ms_per_crop": round(ms, 2),
        "device": device,
        "n_crops": len(insts),
        "categories": len(cats),
        "calibrated": calibrated,
    }
    if args.json:
        print(json.dumps(io.to_jsonable(doc)))
        return 0
    for r in res:
        top = ", ".join(f"{t['label']} {t['score']:.1f}" for t in r["text_topk"][:3])
        best = (
            f"  bank: {r['bank_topk'][0]['bank_id']} {r['bank_topk'][0]['score']:.2f}"
            if r["bank_topk"]
            else ""
        )
        print(f"#{r['id']:<4d} {top}{best}")
    print(
        f"{len(res)} instances, {doc['timing_ms_per_crop']} ms/crop on {device}, calibrated={calibrated}"
    )
    return 0
