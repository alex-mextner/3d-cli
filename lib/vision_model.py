"""vision_model.py — tier (b) of `3d printer vision`: a mid-size image model that rates the WHOLE frame.

Three tiers watch a print (docs/research/printer-vision.md, "Three tiers"):
  (a) cheap OpenCV/ONNX checks every frame: stale/dark camera, Obico spaghetti boxes + CLIP crop
      verifier, bed-change and detached-part geometry (vision_detect.py, vision_scene.py,
      vision_detach.py);
  (b) THIS module: CLIP ViT-B/16 (MIT; vision tower 86 M params, fp16 ONNX 173 MB) on the whole
      frame, scored against prompt sets ("a tangled mess of loose filament", "an empty 3D
      printer build plate", ...) or against a small logistic head fitted on our own labelled
      frames (`3d printer vision fit-head`). Runs on the GPU when onnxruntime has the CUDA
      provider and >= 2 GB of VRAM are free, otherwise on the CPU, every `every_s` seconds
      (default 30) instead of every frame;
  (c) the slow LLM check: the printer agent that a Finding from (a) or (b) wakes up
      (printer_ops/printer_agent.py, subscriptions.py).

Tier (b) only ever adds `Finding`s (severity warning; VisionMonitor turns each into one edge-
triggered event). It never feeds the tracker, `3d printer guard` or any printer write.

Measured (docs/research/printer-vision.md): whole-frame zero-shot spaghetti AUROC 0.89 on the
public test split (tier (a)'s q is more precise; tier (b) adds a second opinion that catches 12 of
the 57 test spaghetti frames tier (a) misses). Zero-shot is NOT reliable for an empty build plate
(it fires on 15 near-identical idle frames, not on the two real shutdown/K1 empties) and has no
skill on "part detached" (AUROC 0.45-0.51): those two classes stay off until a head fitted on
labelled frames of the same camera (`fit-head`) provides them; on held-out data neither a head nor a
partial fine-tune was good enough yet (docs/research/printer-vision.md, 2026-10-23).

The weights are NOT in the repo: `ensure_model(TIER_B)` downloads the Xenova ONNX export into
~/.cache/3d-cli/vision/models/ (sha256-pinned, atomic, idempotent). torch/transformers are never
imported here; only onnxruntime (CPU build, or `onnxruntime-gpu` for the CUDA provider) + OpenCV.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.vision import WARNING, Finding, PrintContext  # noqa: E402
from vision_detect import ModelSpec, ensure_model, models_dir  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

BASE_MODEL_ID = "openai/clip-vit-base-patch16"
BASE_REVISION = "57c216476eefef5ab752ec549e440a49ae4ae5f3"
TIER_B = ModelSpec(
    name="clip-vit-b16-vision-fp16-342fdf2.onnx",
    url=(
        "https://huggingface.co/Xenova/clip-vit-base-patch16/resolve/"
        "342fdf2f67aded64d138ff074745fb4a5d2bba5f/onnx/vision_model_fp16.onnx"
    ),
    sha256="3e261924f7d66d04f10bb131f7f212162d9ee82199544771aa2dd318bb7e2b7b",
    size=172_768_684,
    licence="MIT (OpenAI CLIP ViT-B/16; ONNX export by Xenova/transformers.js)",
    source="https://github.com/openai/CLIP",
)
MODEL_TAG = "clip-vit-b16-fp16-342fdf2"  # what a fitted head must have been fitted on
PROMPTS_FILE = LIB / "data" / "vision" / "model_prompts.json"
HEAD_NAME = "tier-b-head.json"
SIZE = 224
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)
LOGIT_SCALE = 100.0  # CLIP's learned temperature
EMBED_DIM = 512

# The zero-shot prompt sets (their text-tower embeddings are in model_prompts.json; regenerate
# with `python tools/vision_model_bench.py prompts lib/data/vision/model_prompts.json`).
# `healthy` is the reference class: it takes probability mass but never raises a Finding.
PROMPTS: dict[str, list[str]] = {
    "spaghetti": [
        "a tangled mess of loose 3D printer filament strands",
        "a failed 3D print with spaghetti of plastic threads",
        "a bird's nest of plastic filament on a 3D printer bed",
    ],
    "part_detached": [
        "a 3D printed part that has come loose and lifted off the build plate",
        "a 3D print peeling and warping up off the heated bed",
        "a printed part that has detached from the print bed",
        "a failed 3D print that fell off the build plate",
    ],
    "empty_bed": [
        "an empty 3D printer build plate with nothing printed on it",
        "a clean bare print bed inside a 3D printer",
        "a 3D printer with an empty heated bed",
    ],
    "healthy": [
        "a 3D print stuck flat on the build plate of a 3D printer",
        "a healthy 3D print in progress on the print bed",
        "a neat 3D printed part with clean layers on a 3D printer",
    ],
}

# ---- what a class means as a Finding ---------------------------------------------------------


@dataclass(frozen=True)
class ClassRule:
    kind: str  # Finding.kind (and VisionMonitor event kind)
    what: str  # for the event text
    threshold: (
        float | None
    )  # default; None = off until a fitted head or the config enables it
    min_z: float | None = None  # only judged once the print is at least this high (mm)


# Thresholds are measured, not guessed (docs/research/printer-vision.md, "Tier (b) thresholds"):
# spaghetti 0.5 gives 0 of 4952 healthy U1 frames and 0.7 % of public healthy test images;
# empty_bed and part_detached are OFF by default (threshold None): zero-shot flagged none of the
# real empty-bed frames except 15 near-identical idle ones and has no skill on detachment. A
# threshold in the config, or a head fitted by `vision fit-head`, turns a class on.
RULES: dict[str, ClassRule] = {
    "spaghetti": ClassRule("vision_model_spaghetti", "loose filament / spaghetti", 0.5),
    "empty_bed": ClassRule(
        "vision_model_empty_bed",
        "an empty build plate although the print should be well under way",
        None,
        min_z=10.0,
    ),
    "part_detached": ClassRule(
        "vision_model_part_detached", "a part that has come off the build plate", None
    ),
}

# ---- configuration (`watch: vision: model:` and the --model/--no-model flags) ------------------

MIN_FREE_MIB = 2048  # CUDA is chosen only when this much VRAM is free
VRAM_CAP_BYTES = 3 * 1024**3  # onnxruntime's CUDA arena never grows past this
DEVICES = ("auto", "cpu", "cuda")
ENV_DEVICE = "THREED_VISION_DEVICE"


@dataclass(frozen=True)
class ModelConfig:
    enabled: bool | None = None  # None = on when the weights are already cached
    device: str = "auto"
    every_s: float = 30.0
    thresholds: dict[str, float] = field(default_factory=dict)  # per-class overrides
    confirm: int = 3  # consecutive model runs at/above the threshold before a Finding
    clear: int = 2  # consecutive runs below it before the Finding clears
    min_z: float | None = None  # overrides every rule's min_z
    head: Path | None = None  # fitted head JSON (default: models dir / tier-b-head.json)
    crop: tuple[float, float, float, float] | None = None  # normalised x1,y1,x2,y2 to look at


def parse_config(cfg: object, *, flag: bool | None = None) -> ModelConfig:
    """`watch.vision.model` (a mapping, a bare bool, or absent) + the CLI flag -> ModelConfig.
    The flag (--model / --no-model) beats `enabled`."""
    from errors import UsageError

    if cfg is None:
        raw: dict[str, Any] = {}
    elif isinstance(cfg, bool):
        raw = {"enabled": cfg}
    elif isinstance(cfg, dict):
        raw = dict(cfg)
    else:
        raise UsageError(
            "watch.vision.model must be a mapping "
            "({enabled, device, every_s, threshold, confirm, clear, min_z, head, crop}) or a bool",
            command="printer vision",
        )
    known = {
        "enabled",
        "device",
        "every_s",
        "threshold",
        "confirm",
        "clear",
        "min_z",
        "head",
        "crop",
    }
    if set(raw) - known:
        raise UsageError(
            f"watch.vision.model: unknown key(s) {sorted(set(raw) - known)}; accepted: {sorted(known)}",
            command="printer vision",
        )
    enabled = raw.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise UsageError(
            "watch.vision.model.enabled must be true or false", command="printer vision"
        )
    device = str(raw.get("device", "auto"))
    if device not in DEVICES:
        from errors import InvalidArgument

        raise InvalidArgument(
            "watch.vision.model.device", device, DEVICES, command="printer vision"
        )
    every_s = _number(raw, "every_s", 30.0, low=1.0)
    confirm = int(_number(raw, "confirm", 3, low=1))
    clear = int(_number(raw, "clear", 2, low=1))
    min_z = None if raw.get("min_z") is None else _number(raw, "min_z", 0.0, low=0.0)
    thr_raw = raw.get("threshold")
    thresholds: dict[str, float] = {}
    if isinstance(thr_raw, dict):
        for cls, value in thr_raw.items():
            if cls not in RULES:
                raise UsageError(
                    f"watch.vision.model.threshold: unknown class {cls!r}; accepted: {sorted(RULES)}",
                    command="printer vision",
                )
            thresholds[cls] = _unit(value, f"threshold.{cls}")
    elif thr_raw is not None:
        thresholds = dict.fromkeys(RULES, _unit(thr_raw, "threshold"))
    head = raw.get("head")
    crop = _crop(raw.get("crop"))
    return ModelConfig(
        enabled=flag if flag is not None else enabled,
        device=device,
        every_s=every_s,
        thresholds=thresholds,
        confirm=confirm,
        clear=clear,
        min_z=min_z,
        head=None if head is None else Path(str(head)).expanduser(),
        crop=crop,
    )


def _crop(value: Any) -> tuple[float, float, float, float] | None:
    """`crop: [x1, y1, x2, y2]` (0..1): the part of the picture the model looks at, e.g. the bed
    of a wide-angle camera that also shows the room."""
    from errors import UsageError

    if value is None:
        return None
    ok = (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )
    if ok:
        x1, y1, x2, y2 = (float(v) for v in value)
        ok = 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0
    if not ok:
        raise UsageError(
            f"watch.vision.model.crop must be [x1, y1, x2, y2] with 0 <= x1 < x2 <= 1 and "
            f"0 <= y1 < y2 <= 1, got {value!r}",
            command="printer vision",
        )
    return (x1, y1, x2, y2)


def crop_frame(bgr: np.ndarray, crop: tuple[float, float, float, float] | None) -> np.ndarray:
    if crop is None:
        return bgr
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = crop
    return bgr[int(y1 * h) : max(int(y1 * h) + 1, int(y2 * h)), int(x1 * w) : max(int(x1 * w) + 1, int(x2 * w))]


def _number(raw: dict[str, Any], key: str, default: float, *, low: float) -> float:
    from errors import UsageError

    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
        raise UsageError(
            f"watch.vision.model.{key} must be a number >= {low:g}, got {value!r}",
            command="printer vision",
        )
    return float(value)


def _unit(value: Any, name: str) -> float:
    from errors import UsageError

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 < value <= 1.0
    ):
        raise UsageError(
            f"watch.vision.model.{name} must be a probability in (0, 1], got {value!r}",
            command="printer vision",
        )
    return float(value)


# ---- device choice ----------------------------------------------------------------------------


def free_vram_mib() -> int | None:
    """Free VRAM of GPU 0 in MiB from the driver's own tool; None without an NVIDIA driver."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def available_providers() -> list[str]:
    import onnxruntime as ort

    return list(ort.get_available_providers())


def resolve_device(
    requested: str,
    *,
    providers: list[str],
    free_mib: int | None,
    min_free_mib: int = MIN_FREE_MIB,
) -> str:
    """'cpu' or 'cuda'. `auto` picks CUDA only when onnxruntime has the CUDA provider and the
    driver reports >= min_free_mib free (the desktop shares this card); `cuda` is a promise:
    without the provider it is a structured error, never a silent CPU run."""
    from errors import MissingDependency

    if requested == "cpu":
        return "cpu"
    has_cuda = "CUDAExecutionProvider" in providers
    if requested == "cuda":
        if not has_cuda:
            raise MissingDependency(
                "onnxruntime CUDAExecutionProvider (device: cuda)",
                install="uv pip install onnxruntime-gpu  (it replaces the CPU onnxruntime)",
                degrades="tier (b) runs on the CPU only with device: cpu/auto",
                command="printer vision",
            )
        return "cuda"
    return (
        "cuda"
        if has_cuda and free_mib is not None and free_mib >= min_free_mib
        else "cpu"
    )


def requested_device(cfg: ModelConfig) -> str:
    """THREED_VISION_DEVICE beats the config, as with every other 3d-cli env override."""
    env = os.environ.get(ENV_DEVICE, "").strip().lower()
    if not env:
        return cfg.device
    if env not in DEVICES:
        from errors import InvalidArgument

        raise InvalidArgument(ENV_DEVICE, env, DEVICES, command="printer vision")
    return env


# ---- prompts, head, scorer ----------------------------------------------------------------------


@dataclass(frozen=True)
class PromptTable:
    classes: tuple[str, ...]
    owner: tuple[int, ...]  # class index of each prompt row
    emb: Any  # (n_prompts, 512) float32, L2-normalised

    @classmethod
    def load(cls, path: Path = PROMPTS_FILE) -> PromptTable:
        import numpy as np

        d = json.loads(path.read_text())
        classes = tuple(d["classes"])
        rows, owner = [], []
        for ci, c in enumerate(classes):
            for p in d["classes"][c]:
                rows.append(d["embeddings"][p])
                owner.append(ci)
        emb = np.array(rows, np.float32)
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        return cls(classes, tuple(owner), emb)

    def probs(self, img_emb: np.ndarray) -> dict[str, float]:
        """Softmax over every prompt, summed per class: P(class) for one image embedding."""
        import numpy as np

        z = LOGIT_SCALE * (self.emb @ img_emb)
        e = np.exp(z - z.max())
        pr = e / e.sum()
        return {
            c: float(sum(pr[i] for i, o in enumerate(self.owner) if o == ci))
            for ci, c in enumerate(self.classes)
        }


@dataclass(frozen=True)
class HeadClass:
    weights: Any  # (512,) float32
    bias: float
    threshold: float | None  # the Finding threshold chosen at fit time


@dataclass(frozen=True)
class Head:
    """A logistic head per class on the L2-normalised embedding, fitted by `vision fit-head`
    (lib/vision_fit.py) or tools/vision_train; replaces the zero-shot probability of its class."""

    classes: dict[str, HeadClass]
    meta: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> Head:
        import numpy as np
        from errors import ThreeDError

        try:
            d = json.loads(path.read_text())
            if d.get("model") != MODEL_TAG:
                raise ThreeDError(
                    f"{path}: head was fitted on {d.get('model')!r}, this build embeds with {MODEL_TAG!r}",
                    command="printer vision",
                    remediation=["Refit it: 3d printer vision fit-head --help"],
                )
            classes = {
                c: HeadClass(
                    np.array(v["weights"], np.float32),
                    float(v["bias"]),
                    None if v.get("threshold") is None else float(v["threshold"]),
                )
                for c, v in d["classes"].items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ThreeDError(
                f"cannot read the tier (b) head {path}: {exc}",
                command="printer vision",
                remediation=["Refit it: 3d printer vision fit-head --help"],
            ) from exc
        unknown = sorted(set(classes) - set(RULES))
        if unknown or any(w.weights.shape != (EMBED_DIM,) for w in classes.values()):
            raise ThreeDError(
                f"{path}: head classes must be among {sorted(RULES)} with {EMBED_DIM} weights each",
                command="printer vision",
            )
        return cls(classes, {k: v for k, v in d.items() if k != "classes"})

    def prob(self, cls: str, emb: np.ndarray) -> float:
        import numpy as np

        h = self.classes[cls]
        return float(1.0 / (1.0 + np.exp(-(float(h.weights @ emb) + h.bias))))


def head_path(cfg: ModelConfig) -> Path:
    return cfg.head if cfg.head is not None else models_dir() / HEAD_NAME


def preprocess(bgr: np.ndarray) -> np.ndarray:
    """Whole frame, squashed (no centre crop: the part may sit at the edge of a 16:9 picture)
    to 224x224, CLIP-normalised, NCHW float32."""
    import cv2
    import numpy as np

    rgb = cv2.cvtColor(
        cv2.resize(bgr, (SIZE, SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB
    )
    x = (rgb.astype(np.float32) / 255.0 - np.array(MEAN, np.float32)) / np.array(
        STD, np.float32
    )
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None])


class ModelScorer:
    """Frame -> embedding -> per-class probabilities. One onnxruntime session, kept until close().

    `device` is what `resolve_device` decided; on 'cuda' the session's memory arena is capped at
    VRAM_CAP_BYTES. If the CUDA provider fails to load (missing cuDNN/cuBLAS), onnxruntime would
    silently run on the CPU; this class notices and reports `device == 'cpu'`."""

    def __init__(
        self,
        model: Path,
        *,
        device: str = "cpu",
        threads: int | None = None,
        prompts: PromptTable | None = None,
        head: Head | None = None,
    ) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or _threads()
        opts.inter_op_num_threads = 1
        providers: list[Any] = ["CPUExecutionProvider"]
        if device == "cuda":
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()  # cuDNN/cuBLAS from the nvidia-* pip wheels
            providers = [
                (
                    "CUDAExecutionProvider",
                    {
                        "gpu_mem_limit": VRAM_CAP_BYTES,
                        "arena_extend_strategy": "kSameAsRequested",
                        "cudnn_conv_algo_search": "HEURISTIC",
                    },
                ),
                "CPUExecutionProvider",
            ]
        self.session: Any = ort.InferenceSession(str(model), opts, providers=providers)
        self.device = (
            "cuda"
            if self.session.get_providers()[0] == "CUDAExecutionProvider"
            else "cpu"
        )
        self.input = self.session.get_inputs()[0].name
        self.prompts = prompts or PromptTable.load()
        self.head = head

    def embed(self, bgr: np.ndarray) -> np.ndarray:
        import numpy as np

        if self.session is None:
            raise RuntimeError("ModelScorer is closed")
        y = (
            self.session.run(None, {self.input: preprocess(bgr)})[0]
            .astype(np.float32)
            .reshape(-1)
        )
        if y.shape != (EMBED_DIM,):
            raise ValueError(
                f"tier (b) model returned {y.shape}, expected ({EMBED_DIM},)"
            )
        return y / np.linalg.norm(y)

    def embed_many(self, frames: list[np.ndarray]) -> np.ndarray:
        import numpy as np

        if not frames:
            return np.zeros((0, EMBED_DIM), np.float32)
        return np.stack([self.embed(f) for f in frames])

    def probs(self, emb: np.ndarray) -> dict[str, float]:
        """Zero-shot class probabilities, with the fitted head's classes overriding theirs."""
        out = self.prompts.probs(emb)
        if self.head is not None:
            for cls in self.head.classes:
                out[cls] = self.head.prob(cls, emb)
        return out

    def score_bgr(self, bgr: np.ndarray) -> dict[str, float]:
        return self.probs(self.embed(bgr))

    def close(self) -> None:
        """Drop the session (frees its VRAM arena); safe to call twice."""
        self.session = None


def _threads() -> int:
    from cli.resources import max_workers

    return max(1, min(2, max_workers()))


# ---- the tier: cadence, debounce, findings -----------------------------------------------------


@dataclass
class _ClassState:
    hits: int = 0
    misses: int = 0
    active: bool = False
    score: float = 0.0


@dataclass
class ModelTier:
    """Runs the scorer every `every_s` seconds and keeps each class' Finding alive in between
    (VisionMonitor clears a Finding kind as soon as one frame lacks it, and would re-fire it)."""

    scorer: ModelScorer
    config: ModelConfig = field(default_factory=ModelConfig)
    clock: Callable[[], float] = time.time
    last_run: float | None = None
    last_scores: dict[str, float] = field(default_factory=dict)
    last_latency_s: float = 0.0
    runs: int = 0
    _state: dict[str, _ClassState] = field(default_factory=dict)
    _held: list[Finding] = field(default_factory=list)

    def threshold(self, cls: str) -> float | None:
        if cls in self.config.thresholds:
            return self.config.thresholds[cls]
        if self.scorer.head is not None and cls in self.scorer.head.classes:
            fitted = self.scorer.head.classes[cls].threshold
            if fitted is not None:
                return fitted
        return RULES[cls].threshold

    def _min_z(self, cls: str) -> float | None:
        return self.config.min_z if self.config.min_z is not None else RULES[cls].min_z

    def reset_print(self) -> None:
        """A new print: forget the debounce counters and any standing Finding."""
        self._state.clear()
        self._held = []
        self.last_run = None

    def update(
        self, bgr: np.ndarray, context: PrintContext | None, now: float | None = None
    ) -> list[Finding]:
        """The Findings standing after this frame (scored only if `every_s` has passed)."""
        now = self.clock() if now is None else now
        if self.last_run is not None and 0 <= now - self.last_run < self.config.every_s:
            return list(self._held)
        t0 = time.perf_counter()
        scores = self.scorer.score_bgr(crop_frame(bgr, self.config.crop))
        self.last_latency_s = time.perf_counter() - t0
        self.last_run, self.last_scores, self.runs = now, scores, self.runs + 1
        held: list[Finding] = []
        for cls, rule in RULES.items():
            thr = self.threshold(cls)
            min_z = self._min_z(cls)
            st = self._state.setdefault(cls, _ClassState())
            st.score = scores.get(cls, 0.0)
            judged = thr is not None and (
                min_z is None or (context is not None and context.z >= min_z)
            )
            if not judged or thr is None:
                st.hits = st.misses = 0
                st.active = False
                continue
            if st.score >= thr:
                st.hits, st.misses = st.hits + 1, 0
                st.active = st.active or st.hits >= self.config.confirm
            else:
                st.hits, st.misses = 0, st.misses + 1
                if st.misses >= self.config.clear:
                    st.active = False
            if st.active:
                held.append(self._finding(cls, rule, st, thr, context))
        self._held = held
        return list(held)

    def _finding(
        self,
        cls: str,
        rule: ClassRule,
        st: _ClassState,
        thr: float,
        ctx: PrintContext | None,
    ) -> Finding:
        text = (
            f"the frame-level vision model sees {rule.what} (score {st.score:.2f} >= {thr:.2f}, "
            f"{st.hits} checks in a row, {self.scorer.device}); "
            "look at the frame, this is a second opinion, not a verdict"
        )
        data: dict[str, Any] = {
            "class": cls,
            "score": round(st.score, 3),
            "threshold": thr,
            "scores": {k: round(v, 3) for k, v in self.last_scores.items()},
            "model": MODEL_TAG,
            "device": self.scorer.device,
            "latency_ms": round(self.last_latency_s * 1000, 1),
        }
        if ctx is not None:
            data["z"] = round(ctx.z, 2)
        return Finding(rule.kind, WARNING, text, data)

    def report(self) -> dict[str, Any]:
        """The last run's class scores, for `vision run --once` and logs."""
        return {
            "scores": {k: round(v, 3) for k, v in self.last_scores.items()},
            "device": self.scorer.device,
            "latency_ms": round(self.last_latency_s * 1000, 1),
            "runs": self.runs,
        }

    def close(self) -> None:
        self.scorer.close()


# ---- construction -----------------------------------------------------------------------------


def cached(spec: ModelSpec = TIER_B) -> bool:
    """The weights are on disk and pass their sha256 (never downloads)."""
    from errors import ThreeDError

    try:
        ensure_model(spec, fetch=False)
    except ThreeDError:
        return False
    return True


def fetch(spec: ModelSpec = TIER_B) -> Path:
    """`vision fetch-model --tier b`: the verified weights, downloaded once."""
    return ensure_model(spec)


def open_scorer(mc: ModelConfig, *, download: bool, use_head: bool = True) -> ModelScorer:
    """The scorer for `mc`: verified weights (downloaded when `download`), the fitted head if one
    exists, and the device `resolve_device` picks. Raises a structured MissingDependency when the
    weights cannot be had (offline) or device: cuda has no CUDA provider."""
    from errors import MissingDependency

    try:
        path = ensure_model(TIER_B, fetch=download)
    except (OSError, ValueError) as exc:  # offline / DNS / TLS while downloading
        raise MissingDependency(
            f"tier (b) vision model {TIER_B.name} ({exc})",
            install="3d printer vision fetch-model --tier b",
            degrades="the frame-level model check cannot run (tier (a) is unaffected)",
            command="printer vision",
        ) from exc
    hp = head_path(mc)
    head = Head.load(hp) if use_head and hp.is_file() else None
    device = resolve_device(
        requested_device(mc), providers=available_providers(), free_mib=free_vram_mib()
    )
    scorer = ModelScorer(path, device=device, head=head)
    if device == "cuda" and scorer.device == "cpu":
        print(
            "vision: onnxruntime could not start the CUDA provider; tier (b) runs on the CPU",
            file=sys.stderr,
        )
    return scorer


def build_tier(cfg: object, *, flag: bool | None = None) -> ModelTier | None:
    """The tier from a device's `watch: vision: model:` block and the --model/--no-model flag.

    Default (no flag, no `enabled`): ON when the weights are already cached, OFF otherwise, so a
    live loop never starts a 173 MB download by itself. `enabled: true` / --model is a request:
    it downloads the weights if needed and fails with a structured error when it cannot."""
    mc = parse_config(cfg, flag=flag)
    if mc.enabled is False or (mc.enabled is None and not cached()):
        return None
    return ModelTier(open_scorer(mc, download=mc.enabled is True), mc)


# ---- `vision eval --model`: per-class numbers on a folder of frames ------------------------------

EVAL_THRESHOLDS = (0.1, 0.3, 0.5, 0.7, 0.8, 0.9)


def eval_sources(
    sources: list[tuple[Path, str]], mc: ModelConfig, *, every: int = 1, cls: str = "spaghetti"
) -> dict[str, Any]:
    """Score every frame of the (path, "pos"|"neg") sources with tier (b) (every frame, no
    cadence, no debounce) and report the per-frame table of class `cls` plus the mean of every
    class on each side. The model-on-a-folder counterpart of the tier (a) tables in
    vision_detect.metrics."""
    from errors import InvalidArgument
    from vision_detect import _table, iter_frames

    if cls not in RULES:
        raise InvalidArgument("--model-class", cls, sorted(RULES), command="printer vision eval")
    scorer = open_scorer(mc, download=True)
    try:
        pos: list[dict[str, float]] = []
        neg: list[dict[str, float]] = []
        pos_names: list[str] = []
        neg_names: list[str] = []
        t0 = time.perf_counter()
        for path, label in sources:
            for name, img in iter_frames(path, every):
                (pos if label == "pos" else neg).append(scorer.score_bgr(crop_frame(img, mc.crop)))
                (pos_names if label == "pos" else neg_names).append(name)
        n = len(pos) + len(neg)
        seconds = (time.perf_counter() - t0) / max(1, n)
        ps, ns = [s[cls] for s in pos], [s[cls] for s in neg]
        worst = sorted(zip(ns, neg_names, strict=True), reverse=True)[:5]
        return {
            "class": cls,
            "device": scorer.device,
            "head": sorted(scorer.head.classes) if scorer.head else [],
            "frames": {"pos": len(pos), "neg": len(neg)},
            "per_frame": _table(ps, ns, EVAL_THRESHOLDS),
            "mean": {
                side: {c: round(sum(s[c] for s in rows) / len(rows), 3) for c in scorer.prompts.classes}
                for side, rows in (("pos", pos), ("neg", neg))
                if rows
            },
            "worst_negatives": [{"frame": f, "score": round(v, 3)} for v, f in worst],
            "seconds_per_frame": round(seconds, 4),
        }
    finally:
        scorer.close()


def print_eval(m: dict[str, Any]) -> None:
    print(
        f"tier (b) {MODEL_TAG} on {m['device']}"
        + (f", head for {m['head']}" if m["head"] else ", zero-shot prompts")
        + f": {m['frames']['pos']} positive, {m['frames']['neg']} negative frames, class {m['class']}"
    )
    print("per frame        threshold  precision  recall   FPR      (tp/fp/fn/tn)")
    for t, r in m["per_frame"].items():
        print(
            f"                 >={t:<8} {r['precision']!s:<10} {r['recall']!s:<8} {r['fpr']!s:<8} "
            f"({r['tp']}/{r['fp']}/{r['fn']}/{r['tn']})"
        )
    for side, means in m["mean"].items():
        print(f"mean class score on {side}: " + ", ".join(f"{k} {v}" for k, v in means.items()))
    print(
        "highest-scoring negatives: "
        + ", ".join(f"{w['frame']} {w['score']}" for w in m["worst_negatives"])
    )
    print(f"speed: {m['seconds_per_frame']} s/frame (decode + preprocess + model)")
