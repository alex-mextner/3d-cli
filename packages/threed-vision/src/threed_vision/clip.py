"""CLIP ViT-B/32 and DINOv2-small ONNX encoders (onnxruntime, CUDA provider with CPU fallback) + crop helpers.

`ClipEncoder` / `DinoEncoder` follow the stage-class lifecycle: `load()` / `unload()` / `loaded` / `device` /
`vram_estimate_mb`. The requested device is only a request: after `load()` `.device` and `.providers` report what the
sessions REALLY run on (`session.get_providers()`), because onnxruntime-gpu silently falls back to CPU when a CUDA
library is missing. Weights come from `models.model_path` (never downloaded here).
"""

from __future__ import annotations

import gc
from typing import Any

import numpy as np

from . import models
from .device import (
    Device,
    ort_missing_cuda_libs,
    ort_providers,
    resolve_device,
    setup_ort_cuda,
)
from .errors import MissingDependency

CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
IN_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IN_STD = np.array([0.229, 0.224, 0.225], np.float32)
PAD_RGB = (
    124,
    116,
    104,
)  # CLIP-mean grey used for letterbox padding and for blanked crop backgrounds


def letterbox(
    rgb: np.ndarray, size: int, fill: tuple[int, int, int] = PAD_RGB
) -> np.ndarray:
    """Aspect-preserving resize onto a `size` x `size` canvas (no distortion, centred)."""
    import cv2

    h, w = rgb.shape[:2]
    s = size / max(h, w)
    r = cv2.resize(
        rgb, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA
    )
    canvas = np.empty((size, size, 3), np.uint8)
    canvas[:] = fill
    y0, x0 = (size - r.shape[0]) // 2, (size - r.shape[1]) // 2
    canvas[y0 : y0 + r.shape[0], x0 : x0 + r.shape[1]] = r
    return canvas


def prep(
    crops: list[np.ndarray], size: int, mean: np.ndarray, std: np.ndarray
) -> np.ndarray:
    """RGB uint8 crops -> (N,3,size,size) float32 normalised batch (letterboxed)."""
    x = np.stack(
        [(letterbox(c, size).astype(np.float32) / 255 - mean) / std for c in crops]
    ).transpose(0, 3, 1, 2)
    return np.ascontiguousarray(x)


def crop_masked(
    rgb: np.ndarray,
    mask: np.ndarray,
    pad: float = 0.12,
    bg: tuple[int, int, int] = PAD_RGB,
    keep_bg: bool = False,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Padded bbox crop of `mask`; outside the (slightly dilated) mask the crop is painted `bg` unless `keep_bg`."""
    import cv2

    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        raise ValueError("empty mask")
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    p = int(pad * max(x1 - x0, y1 - y0)) + 2
    x0, y0, x1, y1 = (
        max(0, x0 - p),
        max(0, y0 - p),
        min(rgb.shape[1], x1 + p),
        min(rgb.shape[0], y1 + p),
    )
    c = rgb[y0:y1, x0:x1].copy()
    if not keep_bg:
        m = (
            cv2.dilate(mask.astype(np.uint8), np.ones((5, 5), np.uint8))[y0:y1, x0:x1]
            > 0
        )
        c[~m] = bg
    return c, (x0, y0, x1, y1)


def _l2(e: np.ndarray) -> np.ndarray:
    return np.asarray(
        e / np.maximum(np.linalg.norm(e, axis=-1, keepdims=True), 1e-12), np.float32
    )


class OrtEncoder:
    """Shared onnxruntime session management: device request -> real providers, optimisation fallback, unload."""

    model_name = ""
    vram_estimate_mb = 0

    def __init__(self, device: str = "auto") -> None:
        self.requested = device
        self.device: Device = "cpu"
        self.providers: list[str] = []
        self._sessions: dict[str, Any] = {}
        self.loaded = False

    def _ort(self) -> Any:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise MissingDependency(
                "onnxruntime is not installed (needed for CLIP/DINOv2/u2net)",
                "uv pip install 'packages/threed-vision[gpu]'  (or [onnx] for CPU only)",
            ) from exc
        return ort

    def _session(self, rel: str) -> Any:
        """Build one session for `rel`; records the providers that are really active."""
        path = models.model_path(
            self.model_name, rel
        )  # a missing model is reported before a missing package
        ort = self._ort()
        want_cuda = self.device == "cuda"
        providers = ort_providers(self.device)
        last: Exception | None = None
        for level in (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
            ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
        ):
            so = ort.SessionOptions()
            so.log_severity_level = 3
            so.graph_optimization_level = level
            try:
                sess = ort.InferenceSession(str(path), so, providers=providers)
            except (
                Exception
            ) as exc:  # ORT raises a bare RuntimeError/Fail for unloadable graphs
                last = exc
                continue
            active = list(sess.get_providers())
            if want_cuda and (not active or active[0] != "CUDAExecutionProvider"):
                if self.requested == "cuda":
                    raise MissingDependency(
                        f"onnxruntime dropped the CUDA provider for {path.name} (active: {active})",
                        "check `3d doctor` (onnxruntime-gpu 1.23.x + the CUDA-12 libs from the torch cu128 wheels)",
                    )
                self.device = (
                    "cpu"  # the CUDA provider was dropped silently: report the truth
                )
            self.providers = active
            return sess
        raise MissingDependency(
            f"cannot load {path.name} with onnxruntime: {last}",
            f"3d vision-models pull {self.model_name}",
        ) from last

    def _resolve(self) -> None:
        self.device = resolve_device(self.requested, self.vram_estimate_mb)
        if self.device == "cuda" and not setup_ort_cuda():
            if self.requested == "cuda":
                missing = ort_missing_cuda_libs()
                raise MissingDependency(
                    f"onnxruntime CUDA provider unusable: missing {', '.join(missing)}"
                    if missing
                    else "onnxruntime has no CUDAExecutionProvider (the CPU-only wheel is installed)",
                    "install onnxruntime-gpu 1.23.x and torch from the cu128 index (its nvidia-* wheels provide the CUDA-12 libs); `3d doctor` shows the state",
                )
            self.device = "cpu"

    def _build(self) -> None:
        raise NotImplementedError

    def load(self) -> None:
        if self.loaded:
            return
        self._resolve()
        self._build()
        self.loaded = True

    def unload(self) -> None:
        self._sessions.clear()
        self.loaded = False
        gc.collect()


class ClipEncoder(OrtEncoder):
    """CLIP ViT-B/32: `images(crops) -> (N,512)` and `texts(prompts) -> (N,512)`, both L2-normalised."""

    model_name = "clip-b32"
    vram_estimate_mb = 500

    def _build(self) -> None:
        sessions = {
            "v": self._session("clipb32_vision.onnx"),
            "t": self._session("clip_text.onnx"),
        }
        tok_path = models.model_path(self.model_name, "clip_tokenizer.json")
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise MissingDependency(
                "tokenizers is not installed (needed for the CLIP text tower)",
                "uv pip install 'packages/threed-vision[gpu]'",
            ) from exc
        self._sessions = sessions
        self._tok = Tokenizer.from_file(str(tok_path))

    def images(self, crops: list[np.ndarray], batch_size: int = 32) -> np.ndarray:
        self.load()
        if not crops:
            return np.zeros((0, 512), np.float32)
        out = []
        for i in range(0, len(crops), batch_size):
            e = self._sessions["v"].run(
                None,
                {
                    "pixel_values": prep(
                        crops[i : i + batch_size], 224, CLIP_MEAN, CLIP_STD
                    )
                },
            )[0]
            out.append(_l2(e))
        return np.concatenate(out)

    def texts(self, prompts: list[str]) -> np.ndarray:
        self.load()
        out = []
        for p in prompts:
            ids = self._tok.encode(p).ids
            if len(ids) > 77:  # keep the end-of-text token (the model pools on it)
                ids = [*ids[:76], ids[-1]]
            e = self._sessions["t"].run(None, {"input_ids": np.array([ids], np.int64)})[
                0
            ]
            out.append(_l2(e[0]))
        return np.stack(out) if out else np.zeros((0, 512), np.float32)


class DinoEncoder(OrtEncoder):
    """DINOv2-small: `images(crops) -> (N,768)` = L2-normalised [CLS, mean patch token]."""

    model_name = "dinov2-s"
    vram_estimate_mb = 300

    def _build(self) -> None:
        self._sessions = {"d": self._session("dinov2s.onnx")}

    def images(self, crops: list[np.ndarray], batch_size: int = 32) -> np.ndarray:
        self.load()
        if not crops:
            return np.zeros((0, 768), np.float32)
        out = []
        for i in range(0, len(crops), batch_size):
            h = self._sessions["d"].run(
                None,
                {"pixel_values": prep(crops[i : i + batch_size], 224, IN_MEAN, IN_STD)},
            )[0]
            out.append(_l2(np.concatenate([h[:, 0], h[:, 1:].mean(1)], axis=1)))
        return np.concatenate(out)
