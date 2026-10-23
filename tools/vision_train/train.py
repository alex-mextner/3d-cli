"""train.py — fit, evaluate and fine-tune tier (b) on the labelled frame set (dataset.py output).

Runs on the owner's laptop (`remote.py run --entry train.py`: it copies lib/ + this directory + the
data there and runs it under the load/VRAM guard); the same file works on any CUDA box and, slowly,
on a CPU. experiment.py is the held-out comparison; this is the single-split tool. The workstation only ever runs it as a tiny smoke test (`--smoke`: 48 frames, a few steps,
<= 3 GB of VRAM). Needs the packages of requirements.txt (torch, transformers, numpy, opencv).

    train.py head      DATASET OUT   embed every frame once, fit one logistic head per class on the
                                     train split, report on the test split next to the zero-shot
                                     prompts, then refit on everything -> OUT/tier-b-head.json
    train.py finetune  DATASET OUT   unfreeze the last blocks of CLIP ViT-B/16's vision tower and
                                     train tower + a classifier initialised from the text prompts
                                     -> OUT/finetuned.pt, OUT/metrics.json (+ ONNX with --export-onnx)
    train.py eval      DATASET HEAD  the tables of `3d printer vision eval --model` for a head file

DATASET is the directory of `dataset.py build [--third-party]`: labels.jsonl + frames. Classes
are tier (b)'s: spaghetti, part_detached, empty_bed; for class C the positives are the frames
labelled C and the negatives the `healthy` ones (other failure labels are ignored for C).

METRICS are the ones `3d printer vision eval --model` prints (lib/vision_model.eval_sources):
`vision_detect._table` precision/recall/FPR at thresholds 0.1..0.9 per class, plus AUROC and
recall at 1 % FPR; they are computed by the SAME code (lib/vision_detect.py, lib/vision_fit.py are
imported, not copied), on the `test` split, per provenance (own frames vs third-party).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for cand in (HERE.parent.parent / "lib", HERE.parent / "lib"):
    if (cand / "vision_model.py").is_file():
        sys.path.insert(0, str(cand))
        break

CLASSES = ("spaghetti", "part_detached", "empty_bed")
TEXT_PROMPTS_PER_CLASS = 4


def load_rows(dataset: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in (dataset / "labels.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for r in rows:
        r["path"] = str(dataset / r["file"])
        r["provenance"] = "own" if r["provenance"].startswith("own") else "third_party"
        r["split"] = "train" if r["split"] in ("train", "tune") else "test"
    if limit:  # smoke: a balanced handful, both splits
        keep: list[dict[str, Any]] = []
        for lab in sorted({r["label"] for r in rows}):
            for split in ("train", "test"):
                keep += [r for r in rows if r["label"] == lab and r["split"] == split][
                    : limit // 8
                ]
        rows = keep
    return rows


# ---- embeddings ---------------------------------------------------------------------------------


def device_name(requested: str) -> str:
    import torch

    if requested == "cpu":
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if requested == "cuda":
        raise SystemExit(
            "--device cuda requested but torch.cuda.is_available() is False"
        )
    return "cpu"


def cap_vram(max_gb: float) -> None:
    import torch

    if torch.cuda.is_available():
        total = torch.cuda.get_device_properties(0).total_memory / 2**30
        torch.cuda.set_per_process_memory_fraction(min(1.0, max_gb / total))


def embed_rows(rows: list[dict[str, Any]], device: str, batch: int = 32) -> Any:
    """(n, 512) unit embeddings of the frames with the base CLIP B/16 (torch), preprocessed by the
    very function the runtime uses (vision_model.preprocess), so heads transfer to the ONNX path."""
    import cv2
    import numpy as np
    import torch
    import vision_model as vm
    from transformers import CLIPModel

    model = (
        CLIPModel.from_pretrained(
            vm.BASE_MODEL_ID,
            revision=vm.BASE_REVISION,
            dtype=torch.float16 if device == "cuda" else torch.float32,
        )
        .to(device)
        .eval()
    )
    out = []
    for i in range(0, len(rows), batch):
        x = np.concatenate(
            [vm.preprocess(cv2.imread(r["path"])) for r in rows[i : i + batch]]
        )
        with torch.no_grad():
            f = model.get_image_features(
                pixel_values=torch.from_numpy(x).to(device, model.dtype)
            )
            f = f.pooler_output if hasattr(f, "pooler_output") else f
        out.append(torch.nn.functional.normalize(f.float(), dim=-1).cpu().numpy())
        print(f"embedded {min(i + batch, len(rows))}/{len(rows)}", flush=True)
    return np.concatenate(out)


# ---- metrics: the same tables as `vision eval --model` --------------------------------------------


def class_tables(
    emb: Any, rows: list[dict[str, Any]], scores: dict[str, Any], split: str = "test"
) -> dict[str, Any]:
    """Per class and provenance: frames, AUROC, recall at 1 % FPR, and vision_detect._table."""
    import numpy as np
    import vision_fit as vf
    import vision_model as vm
    from vision_detect import _table

    out: dict[str, Any] = {}
    for cls, s in scores.items():
        for prov in ("own", "third_party", "all"):
            sel = [
                i
                for i, r in enumerate(rows)
                if r["split"] == split and (prov == "all" or r["provenance"] == prov)
            ]
            pos = np.array([s[i] for i in sel if rows[i]["label"] == cls])
            neg = np.array([s[i] for i in sel if rows[i]["label"] == "healthy"])
            if len(pos) == 0 or len(neg) == 0:
                continue
            thr = float(np.quantile(neg, 0.99))
            out[f"{cls}/{prov}"] = {
                "frames": {"pos": int(len(pos)), "neg": int(len(neg))},
                "auroc": round(vf.auroc(pos, neg), 4),
                "recall_at_1pct_fpr": round(float((pos > thr).mean()), 4),
                "per_frame": _table(
                    list(map(float, pos)), list(map(float, neg)), vm.EVAL_THRESHOLDS
                ),
            }
    return out


def zero_shot_scores(emb: Any) -> dict[str, Any]:
    import vision_model as vm

    table = vm.PromptTable.load()
    probs = [table.probs(e) for e in emb]
    return {c: [p[c] for p in probs] for c in CLASSES}


def cmd_head(
    dataset: Path,
    out: Path,
    device: str,
    smoke: bool,
    l2: float,
    fpr: float,
    max_gb: float,
) -> None:
    import numpy as np
    import vision_fit as vf
    import vision_model as vm

    cap_vram(max_gb)
    rows = load_rows(dataset, 48 if smoke else None)
    emb = embed_rows(rows, device)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "embeddings.npy", emb)
    labels = np.array([r["label"] for r in rows])
    split = np.array([r["split"] for r in rows])
    head: dict[str, Any] = {"version": 1, "model": vm.MODEL_TAG, "classes": {}}
    metrics: dict[str, Any] = {
        "model": vm.MODEL_TAG,
        "frames": {
            "train": int((split == "train").sum()),
            "test": int((split == "test").sum()),
        },
        "zero_shot": class_tables(emb, rows, zero_shot_scores(emb)),
        "head": {},
    }
    held: dict[str, Any] = {}
    for cls in CLASSES:
        tr_pos, tr_neg = (
            (split == "train") & (labels == cls),
            (split == "train") & (labels == "healthy"),
        )
        if tr_pos.sum() < 5 or tr_neg.sum() < 5:
            held[cls] = (
                f"skipped: {int(tr_pos.sum())} positive / {int(tr_neg.sum())} negative train frames (need >= 5 each)"
            )
            continue
        w, b = vf.fit_logistic(
            np.vstack([emb[tr_pos], emb[tr_neg]]),
            np.r_[np.ones(tr_pos.sum()), np.zeros(tr_neg.sum())],
            l2,
        )
        scores = {cls: list(vf.sigmoid_scores(emb, w, b))}
        metrics["head"].update(class_tables(emb, rows, scores))
        # the deployed head: all frames; threshold from block cross-validation over all of them
        all_pos, all_neg = labels == cls, labels == "healthy"
        cv = vf.cross_validate(emb[all_pos], emb[all_neg], l2=l2, folds=5, fpr=fpr)
        wa, ba = vf.fit_logistic(
            np.vstack([emb[all_pos], emb[all_neg]]),
            np.r_[np.ones(all_pos.sum()), np.zeros(all_neg.sum())],
            l2,
        )
        head["classes"][cls] = {
            "weights": [round(float(v), 6) for v in wa],
            "bias": round(ba, 6),
            "threshold": cv.get("threshold"),
            "fitted": {
                "frames": {"pos": int(all_pos.sum()), "neg": int(all_neg.sum())},
                "l2": l2,
                "fpr_target": fpr,
                "cross_validation": cv,
            },
        }
    metrics["skipped"] = held
    (out / "tier-b-head.json").write_text(
        json.dumps(head, separators=(",", ":")) + "\n"
    )
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print_summary(metrics)


def print_summary(metrics: dict[str, Any]) -> None:
    for kind in ("zero_shot", "head"):
        for key, m in metrics.get(kind, {}).items():
            print(
                f"{kind:9s} {key:24s} pos/neg {m['frames']['pos']}/{m['frames']['neg']}  AUROC {m['auroc']}  recall@1%FPR {m['recall_at_1pct_fpr']}"
            )
    for cls, why in metrics.get("skipped", {}).items():
        print(f"{cls}: {why}")


def cmd_eval(dataset: Path, head_path: Path, device: str) -> None:
    import numpy as np
    import vision_model as vm

    rows = load_rows(dataset)
    emb = embed_rows(rows, device)
    head = vm.Head.load(head_path)
    scores = {c: [head.prob(c, e) for e in emb] for c in head.classes}
    print(
        json.dumps(
            {
                "head": class_tables(emb, rows, scores),
                "zero_shot": class_tables(emb, rows, zero_shot_scores(emb)),
            },
            indent=2,
        )
    )
    del np


# ---- fine-tune ----------------------------------------------------------------------------------


def cmd_finetune(
    dataset: Path,
    out: Path,
    device: str,
    smoke: bool,
    epochs: int,
    lr: float,
    unfreeze: int,
    batch: int,
    max_minutes: float,
    max_gb: float,
    export_onnx: bool,
) -> None:
    import cv2
    import numpy as np
    import torch
    import vision_fit as vf
    import vision_model as vm
    from transformers import CLIPModel

    cap_vram(max_gb)
    rows = load_rows(dataset, 48 if smoke else None)
    classes = ("healthy", *CLASSES)
    rows = [r for r in rows if r["label"] in classes]
    idx = {c: i for i, c in enumerate(classes)}
    train = [r for r in rows if r["split"] == "train"]
    test = [r for r in rows if r["split"] == "test"]
    if not train or not test:
        raise SystemExit(
            f"finetune needs both splits: {len(train)} train / {len(test)} test frames"
        )
    model = CLIPModel.from_pretrained(
        vm.BASE_MODEL_ID, revision=vm.BASE_REVISION, dtype=torch.float32
    ).to(device)
    for p in model.parameters():
        p.requires_grad = False
    layers = model.vision_model.encoder.layers
    trainable = [
        *layers[-unfreeze:],
        model.vision_model.post_layernorm,
        model.visual_projection,
    ]
    for mod in trainable:
        for p in mod.parameters():
            p.requires_grad = True
    # classifier initialised from the zero-shot prompts: training starts at the zero-shot answer
    table = vm.PromptTable.load()
    w0 = np.stack(
        [
            table.emb[
                [i for i, o in enumerate(table.owner) if table.classes[o] == c]
            ].mean(0)
            for c in classes
        ]
    )
    clf = torch.nn.Linear(512, len(classes), bias=True).to(device)
    clf.weight.data = torch.tensor(
        w0 * vm.LOGIT_SCALE / np.linalg.norm(w0, axis=1, keepdims=True),
        dtype=torch.float32,
        device=device,
    )
    clf.bias.data.zero_()
    counts = np.bincount(
        [idx[r["label"]] for r in train], minlength=len(classes)
    ).astype(np.float64)
    weight = torch.tensor(
        np.where(
            counts > 0, counts.sum() / (len(classes) * np.maximum(counts, 1)), 0.0
        ),
        dtype=torch.float32,
        device=device,
    )
    opt = torch.optim.AdamW(
        [
            {"params": [p for m in trainable for p in m.parameters()], "lr": lr},
            {"params": clf.parameters(), "lr": lr * 100},
        ],
        weight_decay=0.01,
    )
    rng = np.random.RandomState(0)

    def batch_of(items: list[dict[str, Any]], augment: bool) -> Any:
        xs = []
        for r in items:
            img = cv2.imread(r["path"])
            if augment:
                h, w = img.shape[:2]
                s = rng.uniform(0.85, 1.0)
                y0, x0 = int(rng.uniform(0, 1 - s) * h), int(rng.uniform(0, 1 - s) * w)
                img = img[y0 : y0 + int(s * h), x0 : x0 + int(s * w)]
                img = np.clip(
                    img.astype(np.float32) * rng.uniform(0.8, 1.2)
                    + rng.uniform(-20, 20),
                    0,
                    255,
                ).astype(np.uint8)
            xs.append(vm.preprocess(img))
        return torch.from_numpy(np.concatenate(xs)).to(device)

    def embed(x: Any) -> Any:
        f = model.get_image_features(pixel_values=x)
        f = f.pooler_output if hasattr(f, "pooler_output") else f
        return torch.nn.functional.normalize(f, dim=-1)

    def evaluate() -> tuple[float, Any, dict[str, Any]]:
        model.eval()
        with torch.no_grad():
            em = [embed(batch_of(test[i : i + 32], False)).cpu().numpy() for i in range(0, len(test), 32)]
        emb = np.concatenate(em)
        with torch.no_grad():
            logits = clf(torch.from_numpy(emb).to(device)).softmax(-1).cpu().numpy()
        scores = {c: list(logits[:, idx[c]]) for c in CLASSES}
        tables = class_tables(emb, test, scores)
        aurocs = [t["auroc"] for k, t in tables.items() if k.endswith("/all")]
        return (float(np.mean(aurocs)) if aurocs else 0.0), emb, tables

    best, history, t0 = -1.0, [], time.time()
    sampler_p = np.array([weight[idx[r["label"]]].item() for r in train])
    sampler_p /= sampler_p.sum()
    steps = max(1, len(train) // batch)
    if smoke:
        epochs, steps = 1, 3
    out.mkdir(parents=True, exist_ok=True)
    for epoch in range(epochs):
        model.train()
        for step in range(steps):
            items = [
                train[j]
                for j in rng.choice(
                    len(train), size=min(batch, len(train)), p=sampler_p
                )
            ]
            y = torch.tensor([idx[r["label"]] for r in items], device=device)
            with torch.autocast(
                device_type=device, dtype=torch.bfloat16, enabled=device == "cuda"
            ):
                logits = clf(embed(batch_of(items, True)))
            loss = torch.nn.functional.cross_entropy(logits.float(), y, weight=weight)
            opt.zero_grad()
            loss.backward()
            opt.step()
            if step % 10 == 0:
                print(
                    f"epoch {epoch} step {step}/{steps} loss {loss.item():.4f}",
                    flush=True,
                )
        score, emb, tables = evaluate()
        history.append(
            {
                "epoch": epoch,
                "mean_test_auroc": round(score, 4),
                "loss": round(loss.item(), 4),
            }
        )
        print(history[-1], flush=True)
        if score > best:
            best = score
            torch.save(
                {
                    "vision": {
                        k: v.cpu()
                        for k, v in model.state_dict().items()
                        if k.startswith(("vision_model", "visual_projection"))
                    },
                    "classifier": clf.state_dict(),
                    "classes": classes,
                    "base": f"{vm.BASE_MODEL_ID}@{vm.BASE_REVISION}",
                },
                out / "finetuned.pt",
            )
            (out / "metrics.json").write_text(
                json.dumps(
                    {
                        "best_epoch": epoch,
                        "history": history,
                        "frames": {"train": len(train), "test": len(test)},
                        "test": tables,
                        "unfrozen_blocks": unfreeze,
                    },
                    indent=2,
                )
                + "\n"
            )
        if (time.time() - t0) / 60 > max_minutes:
            print(f"stopping: --max-minutes {max_minutes} reached")
            break
    if export_onnx:
        export(model, out / "vision_tower.onnx", device)
    del vf


def export(model: Any, path: Path, device: str) -> None:
    """Vision tower + projection -> ONNX with CLIP's contract (pixel_values -> image_embeds)."""
    import torch

    class Tower(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.m = model

        def forward(self, pixel_values: Any) -> Any:
            f = self.m.get_image_features(pixel_values=pixel_values)
            return f.pooler_output if hasattr(f, "pooler_output") else f

    model.eval()
    dummy = torch.zeros(1, 3, 224, 224, device=device)
    torch.onnx.export(
        Tower(),
        (dummy,),
        str(path),
        input_names=["pixel_values"],
        output_names=["image_embeds"],
        dynamic_axes={"pixel_values": {0: "batch"}, "image_embeds": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    print(f"exported {path} ({path.stat().st_size / 1e6:.0f} MB)")


def gpu_info() -> str:
    try:
        return subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "no nvidia-smi"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("head", "finetune", "eval"):
        p = sub.add_parser(name)
        p.add_argument("dataset", type=Path)
        p.add_argument(
            "out", type=Path, help="output dir (head, finetune) or the head file (eval)"
        )
        p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
        p.add_argument(
            "--smoke",
            action="store_true",
            help="48 frames, a few steps: a pipeline check, not a result",
        )
        p.add_argument("--max-vram-gb", type=float, default=3.0)
    sub.choices["head"].add_argument("--l2", type=float, default=1.0)
    sub.choices["head"].add_argument("--fpr", type=float, default=0.005)
    ft = sub.choices["finetune"]
    ft.add_argument("--epochs", type=int, default=8)
    ft.add_argument("--lr", type=float, default=1e-5)
    ft.add_argument(
        "--unfreeze", type=int, default=2, help="last N vision-tower blocks to train"
    )
    ft.add_argument("--batch", type=int, default=16)
    ft.add_argument("--max-minutes", type=float, default=60.0)
    ft.add_argument("--export-onnx", action="store_true")
    args = ap.parse_args(argv)
    device = device_name(args.device)
    print(f"device {device}; {gpu_info()}", flush=True)
    if args.cmd == "head":
        cmd_head(
            args.dataset,
            args.out,
            device,
            args.smoke,
            args.l2,
            args.fpr,
            args.max_vram_gb,
        )
    elif args.cmd == "finetune":
        cmd_finetune(
            args.dataset,
            args.out,
            device,
            args.smoke,
            args.epochs,
            args.lr,
            args.unfreeze,
            args.batch,
            args.max_minutes,
            args.max_vram_gb,
            args.export_onnx,
        )
    else:
        cmd_eval(args.dataset, args.out, device)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
