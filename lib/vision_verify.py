"""vision_verify.py — stage 2 of `3d printer vision`: does a stage-1 detection really look like a failure?

Stage 1 (vision_detect.ObicoScorer) is a texture detector: on the Snapmaker U1 it also boxes
speckled grey walls (black/white mixed filament with green purge specks) — p 0.83 and 0.99 on
healthy frames on 2026-10-14, while real spaghetti scores 2.2-3.2 with 8-14 boxes. Each stage-1 box
is cropped (square, 1.4x the box, at least 12 % of the frame height) and embedded with OpenAI CLIP
ViT-B/32 (image tower, ONNX fp16, downloaded at runtime and sha256-pinned like the Obico weights).
The crop's failure probability is CLIP's zero-shot softmax over fixed text prompts — failure prompts
against healthy-print prompts (`lib/data/vision/clip_prompts.json`, embeddings precomputed with the
CLIP text tower so the CLI needs neither the text model nor a tokenizer). Zero-shot on purpose: no
weights are fitted to our few failure frames, so it cannot overfit one U1 episode.

The frame's verified score q = sum(box confidence x crop failure probability) over the strongest
`MAX_CROPS` boxes; the temporal tracker (printer_ops.vision.FailureTracker) runs on q instead of the
raw p. Cost ~0.05-0.1 s per crop on the CPU, only for frames that have boxes.

Regenerate the prompt file (dev only; needs the `tokenizers` package and the text tower):
    python lib/vision_verify.py prompts TEXT_MODEL.onnx TOKENIZER.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vision_detect import ModelSpec, ensure_model  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

CLIP = ModelSpec(
    name="clip-vit-b32-vision-fp16-d15189d.onnx",
    url=(
        "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/"
        "d15189d7028b43f1d3e65039190477f6af591c2a/onnx/vision_model_fp16.onnx"
    ),
    sha256="35c4e0fb0aeee527dcde1693520b214a34424a786babd530f35366bad5844efd",
    size=176_080_659,
    licence="MIT (OpenAI CLIP; ONNX export by Xenova/transformers.js)",
    source="https://github.com/openai/CLIP",
)
PROMPTS = LIB / "data" / "vision" / "clip_prompts.json"
MAX_CROPS = 8  # the strongest boxes are verified; spaghetti already has >= 6 of them
CROP_SCALE = 1.4
CROP_MIN = 0.12  # of the frame height: CLIP needs some context around a thin box
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)
LOGIT_SCALE = 100.0  # CLIP's learned temperature


@dataclass(frozen=True)
class PromptSet:
    """Text embeddings per class; `positive` names the classes that mean "failure"."""

    classes: tuple[str, ...]
    prompts: tuple[str, ...]
    owner: tuple[int, ...]  # class index of each prompt
    emb: Any  # (n_prompts, 512) float32, L2-normalised
    positive: frozenset[str]

    @classmethod
    def load(cls, path: Path = PROMPTS, task: str = "spaghetti") -> PromptSet:
        import numpy as np

        d = json.loads(path.read_text())
        t = d["tasks"][task]
        prompts, owner = [], []
        for ci, c in enumerate(t["classes"]):
            for p in t["prompts"][c]:
                prompts.append(p)
                owner.append(ci)
        emb = np.array([d["embeddings"][p] for p in prompts], np.float32)
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        return cls(
            tuple(t["classes"]),
            tuple(prompts),
            tuple(owner),
            emb,
            frozenset(t["positive"]),
        )

    def prob(self, img_emb: np.ndarray) -> np.ndarray:
        """P(failure) per image embedding: softmax over all prompts, summed over failure prompts."""
        import numpy as np

        logits = LOGIT_SCALE * img_emb @ self.emb.T
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        pr = e / e.sum(axis=1, keepdims=True)
        pos = [i for i, c in enumerate(self.owner) if self.classes[c] in self.positive]
        return pr[:, pos].sum(axis=1)


def crop_box(
    img: np.ndarray, box: tuple[float, float, float, float], scale: float = CROP_SCALE
) -> np.ndarray:
    """Square crop around a normalised x1,y1,x2,y2 box, grown by `scale`, kept inside the frame."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2 * w, (y1 + y2) / 2 * h
    side = max((x2 - x1) * w, (y2 - y1) * h) * scale
    side = min(max(side, CROP_MIN * h), float(min(w, h)))
    x0 = int(round(min(max(cx - side / 2, 0.0), w - side)))
    y0 = int(round(min(max(cy - side / 2, 0.0), h - side)))
    s = int(round(side))
    return img[y0 : y0 + s, x0 : x0 + s]


class ClipVerifier:
    """Crops -> CLIP image embeddings -> zero-shot failure probability."""

    def __init__(
        self, model: Path, prompts: PromptSet | None = None, threads: int = 2
    ) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model), opts, providers=["CPUExecutionProvider"]
        )
        self.prompts = prompts or PromptSet.load()

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        import cv2
        import numpy as np

        if not crops:
            return np.zeros((0, 512), np.float32)
        mean, std = np.array(MEAN, np.float32), np.array(STD, np.float32)
        x = np.stack(
            [
                (
                    (
                        cv2.cvtColor(
                            cv2.resize(c, (224, 224), interpolation=cv2.INTER_AREA),
                            cv2.COLOR_BGR2RGB,
                        ).astype(np.float32)
                        / 255.0
                        - mean
                    )
                    / std
                ).transpose(2, 0, 1)
                for c in crops
            ]
        )
        y = self.session.run(None, {"pixel_values": x})[0].astype(np.float32)
        return y / np.linalg.norm(y, axis=1, keepdims=True)

    def box_probs(self, bgr: np.ndarray, boxes: list[Any]) -> list[float]:
        """Failure probability of each of the first MAX_CROPS boxes (ObicoScorer lists them
        strongest first)."""
        top = boxes[:MAX_CROPS]
        if not top:
            return []
        emb = self.embed([crop_box(bgr, (b.x1, b.y1, b.x2, b.y2)) for b in top])
        return [float(v) for v in self.prompts.prob(emb)]


def verified_score(boxes: list[Any], probs: list[float]) -> float:
    """q = sum(conf x P(failure)) over the verified boxes; unverified weaker boxes add nothing."""
    return float(sum(b.conf * p for b, p in zip(boxes, probs, strict=False)))


def ensure_clip(*, fetch: bool = True) -> Path:
    return ensure_model(CLIP, fetch=fetch)


# ---- dev: regenerate the prompt embeddings -------------------------------------------------------

TASKS: dict[str, dict[str, Any]] = {
    "spaghetti": {
        "positive": ["failure"],
        "classes": ["failure", "healthy"],
        "prompts": {
            "failure": [
                "a tangled mess of loose 3D printer filament strands",
                "failed 3D print spaghetti of plastic threads",
                "a bird's nest of plastic filament",
            ],
            "healthy": [
                "a 3D printed part with a speckled granite texture",
                "a smooth 3D printed plastic part",
                "the inside of a 3D printer",
                "a 3D printer toolhead",
                "a flat 3D printed wall with layer lines",
                "a dark printer bed surface",
            ],
        },
    },
}


def encode_prompts(text_model: str, tokenizer: str, out: Path = PROMPTS) -> None:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer  # dev-only dependency

    tok = Tokenizer.from_file(tokenizer)
    tok.enable_padding(length=77, pad_id=49407)
    tok.enable_truncation(77)
    texts = sorted(
        {p for t in TASKS.values() for ps in t["prompts"].values() for p in ps}
    )
    ids = np.array([e.ids for e in tok.encode_batch(texts)], np.int64)
    sess = ort.InferenceSession(text_model, providers=["CPUExecutionProvider"])
    y = sess.run(None, {"input_ids": ids})[0]
    y = y / np.linalg.norm(y, axis=1, keepdims=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "model": "openai/clip-vit-base-patch32 text tower (Xenova ONNX d15189d)",
                "tasks": TASKS,
                "embeddings": {
                    t: [round(float(v), 5) for v in row]
                    for t, row in zip(texts, y, strict=True)
                },
            },
            indent=None,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "prompts":
        encode_prompts(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
