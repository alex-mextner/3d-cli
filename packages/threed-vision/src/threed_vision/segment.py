"""Multi-object segmentation: SAM2.1-tiny automatic masks + OWLv2 box fill + CLIP background rejection.

Pipeline (spike `pipeline_seg.py`, measured 10.6 s / 98 masks on the carpet photo, GPU):
  amg        SAM2.1-tiny automatic mask generator (points_per_side x points_per_side grid, points_per_batch 32)
  detect     OWLv2 open-vocabulary boxes (`queries`)
  fill       SAM2 box prompts for boxes that are < 60 % explained by the AMG masks (new mask overlap < 30 %)
  bg_reject  CLIP zero-shot "background vs object" prompts drop carpet / floor / cabinet patches
Methods: "sam2" (grid 32 on GPU), "sam2-dense" (grid 64, batch 16), "u2net" (single salient object, ~0.06 s GPU).
`fill` is "owlv2" or "none"; `reject_bg` toggles the CLIP filter (the area filter always applies).

On CPU the full-resolution 32x32 AMG takes ~90 s (measured). The CPU default therefore skips the grid (points_per_side
0): OWLv2 boxes become SAM2 box prompts on a copy of the image whose longest side is CPU_MAX_SIDE (see `CPU_*` below);
masks are always returned at the input resolution. `points_per_side=N` on CPU still runs an N x N AMG.

Model-backed stages are classes (`load()` / `unload()` / `loaded` / `device` / `vram_estimate_mb`); a ClipEncoder /
OwlDetector injected into the constructor is shared and is NOT unloaded by `Segmenter.unload()`.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from . import models
from .clip import ClipEncoder, OrtEncoder, crop_masked
from .detect import DEFAULT_QUERIES, OwlDetector
from .device import resolve_device
from .errors import BadInput
from .torchutil import free_cuda, is_oom, peak_mb, require_torch, reset_peak

Progress = Callable[[str, float], None]  # (stage, fraction 0..1)
Instance = dict[str, Any]

METHODS = ("sam2", "sam2-dense", "u2net")
FILLS = ("owlv2", "none")
# CPU default (measurements in docs/commands/segment.md): no AMG grid, OWLv2 boxes -> SAM2 box prompts, all on a copy of the
# image whose longest side is CPU_MAX_SIDE. points_per_side=0 means "no AMG".
CPU_POINTS_PER_SIDE = 0
CPU_AMG_ONLY_POINTS_PER_SIDE = 16  # CPU with fill="none" has nothing but the grid: 16x16 (measured 36 s, 29 instances)
CPU_MAX_SIDE = 1024

BG_PROMPTS = [
    "a photo of a carpet",
    "a photo of a wooden parquet floor",
    "a white cabinet",
    "a photo of a shadow on the floor",
    "a photo of a person's foot in a sock",
    "a photo of a black plastic bin",
    "a cable on the floor",
]
FG_PROMPTS = [
    "a photo of a hand tool",
    "a photo of a power tool",
    "a photo of a box",
    "a photo of a screwdriver",
    "a photo of pliers",
    "a photo of tape",
    "a photo of a tube of glue",
    "a photo of a clamp",
    "a photo of a plastic case",
    "a photo of a measuring instrument",
]


# --- pure instance bookkeeping (numpy only; the UI's click-to-merge / delete / pick) ---------------------------------


def make_instance(
    mask: np.ndarray,
    iid: int,
    source: str,
    bg_prob: float = 0.0,
    stability: float = 0.0,
) -> Instance:
    from .io import mask_bbox_xywh

    m = np.asarray(mask, bool)
    return {
        "id": int(iid),
        "source": source,
        "bbox_xywh": mask_bbox_xywh(m),
        "area_px": int(m.sum()),
        "bg_prob": float(bg_prob),
        "stability": float(stability),
        "mask": m,
    }


def delete_instances(
    instances: Sequence[Instance], ids: Sequence[int]
) -> list[Instance]:
    """New list without the instances whose id is in `ids` (unknown ids are an error so UI bugs surface)."""
    have = {int(i["id"]) for i in instances}
    unknown = sorted(set(map(int, ids)) - have)
    if unknown:
        raise BadInput(
            f"no instance with id {unknown}", f"existing ids: {sorted(have)}"
        )
    drop = set(map(int, ids))
    return [i for i in instances if int(i["id"]) not in drop]


def merge_instances(
    instances: Sequence[Instance], ids: Sequence[int]
) -> list[Instance]:
    """Union of the instances in `ids` replaces them as ONE instance carrying the smallest id (source 'merged')."""
    if len(set(map(int, ids))) < 2:
        raise BadInput("merge needs at least two distinct instance ids")
    by_id = {int(i["id"]): i for i in instances}
    unknown = sorted(set(map(int, ids)) - set(by_id))
    if unknown:
        raise BadInput(
            f"no instance with id {unknown}", f"existing ids: {sorted(by_id)}"
        )
    parts = [by_id[int(i)] for i in ids]
    union = np.zeros_like(parts[0]["mask"], dtype=bool)
    for p in parts:
        union |= p["mask"]
    keep = min(int(i) for i in ids)
    merged = make_instance(
        union,
        keep,
        "merged",
        bg_prob=min(p["bg_prob"] for p in parts),
        stability=min(p["stability"] for p in parts),
    )
    out: list[Instance] = []
    placed = False
    for inst in instances:
        if int(inst["id"]) in set(map(int, ids)):
            if not placed:
                out.append(merged)
                placed = True
            continue
        out.append(inst)
    return out


def pick_instance(instances: Sequence[Instance], x: float, y: float) -> int | None:
    """Id of the smallest instance whose mask contains pixel (x, y) (the one a click means), else None."""
    xi, yi = int(round(x)), int(round(y))
    best: tuple[int, int] | None = None
    for inst in instances:
        m = inst["mask"]
        if (
            0 <= yi < m.shape[0]
            and 0 <= xi < m.shape[1]
            and m[yi, xi]
            and (best is None or inst["area_px"] < best[0])
        ):
            best = (int(inst["area_px"]), int(inst["id"]))
    return None if best is None else best[1]


def next_id(instances: Sequence[Instance]) -> int:
    return max((int(i["id"]) for i in instances), default=0) + 1


def instances_to_labelmap(
    instances: Sequence[Instance], shape: tuple[int, int]
) -> np.ndarray:
    """uint16 label map whose pixel values ARE the instance ids (0 = background); larger masks are painted first."""
    lab = np.zeros(shape, np.uint16)
    for inst in sorted(instances, key=lambda i: -int(i["area_px"])):
        lab[inst["mask"]] = int(inst["id"])
    return lab


def bg_probability(img_emb: np.ndarray, txt_emb: np.ndarray, n_bg: int) -> np.ndarray:
    """Softmax over CLIP similarities of [bg prompts + fg prompts]; returns the summed background probability per crop."""
    sim = 100.0 * img_emb @ txt_emb.T
    p = np.exp(sim - sim.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    return np.asarray(p[:, :n_bg].sum(axis=1))


def _fraction_covered(union: np.ndarray, box: Sequence[float]) -> float:
    x0, y0, x1, y1 = (int(v) for v in box)
    reg = union[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
    return float(reg.mean()) if reg.size else 1.0


# --- u2net single-object matte -------------------------------------------------------------------------------------


class U2Net(OrtEncoder):
    """U2-Net salient-object matte through our own onnxruntime session (rembg's pre/post-processing, no implicit download)."""

    model_name = "u2net"
    vram_estimate_mb = 300

    def _build(self) -> None:
        self._sessions = {"u": self._session("u2net.onnx")}

    def probability(self, rgb: np.ndarray) -> np.ndarray:
        """float32 HxW salient-object probability in 0..1."""
        import cv2

        self.load()
        h, w = rgb.shape[:2]
        x = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_AREA).astype(np.float32)
        x = x / max(float(x.max()), 1e-6)
        x = np.asarray(
            (x - np.array([0.485, 0.456, 0.406], np.float32))
            / np.array([0.229, 0.224, 0.225], np.float32),
            np.float32,
        )
        out = self._sessions["u"].run(
            None,
            {
                self._sessions["u"].get_inputs()[0].name: x.transpose(2, 0, 1)[
                    None
                ].copy()
            },
        )[0][0, 0]
        out = (out - out.min()) / max(float(out.max() - out.min()), 1e-6)
        return np.asarray(
            cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR), np.float32
        )


def segment_single_object(
    rgb: np.ndarray,
    device: str = "auto",
    *,
    encoder: U2Net | None = None,
    min_area: int = 150,
) -> np.ndarray:
    """One tool on a plain background: u2net matte -> largest connected component as a bool HxW mask (0.06 s GPU / 0.4 s CPU)."""
    import cv2

    _check_rgb(rgb)
    enc = encoder or U2Net(device)
    try:
        prob = enc.probability(rgb)
    finally:
        if encoder is None:
            enc.unload()
    fg = (prob > 0.5).astype(np.uint8)
    n, cc, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if n <= 1:
        return np.zeros(rgb.shape[:2], bool)
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = np.asarray(cc == best)
    return mask if int(mask.sum()) >= min_area else np.zeros(rgb.shape[:2], bool)


def _check_rgb(rgb: np.ndarray) -> None:
    if (
        not isinstance(rgb, np.ndarray)
        or rgb.ndim != 3
        or rgb.shape[2] != 3
        or rgb.dtype != np.uint8
    ):
        raise BadInput("expected an HxWx3 uint8 RGB array")


# --- the multi-object segmenter -----------------------------------------------------------------------------------


class Segmenter:
    """SAM2.1-tiny AMG (+ OWLv2 fill + CLIP background rejection). Weights resident between `run()` calls."""

    vram_estimate_mb = 2000

    def __init__(
        self,
        method: str = "sam2",
        fill: str = "owlv2",
        reject_bg: bool = True,
        device: str = "auto",
        points_per_side: int | None = None,
        points_per_batch: int = 32,
        max_side: int | None = None,
        min_area: int = 150,
        max_area_frac: float = 0.03,
        queries: Sequence[str] | None = None,
        owl_threshold: float = 0.15,
        clip: ClipEncoder | None = None,
        detector: OwlDetector | None = None,
    ) -> None:
        if method not in METHODS:
            raise BadInput(
                f"unknown segmentation method '{method}'",
                f"one of: {', '.join(METHODS)}",
            )
        if fill not in FILLS:
            raise BadInput(f"unknown fill '{fill}'", f"one of: {', '.join(FILLS)}")
        self.method, self.fill, self.reject_bg = method, fill, reject_bg
        self.requested = device
        self.points_per_side, self.points_per_batch, self.max_side = (
            points_per_side,
            points_per_batch,
            max_side,
        )
        self.min_area, self.max_area_frac = min_area, max_area_frac
        self.queries = list(queries) if queries else list(DEFAULT_QUERIES)
        self.owl_threshold = owl_threshold
        self.device = "cpu"
        self.loaded = False
        self._owns_clip, self._owns_detector = clip is None, detector is None
        self._clip = clip
        self._detector = detector
        self._sam: Any = None
        self._u2: U2Net | None = None
        self._pred: Any = None
        self._pred_key: str | None = None
        self._bg_text: np.ndarray | None = None

    # lifecycle
    @property
    def uses_sam(self) -> bool:
        return self.method != "u2net"

    def _effective(self) -> tuple[int, int | None]:
        """(points_per_side, processing max_side) for the resolved device; points_per_side 0 = no AMG (OWLv2 boxes only)."""
        pps, side = self.points_per_side, self.max_side
        if self.device == "cuda":
            if pps is None:
                pps = 64 if self.method == "sam2-dense" else 32
            return pps, side
        if pps is None:
            pps = (
                CPU_POINTS_PER_SIDE
                if self.fill == "owlv2"
                else CPU_AMG_ONLY_POINTS_PER_SIDE
            )
        return pps, CPU_MAX_SIDE if side is None else (side or None)

    def load(self) -> None:
        if self.loaded:
            return
        self.device = resolve_device(self.requested, self.vram_estimate_mb)
        if self.uses_sam:
            self._load_sam()
        else:
            self._u2 = U2Net(self.device)
            self._u2.load()
            self.device = self._u2.device
        self.loaded = True

    def _load_sam(self) -> None:
        ckpt = models.model_path(
            "sam2-tiny"
        )  # missing weights are reported before missing python packages
        require_torch()
        try:
            from sam2.build_sam import build_sam2
        except ImportError as exc:
            from .errors import MissingDependency

            raise MissingDependency(
                "the `sam2` package is not installed",
                "uv pip install -e 'packages/threed-vision[seg]'",
            ) from exc
        try:
            self._sam = build_sam2(
                "configs/sam2.1/sam2.1_hiera_t.yaml",
                str(ckpt),
                device=self.device,
                apply_postprocessing=False,
            )
        except Exception as exc:  # noqa: BLE001
            if self.device == "cuda" and is_oom(exc) and self.requested != "cuda":
                free_cuda()
                self.device = "cpu"
                self._sam = build_sam2(
                    "configs/sam2.1/sam2.1_hiera_t.yaml",
                    str(ckpt),
                    device="cpu",
                    apply_postprocessing=False,
                )
            else:
                raise

    def unload(self) -> None:
        """Free SAM2 (and any detector/CLIP/u2net this Segmenter created itself); injected ones stay with their owner."""
        self._sam = None
        self._pred = None
        self._pred_key = None
        if self._u2 is not None:
            self._u2.unload()
            self._u2 = None
        if self._owns_detector and self._detector is not None:
            self._detector.unload()
            self._detector = None
        if self._owns_clip and self._clip is not None:
            self._clip.unload()
            self._clip = None
        self.loaded = False
        free_cuda()

    def _get_detector(self) -> OwlDetector:
        if self._detector is None:
            self._detector = OwlDetector(self.device)
        return self._detector

    def _get_clip(self) -> ClipEncoder:
        if self._clip is None:
            self._clip = ClipEncoder(self.device)
        return self._clip

    # the main entry point
    def run(self, rgb: np.ndarray, progress: Progress | None = None) -> dict[str, Any]:
        _check_rgb(rgb)
        try:
            return self._run(rgb, progress)
        except Exception as exc:  # noqa: BLE001
            if not (self.device == "cuda" and is_oom(exc) and self.requested != "cuda"):
                raise
            self.unload()
            self.requested = "cpu"
            return self._run(rgb, progress)

    def _run(self, rgb: np.ndarray, progress: Progress | None) -> dict[str, Any]:
        t_all = time.perf_counter()
        tm: dict[str, float] = {}

        def stage(name: str, frac: float) -> None:
            if progress:
                progress(name, frac)

        reset_peak()
        if not self.loaded:
            stage("load", 0.0)
            t0 = time.perf_counter()
            self.load()
            tm["load"] = time.perf_counter() - t0
            stage("load", 1.0)
        if self.method == "u2net":
            return self._run_u2net(rgb, tm, t_all, stage)
        h, w = rgb.shape[:2]
        pps, max_side = self._effective()
        scale = 1.0
        proc = rgb
        if max_side and max(h, w) > max_side:
            import cv2

            scale = max_side / max(h, w)
            proc = cv2.resize(
                rgb, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
            )
        ph, pw = proc.shape[:2]

        cands: list[Instance] = []
        if pps > 0:
            stage("amg", 0.0)
            t0 = time.perf_counter()
            cands = self._amg(proc, pps)
            tm["amg"] = time.perf_counter() - t0
            stage("amg", 1.0)
        elif self.fill != "owlv2":
            raise BadInput(
                "points_per_side=0 (boxes only) needs fill='owlv2'",
                "use --points-per-side N>0 or --fill owlv2",
            )
        n_amg = len(cands)

        n_owl = 0
        if self.fill == "owlv2":
            stage("detect", 0.0)
            t0 = time.perf_counter()
            det = self._get_detector()
            boxes = det.detect(proc, self.queries, threshold=self.owl_threshold)[
                "boxes"
            ]
            tm["detect"] = time.perf_counter() - t0
            stage("detect", 1.0)
            stage("fill", 0.0)
            t0 = time.perf_counter()
            added = self._fill(
                proc,
                cands,
                [b["xyxy"] for b in boxes],
                min_px=self.min_area * scale * scale,
            )
            tm["fill"] = time.perf_counter() - t0
            n_owl = len(added)
            cands = cands + added
            stage("fill", 1.0)

        bg = np.zeros(len(cands))
        if self.reject_bg and cands:
            stage("bg_reject", 0.0)
            t0 = time.perf_counter()
            bg = self._reject(proc, [c["mask"] for c in cands])
            tm["bg_reject"] = time.perf_counter() - t0
            stage("bg_reject", 1.0)

        instances: list[Instance] = []
        min_proc = self.min_area * scale * scale
        max_proc = self.max_area_frac * ph * pw
        for c, b in zip(cands, bg, strict=True):
            area = int(c["mask"].sum())
            if (self.reject_bg and b >= 0.5) or area >= max_proc or area < min_proc:
                continue
            mask = c["mask"] if scale == 1.0 else _upscale(c["mask"], (h, w))
            instances.append(
                make_instance(
                    mask, len(instances) + 1, c["source"], float(b), c["stability"]
                )
            )
        tm["total"] = time.perf_counter() - t_all
        return {
            "instances": instances,
            "method": self.method + ("+owlv2" if self.fill == "owlv2" else ""),
            "device": self.device,
            "timing_s": {k: round(v, 3) for k, v in tm.items()},
            "vram_mb": peak_mb(),
            "n_amg": n_amg,
            "n_owl_added": n_owl,
            "points_per_side": pps,
            "process_scale": round(scale, 4),
        }

    def _run_u2net(
        self,
        rgb: np.ndarray,
        tm: dict[str, float],
        t_all: float,
        stage: Callable[[str, float], None],
    ) -> dict[str, Any]:
        stage("matte", 0.0)
        t0 = time.perf_counter()
        assert self._u2 is not None
        mask = segment_single_object(
            rgb, self.device, encoder=self._u2, min_area=self.min_area
        )
        tm["matte"] = time.perf_counter() - t0
        stage("matte", 1.0)
        tm["total"] = time.perf_counter() - t_all
        insts = [make_instance(mask, 1, "u2net", 0.0, 1.0)] if mask.any() else []
        return {
            "instances": insts,
            "method": "u2net",
            "device": self.device,
            "timing_s": {k: round(v, 3) for k, v in tm.items()},
            "vram_mb": peak_mb(),
        }

    # SAM2 stages
    def _amg(self, rgb: np.ndarray, pps: int) -> list[Instance]:
        import torch
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

        batch = (
            self.points_per_batch
            if self.method == "sam2"
            else min(self.points_per_batch, 16)
        )
        gen = SAM2AutomaticMaskGenerator(
            self._sam,
            points_per_side=pps,
            points_per_batch=batch,
            pred_iou_thresh=0.8,
            stability_score_thresh=0.9,
            min_mask_region_area=100,
            box_nms_thresh=0.6,
            crop_n_layers=0,
        )
        with torch.inference_mode():
            res = gen.generate(rgb)
        free_cuda()
        return [
            {
                "mask": np.asarray(r["segmentation"], bool),
                "source": "amg",
                "stability": float(r["stability_score"]),
            }
            for r in res
        ]

    def _predictor(self, rgb: np.ndarray) -> Any:
        """SAM2 image predictor with the image embedded (cached for the last image: clicks on one photo are cheap)."""
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        # full-content hash (~10 ms for 6 MB): a subsampled key could alias two edited versions of one photo
        key = hashlib.blake2b(
            np.ascontiguousarray(rgb).data, digest_size=16
        ).hexdigest() + str(rgb.shape)
        if self._pred is None or self._pred_key != key:
            self._pred = SAM2ImagePredictor(self._sam)
            self._pred.set_image(rgb)
            self._pred_key = key
        return self._pred

    def _fill(
        self,
        rgb: np.ndarray,
        cands: list[Instance],
        boxes: list[list[float]],
        min_px: float,
        max_overlap: float = 0.3,
    ) -> list[Instance]:
        import torch

        union = np.zeros(rgb.shape[:2], bool)
        for c in cands:
            union |= c["mask"]
        added: list[Instance] = []
        if not boxes:
            return added
        pred = self._predictor(rgb)
        with torch.inference_mode():
            for b in boxes:
                if _fraction_covered(union, b) > 0.6:
                    continue  # already explained by AMG masks
                m, s, _ = pred.predict(
                    box=np.asarray(b, np.float32)[None], multimask_output=False
                )
                mask = m[0] > 0
                n = int(mask.sum())
                if n < min_px or (mask & union).sum() / max(n, 1) > max_overlap:
                    continue
                added.append({"mask": mask, "source": "owl", "stability": float(s[0])})
                union |= mask
        return added

    # CLIP background rejection
    def _reject(self, rgb: np.ndarray, masks: list[np.ndarray]) -> np.ndarray:
        clip = self._get_clip()
        crops = [crop_masked(rgb, m, keep_bg=True)[0] for m in masks]
        img = clip.images(crops)
        if self._bg_text is None:
            self._bg_text = clip.texts(BG_PROMPTS + FG_PROMPTS)
        return bg_probability(img, self._bg_text, len(BG_PROMPTS))

    # UI helpers: click-to-add
    def add_by_point(
        self,
        rgb: np.ndarray,
        instances: Sequence[Instance],
        points_xy: Sequence[Sequence[float]],
        labels: Sequence[int] | None = None,
    ) -> list[Instance]:
        """New list = `instances` + the object under the click(s). Labels: 1 foreground (default), 0 background refinement."""
        if not points_xy:
            raise BadInput("add_by_point needs at least one point")
        pts = np.asarray(points_xy, np.float32).reshape(-1, 2)
        lab = (
            np.ones(len(pts), np.int32)
            if labels is None
            else np.asarray(labels, np.int32)
        )
        if len(lab) != len(pts):
            raise BadInput("labels must have one entry per point")
        return self._add_prompt(
            rgb, instances, "click", point_coords=pts, point_labels=lab
        )

    def add_by_box(
        self,
        rgb: np.ndarray,
        instances: Sequence[Instance],
        box_xyxy: Sequence[float],
        source: str = "click",
    ) -> list[Instance]:
        """New list = `instances` + the object inside `box_xyxy` (SAM2 box prompt)."""
        box = np.asarray(box_xyxy, np.float32).reshape(4)
        return self._add_prompt(rgb, instances, source, box=box[None])

    def masks_for_boxes(
        self, rgb: np.ndarray, boxes: Sequence[dict[str, Any]]
    ) -> list[Instance]:
        """One SAM2 box-prompted instance per detector box (`{"xyxy", "score", "label"}`), source "owl".

        Each instance also carries the box's `label`, `score` and `box_xyxy`. A box whose prompt yields an empty mask
        is skipped. The image is embedded once (the predictor caches it). Ids are 1..N in box order.
        """
        out: list[Instance] = []
        for b in boxes:
            try:
                out = self.add_by_box(rgb, out, b["xyxy"], source="owl")
            except BadInput:
                continue
            out[-1].update(
                label=b["label"],
                score=float(b["score"]),
                box_xyxy=[float(v) for v in b["xyxy"]],
            )
        return out

    def _add_prompt(
        self,
        rgb: np.ndarray,
        instances: Sequence[Instance],
        source: str,
        **prompt: np.ndarray,
    ) -> list[Instance]:
        import torch

        _check_rgb(rgb)
        if self.method == "u2net":
            raise BadInput("click-to-add needs a SAM2 method (sam2 / sam2-dense)")
        self.load()
        h, w = rgb.shape[:2]
        pps, max_side = self._effective()
        del pps
        scale, proc = 1.0, rgb
        if max_side and max(h, w) > max_side:
            import cv2

            scale = max_side / max(h, w)
            proc = cv2.resize(
                rgb, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
            )
        scaled = {
            k: (v * scale if k in ("point_coords", "box") else v)
            for k, v in prompt.items()
        }
        pred = self._predictor(proc)
        with torch.inference_mode():
            m, s, _ = pred.predict(
                multimask_output="box" not in prompt
                and len(prompt["point_labels"]) == 1,
                **scaled,
            )
        best = int(np.argmax(s))
        mask = m[best] > 0
        if scale != 1.0:
            mask = _upscale(mask, (h, w))
        if int(mask.sum()) == 0:
            raise BadInput("the prompt produced an empty mask")
        return [
            *instances,
            make_instance(mask, next_id(instances), source, 0.0, float(s[best])),
        ]

    merge = staticmethod(merge_instances)
    delete = staticmethod(delete_instances)
    pick = staticmethod(pick_instance)


def _upscale(mask: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    import cv2

    up = cv2.resize(
        mask.astype(np.uint8) * 255, (hw[1], hw[0]), interpolation=cv2.INTER_LINEAR
    )
    return np.asarray(up >= 128)


__all__ = [
    "BG_PROMPTS",
    "FG_PROMPTS",
    "Segmenter",
    "U2Net",
    "bg_probability",
    "delete_instances",
    "instances_to_labelmap",
    "make_instance",
    "merge_instances",
    "pick_instance",
    "segment_single_object",
]
