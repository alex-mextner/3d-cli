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
from .errors import BadInput, MissingDependency
from .torchutil import free_cuda, is_oom, require_cuda_if_requested, require_torch

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


def tile_windows(
    h: int, w: int, n: int, overlap: float
) -> list[tuple[int, int, int, int]]:
    """Pass windows (x0, y0, x1, y1): the whole image first, then (for n > 1) an n x n grid whose neighbours overlap by `overlap`."""
    wins = [(0, 0, w, h)]
    if n <= 1:
        return wins
    tw, th = w / (n - (n - 1) * overlap), h / (n - (n - 1) * overlap)
    for j in range(n):
        for i in range(n):
            x0, y0 = round(i * tw * (1 - overlap)), round(j * th * (1 - overlap))
            wins.append((x0, y0, min(w, round(x0 + tw)), min(h, round(y0 + th))))
    return wins


def cut_by_inner_edge(
    xyxy: np.ndarray,
    win: tuple[int, int, int, int],
    hw: tuple[int, int],
    tol: float = 3.0,
) -> np.ndarray:
    """Bool per box: it touches a side of the tile window that is not also a side of the image (the object continues beyond it)."""
    x0, y0, x1, y1 = win
    h, w = hw
    cut = np.zeros(len(xyxy), bool)
    if x0 > 0:
        cut |= xyxy[:, 0] < x0 + tol
    if y0 > 0:
        cut |= xyxy[:, 1] < y0 + tol
    if x1 < w:
        cut |= xyxy[:, 2] > x1 - tol
    if y1 < h:
        cut |= xyxy[:, 3] > y1 - tol
    return cut


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
        self._cpu_only = (
            False  # set only while one call is being redone on CPU after a GPU OOM
        )
        self._model: Any = None
        self._proc: Any = None

    def load(self) -> None:
        if self.loaded:
            return
        path = models.model_path(
            "owlv2-base"
        )  # missing weights are reported before missing python packages
        require_torch()
        import torch

        try:
            from transformers import Owlv2ForObjectDetection, Owlv2Processor
        except ImportError as exc:
            raise MissingDependency(
                "the `transformers` package is not installed (needed for OWLv2)",
                "uv pip install -e 'packages/threed-vision[seg]'",
            ) from exc

        self.device = (
            "cpu"
            if self._cpu_only
            else resolve_device(self.requested, self.vram_estimate_mb)
        )
        require_cuda_if_requested(self.device if self.requested == "cuda" else "cpu")
        self._proc = Owlv2Processor.from_pretrained(str(path), local_files_only=True)
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        model = Owlv2ForObjectDetection.from_pretrained(
            str(path), local_files_only=True, torch_dtype=dtype
        ).eval()
        try:
            self._model = model.to(self.device)
        except Exception as exc:
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

    def _boxes(
        self,
        rgb: np.ndarray,
        qs: list[str],
        threshold: float,
        tiles: int,
        overlap: float,
    ) -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray]:
        """Candidate boxes (xyxy px of `rgb`, scores, labels, whole-image objectness map) from the whole image plus, for `tiles` > 1, a tiles x tiles grid.

        OWLv2 sees a 960 px square whatever the input size, so a tile of a 2000 px photo is magnified ~2x: thin or small
        tools that fall under the detector's patch size in the full pass get boxes from the tile pass.
        """
        import torch

        h, w = rgb.shape[:2]
        boxes_all: list[np.ndarray] = []
        scores_all: list[np.ndarray] = []
        labels_all: list[str] = []
        objectness: np.ndarray | None = None
        for x0, y0, x1, y1 in tile_windows(h, w, tiles, overlap):
            out = self._forward(rgb[y0:y1, x0:x1], qs)
            if objectness is None:  # the first window is the whole image
                side = int(round(out.objectness_logits.shape[-1] ** 0.5))
                objectness = (
                    torch.sigmoid(out.objectness_logits[0].float())
                    .reshape(side, side)
                    .cpu()
                    .numpy()
                )
            logits = out.logits[0].float()  # (patches, n_queries)
            sc, lab_idx = torch.sigmoid(logits).max(dim=-1)
            keep = (sc >= threshold).nonzero().flatten()
            if not len(keep):
                continue
            norm = out.pred_boxes[0].float()[keep].cpu().numpy()
            xyxy = boxes_from_square(norm, y1 - y0, x1 - x0) + np.array(
                [x0, y0, x0, y0], np.float32
            )
            sc_np = sc[keep].cpu().numpy()
            labs = [qs[int(i)] for i in lab_idx[keep].cpu().numpy()]
            if (
                (x1 - x0, y1 - y0) != (w, h)
            ):  # a tile: a box cut by a tile edge is a fragment, the full pass has the whole
                ok = ~cut_by_inner_edge(xyxy, (x0, y0, x1, y1), (h, w))
                xyxy, sc_np = xyxy[ok], sc_np[ok]
                labs = [lb for lb, k in zip(labs, ok, strict=True) if k]
            boxes_all.append(xyxy)
            scores_all.append(sc_np)
            labels_all += labs
        assert objectness is not None
        if not boxes_all:
            return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), [], objectness
        return (
            np.concatenate(boxes_all),
            np.concatenate(scores_all),
            labels_all,
            objectness,
        )

    def detect(
        self,
        rgb: np.ndarray,
        queries: list[str] | None = None,
        threshold: float = 0.15,
        nms: float = 0.5,
        max_area_frac: float = 0.05,
        tiles: int = 1,
        tile_overlap: float = 0.25,
        with_objectness: bool = False,
    ) -> dict[str, Any]:
        """Boxes for `queries`; `with_objectness` adds OWLv2's class-agnostic objectness map ((g, g) float32 in 0..1 over the image
        padded to a square: row / column i covers [i, i+1) * max(H, W) / g px) under `"objectness"`."""
        if (
            not isinstance(rgb, np.ndarray)
            or rgb.ndim != 3
            or rgb.shape[2] != 3
            or rgb.dtype != np.uint8
        ):
            raise BadInput("detect expects an HxWx3 uint8 RGB array")
        if not (1 <= tiles <= 4) or not (0.0 <= tile_overlap < 0.5):
            raise BadInput(
                f"tiles={tiles} / tile_overlap={tile_overlap} out of range",
                "tiles 1..4 (1 = whole image only), tile_overlap in [0, 0.5)",
            )
        qs = list(queries) if queries else list(DEFAULT_QUERIES)
        t0 = time.perf_counter()
        self.load()
        found: tuple[np.ndarray, np.ndarray, list[str], np.ndarray] | None
        try:
            found = self._boxes(rgb, qs, threshold, tiles, tile_overlap)
        except Exception as exc:
            if not (self.device == "cuda" and is_oom(exc) and self.requested != "cuda"):
                raise
            found = None  # retry below, outside the `except` block, so the traceback does not pin GPU tensors
        if found is None:
            self.unload()
            self._cpu_only = True
            try:
                self.load()
                found = self._boxes(rgb, qs, threshold, tiles, tile_overlap)
            finally:
                self._cpu_only = False
                self.unload()  # the next call re-evaluates `auto` instead of staying on CPU
        xyxy, scores, labels, objectness = found
        h, w = rgb.shape[:2]
        res = (
            select_boxes(xyxy, scores, labels, h, w, nms, max_area_frac)
            if len(xyxy)
            else []
        )
        doc: dict[str, Any] = {
            "boxes": res,
            "timing_s": round(time.perf_counter() - t0, 3),
        }
        if with_objectness:
            doc["objectness"] = objectness
        return doc
