"""vision_fit.py — `3d printer vision fit-head`: fit tier (b)'s logistic head on labelled frames.

Tier (b) (vision_model.py) scores the whole frame with CLIP ViT-B/16 against zero-shot prompts.
Zero-shot has no skill on "part detached" (docs/research/printer-vision.md), so the way to a
detached-part / empty-bed / spaghetti check tuned to YOUR cameras is labelled frames:

    3d printer vision record u1 ~/vision/u1-panel/ok       # frames of healthy prints (DIRs of jpg)
    3d printer vision fit-head --class part_detached \\
        --pos ~/vision/detached/*  --neg ~/vision/ok/* ~/vision/u1-timelapse.mp4

It embeds every frame once (GPU when available), fits an L2-regularised logistic regression on
the unit-length embeddings (numpy only, Newton/IRLS, balanced class weights), measures it by
block cross-validation (contiguous time blocks, so neighbouring frames never sit on both sides),
sets the Finding threshold at the `--fpr` quantile of the OUT-OF-FOLD negatives, and writes
models/tier-b-head.json (other classes already in the file are kept). The head replaces the
zero-shot probability of its class; `vision eval --model` then reports it on any folder.
The held-out experiments (zero-shot vs head vs partial fine-tune) live in tools/vision_train and run
on the owner's laptop over ssh; this CLI is the seconds-on-any-machine path. Measured on held-out
data a head helps spaghetti little and `part_detached` not enough to alert (docs/research/printer-vision.md).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np


def add_fit_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--class", dest="cls", required=True, help="part_detached | empty_bed | spaghetti")
    ap.add_argument("--pos", nargs="+", required=True, help="frames showing the condition (dirs, images, videos)")
    ap.add_argument("--neg", nargs="+", required=True, help="frames that must not raise it")
    ap.add_argument("--every", type=int, default=1, help="keep one frame in N of each source")
    ap.add_argument("--l2", type=float, default=1.0, help="ridge strength on the weights")
    ap.add_argument("--fpr", type=float, default=0.005, help="target false-positive rate of the threshold")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--out", help="head file (default: the models dir, tier-b-head.json)")
    ap.add_argument("--json", action="store_true")


def fit_logistic(x: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 30) -> tuple[np.ndarray, float]:
    """Ridge logistic regression by Newton steps; balanced class weights; the bias is free."""
    import numpy as np

    n, d = x.shape
    pos = max(1, int(y.sum()))
    neg = max(1, n - int(y.sum()))
    sw = np.where(y == 1, n / (2.0 * pos), n / (2.0 * neg))
    xa = np.hstack([x, np.ones((n, 1), x.dtype)]).astype(np.float64)
    w = np.zeros(d + 1)
    reg = np.full(d + 1, l2)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(xa @ w, -30, 30)))
        grad = xa.T @ (sw * (p - y)) + reg * w
        h = (xa * (sw * p * (1 - p))[:, None]).T @ xa + np.diag(reg) + 1e-9 * np.eye(d + 1)
        step = np.linalg.solve(h, grad)
        w -= step
        if float(np.abs(step).max()) < 1e-6:
            break
    return w[:-1].astype(np.float32), float(w[-1])


def sigmoid_scores(x: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
    import numpy as np

    return np.asarray(1.0 / (1.0 + np.exp(-np.clip(x @ w + b, -30, 30))))


def auroc(pos: np.ndarray, neg: np.ndarray) -> float:
    """Mann-Whitney AUROC (ties count half)."""
    import numpy as np

    if pos.size == 0 or neg.size == 0:
        return float("nan")
    s = np.concatenate([pos, neg])
    order = s.argsort(kind="mergesort")
    ranks = np.empty(len(s))
    sorted_s = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def block_folds(n: int, folds: int) -> list[np.ndarray]:
    """Contiguous blocks of indices 0..n-1 (time order), at most `folds` of them."""
    import numpy as np

    return [b for b in np.array_split(np.arange(n), max(1, min(folds, n))) if b.size]


def cross_validate(
    xp: np.ndarray, xn: np.ndarray, *, l2: float, folds: int, fpr: float
) -> dict[str, Any]:
    """Out-of-fold probabilities for both classes by contiguous blocks; AUROC and the
    out-of-fold threshold at the `fpr` quantile of the negatives."""
    import numpy as np

    bp, bn = block_folds(len(xp), folds), block_folds(len(xn), folds)
    k = min(len(bp), len(bn))
    if k < 2:
        return {"folds": 0}
    pos_oof, neg_oof = np.zeros(len(xp)), np.zeros(len(xn))
    for i in range(k):
        tp, tn = bp[i], bn[i]
        mp = np.ones(len(xp), bool)
        mp[tp] = False
        mn = np.ones(len(xn), bool)
        mn[tn] = False
        xtr = np.vstack([xp[mp], xn[mn]])
        ytr = np.r_[np.ones(mp.sum()), np.zeros(mn.sum())]
        w, b = fit_logistic(xtr, ytr, l2)
        pos_oof[tp] = sigmoid_scores(xp[tp], w, b)
        neg_oof[tn] = sigmoid_scores(xn[tn], w, b)
    scored_p = np.concatenate([pos_oof[bp[i]] for i in range(k)])
    scored_n = np.concatenate([neg_oof[bn[i]] for i in range(k)])
    thr = float(np.quantile(scored_n, 1.0 - fpr))
    return {
        "folds": k,
        "auroc": round(auroc(scored_p, scored_n), 4),
        "threshold": round(thr, 4),
        "tpr_at_threshold": round(float((scored_p > thr).mean()), 4),
        "fpr_at_threshold": round(float((scored_n > thr).mean()), 4),
    }


def embed_sources(scorer: Any, paths: list[str], every: int, crop: Any = None) -> np.ndarray:
    import numpy as np
    from vision_detect import iter_frames
    from vision_model import EMBED_DIM, crop_frame

    rows = [
        scorer.embed(crop_frame(img, crop))
        for p in paths
        for _, img in iter_frames(Path(p).expanduser(), every)
    ]
    return np.stack(rows) if rows else np.zeros((0, EMBED_DIM), np.float32)


def run_fit(args: argparse.Namespace) -> int:
    from errors import InvalidArgument, UsageError
    from vision_model import (
        HEAD_NAME,
        MODEL_TAG,
        RULES,
        ModelConfig,
        models_dir,
        open_scorer,
    )

    if args.cls not in RULES:
        raise InvalidArgument("--class", args.cls, sorted(RULES), command="printer vision fit-head")
    missing = [p for p in [*args.pos, *args.neg] if not Path(p).expanduser().exists()]
    if missing:
        raise UsageError(f"fit-head: missing path(s): {', '.join(missing)}", command="printer vision fit-head")
    t0 = time.time()
    # raw embeddings only: a head already on disk must not feed back into its own training
    scorer = open_scorer(ModelConfig(), download=True, use_head=False)
    try:
        xp = embed_sources(scorer, args.pos, args.every)
        xn = embed_sources(scorer, args.neg, args.every)
        device = scorer.device
    finally:
        scorer.close()
    if len(xp) < 5 or len(xn) < 5:
        raise UsageError(
            f"fit-head needs at least 5 frames per side, got {len(xp)} positive / {len(xn)} negative",
            command="printer vision fit-head",
        )
    cv = cross_validate(xp, xn, l2=args.l2, folds=args.folds, fpr=args.fpr)
    import numpy as np

    w, b = fit_logistic(np.vstack([xp, xn]), np.r_[np.ones(len(xp)), np.zeros(len(xn))], args.l2)
    thr = cv.get("threshold")
    if thr is None:  # too few frames for cross-validation: threshold on the training negatives
        thr = round(float(np.quantile(sigmoid_scores(xn, w, b), 1.0 - args.fpr)), 4)
    out = Path(args.out).expanduser() if args.out else models_dir() / HEAD_NAME
    doc: dict[str, Any] = {"version": 1, "model": MODEL_TAG, "classes": {}}
    if out.is_file():
        try:
            old = json.loads(out.read_text())
            if old.get("model") == MODEL_TAG:
                doc = old
        except (OSError, ValueError):
            pass  # an unreadable old head is replaced
    doc["classes"][args.cls] = {
        "weights": [round(float(v), 6) for v in w],
        "bias": round(b, 6),
        "threshold": thr,
        "fitted": {
            "date": time.strftime("%Y-%m-%d", time.gmtime()),
            "frames": {"pos": len(xp), "neg": len(xn)},
            "sources": sorted({Path(p).name for p in [*args.pos, *args.neg]}),
            "l2": args.l2,
            "fpr_target": args.fpr,
            "cross_validation": cv,
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    tmp.replace(out)
    summary = {
        "class": args.cls,
        "head": str(out),
        "device": device,
        "frames": {"pos": len(xp), "neg": len(xn)},
        "cross_validation": cv,
        "threshold": thr,
        "seconds": round(time.time() - t0, 1),
    }
    print(json.dumps(summary, indent=None if args.json else 2))
    return 0
