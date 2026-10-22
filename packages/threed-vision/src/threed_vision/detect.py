"""OWLv2 open-vocabulary box detection.

`OwlDetector.detect(rgb, queries, ...)` -> `{"boxes": [{"xyxy", "score", "label"}], "timing_s"}`.

OWLv2 pads the image to a SQUARE (bottom/right) before inference, so its normalised boxes live in the padded frame:
they must be scaled by max(H, W) for both axes (not by W and H) and then clipped to the real image.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from . import models
from .device import resolve_device
from .errors import BadInput
from .torchutil import free_cuda, is_oom, require_torch

DEFAULT_QUERIES = [
    "a hand tool",
    "a power tool",
    "a box",
    "a tube of glue",
    "a tape roll",
    "pliers",
    "a screwdriver",
]


def nms_indices(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """Greedy class-agnostic NMS over (N,4) xyxy boxes; returns kept indices by descending score."""
    if len(boxes) == 0:
        return []
    x0, y0, x1, y1 = (boxes[:, i].astype(np.float64) for i in range(4))
    areas = np.maximum(x1 - x0, 0) * np.maximum(y1 - y0, 0)
    order = np.argsort(-scores, kind="stable")
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        iw = np.maximum(np.minimum(x1[i], x1[rest]) - np.maximum(x0[i], x0[rest]), 0)
        ih = np.maximum(np.minimum(y1[i], y1[rest]) - np.maximum(y0[i], y0[rest]), 0)
        inter = iw * ih
        iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[iou <= iou_thr]
    return keep


def boxes_from_square(cxcywh_norm: np.ndarray, h: int, w: int) -> np.ndarray:
    """OWLv2 normalised (cx,cy,w,h) in the padded square frame -> xyxy pixels in the real image (scaled by max(H,W))."""
    s = float(max(h, w))
    cx, cy, bw, bh = (cxcywh_norm[:, i] * s for i in range(4))
    xyxy = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, w)
    xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, h)
    return xyxy


def select_boxes(
    xyxy: np.ndarray,
    scores: np.ndarray,
    labels: list[str],
    h: int,
    w: int,
    nms: float,
    max_area_frac: float,
) -> list[dict[str, Any]]:
    """NMS + area filter (+ drop empty boxes) -> result records, sorted by score."""
    area = np.maximum(xyxy[:, 2] - xyxy[:, 0], 0) * np.maximum(
        xyxy[:, 3] - xyxy[:, 1], 0
    )
    ok = (area > 1) & (area / float(h * w) <= max_area_frac)
    idx = np.nonzero(ok)[0]
    keep = [int(idx[i]) for i in nms_indices(xyxy[idx], scores[idx], nms)]
    return [
        {
            "xyxy": [round(float(v), 1) for v in xyxy[i]],
            "score": round(float(scores[i]), 4),
            "label": labels[i],
        }
        for i in keep
    ]


class OwlDetector:
    """OWLv2-base-patch16-ensemble (fp16 on CUDA, fp32 on CPU); loaded from the local models dir only."""

    vram_estimate_mb = 700

    def __init__(self, device: str = "auto") -> None:
        self.requested = device
        self.device = "cpu"
        self.loaded = False
        self._model: Any = None
        self._proc: Any = None

    def load(self) -> None:
        if self.loaded:
            return
        path = models.model_path("owlv2-base")  # missing weights are reported before missing python packages
        require_torch()
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor

        self.device = resolve_device(self.requested, self.vram_estimate_mb)
        self._proc = Owlv2Processor.from_pretrained(str(path), local_files_only=True)
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        model = Owlv2ForObjectDetection.from_pretrained(
            str(path), local_files_only=True, torch_dtype=dtype
        ).eval()
        try:
            self._model = model.to(self.device)
        except Exception as exc:  # noqa: BLE001
            if not (self.device == "cuda" and is_oom(exc) and self.requested != "cuda"):
                raise
            del model
            free_cuda()
            self.device = "cpu"
            self._model = Owlv2ForObjectDetection.from_pretrained(
                str(path), local_files_only=True, torch_dtype=torch.float32
            ).eval()
        self.loaded = True

    def unload(self) -> None:
        self._model = None
        self._proc = None
        self.loaded = False
        free_cuda()

    def _forward(self, rgb: np.ndarray, queries: list[str]) -> Any:
        import torch
        from PIL import Image

        inp = self._proc(
            text=[queries], images=Image.fromarray(rgb), return_tensors="pt"
        ).to(self.device)
        if self.device == "cuda":
            inp["pixel_values"] = inp["pixel_values"].half()
        with torch.inference_mode():
            return self._model(**inp)

    def detect(
        self,
        rgb: np.ndarray,
        queries: list[str] | None = None,
        threshold: float = 0.15,
        nms: float = 0.5,
        max_area_frac: float = 0.05,
    ) -> dict[str, Any]:
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise BadInput(f"detect expects an HxWx3 RGB array, got shape {rgb.shape}")
        qs = list(queries) if queries else list(DEFAULT_QUERIES)
        t0 = time.perf_counter()
        self.load()
        try:
            out = self._forward(rgb, qs)
        except Exception as exc:  # noqa: BLE001
            if not (self.device == "cuda" and is_oom(exc) and self.requested != "cuda"):
                raise
            self.unload()
            self.requested = "cpu"
            self.load()
            out = self._forward(rgb, qs)
        import torch

        h, w = rgb.shape[:2]
        logits = out.logits[0].float()  # (patches, n_queries)
        scores_all, lab_idx = torch.sigmoid(logits).max(dim=-1)
        keep = (scores_all >= threshold).nonzero().flatten()
        boxes = out.pred_boxes[0].float()[keep].cpu().numpy()
        scores = scores_all[keep].cpu().numpy()
        labels = [qs[int(i)] for i in lab_idx[keep].cpu().numpy()]
        xyxy = (
            boxes_from_square(boxes, h, w)
            if len(boxes)
            else np.zeros((0, 4), np.float32)
        )
        res = (
            select_boxes(xyxy, scores, labels, h, w, nms, max_area_frac)
            if len(boxes)
            else []
        )
        return {"boxes": res, "timing_s": round(time.perf_counter() - t0, 3)}
