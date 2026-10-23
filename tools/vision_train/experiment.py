"""experiment.py — does anything beat the zero-shot tier (b) on our labelled frames? Held-out numbers.

Compares, on the frames of a `dataset.py build [--third-party]` directory:
  zeroshot   the shipped prompt scores of CLIP ViT-B/16 (lib/vision_model.py), no training
  head       logistic head on the frozen CLIP B/16 embedding (what `3d printer vision fit-head` fits)
  finetune   partial fine-tune of the last N vision blocks + projection + a 4-way classifier
             initialised from the prompts (fp16 autocast, small batch, VRAM capped)
  tier_a     (optional, --tier-a FILE.json {id: q}) the tier (a) verified score, spaghetti only

HELD OUT BY GROUP. Every frame has a `group` (one print / event / video; third-party frames: the
video for emptydeck, else the whole source). Groups are dealt over --folds folds, stratified so
each fold gets groups of every class where possible; a group is never in two folds, so a frame is
always scored by a model that never saw its print. The scores of the held-out folds are pooled.

PER CLASS (spaghetti, part_detached, empty_bed), positives = frames of that class, negatives =
`healthy` frames, over the subsets own / third_party / all: n, AUROC, a 95 % cluster-bootstrap
interval over groups, recall at 1 % and 5 % false-positive rate (the threshold is taken from the
held-out negatives themselves: an oracle that flatters every method equally), the per-fold AUROCs,
and `vision_detect._table` precision/recall at fixed thresholds. Also VRAM peak and wall time.
Subset `matched` keeps only the healthy frames of the sources that have positives of the class
(same cameras/videos): pooled negatives let a model win by recognising the source, not the condition.
A class with only a handful of groups (part_detached: ONE own event) yields numbers that are not
evidence; they are printed with their group counts so that nobody reads them as such.

    experiment.py DATASET OUT [--folds 5] [--blocks 2] [--epochs 4] [--batch 16] [--lr 1e-5]
                  [--methods zeroshot,head,finetune] [--tier-a tier_a.json] [--device auto] [--smoke]

The VRAM cap comes from THREED_VRAM_CAP_MIB (set by hostrun.py; default 4608): torch's allocator is
limited to the cap minus 700 MiB for the CUDA context, and the run asserts the peak afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for cand in (HERE.parent.parent / "lib", HERE.parent / "lib"):
    if (cand / "vision_model.py").is_file():
        sys.path.insert(0, str(cand))
        break

CLASSES = ("spaghetti", "part_detached", "empty_bed")
LABELS = ("healthy", *CLASSES)
CONTEXT_MIB = 700  # CUDA context + cuDNN workspace outside torch's allocator
CACHE_SIDE = 288  # squashed copy kept for augmentation (random crops are cut from it)
FPRS = (0.01, 0.05)


# ---- data ----------------------------------------------------------------------------------------


def load_rows(dataset: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in (dataset / "labels.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = [r for r in rows if r["label"] in LABELS]
    for r in rows:
        r["path"] = str(dataset / r["file"])
        r["domain"] = "own" if r["provenance"].startswith("own") else "third_party"
    return rows


def load_images(rows: list[dict[str, Any]], threads: int) -> tuple[Any, Any]:
    """(uint8 RGB (N,224,224,3) as vision_model.preprocess squashes it, uint8 RGB (N,288,288,3))."""
    import cv2
    import numpy as np

    def one(r: dict[str, Any]) -> tuple[Any, Any]:
        bgr = cv2.imread(r["path"])
        if bgr is None:
            raise SystemExit(f"cannot read {r['path']}")
        a = cv2.cvtColor(
            cv2.resize(bgr, (224, 224), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB
        )
        b = cv2.cvtColor(
            cv2.resize(bgr, (CACHE_SIDE, CACHE_SIDE), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2RGB,
        )
        return a, b

    with ThreadPoolExecutor(threads) as ex:
        out = list(ex.map(one, rows))
    return np.stack([a for a, _ in out]), np.stack([b for _, b in out])


def regroup_by_setup(rows: list[dict[str, Any]], emb: Any, k: int) -> None:
    """Leave-SETUP-out: the emptydeck videos come from a few printers/cameras, so a video-level
    split still lets a head learn "this printer's bed". Cluster the videos by the mean embedding of
    their frames (k-means, fixed seed) and make each cluster one group."""
    import numpy as np

    videos = sorted({r["group"] for r in rows if r["source"] == "emptydeck"})
    if len(videos) < k:
        return
    mean = np.stack([emb[[i for i, r in enumerate(rows) if r["group"] == v]].mean(0) for v in videos])
    mean /= np.linalg.norm(mean, axis=1, keepdims=True)
    rng = np.random.RandomState(0)
    centre = mean[rng.choice(len(videos), k, replace=False)]
    for _ in range(50):
        assign = np.argmax(mean @ centre.T, axis=1)
        new = np.stack([mean[assign == c].mean(0) if (assign == c).any() else centre[c] for c in range(k)])
        new /= np.linalg.norm(new, axis=1, keepdims=True)
        if np.allclose(new, centre):
            break
        centre = new
    of = dict(zip(videos, assign.tolist(), strict=True))
    for r in rows:
        if r["source"] == "emptydeck":
            r["group"] = f"emptydeck-setup{of[r['group']]}"


def make_folds(rows: list[dict[str, Any]], k: int) -> list[int]:
    """fold index per row: whole groups, dealt so that each label's groups spread over the folds."""
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        groups[r["group"]][r["label"]] += 1
    total: dict[str, int] = defaultdict(int)
    for g in groups.values():
        for lab, n in g.items():
            total[lab] += n
    order = sorted(
        groups,
        key=lambda g: (
            min(total[lab] for lab in groups[g]),
            -sum(groups[g].values()),
            g,
        ),
    )
    load: list[dict[str, int]] = [defaultdict(int) for _ in range(k)]
    fold_of: dict[str, int] = {}
    for g in order:
        rare = min(groups[g], key=lambda lab: total[lab])
        f = min(range(k), key=lambda i: (load[i][rare], sum(load[i].values()), i))
        fold_of[g] = f
        for lab, n in groups[g].items():
            load[f][lab] += n
    return [fold_of[r["group"]] for r in rows]


# ---- metrics -------------------------------------------------------------------------------------


def tpr_at_fpr(pos: Any, neg: Any, fpr: float) -> float:
    import numpy as np

    thr = float(np.quantile(neg, 1.0 - fpr))
    return float((pos > thr).mean())


def class_metrics(
    scores: Any,
    rows: list[dict[str, Any]],
    folds: list[int],
    cls: str,
    domain: str,
    *,
    boot: int = 200,
) -> dict[str, Any] | None:
    import numpy as np
    import vision_fit as vf
    import vision_model as vm
    from vision_detect import _table

    # "matched": third-party frames only, and the healthy ones only from the SOURCES that have
    # positives of this class (same cameras, same videos). Pooled negatives from other sources let a model win by
    # recognising the source, not the condition (emptydeck bed-camera views vs everything else).
    pos_sources = {r["source"] for r in rows if r["label"] == cls and r["domain"] == "third_party"}
    idx = [
        i
        for i, r in enumerate(rows)
        if (domain == "all" or r["domain"] == ("third_party" if domain == "matched" else domain))
        and (domain != "matched" or r["source"] in pos_sources)
        and r["label"] in (cls, "healthy")
        and not np.isnan(scores[i])
    ]
    pos = [i for i in idx if rows[i]["label"] == cls]
    neg = [i for i in idx if rows[i]["label"] == "healthy"]
    if not pos or not neg:
        return None
    sp, sn = scores[pos], scores[neg]
    per_fold = []
    for f in sorted({folds[i] for i in idx}):
        fp, fn = [i for i in pos if folds[i] == f], [i for i in neg if folds[i] == f]
        if fp and fn:
            per_fold.append(round(vf.auroc(scores[fp], scores[fn]), 3))
    g_pos = sorted({rows[i]["group"] for i in pos})
    g_neg = sorted({rows[i]["group"] for i in neg})
    rng = np.random.RandomState(0)
    by_group: dict[str, list[int]] = defaultdict(list)
    for i in idx:
        by_group[rows[i]["group"]].append(i)
    aucs = []
    for _ in range(boot):
        pick = rng.choice(len(by_group), len(by_group))
        names = list(by_group)
        sel = [i for j in pick for i in by_group[names[j]]]
        bp = [scores[i] for i in sel if rows[i]["label"] == cls]
        bn = [scores[i] for i in sel if rows[i]["label"] == "healthy"]
        if bp and bn:
            aucs.append(vf.auroc(np.array(bp), np.array(bn)))
    ci = (
        [
            round(float(np.quantile(aucs, 0.025)), 3),
            round(float(np.quantile(aucs, 0.975)), 3),
        ]
        if len(aucs) >= 20
        else None
    )
    return {
        "n_pos": len(pos),
        "n_neg": len(neg),
        "groups_pos": len(g_pos),
        "groups_neg": len(g_neg),
        "auroc": round(vf.auroc(sp, sn), 4),
        "auroc_ci95_group_bootstrap": ci,
        "tpr_at_1pct_fpr": round(tpr_at_fpr(sp, sn, 0.01), 4),
        "tpr_at_5pct_fpr": round(tpr_at_fpr(sp, sn, 0.05), 4),
        "auroc_per_fold": per_fold,
        "table": _table(list(map(float, sp)), list(map(float, sn)), vm.EVAL_THRESHOLDS),
    }


def all_metrics(
    method_scores: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    folds: list[int],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for method, per_class in method_scores.items():
        for cls, scores in per_class.items():
            for domain in ("own", "third_party", "matched", "all"):
                m = class_metrics(scores, rows, folds, cls, domain)
                if m is not None:
                    out[f"{method}/{cls}/{domain}"] = m
    return out


# ---- torch side ----------------------------------------------------------------------------------


class Vision:
    """CLIP ViT-B/16 vision tower + projection on `device`, fed uint8 RGB 224 batches."""

    def __init__(self, device: str, dtype: Any) -> None:
        import torch
        import vision_model as vm
        from transformers import CLIPVisionModelWithProjection

        self.torch = torch
        self.device = device
        self.model = (
            CLIPVisionModelWithProjection.from_pretrained(
                vm.BASE_MODEL_ID, revision=vm.BASE_REVISION, dtype=dtype
            )
            .to(device)
            .eval()
        )
        self.mean = torch.tensor(vm.MEAN, device=device).view(1, 3, 1, 1)
        self.std = torch.tensor(vm.STD, device=device).view(1, 3, 1, 1)

    def tensor(self, batch_u8: Any) -> Any:
        """uint8 (B,H,W,3) numpy RGB -> normalised float (B,3,224,224); H/W other than 224 are resized."""
        torch = self.torch
        x = (
            torch.from_numpy(batch_u8).to(self.device).permute(0, 3, 1, 2).float()
            / 255.0
        )
        if x.shape[-1] != 224:
            x = torch.nn.functional.interpolate(
                x, size=(224, 224), mode="bilinear", antialias=True, align_corners=False
            )
        return (x - self.mean) / self.std

    def embed(self, x: Any) -> Any:
        e = self.model(
            pixel_values=x.to(next(self.model.parameters()).dtype)
        ).image_embeds
        return self.torch.nn.functional.normalize(e.float(), dim=-1)


def frozen_embeddings(vis: Vision, imgs: Any, batch: int = 64) -> Any:
    import numpy as np

    with vis.torch.no_grad():
        out = [
            vis.embed(vis.tensor(imgs[i : i + batch])).cpu().numpy()
            for i in range(0, len(imgs), batch)
        ]
    return np.concatenate(out)


def finetune_fold(
    vis: Vision,
    imgs224: Any,
    imgs_big: Any,
    rows: list[dict[str, Any]],
    train: list[int],
    test: list[int],
    args: argparse.Namespace,
    seed: int,
) -> Any:
    """Train on `train` (class-balanced sampling), return (len(test), 4) softmax probabilities."""
    import numpy as np
    import torch
    import vision_model as vm

    rng = np.random.RandomState(seed)
    torch.manual_seed(seed)
    model = (
        vis.model.float().train()
    )  # fp32 master weights; autocast does the fp16 maths
    for p in model.parameters():
        p.requires_grad = False
    layers = model.vision_model.encoder.layers
    trainable = [
        *layers[-args.blocks :],
        model.vision_model.post_layernorm,
        model.visual_projection,
    ]
    for m in trainable:
        for p in m.parameters():
            p.requires_grad = True
    start = {
        k: v.detach().clone()
        for k, v in model.state_dict().items()
        if any(
            k.startswith(f"vision_model.encoder.layers.{len(layers) - j}.")
            for j in range(1, args.blocks + 1)
        )
        or k.startswith(("vision_model.post_layernorm", "visual_projection"))
    }
    table = vm.PromptTable.load()
    w0 = np.stack(
        [
            table.emb[
                [i for i, o in enumerate(table.owner) if table.classes[o] == c]
            ].mean(0)
            for c in ("healthy", *CLASSES)
        ]
    )
    clf = torch.nn.Linear(512, 4).to(vis.device)
    clf.weight.data = torch.tensor(
        w0 / np.linalg.norm(w0, axis=1, keepdims=True) * vm.LOGIT_SCALE,
        dtype=torch.float32,
        device=vis.device,
    )
    clf.bias.data.zero_()
    opt = torch.optim.AdamW(
        [
            {"params": [p for m in trainable for p in m.parameters()], "lr": args.lr},
            {"params": clf.parameters(), "lr": args.lr * 100},
        ],
        weight_decay=0.01,
    )
    scaler = torch.amp.GradScaler(enabled=vis.device == "cuda")
    y_all = np.array([LABELS.index(rows[i]["label"]) for i in range(len(rows))])
    counts = np.bincount(y_all[train], minlength=4).astype(np.float64)
    class_w = np.where(counts > 0, 1.0 / np.maximum(counts, 1), 0.0)
    p_sample = class_w[y_all[train]]
    p_sample /= p_sample.sum()
    steps = args.smoke_steps or max(1, len(train) // args.batch)
    for epoch in range(1 if args.smoke_steps else args.epochs):
        for step in range(steps):
            pick = [
                train[j] for j in rng.choice(len(train), size=args.batch, p=p_sample)
            ]
            batch = np.stack([crop_resize(imgs_big[i], rng) for i in pick])
            y = torch.tensor(y_all[pick], device=vis.device)
            with torch.autocast(
                device_type=vis.device,
                dtype=torch.float16,
                enabled=vis.device == "cuda",
            ):
                logits = clf(vis.embed(vis.tensor(batch)))
            loss = torch.nn.functional.cross_entropy(logits.float(), y)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            if step % 50 == 0:
                print(
                    f"  epoch {epoch} step {step}/{steps} loss {loss.item():.4f}",
                    flush=True,
                )
    model.eval()
    probs = []
    with (
        torch.no_grad(),
        torch.autocast(
            device_type=vis.device, dtype=torch.float16, enabled=vis.device == "cuda"
        ),
    ):
        for i in range(0, len(test), 64):
            idx = test[i : i + 64]
            probs.append(
                clf(vis.embed(vis.tensor(imgs224[idx])))
                .float()
                .softmax(-1)
                .cpu()
                .numpy()
            )
    with torch.no_grad():  # back to the pretrained weights for the next fold
        model.load_state_dict({**model.state_dict(), **start})
    return np.concatenate(probs)


def crop_resize(img: Any, rng: Any) -> Any:
    """Random crop of 82-100 % of each side from the 288 px cache, resized to 224, brightness jitter."""
    import cv2
    import numpy as np

    s = img.shape[0]
    w, h = (int(s * rng.uniform(0.82, 1.0)) for _ in range(2))
    x0, y0 = int(rng.uniform(0, s - w + 1)), int(rng.uniform(0, s - h + 1))
    out = cv2.resize(
        img[y0 : y0 + h, x0 : x0 + w], (224, 224), interpolation=cv2.INTER_AREA
    ).astype(np.float32)
    return np.clip(out * rng.uniform(0.8, 1.2) + rng.uniform(-15, 15), 0, 255).astype(
        np.uint8
    )


def vram_report() -> dict[str, float]:
    import torch

    if not torch.cuda.is_available():
        return {}
    return {
        "allocated_peak_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1),
        "reserved_peak_mib": round(torch.cuda.max_memory_reserved() / 2**20, 1),
    }


# ---- main ----------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import numpy as np
    import torch
    import vision_fit as vf
    import vision_model as vm

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("dataset", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument(
        "--blocks", type=int, default=2, help="last N vision blocks to fine-tune"
    )
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0, help="offset of the fine-tune seeds (seed = fold + offset)")
    ap.add_argument("--setup-clusters", type=int, default=0, help="cluster the emptydeck videos into K printer setups and hold setups out")
    ap.add_argument("--tag", default="", help="write into OUT/<tag>/ (several runs per pull)")
    ap.add_argument("--methods", default="zeroshot,head,finetune")
    ap.add_argument(
        "--tier-a", type=Path, help="JSON {frame id: tier (a) verified score q}"
    )
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="a few frames and steps: checks the pipeline, not a result",
    )
    args = ap.parse_args(argv)
    args.smoke_steps = 3 if args.smoke else 0
    methods = args.methods.split(",")
    device = "cuda" if args.device != "cpu" and torch.cuda.is_available() else "cpu"
    if args.device == "cuda" and device != "cuda":
        raise SystemExit("--device cuda but torch.cuda.is_available() is False")
    threads = int(os.environ.get("THREED_MAX_THREADS", "6"))
    torch.set_num_threads(threads)
    cap_mib = int(os.environ.get("THREED_VRAM_CAP_MIB", "4608"))
    if device == "cuda":
        total = torch.cuda.get_device_properties(0).total_memory / 2**20
        torch.cuda.set_per_process_memory_fraction(
            min(1.0, (cap_mib - CONTEXT_MIB) / total)
        )
        torch.cuda.reset_peak_memory_stats()
    rows = load_rows(args.dataset)
    if args.smoke:
        keep: list[dict[str, Any]] = []
        for lab in LABELS:
            keep += [r for r in rows if r["label"] == lab][:30]
        rows = keep
    t0 = time.time()
    imgs224, imgs_big = load_images(rows, threads)
    timings = {"load_images_s": round(time.time() - t0, 1)}
    vis = Vision(device, torch.float16 if device == "cuda" else torch.float32)
    t0 = time.time()
    emb = frozen_embeddings(vis, imgs224)
    timings["embed_s"] = round(time.time() - t0, 1)
    if args.setup_clusters:
        regroup_by_setup(rows, emb, args.setup_clusters)
    folds = make_folds(rows, 2 if args.smoke else args.folds)
    k = max(folds) + 1
    labels = np.array([r["label"] for r in rows])
    summary = {
        "frames": {lab: int((labels == lab).sum()) for lab in LABELS},
        "groups": {
            lab: len({r["group"] for r in rows if r["label"] == lab}) for lab in LABELS
        },
        "domains": {
            d: sum(r["domain"] == d for r in rows) for d in ("own", "third_party")
        },
        "folds": k,
        "fold_sizes": [int(sum(f == i for f in folds)) for i in range(k)],
        "setup_clusters": args.setup_clusters,
    }
    print(json.dumps(summary), flush=True)
    scores: dict[str, dict[str, Any]] = {}
    if "zeroshot" in methods:
        table = vm.PromptTable.load()
        probs = [table.probs(e) for e in emb]
        scores["zeroshot"] = {c: np.array([p[c] for p in probs]) for c in CLASSES}
    if "tier_a" in methods and not args.tier_a:
        raise SystemExit("--methods tier_a needs --tier-a FILE.json ({id: q}, tier_a_scores.py)")
    if args.tier_a:
        q = json.loads(args.tier_a.read_text())
        # a frame tier (a) never scored is NaN (excluded), not a confident 0.0
        scores["tier_a"] = {
            "spaghetti": np.array([float(q[r["id"]]) if r["id"] in q else np.nan for r in rows])
        }
    if "head" in methods:
        t0 = time.time()
        head_scores = {c: np.zeros(len(rows)) for c in CLASSES}
        for f in range(k):
            tr = [i for i in range(len(rows)) if folds[i] != f]
            te = [i for i in range(len(rows)) if folds[i] == f]
            for c in CLASSES:
                pos = [i for i in tr if labels[i] == c]
                neg = [i for i in tr if labels[i] == "healthy"]
                if len(pos) < 5 or len(neg) < 5:
                    head_scores[c][te] = (
                        np.nan
                    )  # nothing to learn from: no score, not a made-up one
                    continue
                w, b = vf.fit_logistic(
                    np.vstack([emb[pos], emb[neg]]),
                    np.r_[np.ones(len(pos)), np.zeros(len(neg))],
                    args.l2,
                )
                head_scores[c][te] = vf.sigmoid_scores(emb[te], w, b)
        scores["head"] = head_scores
        timings["head_s"] = round(time.time() - t0, 1)
    ft_vram: dict[str, float] = {}
    embed_vram = vram_report()
    if "finetune" in methods:
        del vis  # the fine-tune starts from the exact fp32 weights, not the fp16 copy
        if device == "cuda":
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()  # peak_through_finetune is the fine-tune phase alone
        vis_ft = Vision(device, torch.float32)
        t0 = time.time()
        ft_scores = {c: np.zeros(len(rows)) for c in CLASSES}
        for f in range(k):
            tr = [i for i in range(len(rows)) if folds[i] != f]
            te = [i for i in range(len(rows)) if folds[i] == f]
            print(
                f"finetune fold {f + 1}/{k}: {len(tr)} train / {len(te)} held-out frames",
                flush=True,
            )
            tf = time.time()
            probs = finetune_fold(vis_ft, imgs224, imgs_big, rows, tr, te, args, seed=f + args.seed)
            for j, c in enumerate(CLASSES, start=1):
                ft_scores[c][te] = probs[:, j]
            print(f"  fold {f + 1} took {time.time() - tf:.0f} s", flush=True)
        scores["finetune"] = ft_scores
        timings["finetune_s"] = round(time.time() - t0, 1)
        ft_vram = vram_report()
    vram = {k_: max(embed_vram.get(k_, 0.0), ft_vram.get(k_, 0.0)) for k_ in (embed_vram or ft_vram)}
    over_cap = (
        f"VRAM peak {vram['reserved_peak_mib'] + CONTEXT_MIB} MiB (reserved + context) above the cap {cap_mib} MiB"
        if device == "cuda" and vram["reserved_peak_mib"] + CONTEXT_MIB > cap_mib + 1
        else None
    )
    # NaN = the fold had too few training positives: no score, excluded from that method's metrics
    metrics = all_metrics(scores, rows, folds)
    if args.tag:
        args.out = args.out / args.tag
    args.out.mkdir(parents=True, exist_ok=True)
    result = {
        "dataset": str(args.dataset.name),
        "args": {
            k_: (str(v) if isinstance(v, Path) else v) for k_, v in vars(args).items()
        },
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "summary": summary,
        "timings_s": timings,
        "vram_mib": {
            "cap": cap_mib,
            "context_allowance": CONTEXT_MIB,
            "peak_all_phases": vram,
            "peak_through_finetune": ft_vram,
            "over_cap": over_cap,
        },
        "metrics": metrics,
    }
    (args.out / "experiment.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.out / "oof_scores.json").write_text(
        json.dumps(
            {
                "ids": [r["id"] for r in rows],
                "folds": folds,
                "scores": {
                    m: {c: [None if v != v else round(float(v), 5) for v in s] for c, s in per.items()}
                    for m, per in scores.items()
                },
            }
        )
        + "\n"
    )
    print_table(metrics)
    if over_cap:  # after the scores are on disk: a breach is reported, not a reason to lose the run
        raise SystemExit(over_cap)
    return 0


def print_table(metrics: dict[str, Any]) -> None:
    print(
        f"{'method/class/domain':44s} {'n+/n-':>9s} {'grp+':>4s} {'AUROC':>6s} {'95% CI':>13s} {'TPR@1%':>7s} {'TPR@5%':>7s}"
    )
    for key, m in metrics.items():
        ci = (
            "-"
            if m["auroc_ci95_group_bootstrap"] is None
            else f"{m['auroc_ci95_group_bootstrap'][0]:.2f}-{m['auroc_ci95_group_bootstrap'][1]:.2f}"
        )
        print(
            f"{key:44s} {m['n_pos']:>4d}/{m['n_neg']:<4d} {m['groups_pos']:>4d} {m['auroc']:>6.3f} {ci:>13s} {m['tpr_at_1pct_fpr']:>7.3f} {m['tpr_at_5pct_fpr']:>7.3f}"
        )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
