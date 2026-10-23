"""`bank thumbs|index|query`: reference bank of lie-flat thumbnails, an embedding index on disk, and retrieval over it."""

from __future__ import annotations

import argparse
import json
import sys
import time

from .. import bank, io
from ..errors import BadInput
from ..recognise import load_instances

NAME = "bank"
SUMMARY = "reference bank: `thumbs` renders lie-flat thumbnails from meshes, `index` embeds them (CLIP+DINOv2), `query` finds the nearest entries for a crop"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(
        dest="action", metavar="thumbs|index|query", required=True
    )

    th = sub.add_parser(
        "thumbs",
        help="render lie-flat thumbnails + silhouettes from a mesh directory (writes bank.json)",
    )
    th.add_argument(
        "src",
        help="directory of meshes (stl/obj/ply/glb/gltf/3mf/off) or a hand-made bank.json",
    )
    th.add_argument("-o", "--out", required=True, help="output directory")
    th.add_argument(
        "--view",
        choices=bank.VIEWS,
        default="longest-axis",
        help="longest-axis (lie flat, default) | top | iso",
    )
    th.add_argument(
        "--px", type=int, default=192, help="thumbnail size in pixels (default 192)"
    )
    th.add_argument(
        "--categories",
        default=None,
        help='JSON {"mesh name": "category"} (default: categories.json next to the meshes, else name rules)',
    )
    th.add_argument(
        "--unit-scale",
        type=float,
        default=1.0,
        help="mesh units -> millimetres (default 1; glTF is metres: 1000)",
    )
    th.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object (the bank.json content)",
    )

    ix = sub.add_parser(
        "index",
        help="embed a bank (CLIP + DINOv2 + silhouette descriptors) into index.npz + index.json",
    )
    ix.add_argument("bank", help="bank directory or bank.json from `bank thumbs`")
    ix.add_argument(
        "-o", "--out", default=None, help="index directory (default: next to bank.json)"
    )
    ix.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    ix.add_argument("--json", action="store_true", help="print one JSON object")

    qr = sub.add_parser("query", help="top-k bank entries for an object in an image")
    qr.add_argument(
        "image", help="a crop, or a photo together with --mask / --instances + --id"
    )
    qr.add_argument("--index", required=True, help="index directory from `bank index`")
    qr.add_argument(
        "--mask",
        default=None,
        help="mask PNG (same size as the image) marking the object",
    )
    qr.add_argument(
        "--instances", default=None, help="instances.json from `3d segment` (with --id)"
    )
    qr.add_argument(
        "--id", type=int, default=None, help="instance id inside --instances"
    )
    qr.add_argument("--topk", type=int, default=5, help="matches to return (default 5)")
    qr.add_argument(
        "--metric",
        choices=bank.METRICS,
        default="auto",
        help="auto (appearance for textured banks, else silhouette) | clip | dino | sil | clip+dino | all",
    )
    qr.add_argument(
        "--category",
        default=None,
        help="only entries of these categories (comma separated)",
    )
    qr.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    qr.add_argument("--json", action="store_true", help="print one JSON object")


def _thumbs(args: argparse.Namespace) -> int:
    def progress(ident: str) -> None:
        if not args.json:
            print(f"  rendered {ident}", file=sys.stderr)

    path = bank.build_thumbnails(
        args.src,
        args.out,
        args.view,
        args.px,
        categories=args.categories,
        unit_scale=args.unit_scale,
        progress=progress,
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "warning" in doc:
        print(f"WARNING: {doc['warning']}", file=sys.stderr)
    if args.json:
        print(json.dumps(doc))
    else:
        print(f"{len(doc['entries'])} entries ({doc['n_textured']} textured) -> {path}")
    return 0


def _index(args: argparse.Namespace) -> int:
    out = bank.build_index(args.bank, args.out, device=args.device)
    meta = json.loads((out / "index.json").read_text(encoding="utf-8"))["meta"]
    if "warning" in meta:
        print(f"WARNING: {meta['warning']}", file=sys.stderr)
    if args.json:
        print(json.dumps({"index": str(out), **meta}))
    else:
        print(
            f"indexed {meta['n']} entries ({meta['n_textured']} textured) in {meta['embed_seconds']} s -> {out}/index.npz"
        )
    return 0


def _query(args: argparse.Namespace) -> int:
    rgb = io.load_rgb(args.image)
    h, w = rgb.shape[:2]
    mask = None
    if args.mask and (args.instances or args.id is not None):
        raise BadInput("--mask and --instances/--id are alternatives, give one")
    if args.mask:
        mask = io.load_mask(args.mask)
        if mask.shape != (h, w):
            raise BadInput(
                f"mask is {mask.shape[1]}x{mask.shape[0]}, the image is {w}x{h}"
            )
    elif args.instances or args.id is not None:
        if not args.instances or args.id is None:
            raise BadInput("--instances and --id go together")
        found = [
            i for i in load_instances(args.instances, (h, w)) if i["id"] == args.id
        ]
        if not found:
            raise BadInput(f"no instance with id {args.id} in {args.instances}")
        mask = found[0]["mask"]
    categories = (
        [c.strip() for c in args.category.split(",") if c.strip()]
        if args.category
        else None
    )
    t0 = time.perf_counter()
    matches = bank.query(
        args.index,
        rgb,
        args.topk,
        mask=mask,
        metric=args.metric,
        categories=categories,
        device=args.device,
    )
    ms = (time.perf_counter() - t0) * 1e3
    doc = {
        "matches": matches,
        "metric": matches[0]["metric"] if matches else args.metric,
        "timing_ms": round(ms, 1),
        "masked": mask is not None,
    }
    if args.json:
        print(json.dumps(io.to_jsonable(doc)))
    else:
        for m in matches:
            ext = (
                "x".join(f"{v:g}" for v in m["extents_mm"]) + " mm"
                if m.get("extents_mm")
                else "-"
            )
            print(f"{m['score']:.4f}  {m['bank_id']:<28s} {m['category']:<16s} {ext}")
        print(f"{len(matches)} matches ({doc['metric']}) in {doc['timing_ms']} ms")
    return 0


def run(args: argparse.Namespace) -> int:
    return {"thumbs": _thumbs, "index": _index, "query": _query}[args.action](args)
