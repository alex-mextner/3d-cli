#!/usr/bin/env python3
"""Score `Segmenter` variants on the carpet photo: warm / cold time, recall of the 33 hand labels and mask parity with a reference run.

    python packages/threed-vision/eval/segment_recall.py tools-on-carpet.heic --device cuda \\
        --variant accurate='{"quality":"accurate"}' --variant fast='{"quality":"fast"}' \\
        --warm 2 --out res.json --masks-dir masks/ --overlay-dir overlays/
    python packages/threed-vision/eval/segment_recall.py tools-on-carpet.heic --device cpu \\
        --variant fast-cpu='{}' --reference masks/accurate.npz --out cpu.json

* **recall** = the fraction of the 33 labels in `labels.json` (bbox_xywh of the spike's hand-checked masks) for which some instance
  has a bbox IoU >= `--iou` (0.5). A label may be explained by any instance (a part of a tool counts only if its box overlaps the
  labelled box that much); the labels that no instance reaches are listed.
* **parity** (`--reference ref.npz`, written by `--masks-dir` of an earlier run): the fraction of the reference masks that this run
  reproduces with a mask IoU >= `--parity-iou` (0.7). This is the CPU-vs-GPU number of 3d-cli #478.
* time: `cold` is the first `run()` of a fresh `Segmenter` (it builds the models; the weights are in the page cache), `warm` the
  median of `--warm` further runs (`timing_s.total`, stage breakdown of the last one). A variant is a JSON dict of `Segmenter`
  keyword arguments; the device comes from `--device`.

Heavy: run it under `flock /tmp/3d-cli-heavy.lock systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 nice -n 19 ...`.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from threed_vision import io  # noqa: E402
from threed_vision.segment import Segmenter  # noqa: E402


def bbox_iou(a: list[int], b: list[int]) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    return inter / max(a[2] * a[3] + b[2] * b[3] - inter, 1)


def found(
    label: dict[str, Any], instances: list[dict[str, Any]], iou_min: float
) -> bool:
    """True when some instance's bbox reaches `iou_min` IoU with the label's bbox."""
    return any(
        bbox_iou(label["bbox_xywh"], i["bbox_xywh"]) >= iou_min for i in instances
    )


def label_recall(
    labels: list[dict[str, Any]], instances: list[dict[str, Any]], iou_min: float
) -> tuple[float, list[dict[str, Any]]]:
    """(recall, missed labels): a label is found when some instance's bbox reaches `iou_min` IoU with it."""
    missed = [lab for lab in labels if not found(lab, instances, iou_min)]
    return (len(labels) - len(missed)) / max(len(labels), 1), missed


def boxes_intersect(a: list[int], b: list[int]) -> bool:
    return not (
        a[0] + a[2] <= b[0]
        or b[0] + b[2] <= a[0]
        or a[1] + a[3] <= b[1]
        or b[1] + b[3] <= a[1]
    )


def parity(ref: list[np.ndarray], masks: list[np.ndarray], iou_min: float) -> float:
    """Fraction of `ref` masks with a mask in `masks` at mask IoU >= `iou_min` (bounding boxes only reject disjoint pairs)."""
    boxes = [io.mask_bbox_xywh(m) for m in masks]
    hit = 0
    for r in ref:
        rb = io.mask_bbox_xywh(r)
        if any(
            boxes_intersect(rb, b) and io.mask_iou(r, m) >= iou_min
            for b, m in zip(boxes, masks, strict=True)
        ):
            hit += 1
    return hit / max(len(ref), 1)


def save_masks(path: Path, masks: list[np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shape = np.array(masks[0].shape) if masks else np.zeros(2, int)
    np.savez_compressed(
        path, shape=shape, bits=np.array([np.packbits(m.ravel()) for m in masks])
    )


def load_masks(path: Path) -> list[np.ndarray]:
    z = np.load(path)
    h, w = (int(v) for v in z["shape"])
    return [np.unpackbits(b)[: h * w].reshape(h, w).astype(bool) for b in z["bits"]]


def run_variant(
    name: str, kwargs: dict[str, Any], rgb: np.ndarray, device: str, warm: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    seg = Segmenter(device=device, **kwargs)
    try:
        cold = seg.run(rgb)
        runs = [seg.run(rgb) for _ in range(warm)]
    finally:
        seg.unload()
    last = runs[-1] if runs else cold
    row = {
        "variant": name,
        "kwargs": kwargs,
        "device": last["device"],
        "cold_s": cold["timing_s"]["total"],
        "warm_s": round(statistics.median(r["timing_s"]["total"] for r in runs), 3)
        if runs
        else None,
        "stages_s": last["timing_s"],
        "instances": len(last["instances"]),
        "n_amg": last.get("n_amg"),
        "n_owl_added": last.get("n_owl_added"),
        "vram_mb": last["vram_mb"],
    }
    return row, last


def table(rows: list[dict[str, Any]]) -> str:
    head = "| variant | device | cold s | warm s | instances | recall 33 | wrench / hammer | parity | missed labels |\n|---|---|---|---|---|---|---|---|---|"
    lines = [head]
    for r in rows:
        par = "-" if r.get("parity") is None else f"{r['parity'] * 100:.0f} %"
        lines.append(
            f"| {r['variant']} | {r['device']} | {r['cold_s']} | {r['warm_s']} | {r['instances']} | "
            f"{r['recall'] * 100:.0f} % ({r['n_found']}/{r['n_labels']}) | "
            f"{' / '.join('yes' if ok else 'no' for ok in r['extra'].values()) or '-'} | {par} | "
            f"{', '.join(m['label'] for m in r['missed']) or '-'} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("image")
    ap.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="Segmenter kwargs as JSON (repeatable)",
    )
    ap.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    ap.add_argument(
        "--warm",
        type=int,
        default=2,
        help="warm runs per variant after the cold one (default 2)",
    )
    ap.add_argument("--labels", default=str(HERE / "labels.json"))
    ap.add_argument(
        "--iou",
        type=float,
        default=0.5,
        help="bbox IoU that counts a label as found (default 0.5)",
    )
    ap.add_argument(
        "--reference",
        help="masks .npz of a reference run (written by --masks-dir) for the parity column",
    )
    ap.add_argument("--parity-iou", type=float, default=0.7)
    ap.add_argument(
        "--masks-dir",
        help="write each variant's masks to DIR/<variant>.npz (parity reference for later runs)",
    )
    ap.add_argument("--overlay-dir", help="write DIR/<variant>.jpg overlays")
    ap.add_argument("--out", help="write the raw result rows as JSON")
    args = ap.parse_args(argv)

    variants: list[tuple[str, dict[str, Any]]] = []
    for spec in args.variant or ["default={}"]:
        name, _, raw = spec.partition("=")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            ap.error(
                f"--variant {spec!r}: the name must be a plain file name (letters, digits, . _ -)"
            )
        try:
            variants.append((name, json.loads(raw or "{}")))
        except ValueError as exc:
            ap.error(f"--variant {spec!r}: {exc}")
    label_doc = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    labels = label_doc["labels"]
    extra = label_doc.get("extra_labels", {}).get("labels", [])
    rgb = io.load_rgb(args.image)
    ref = load_masks(Path(args.reference)) if args.reference else None

    rows: list[dict[str, Any]] = []
    for name, kwargs in variants:
        row, last = run_variant(name, kwargs, rgb, args.device, args.warm)
        insts = last["instances"]
        row["recall"], row["missed"] = label_recall(labels, insts, args.iou)
        row["n_labels"] = len(labels)
        # objects annotated by eye (wrench, hammer): found as ONE instance at bbox IoU >= --iou?
        row["extra"] = {lab["label"]: found(lab, insts, args.iou) for lab in extra}
        row["n_found"] = row["n_labels"] - len(row["missed"])
        masks = [i["mask"] for i in insts]
        row["parity"] = parity(ref, masks, args.parity_iou) if ref is not None else None
        if args.masks_dir:
            save_masks(Path(args.masks_dir) / f"{name}.npz", masks)
        if args.overlay_dir:
            import cv2

            out = Path(args.overlay_dir) / f"{name}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(
                str(out), cv2.cvtColor(io.draw_overlay(rgb, masks), cv2.COLOR_RGB2BGR)
            )
        rows.append(row)
        print(
            json.dumps({k: v for k, v in row.items() if k != "missed"}),
            file=sys.stderr,
            flush=True,
        )
    print(table(rows))
    if args.out:
        Path(args.out).write_text(
            json.dumps(io.to_jsonable(rows), indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
