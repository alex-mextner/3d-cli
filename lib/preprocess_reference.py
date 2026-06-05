#!/usr/bin/env python3
"""
preprocess_reference.py -- one-shot reference-photo pre-processor for the
pixel-perfect 2D->3D pipeline (report Sec 6.1-6.2, Sec 8.2 #5).

ACCESSED VIA: `3d preprocess <reference.jpg>` (lib/commands/preprocess.py shells out to
this script via cli.pyrun.exec_tool, passing args straight through).

INVARIANTS:
  - SUBJECT-AGNOSTIC: this processes whatever subject the caller's photo contains. No
    subject identity is hardcoded -- the segmentation/depth heuristics assume only that
    the subject is roughly centered and fills the frame; nothing branches on what the
    subject actually is.
  - Always produces both outputs (mask.png + depth.png), degrading to the OpenCV/numpy
    floor when heavy model deps are absent (never blocks, never leaves outputs missing).
  - mask.png is a solid 0/255 uint8 silhouette; depth.png is 8-bit with background=0.

Produces, from a single reference photo of the subject:
  * mask.png  -- clean binary SUBJECT MASK (subject vs background), white=subject
  * depth.png -- proportional (relative) DEPTH map, 8-bit, brighter = nearer camera

These two artifacts are what make the silhouette metric trustworthy:
  - mask.png is the target silhouette for the IoU / ImageMagick-AE loss (Sec 7.1-7.3).
  - depth.png is the second critic channel / proportion sanity check (Sec 6.1).

TIERED DESIGN (auto-selected at runtime, always produces output)
----------------------------------------------------------------
MASK:
  1. rembg  (ONNX U2-Net salient-object seg) -- light "real" path, attempted.
  2. SAM 2  (Meta Segment-Anything-2)        -- documented full path, see --help.
  3. cv2.grabCut                              -- always-available OpenCV FALLBACK.
DEPTH:
  1. Depth Anything V2 (transformers pipeline) -- "real" monocular depth, attempted.
  2. gradient / intensity pseudo-depth         -- always-available FALLBACK.

The script prints which TIER actually ran for each artifact, so test output proves
the path. Heavy paths are import-guarded: if the package is missing or the model
download is unavailable, it silently degrades to the OpenCV/numpy floor -- it never
blocks and never leaves the outputs missing.

--------------------------------------------------------------------------------
ENABLING THE FULL "BEST PATH" (SAM 2 + Depth Anything V2)
--------------------------------------------------------------------------------
Run this script inside an env that has the heavy deps. Python 3.12 is recommended
(torch/onnxruntime have no cp314 wheels yet). Example with `uv`:

  # Depth Anything V2 (real monocular depth) -- lightest real upgrade, ~100MB model:
  uv run --python 3.12 --with opencv-python-headless,numpy,pillow \
         --with "transformers>=4.45" --with torch \
         preprocess/preprocess_reference.py references/subject.jpg

  # rembg (real salient mask, ONNX, lighter than SAM 2):
  uv run --python 3.12 --with opencv-python-headless,numpy,pillow \
         --with "rembg[cpu]" \
         preprocess/preprocess_reference.py references/subject.jpg

  # SAM 2 (the report's named "best" segmenter) -- heaviest, do this in a venv:
  #   pip install "git+https://github.com/facebookresearch/sam2.git"
  #   # download a checkpoint, e.g. sam2.1_hiera_small:
  #   curl -L -o sam2.1_hiera_small.pt \
  #     https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt
  #   then pass:  --sam2-checkpoint sam2.1_hiera_small.pt \
  #               --sam2-config configs/sam2.1/sam2.1_hiera_s.yaml
  #   (SAM 2 is prompt-based; this script prompts with a centered box covering the
  #    subject. For per-feature sub-masks -- e.g. a locomotive's funnel/boiler/cab --
  #    prompt with points instead.)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

# OpenCV is the guaranteed floor. If even this is missing we cannot run at all.
try:
    import cv2
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "FATAL: OpenCV (cv2) is required for the fallback path.\n"
        "  install:  pip install opencv-python-headless numpy pillow\n"
    )
    raise


# --------------------------------------------------------------------------- #
#  IO helpers
# --------------------------------------------------------------------------- #
def load_rgb(path: Path) -> np.ndarray:
    """Load an image as HxWx3 uint8 RGB (handles RGBA/grayscale)."""
    img = Image.open(path).convert("RGB")
    return np.asarray(img)


def save_gray(arr: np.ndarray, path: Path) -> None:
    """Save a HxW uint8 array as a single-channel PNG."""
    Image.fromarray(arr.astype(np.uint8), mode="L").save(path)


# --------------------------------------------------------------------------- #
#  MASK -- tier 1: rembg
# --------------------------------------------------------------------------- #
def mask_rembg(rgb: np.ndarray) -> np.ndarray | None:
    """Salient-object mask via rembg (ONNX U2-Net). Returns 0/255 mask or None."""
    try:
        from rembg import remove, new_session
    except Exception:
        return None
    try:
        session = new_session("u2net")  # may download ~170MB on first run
        cut = remove(rgb, session=session, only_mask=True)  # HxW uint8 alpha
        cut = np.asarray(cut)
        if cut.ndim == 3:
            cut = cut[..., -1]
        mask = (cut > 127).astype(np.uint8) * 255
        return mask
    except Exception as exc:  # download / runtime failure -> degrade
        sys.stderr.write(f"[mask] rembg available but failed ({exc}); degrading.\n")
        return None


# --------------------------------------------------------------------------- #
#  MASK -- tier 2: SAM 2 (documented best path)
# --------------------------------------------------------------------------- #
def mask_sam2(rgb: np.ndarray, checkpoint: str | None, config: str | None) -> np.ndarray | None:
    """SAM 2 box-prompted mask. Needs an installed sam2 + checkpoint path."""
    if not checkpoint:
        return None
    try:
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
    except Exception:
        sys.stderr.write("[mask] SAM2 requested but sam2/torch not importable; degrading.\n")
        return None
    try:
        cfg = config or "configs/sam2.1/sam2.1_hiera_s.yaml"
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = build_sam2(cfg, checkpoint, device=device)
        predictor = SAM2ImagePredictor(model)
        predictor.set_image(rgb)
        h, w = rgb.shape[:2]
        # Centered box covering the bulk of the subject (side elevation fills the frame).
        box = np.array([0.04 * w, 0.10 * h, 0.96 * w, 0.92 * h])
        masks, scores, _ = predictor.predict(box=box[None, :], multimask_output=False)
        m = masks[0].astype(np.uint8) * 255
        return m
    except Exception as exc:
        sys.stderr.write(f"[mask] SAM2 available but failed ({exc}); degrading.\n")
        return None


# --------------------------------------------------------------------------- #
#  MASK -- tier 3: OpenCV grabCut (guaranteed fallback)
# --------------------------------------------------------------------------- #
def _grabcut_attempt(bgr: np.ndarray, rect: tuple[int, int, int, int], iters: int) -> np.ndarray:
    """Single GrabCut attempt with a specific rect. Returns 0/255 mask."""
    h, w = bgr.shape[:2]
    gc = np.zeros((h, w), np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(bgr, gc, rect, bgd, fgd, iters, cv2.GC_INIT_WITH_RECT)
    return np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)


def _mask_quality(mask: np.ndarray) -> float:
    """Score a binary 0/255 mask for quality: area fraction near 30–70% is best.

    Front-facing symmetric subjects (buildings, monuments) typically fill 30–70% of
    the frame. Near-zero means segmentation collapsed (grabbed nothing); near-100%
    means it grabbed the whole image (background included). Penalise both extremes.
    """
    frac = float(mask.mean()) / 255.0
    if frac < 0.05 or frac > 0.95:
        return 0.0
    # Prefer masks whose area fraction is near 0.5 (centred, neither too small nor too large).
    return 1.0 - abs(frac - 0.40) * 2.0


def _clean_mask(mask: np.ndarray) -> np.ndarray:
    """Morphological cleanup + largest-component + hole-fill on a 0/255 mask."""
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == largest, 255, 0).astype(np.uint8)
    # Fill internal holes.
    ff = mask.copy()
    fh, fw = ff.shape
    flood = np.zeros((fh + 2, fw + 2), np.uint8)
    cv2.floodFill(ff, flood, (0, 0), 255)
    return (mask | cv2.bitwise_not(ff)).astype(np.uint8)


def mask_grabcut(rgb: np.ndarray, iters: int = 6) -> np.ndarray:
    """GrabCut subject mask with multi-attempt border-inset selection.

    Front-facing symmetric subjects (buildings, monuments) confuse GrabCut when the
    subject fills most of the frame — a 2% border gives it almost nothing to anchor
    as definite background, causing the model to label the subject interior as background.

    Fix: try three border-inset configurations (tight 2%, medium 8%, generous 15%)
    and pick the one with the highest quality score (area fraction near 30–70%, maximally
    connected). The winning mask is then cleaned (morphology + largest-cc + hole-fill).
    Uses an asymmetric inset: more space at top (sky) and bottom (ground) than at the sides.
    """
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]

    # Border inset configs: (x_frac, y_frac) — fraction of width/height to inset on each side.
    # Asymmetric: buildings typically have sky above and ground below, so top/bottom get
    # more inset than sides to give GrabCut better background anchors.
    configs = [
        (0.02, 0.04),   # original tight config — works when subject is small-ish
        (0.06, 0.12),   # medium inset — better for full-frame facades
        (0.12, 0.18),   # generous inset — for very full-frame subjects
    ]

    best_mask: np.ndarray | None = None
    best_score = -1.0
    for (xf, yf) in configs:
        mx, my = max(1, int(xf * w)), max(1, int(yf * h))
        rect = (mx, my, w - 2 * mx, h - 2 * my)
        try:
            raw = _grabcut_attempt(bgr, rect, iters)
        except cv2.error:
            continue
        score = _mask_quality(raw)
        if score > best_score:
            best_score = score
            best_mask = raw

    if best_mask is None:
        # All attempts failed — return empty mask so downstream raises/degrades gracefully.
        return np.zeros((h, w), np.uint8)

    return _clean_mask(best_mask)


# --------------------------------------------------------------------------- #
#  DEPTH -- tier 1: Depth Anything V2
# --------------------------------------------------------------------------- #
def depth_depthanything(rgb: np.ndarray, model_id: str) -> np.ndarray | None:
    """Monocular relative depth via the transformers depth-estimation pipeline.

    Returns the RAW float32 relative-depth field (larger = nearer). Normalization
    is deferred to the caller so it can be scaled WITHIN the subject mask -- this
    avoids extreme background values squashing the subject's depth contrast.
    """
    try:
        from transformers import pipeline
    except Exception:
        return None
    try:
        pipe = pipeline(task="depth-estimation", model=model_id)  # may download model
        out = pipe(Image.fromarray(rgb))
        depth = np.asarray(out["depth"], dtype=np.float32)
        return depth  # raw relative depth; normalized within-subject by the caller
    except Exception as exc:
        sys.stderr.write(f"[depth] Depth-Anything available but failed ({exc}); degrading.\n")
        return None


# --------------------------------------------------------------------------- #
#  DEPTH -- tier 2: gradient/intensity pseudo-depth (guaranteed fallback)
# --------------------------------------------------------------------------- #
def depth_pseudo(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    Cheap proportional pseudo-depth, masked to the subject.

    Heuristic blend (each in 0..1), only where mask is set:
      - shading/luminance: lit (brighter) surfaces tend to face the camera -> nearer.
      - distance-from-mask-edge: the central bulk of the body is nearer than the
        thin extremities at the silhouette edge (e.g. a locomotive's chimney/buffers).
      - vertical bias: very mild -- lower pixels slightly nearer than the high
        background-ward top for a side elevation (e.g. a loco's footplate vs cab roof).
    This is NOT metric; it only gives a plausible relative ordering for the critic
    and for proportion sanity-checks. Background is 0 (black).
    """
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    m = mask > 0

    # 1) shading
    shade = cv2.GaussianBlur(gray, (0, 0), sigmaX=3.0)

    # 2) normalized distance transform inside the subject (center -> 1, edge -> 0)
    dist = cv2.distanceTransform((m * 255).astype(np.uint8), cv2.DIST_L2, 5)
    if dist.max() > 0:
        dist = dist / dist.max()

    # 3) mild vertical bias
    h = rgb.shape[0]
    yy = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    vbias = np.broadcast_to(yy, gray.shape)

    depth = 0.45 * dist + 0.40 * shade + 0.15 * vbias
    depth = np.where(m, depth, 0.0)

    # normalize within the subject only
    if m.any():
        vals = depth[m]
        lo, hi = float(vals.min()), float(vals.max())
        if hi > lo:
            depth = np.where(m, (depth - lo) / (hi - lo), 0.0)

    depth = cv2.GaussianBlur(depth, (0, 0), sigmaX=1.5)
    depth = np.where(m, depth, 0.0)
    return (depth * 255.0).astype(np.uint8)


# --------------------------------------------------------------------------- #
#  driver
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    src = Path(args.image)
    if not src.exists():
        sys.stderr.write(f"FATAL: input image not found: {src}\n")
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mask_path = out_dir / args.mask_name
    depth_path = out_dir / args.depth_name

    rgb = load_rgb(src)
    print(f"[input] {src}  ({rgb.shape[1]}x{rgb.shape[0]} RGB)")

    # ----- MASK ------------------------------------------------------------- #
    mask = None
    mask_tier = "grabcut(cv2-fallback)"
    if not args.force_fallback:
        if args.sam2_checkpoint:
            t = time.time()
            mask = mask_sam2(rgb, args.sam2_checkpoint, args.sam2_config)
            if mask is not None:
                mask_tier = f"sam2 ({time.time()-t:.1f}s)"
        if mask is None and not args.no_rembg:
            t = time.time()
            mask = mask_rembg(rgb)
            if mask is not None:
                mask_tier = f"rembg ({time.time()-t:.1f}s)"
    if mask is None:
        t = time.time()
        mask = mask_grabcut(rgb)
        mask_tier = f"grabcut(cv2-fallback) ({time.time()-t:.1f}s)"
    save_gray(mask, mask_path)
    cov = 100.0 * (mask > 0).mean()
    print(f"[mask ] tier={mask_tier}  -> {mask_path}  (subject covers {cov:.1f}% of frame)")

    # ----- DEPTH ------------------------------------------------------------ #
    depth = None
    depth_tier = "pseudo(gradient-fallback)"
    if not args.force_fallback:
        t = time.time()
        raw = depth_depthanything(rgb, args.depth_model)
        if raw is not None:
            depth_tier = f"depth-anything-v2 ({time.time()-t:.1f}s)"
            # Normalize WITHIN the subject (background extremes don't squash subject
            # contrast), then mask so background is black -- keeps depth and mask
            # spatially consistent with the silhouette.
            m = mask > 0
            d = np.zeros_like(raw, dtype=np.float32)
            if m.any():
                vals = raw[m]
                lo, hi = float(vals.min()), float(vals.max())
                if hi > lo:
                    d[m] = (raw[m] - lo) / (hi - lo)
            depth = (d * 255.0).astype(np.uint8)
    if depth is None:
        t = time.time()
        depth = depth_pseudo(rgb, mask)
        depth_tier = f"pseudo(gradient-fallback) ({time.time()-t:.1f}s)"
    save_gray(depth, depth_path)
    print(f"[depth] tier={depth_tier}  -> {depth_path}")

    print("[done ] mask + depth written.")
    print(f"  MASK_TIER={mask_tier.split(' ')[0]}")
    print(f"  DEPTH_TIER={depth_tier.split(' ')[0]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    default_out = here  # write mask.png / depth.png next to this script
    p = argparse.ArgumentParser(
        description="One-shot reference pre-processor: subject mask + proportional depth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("image", help="path to the reference photo of the subject to mask")
    p.add_argument("--out-dir", default=str(default_out),
                   help="output directory (default: this script's dir)")
    p.add_argument("--mask-name", default="mask.png")
    p.add_argument("--depth-name", default="depth.png")
    p.add_argument("--depth-model", default="depth-anything/Depth-Anything-V2-Small-hf",
                   help="HF model id for the depth-estimation pipeline")
    p.add_argument("--sam2-checkpoint", default=None,
                   help="path to a SAM 2 .pt checkpoint to enable the SAM2 mask tier")
    p.add_argument("--sam2-config", default=None,
                   help="SAM 2 config (e.g. configs/sam2.1/sam2.1_hiera_s.yaml)")
    p.add_argument("--no-rembg", action="store_true",
                   help="skip the rembg tier even if installed")
    p.add_argument("--force-fallback", action="store_true",
                   help="force the OpenCV/numpy floor (skip all model tiers)")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
