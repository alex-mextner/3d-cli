"""`fit-silhouette`: fit a lathe / flat loft / box to calibrated silhouettes (closed form first, soft-silhouette refine only if needed)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..errors import EXIT_LOW_CONFIDENCE, BadInput
from ..io import save_png, to_jsonable, write_json
from ._views import parse_view

NAME = "fit-silhouette"
SUMMARY = "fit lathe/flat/box params + pose to mask views with known cameras; writes fit.json (+ mesh)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--view",
        action="append",
        required=True,
        metavar="MASK:CAM.json",
        help="training view (repeatable); CAM.json = {K,R,t} or {K,pose:identity}",
    )
    parser.add_argument(
        "--heldout",
        action="append",
        default=[],
        metavar="MASK:CAM.json",
        help="scored, never fitted: makes confidence.heldout_iou",
    )
    parser.add_argument(
        "--template",
        default="auto",
        choices=["auto", "lathe", "flat", "box"],
        help="auto = by closed-form kind",
    )
    parser.add_argument(
        "--planar",
        action="store_true",
        help="object lies on a plane parallel to the image (top-down photo)",
    )
    parser.add_argument(
        "--known-depth",
        type=float,
        default=None,
        metavar="MM",
        help="camera z-depth of the object (mm); required for one view",
    )
    parser.add_argument(
        "--stations", type=int, default=8, help="profile stations (default 8)"
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "cpu"],
        help="refiner device; auto = GPU with CPU fallback",
    )
    parser.add_argument(
        "--max-tries",
        type=int,
        default=3,
        help="soft-fit restarts from the next-best pose candidate",
    )
    parser.add_argument(
        "--iou-accept",
        type=float,
        default=0.9,
        help="closed-form IoU (every view) at which no optimiser runs (default 0.9)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.8,
        help="train IoU below this -> status low, exit 3 (default 0.8)",
    )
    parser.add_argument(
        "--fixed-length-mm", type=float, default=None, help="hard override: length"
    )
    parser.add_argument(
        "--fixed-width-mm",
        type=float,
        default=None,
        help="hard override: maximum (full) width",
    )
    parser.add_argument(
        "--fixed-thickness-mm",
        type=float,
        default=None,
        help="hard override: (full) thickness",
    )
    parser.add_argument(
        "--no-refine",
        action="store_true",
        help="closed-form fit only: no soft-silhouette refiner, no CMA-ES (no torch needed)",
    )
    parser.add_argument(
        "--no-cma", action="store_true", help="never run the CMA-ES second attempt"
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="seed for every random path (default 0)"
    )
    parser.add_argument(
        "--emit",
        choices=["glb", "stl", "obj", "scad"],
        help="also write model.<ext> (needs -o)",
    )
    parser.add_argument(
        "--mesh-frame",
        default="object",
        choices=["object", "world"],
        help="frame of the emitted mesh (default object: centred, X = length)",
    )
    parser.add_argument(
        "-o", "--out", metavar="DIR", help="write fit.json, overlays and the mesh here"
    )
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object"
    )


def _fixed(args: argparse.Namespace) -> dict[str, float]:
    pairs = (
        ("length_mm", args.fixed_length_mm),
        ("max_width_mm", args.fixed_width_mm),
        ("thickness_mm", args.fixed_thickness_mm),
    )
    return {k: v for k, v in pairs if v is not None}


def run(args: argparse.Namespace) -> int:
    from ..fit import fit_auto
    from ..mesh_export import emit_mesh
    from ..view_check import render_overlays

    if args.emit and not args.out:
        raise BadInput("--emit needs -o DIR")
    views = [parse_view(v) for v in args.view]
    heldout = [parse_view(v) for v in args.heldout] or None
    fit: dict[str, Any] = fit_auto(
        views,
        template=None if args.template == "auto" else args.template,
        K=args.stations,
        known_depth=args.known_depth,
        planar=args.planar,
        fixed_dims=_fixed(args),
        heldout_views=heldout,
        iou_accept=args.iou_accept,
        min_iou=args.min_confidence,
        max_tries=args.max_tries,
        device=args.device,
        seed=args.seed,
        cma=not args.no_cma, refine=not args.no_refine,
    )
    for entry, spec in zip(fit["views"], args.view, strict=True):
        entry["mask"] = spec.rpartition(":")[0]
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(render_overlays(fit, views)):
            save_png(out / f"overlay-{i}.png", img)
        write_json(out / "fit.json", fit)  # first: a missing optional dependency of the mesh export (trimesh) must not lose the fit
        if args.emit:
            mesh = out / f"model.{args.emit}"
            mesh.write_bytes(emit_mesh(fit, args.emit, frame=args.mesh_frame))
            fit["mesh"] = mesh.name
            write_json(out / "fit.json", fit)
    if args.json:
        print(json.dumps(to_jsonable(fit)))
    else:
        d, c = fit["dims_mm"], fit["confidence"]
        print(
            f"template={fit['template']['name']} method={fit['method']} length={d['length_mm']:.1f} width={d['max_width_mm']:.1f} "
            f"thickness={d['thickness_mm']:.1f} train_min_iou={c['train_min_iou']:.3f} status={c['status']} total={fit['timing_s']['total']:.2f}s"
        )
    return EXIT_LOW_CONFIDENCE if fit["confidence"]["status"] == "low" else 0
