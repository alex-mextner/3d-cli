"""vision_model_bench.py — dev tool behind the tier-b (mid-tier model) comparison in
docs/research/printer-vision.md. NOT part of the `3d` runtime: it needs torch + transformers
(`uv venv ~/.cache/3d-cli/venv-vision-model && uv pip install torch transformers pillow numpy
opencv-python-headless scikit-learn`), so it lives outside lib/ and is not imported by the CLI.

  prompts OUT.json            text-tower embeddings of lib/vision_model.PROMPTS for the shipped
                              model -> lib/data/vision/model_prompts.json
  embed LIST.jsonl OUTDIR     embed every frame of LIST (one JSON per line: path, label, group,
                              optional source/seq) with each candidate; writes OUTDIR/emb_NAME.npy,
                              txt_NAME.npy, info_NAME.json (params, images/s, batch-1 latency, VRAM)
  eval OUTDIR LIST.jsonl      per-candidate AUROC / TPR@1%FPR per task from those embeddings
  tiny-onnx OUT.onnx          the deterministic 6 KB stand-in vision tower used by
                              tests/test_vision_model.py (needs the `onnx` package)

The candidate table (model id, licence, why) is CANDIDATES below; the numbers it produced are in
docs/research/printer-vision.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
HALF = (0.5, 0.5, 0.5)
IN_MEAN = (0.485, 0.456, 0.406)
IN_STD = (0.229, 0.224, 0.225)
# name -> (HF model id, mean, std, licence)
CANDIDATES: dict[str, tuple[str, tuple[float, ...], tuple[float, ...], str]] = {
    "clip-b32": ("openai/clip-vit-base-patch32", CLIP_MEAN, CLIP_STD, "MIT"),
    "clip-b16": ("openai/clip-vit-base-patch16", CLIP_MEAN, CLIP_STD, "MIT"),
    "laion-b32": ("laion/CLIP-ViT-B-32-laion2B-s34B-b79K", CLIP_MEAN, CLIP_STD, "MIT"),
    "siglip-b16": ("google/siglip-base-patch16-224", HALF, HALF, "Apache-2.0"),
    "siglip2-b16": ("google/siglip2-base-patch16-224", HALF, HALF, "Apache-2.0"),
    "dinov2-s": ("facebook/dinov2-small", IN_MEAN, IN_STD, "Apache-2.0"),
}
SIZE = 224


def _vram_mib() -> int:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        text=True,
    )
    return int(out.split()[0])


def _text_features(model: Any, tok: Any, name: str, prompts: list[str]) -> Any:
    import torch

    ids = tok(
        prompts,
        padding="max_length",
        max_length=64 if "siglip" in name else 77,
        truncation=True,
        return_tensors="pt",
    ).to(model.device)
    with torch.no_grad():
        t = model.get_text_features(**ids)
    t = t.pooler_output if hasattr(t, "pooler_output") else t
    return torch.nn.functional.normalize(t.float(), dim=-1).cpu().numpy()


def cmd_prompts(out: Path) -> None:
    """Text embeddings (fp32, CPU) of vision_model.PROMPTS with the shipped model's text tower."""
    from transformers import AutoModel, AutoTokenizer
    from vision_model import BASE_MODEL_ID, BASE_REVISION, PROMPTS

    model = AutoModel.from_pretrained(BASE_MODEL_ID, revision=BASE_REVISION).eval()
    tok = AutoTokenizer.from_pretrained(BASE_MODEL_ID, revision=BASE_REVISION)
    texts = sorted({p for ps in PROMPTS.values() for p in ps})
    emb = _text_features(model, tok, "clip", texts)
    out.write_text(
        json.dumps(
            {
                "model": f"{BASE_MODEL_ID}@{BASE_REVISION} text tower, fp32",
                "classes": PROMPTS,
                "embeddings": {
                    t: [round(float(v), 5) for v in row]
                    for t, row in zip(texts, emb, strict=True)
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    print(f"wrote {out} ({len(texts)} prompts)")


def cmd_embed(listing: Path, outdir: Path, names: list[str]) -> None:
    import cv2
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    from vision_model import PROMPTS

    rows = [
        json.loads(line) for line in listing.read_text().splitlines() if line.strip()
    ]
    frames = []
    for r in rows:
        im = cv2.imread(r["path"])
        if im is None:
            raise SystemExit(f"cannot read {r['path']}")
        frames.append(
            cv2.cvtColor(
                cv2.resize(im, (SIZE, SIZE), interpolation=cv2.INTER_AREA),
                cv2.COLOR_BGR2RGB,
            )
        )
    x_all = np.stack(frames)
    outdir.mkdir(parents=True, exist_ok=True)
    prompts = [p for ps in PROMPTS.values() for p in ps]
    for name in names or list(CANDIDATES):
        mid, mean, std, licence = CANDIDATES[name]
        before = _vram_mib()
        model = AutoModel.from_pretrained(mid, dtype=torch.float16).cuda().eval()
        mean_t = torch.tensor(mean, device="cuda", dtype=torch.float16).view(1, 3, 1, 1)
        std_t = torch.tensor(std, device="cuda", dtype=torch.float16).view(1, 3, 1, 1)

        def enc(
            batch: Any,
            _m: Any = model,
            _mt: Any = mean_t,
            _st: Any = std_t,
            _n: str = name,
        ) -> Any:
            x = (
                torch.from_numpy(np.ascontiguousarray(batch))
                .cuda()
                .permute(0, 3, 1, 2)
                .half()
                / 255
            )
            x = (x - _mt) / _st
            with torch.no_grad():
                if _n.startswith("dinov2"):
                    o = _m(pixel_values=x).last_hidden_state
                    y = torch.cat([o[:, 0], o[:, 1:].mean(1)], 1)
                else:
                    y = _m.get_image_features(pixel_values=x)
                    y = y.pooler_output if hasattr(y, "pooler_output") else y
            return torch.nn.functional.normalize(y.float(), dim=-1).cpu().numpy()

        enc(x_all[:4])
        torch.cuda.synchronize()
        t0 = time.time()
        emb = np.concatenate([enc(x_all[i : i + 64]) for i in range(0, len(x_all), 64)])
        torch.cuda.synchronize()
        per_s = len(x_all) / (time.time() - t0)
        t0 = time.time()
        for i in range(30):
            enc(x_all[i : i + 1])
        torch.cuda.synchronize()
        info: dict[str, Any] = {
            "model": mid,
            "licence": licence,
            "params": sum(p.numel() for p in model.parameters()),
            "img_per_s_gpu": per_s,
            "lat_b1_gpu_s": (time.time() - t0) / 30,
            "vram_delta_mib": _vram_mib() - before,
        }
        if name != "dinov2-s":
            tok = AutoTokenizer.from_pretrained(mid)
            np.save(
                outdir / f"txt_{name}.npy", _text_features(model, tok, name, prompts)
            )
            info["logit_scale"] = float(model.logit_scale.exp().item())
        np.save(outdir / f"emb_{name}.npy", emb)
        (outdir / f"info_{name}.json").write_text(json.dumps(info))
        print(name, info, flush=True)
        del model
        torch.cuda.empty_cache()


def cmd_eval(outdir: Path, listing: Path) -> None:
    """AUROC / TPR@1%FPR per candidate. Labels come from LIST (`label`, `source`, `group`)."""
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from vision_model import PROMPTS

    rows = [
        json.loads(line) for line in listing.read_text().splitlines() if line.strip()
    ]
    lab = np.array([r["label"] for r in rows])
    names = [k for k, ps in PROMPTS.items() for _ in ps]
    healthy = lab == "healthy"
    for name in CANDIDATES:
        if not (outdir / f"emb_{name}.npy").is_file() or name == "dinov2-s":
            continue
        e = np.load(outdir / f"emb_{name}.npy")
        t = np.load(outdir / f"txt_{name}.npy")
        info = json.loads((outdir / f"info_{name}.json").read_text())
        z = info["logit_scale"] * e @ t.T
        pr = np.exp(z - z.max(1, keepdims=True))
        pr /= pr.sum(1, keepdims=True)
        print(f"== {name} ({info['model']}, {info['params'] / 1e6:.0f} M params)")
        for cls in PROMPTS:
            s = pr[:, [i for i, n in enumerate(names) if n == cls]].sum(1)
            pos = lab == cls
            if not pos.any():
                continue
            y = np.r_[np.ones(pos.sum()), np.zeros(healthy.sum())]
            sc = np.r_[s[pos], s[healthy]]
            thr = float(np.quantile(s[healthy], 0.99))
            print(
                f"  {cls:10s} n={int(pos.sum())}/{int(healthy.sum())} "
                f"AUROC {roc_auc_score(y, sc):.3f}  TPR@1%FPR {float((s[pos] > thr).mean()):.3f}"
            )


def cmd_tiny_onnx(out: Path) -> None:
    """pixel_values (N,3,224,224) -> mean over H,W -> fixed (3,512) projection -> (N,512): CLIP's
    input/output contract with seeded weights, so the real onnxruntime path is testable without
    the 173 MB weights."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    w = np.random.RandomState(7).randn(3, 512).astype(np.float32)
    graph = helper.make_graph(
        [
            helper.make_node("ReduceMean", ["pixel_values"], ["pooled"], axes=[2, 3], keepdims=0),
            helper.make_node("MatMul", ["pooled", "proj"], ["image_embeds"]),
        ],
        "tiny_tier_b",
        [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, ["batch", 3, 224, 224])],
        [helper.make_tensor_value_info("image_embeds", TensorProto.FLOAT, ["batch", 512])],
        [numpy_helper.from_array(w, "proj")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    out.write_bytes(model.SerializeToString())
    print(f"wrote {out} ({out.stat().st_size} bytes)")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "prompts":
        cmd_prompts(Path(argv[1]))
    elif len(argv) >= 3 and argv[0] == "embed":
        cmd_embed(Path(argv[1]), Path(argv[2]), argv[3:])
    elif len(argv) == 2 and argv[0] == "tiny-onnx":
        cmd_tiny_onnx(Path(argv[1]))
    elif len(argv) == 3 and argv[0] == "eval":
        cmd_eval(Path(argv[1]), Path(argv[2]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
