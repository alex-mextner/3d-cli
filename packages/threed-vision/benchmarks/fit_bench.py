"""Timing benchmark of the threed_vision fitters: closed form, single-view planar soft fit, 3-view soft fit, on real carpet masks and on
synthetic ground-truth scenes. Prints a markdown table and writes every raw row to --out.

    PYTHONPATH=packages/threed-vision/src python packages/threed-vision/benchmarks/fit_bench.py --device cuda --out gpu.json
    ... --device cpu --out cpu.json

Real masks: `pipe_masks.npy` of the hsw-editor vision spike (98 SAM2 masks of the carpet photo, 2000x1500) with the spike's object ids,
scale 1.263 mm/px (caliper = 239.4 mm), pinhole f = 0.375 * diagonal, plane depth 1184 mm. There is only ONE real view per object (no oblique
second photo exists), so the real-mask "3-view" rows use the real mask plus two views rendered from the single-view fit at +-50 degrees:
the timing is representative (same mask sizes, same optimiser), the accuracy is not evidence of 3-D recovery.

Per-object seconds are end-to-end `timing_s.total` of the call (closed-form start, candidate ranking, refinement, hard-raster scoring);
the first object per device is preceded by a warm-up call so CUDA/torch initialisation is not billed to it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import vision_fit_synth as S  # noqa: E402

from threed_vision.fit import fit_auto  # noqa: E402
from threed_vision.fit_profile import fit_profile  # noqa: E402
from threed_vision.fit_silhouette import fit_object  # noqa: E402
from threed_vision.render import hard_mask, mesh_from_fit  # noqa: E402

CARPET_MASKS = Path.home() / ".cache/hsw-editor/spikes/vision/out/pipe_masks.npy"
CARPET_IDS = {
    82: "caliper",
    70: "screwdriver_a",
    40: "screwdriver_b",
    46: "pliers_red",
    20: "multimeter",
    8: "tape_measure",
    24: "power_supply",
    80: "tin_snips",
    76: "drill_body",
    54: "drill",
    4: "bit_set",
    9: "bit_set_2",
}
MM_PER_PX, W, H = 1.263, 2000, 1500
F_PX = 0.375 * math.hypot(W, H)
PLANE_DEPTH = F_PX * MM_PER_PX
K_CARPET = np.array([[F_PX, 0, W / 2], [0, F_PX, H / 2], [0, 0, 1.0]])


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, math.ceil(q * len(s)) - 1)]


def row_stats(label: str, secs: list[float], extra: str = "") -> dict[str, Any]:
    return {
        "label": label,
        "n": len(secs),
        "median_s": statistics.median(secs),
        "p95_s": pct(secs, 0.95),
        "max_s": max(secs),
        "note": extra,
    }


def orbit_cameras(
    centre: np.ndarray, dist: float, azimuths: tuple[float, ...]
) -> list[tuple[np.ndarray, np.ndarray]]:
    cams = []
    for az in azimuths:
        a = math.radians(az)
        eye = centre + dist * np.array([math.sin(a), 0.0, -math.cos(a)])
        cams.append(S.lookat(eye, centre, np.array([0.0, -1.0, 0.0])))
    return cams


def carpet_rows(
    device: str, repeats: int
) -> list[dict[str, Any]]:
    masks = np.load(CARPET_MASKS, mmap_mode="r")
    rows: list[dict[str, Any]] = []
    for oid, name in CARPET_IDS.items():
        m = np.asarray(masks[oid])
        ys, xs = np.nonzero(m)
        crop = m[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        t_full = [fit_profile(m, MM_PER_PX)["time_ms"] for _ in range(repeats)]
        t_crop = [fit_profile(crop, MM_PER_PX)["time_ms"] for _ in range(repeats)]
        view = {"mask": m, "K": K_CARPET, "R": np.eye(3), "t": np.zeros(3)}
        base = {
            "id": oid,
            "name": name,
            "closed_form_ms_fullframe": statistics.median(t_full),
            "closed_form_ms_cropped": statistics.median(t_crop),
        }
        auto = fit_auto([view], known_depth=PLANE_DEPTH, planar=True, device=device)
        one = fit_object(
            [view],
            None,
            known_depth=PLANE_DEPTH,
            planar=True,
            device=device,
            max_tries=1,
        )
        full = fit_object(
            [view],
            None,
            known_depth=PLANE_DEPTH,
            planar=True,
            device=device,
            max_tries=3,
        )
        base.update(
            auto_method=auto["method"],
            auto_s=auto["timing_s"]["total"],
            auto_iou=auto["confidence"]["train_min_iou"],
            planar_1try_s=one["timing_s"]["total"],
            planar_1try_iou=one["confidence"]["train_min_iou"],
            planar_3try_s=full["timing_s"]["total"],
            planar_3try_iou=full["confidence"]["train_min_iou"],
            planar_tries=full["tries"],
            template=one["template"]["name"],
            length_mm=one["dims_mm"]["length_mm"],
        )
        # real mask + two views rendered from the single-view fit: timing of a 3-view fit with real-sized masks
        V, F = mesh_from_fit(one)
        centre = np.array(one["pose"]["t_mm"])
        cams = [(np.eye(3), np.zeros(3))] + orbit_cameras(
            centre, float(np.linalg.norm(centre)), (-50.0, 50.0)
        )
        views3 = [view] + [
            {
                "mask": hard_mask(V, F, K_CARPET, R, t, (W, H)),
                "K": K_CARPET,
                "R": R,
                "t": t,
            }
            for R, t in cams[1:]
        ]
        three = fit_object(views3, None, device=device, max_tries=1)
        base.update(
            threeview_1try_s=three["timing_s"]["total"],
            threeview_1try_iou=three["confidence"]["train_min_iou"],
        )
        rows.append(base)
        print(
            json.dumps(
                {
                    k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in base.items()
                }
            ),
            flush=True,
        )
    return rows


def synthetic_rows(device: str, poses: int) -> list[dict[str, Any]]:
    rows = []
    for name in S.GT:
        for seed in range(poses):
            top = S.topdown_scene(name, 20.0 + 40.0 * seed)
            tmpl = top["spec"]["template"]
            one = fit_object(
                top["views"],
                tmpl,
                known_depth=top["depth"],
                planar=True,
                device=device,
                max_tries=1,
            )
            sc = S.scene(name, seed)
            three = fit_object(
                sc["views"],
                tmpl,
                device=device,
                max_tries=1,
                heldout_views=sc["heldout"],
            )
            three3 = fit_object(
                sc["views"],
                tmpl,
                device=device,
                max_tries=3,
                heldout_views=sc["heldout"],
            )
            cf = fit_auto(
                sc["views"], template=tmpl, refine=False, heldout_views=sc["heldout"]
            )
            row = {
                "shape": name,
                "seed": seed,
                "planar_1try_s": one["timing_s"]["total"],
                "planar_1try_iou": one["confidence"]["train_min_iou"],
                "threeview_1try_s": three["timing_s"]["total"],
                "threeview_3try_s": three3["timing_s"]["total"],
                "threeview_tries": three3["tries"],
                "threeview_train_min_iou": three["confidence"]["train_min_iou"],
                "threeview_heldout_iou": three["confidence"]["heldout_iou"],
                "threeview_L_err_pct": abs(
                    three["dims_mm"]["length_mm"] / sc["spec"]["L"] - 1
                )
                * 100,
                "closed_form_3view_s": cf["timing_s"]["total"],
            }
            rows.append(row)
            print(
                json.dumps(
                    {
                        k: (round(v, 3) if isinstance(v, float) else v)
                        for k, v in row.items()
                    }
                ),
                flush=True,
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--poses", type=int, default=4, help="synthetic poses per shape")
    ap.add_argument(
        "--repeats", type=int, default=15, help="closed-form repetitions per mask"
    )
    ap.add_argument("--skip-carpet", action="store_true")
    args = ap.parse_args()
    import torch

    env = {
        "device": args.device,
        "torch": torch.__version__,
        "threads": torch.get_num_threads(),
        "cpus": os.cpu_count(),
        "load_avg_start": os.getloadavg(),
    }
    if args.device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    # warm-up: CUDA context / first-call overheads are not an object's cost
    w = S.topdown_scene("screwdriver", 30.0)
    fit_object(
        w["views"],
        "lathe",
        known_depth=w["depth"],
        planar=True,
        device=args.device,
        max_tries=1,
    )
    carpet: list[dict[str, Any]] = []
    if not args.skip_carpet:
        carpet = carpet_rows(args.device, args.repeats)
    synth = synthetic_rows(args.device, args.poses)
    env["load_avg_end"] = os.getloadavg()
    if args.device == "cuda":
        env["peak_vram_mb"] = round(torch.cuda.max_memory_allocated() / 2**20)
    summary = []
    if carpet:
        summary += [
            row_stats(
                "carpet closed form, full 2000x1500 frame (ms)",
                [r["closed_form_ms_fullframe"] / 1e3 for r in carpet],
            ),
            row_stats(
                "carpet closed form, bbox crop (ms)",
                [r["closed_form_ms_cropped"] / 1e3 for r in carpet],
            ),
            row_stats(
                "carpet fit_auto single-view planar (closed form first)",
                [r["auto_s"] for r in carpet],
                f"{sum(r['auto_method'] == 'closed_form' for r in carpet)}/{len(carpet)} accepted at closed form",
            ),
            row_stats(
                "carpet single-view planar soft fit, first attempt",
                [r["planar_1try_s"] for r in carpet],
            ),
            row_stats(
                "carpet single-view planar soft fit, up to 3 tries",
                [r["planar_3try_s"] for r in carpet],
            ),
            row_stats(
                "carpet 3-view soft fit (real mask + 2 rendered views), first attempt",
                [r["threeview_1try_s"] for r in carpet],
            ),
        ]
    summary += [
        row_stats(
            "synthetic single-view planar soft fit, first attempt",
            [r["planar_1try_s"] for r in synth],
        ),
        row_stats(
            "synthetic 3-view soft fit, first attempt",
            [r["threeview_1try_s"] for r in synth],
        ),
        row_stats(
            "synthetic 3-view soft fit, up to 3 tries",
            [r["threeview_3try_s"] for r in synth],
        ),
        row_stats(
            "synthetic 3-view closed form (fit_auto, refine=False)",
            [r["closed_form_3view_s"] for r in synth],
        ),
    ]
    Path(args.out).write_text(
        json.dumps(
            {"env": env, "carpet": carpet, "synthetic": synth, "summary": summary},
            indent=1,
            default=float,
        ),
        encoding="utf-8",
    )
    print(
        f"\n{args.device}: torch {env['torch']}, {env['threads']} threads, load {env['load_avg_start'][0]:.1f} -> {env['load_avg_end'][0]:.1f}"
    )
    print(
        "| measurement | n | median s | p95 s | max s | note |\n|---|---|---|---|---|---|"
    )
    for s in summary:
        print(
            f"| {s['label']} | {s['n']} | {s['median_s']:.3f} | {s['p95_s']:.3f} | {s['max_s']:.3f} | {s['note']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
