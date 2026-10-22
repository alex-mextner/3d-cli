"""`view-check`: render a fit into other views with the independent hard rasteriser and flag views it does not explain."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from ..errors import EXIT_LOW_CONFIDENCE, BadInput
from ..io import save_png, to_jsonable
from ._views import parse_view

NAME = "view-check"
SUMMARY = "IoU of a fit (fit.json) against extra views, flag below a threshold; single-photo sanity mode without --view"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("fit", help="fit.json written by `3d fit-silhouette`")
    parser.add_argument(
        "--view",
        action="append",
        default=[],
        metavar="MASK:CAM.json",
        help="extra view (repeatable); none = single-photo sanity mode",
    )
    parser.add_argument(
        "--flag-below",
        type=float,
        default=0.75,
        help="IoU below which a view is flagged (default 0.75)",
    )
    parser.add_argument(
        "--overlay",
        metavar="PNG",
        help="write a review image per view stacked into one PNG (mask green, fit red)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print exactly one JSON object"
    )


def _montage(images: list[np.ndarray]) -> np.ndarray:
    w = max(i.shape[1] for i in images)
    rows = [np.pad(i, ((0, 4), (0, w - i.shape[1]), (0, 0))) for i in images]
    return np.vstack(rows)


def run(args: argparse.Namespace) -> int:
    from ..view_check import check, render_overlays

    p = Path(args.fit)
    if not p.is_file():
        raise BadInput(f"fit file not found: {p}")
    try:
        fit = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BadInput(f"{p} is not valid JSON: {exc}") from exc
    if args.overlay and not args.view:
        raise BadInput("--overlay needs at least one --view")
    views = [parse_view(v) for v in args.view]
    res = check(fit, views, args.flag_below)
    if args.overlay:
        save_png(args.overlay, _montage(render_overlays(fit, views)))
        res["overlay"] = args.overlay
    if args.json:
        print(json.dumps(to_jsonable(res)))
    else:
        for r in res["views"]:
            print(f"{r['name']}: iou={r['iou']:.3f}{'  FLAG' if r['flag'] else ''}")
        failed = [c["name"] for c in res["sanity"]["checks"] if not c["ok"]]
        print(
            f"status={res['status']} mode={res['mode']} sanity_failed={failed or 'none'}"
        )
        for n in res["needs_views"]:
            print(
                f"needs view: azimuth={n['azimuth_deg']:.0f} elevation={n['elevation_deg']:.0f} ({n['reason']})"
            )
    return EXIT_LOW_CONFIDENCE if res["status"] == "flagged" else 0
