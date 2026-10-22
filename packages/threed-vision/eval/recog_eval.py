#!/usr/bin/env python3
"""Score recognition against hand labels of the carpet photo (`3d segment` output + `labels.json`).

Reproduces the spike's table (RESULT.md section 3): top-1 / top-3 / top-5 of CLIP text (raw and class-mean calibrated) and of
bank retrieval (DINOv2, CLIP, silhouette) over the 33 labelled crops.

    3d segment tools-on-carpet.heic -o seg/                                   # the instances being labelled
    3d bank thumbs refit/tools -o bank/ && 3d bank index bank/                 # optional: a bank to retrieve from
    python packages/threed-vision/eval/recog_eval.py tools-on-carpet.heic seg/instances.json [--bank bank/] [--json out.json]

`labels.json` identifies a labelled crop by its bbox_xywh + area_px (a stable descriptor: instance ids change between runs).
A label is matched to the instance with the highest bbox IoU, accepted at IoU >= `match.bbox_iou_min` and an area ratio inside
`match.area_ratio`; labels without a match are reported and left out of every score (so the n is part of the result).

Calibration pool: the library default (`Recogniser.classify`): class means are taken over the 60 largest instances that cover at
least 2500 px of a 2000x1500 photo, not over the 33 labelled crops and not over small fragments (`--pool-size 0` = every instance).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from threed_vision import bank as bank_mod  # noqa: E402
from threed_vision import io  # noqa: E402
from threed_vision.clip import ClipEncoder, DinoEncoder, crop_masked  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402
from threed_vision.recognise import (  # noqa: E402
    CALIBRATION_MIN_AREA_FRAC,
    CALIBRATION_POOL,
    Recogniser,
    calibration_rows,
    load_instances,
)

KS = (1, 3, 5)
Match = tuple[dict[str, Any], dict[str, Any], float]  # (label, instance, bbox IoU)


def bbox_iou(a: list[int], b: list[int]) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    return inter / max(a[2] * a[3] + b[2] * b[3] - inter, 1)


def match_labels(
    labels: dict[str, Any], instances: list[dict[str, Any]]
) -> tuple[list[Match], list[dict[str, Any]]]:
    """(label, instance, bbox IoU) for every matched label, and the unmatched labels. Instances are used at most once."""
    rule = labels["match"]
    lo, hi = rule["area_ratio"]
    taken: set[int] = set()
    matched: list[Match] = []
    missing: list[dict[str, Any]] = []
    for lab in labels["labels"]:
        best, best_iou = None, 0.0
        for inst in instances:
            if inst["id"] in taken:
                continue
            iou = bbox_iou(lab["bbox_xywh"], inst["bbox_xywh"])
            if iou > best_iou and lo <= inst["area_px"] / lab["area_px"] <= hi:
                best, best_iou = inst, iou
        if best is not None and best_iou >= rule["bbox_iou_min"]:
            taken.add(best["id"])
            matched.append((lab, best, best_iou))
        else:
            missing.append(lab)
    return matched, missing


def hit(truth: str, predicted: list[str], equivalent: dict[str, list[str]]) -> bool:
    return bool(set(predicted) & set(equivalent.get(truth, [truth])))


def score_rows(
    truths: list[str],
    predictions: dict[str, list[list[str]]],
    equivalent: dict[str, list[str]],
) -> dict[str, float]:
    """predictions[name][i] = ranked class labels for crop i -> {name_topK: fraction of crops whose truth is in the top K}."""
    out: dict[str, float] = {}
    for name, ranked in predictions.items():
        for k in KS:
            out[f"{name}_top{k}"] = round(
                sum(
                    hit(t, r[:k], equivalent)
                    for t, r in zip(truths, ranked, strict=True)
                )
                / len(truths),
                2,
            )
    return out


def spike_protocol_bank(
    rgb: np.ndarray,
    masks: list[np.ndarray],
    index: bank_mod.BankIndex,
    clip: ClipEncoder,
    dino: DinoEncoder,
) -> dict[str, list[list[str]]]:
    """The spike's retrieval, for comparison: the raw 192 px thumbnail is embedded as is, the query is the unrotated masked crop."""
    import cv2

    root = index.root or Path(".")
    thumbs = []
    for e in index.entries:
        img = cv2.imread(str(root / e["thumb"]), cv2.IMREAD_COLOR)
        if img is None:
            raise BadInput(f"cannot read {root / e['thumb']}")
        thumbs.append(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    crops = [crop_masked(rgb, m)[0] for m in masks]
    cats = [e["category"] for e in index.entries]
    out: dict[str, list[list[str]]] = {}
    for name, enc in (
        ("clip_bank_spike_protocol", clip),
        ("dino_bank_spike_protocol", dino),
    ):
        sims = enc.images(crops) @ enc.images(thumbs).T
        out[name] = [[cats[j] for j in np.argsort(-row, kind="stable")] for row in sims]
    return out


def reference_table(scores: dict[str, float], reference: dict[str, float]) -> str:
    """Markdown table: rows = methods, columns = top-1/3/5 of this run next to the spike's numbers (RESULT.md section 3)."""
    names = list(dict.fromkeys(k.rsplit("_top", 1)[0] for k in scores))
    lines = [
        "| method | top-1 | top-3 | top-5 | spike top-1 / 3 / 5 |",
        "|---|---|---|---|---|",
    ]
    for n in names:
        ours = " | ".join(f"{scores[f'{n}_top{k}']:.2f}" for k in KS)
        ref = " / ".join(
            f"{reference[f'{n}_top{k}']:.2f}" if f"{n}_top{k}" in reference else "-"
            for k in KS
        )
        lines.append(f"| {n} | {ours} | {ref} |")
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("image", help="the carpet photo that was segmented")
    ap.add_argument("instances", help="instances.json from `3d segment <image> -o DIR`")
    ap.add_argument("--labels", default=str(HERE / "labels.json"))
    ap.add_argument(
        "--bank",
        default=None,
        help="bank index dir from `3d bank index` (enables the bank rows)",
    )
    ap.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    ap.add_argument(
        "--pool-size",
        type=int,
        default=CALIBRATION_POOL,
        help="calibrate over the N largest instances (the spike's protocol: 60; 0 = every instance)",
    )
    ap.add_argument(
        "--pool-min-area-frac",
        type=float,
        default=CALIBRATION_MIN_AREA_FRAC,
        help="calibration pool instances must cover at least this image fraction (2500 px of 2000x1500)",
    )
    ap.add_argument("--json", default=None, help="also write the result here")
    return ap.parse_args(argv)


def text_rows(
    rec: Recogniser,
    rgb: np.ndarray,
    insts: list[dict[str, Any]],
    rows: list[int],
    args: argparse.Namespace,
) -> dict[str, list[list[str]]]:
    """Ranked class labels of the labelled crops from CLIP text prompts, raw and class-mean calibrated."""
    predictions: dict[str, list[list[str]]] = {}
    for name, calibrate in (("clip_text_raw", False), ("clip_text_calibrated", True)):
        res = rec.classify(
            rgb,
            insts,
            None,
            calibrate=calibrate,
            topk=5,
            pool_size=args.pool_size,
            pool_min_area_frac=args.pool_min_area_frac,
        )
        predictions[name] = [[t["label"] for t in res[r]["text_topk"]] for r in rows]
    return predictions


def bank_rows(
    rgb: np.ndarray,
    matched: list[Match],
    index: bank_mod.BankIndex,
    clip: ClipEncoder,
    dino: DinoEncoder,
) -> dict[str, list[list[str]]]:
    """Ranked bank-entry classes of the labelled crops per retrieval metric, plus the spike-protocol rerun."""
    predictions: dict[str, list[list[str]]] = {}
    for metric, name in (
        ("dino", "dino_bank"),
        ("clip", "clip_bank"),
        ("sil", "silhouette_bank"),
    ):
        predictions[name] = [
            [
                h["category"]
                for h in bank_mod.query(
                    index,
                    rgb,
                    5,
                    mask=inst["mask"],
                    metric=metric,
                    clip=clip,
                    dino=dino,
                )
            ]
            for _, inst, _ in matched
        ]
    spike = spike_protocol_bank(
        rgb, [inst["mask"] for _, inst, _ in matched], index, clip, dino
    )
    predictions.update({name: ranked[:5] for name, ranked in spike.items()})
    return predictions


def rerank_hit_rate(
    rec: Recogniser,
    rgb: np.ndarray,
    insts: list[dict[str, Any]],
    index: bank_mod.BankIndex,
    matched: list[Match],
    rows: list[int],
    equivalent: dict[str, list[str]],
    args: argparse.Namespace,
) -> float:
    """What `3d recognise --bank` returns: retrieval restricted to the 3 proposed classes; hit = the right class among its 5 entries."""
    res = rec.classify(
        rgb,
        insts,
        None,
        topk=5,
        bank=index,
        metric="sil",
        rerank_classes=3,
        pool_size=args.pool_size,
        pool_min_area_frac=args.pool_min_area_frac,
    )
    inside = sum(
        1
        for (lab, _, _), r in zip(matched, rows, strict=True)
        if any(
            hit(lab["label"], [b["category"]], equivalent) for b in res[r]["bank_topk"]
        )
    )
    return round(inside / len(matched), 2)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    rgb = io.load_rgb(args.image)
    insts = load_instances(args.instances, (rgb.shape[0], rgb.shape[1]))
    matched, missing = match_labels(labels, insts)
    if not matched:
        raise SystemExit("no label matched an instance: wrong photo or instances.json?")
    truths = [lab["label"] for lab, _, _ in matched]
    row_of = {inst["id"]: n for n, inst in enumerate(insts)}
    rows = [row_of[inst["id"]] for _, inst, _ in matched]
    equivalent = labels.get("equivalent", {})
    pool_rows = calibration_rows(
        [(i["id"], i["mask"]) for i in insts], args.pool_size, args.pool_min_area_frac
    )

    clip, dino = ClipEncoder(args.device), DinoEncoder(args.device)
    rec = Recogniser(args.device, clip=clip, dino=dino)
    predictions = text_rows(rec, rgb, insts, rows, args)
    extra: dict[str, float] = {}
    index = bank_mod.load_index(args.bank) if args.bank else None
    if index is not None:
        predictions.update(bank_rows(rgb, matched, index, clip, dino))
        extra["bank_topk_inside_text_top3_has_true_class"] = rerank_hit_rate(
            rec, rgb, insts, index, matched, rows, equivalent, args
        )
    scores = score_rows(truths, predictions, equivalent)
    out = {
        "n_labelled": len(labels["labels"]),
        "n_matched": len(matched),
        "n_unmatched": len(missing),
        "min_bbox_iou": round(min(iou for *_, iou in matched), 3),
        "max_area_deviation": round(
            max(abs(inst["area_px"] / lab["area_px"] - 1) for lab, inst, _ in matched),
            4,
        ),
        "calibration_pool": len(insts) if pool_rows is None else len(pool_rows),
        "scores": scores,
        **extra,
        "bank": args.bank,
        "bank_textured_fraction": None
        if index is None
        else round(index.textured_fraction, 2),
    }
    print(json.dumps(out, indent=1))
    print(reference_table(scores, labels.get("spike_reference", {})))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    rec.unload()
    clip.unload()
    dino.unload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
