#!/usr/bin/env python3
"""fit_camera.py — silhouette-based CAMERA POSE FITTING (generic, project-agnostic).

Iteratively searches OpenSCAD camera parameters (azimuth, elevation, distance,
pan-x, pan-z) so the RENDERED silhouette best overlaps a REFERENCE photo's
silhouette (maximize IoU / minimize 1-IoU). This locks POSITION + SCALE +
PROPORTIONS to the reference, so later per-detail verification is done from the
same, saved viewpoint.

Optimizer: random search, then coordinate-descent refine (deterministic RNG seed
so a smoke test is reproducible).

The search bounds and refine steps are DERIVED FROM THE MODEL's bounding box
(temp STL export -> binary-STL vertex parse -> centroid + diagonal), so the same
tool fits a 20mm cube and a 300mm assembly without hardcoded scales. The look-at
center auto-estimates from that same bbox centroid unless --center is given.

Generalized from garage-band/lego-loco match/fit_camera.py: all loco-specific
defaults (center 125,28,30; distance 200..520; pan +/-120; final size 1037x675)
removed in favor of bbox-derived bounds and ref-image-derived aspect.

Run (via the 3d CLI):  3d fit-camera model.scad ref.jpg --out camera.json
Direct:  pyrun "numpy,pillow" lib/fit_camera.py --model m.scad --ref r.jpg
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
from typing import Any, Sequence

try:
    import numpy as np
    from PIL import Image, ImageDraw
except Exception as e:  # pragma: no cover - import guard
    sys.stderr.write(
        "fit-camera: missing python deps (numpy, pillow): %s\n"
        "  Bootstrap a venv:  python3 -m venv .venv && "
        ".venv/bin/pip install numpy pillow\n"
        "  or install uv so `3d` can resolve deps per-call.\n" % e
    )
    sys.exit(127)


def find_openscad() -> str:
    """Prefer the binary the bash wrapper exported; else search common paths."""
    env = os.environ.get("OPENSCAD")
    if env and (os.path.exists(env) or _on_path(env)):
        return env
    from shutil import which
    p = which("openscad")
    if p:
        return p
    for f in (
        "/opt/homebrew/bin/openscad",
        "/usr/local/bin/openscad",
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    ):
        if os.path.exists(f):
            return f
    sys.exit("fit-camera: openscad not found (install: brew install --cask openscad)")


def _on_path(name: str) -> bool:
    from shutil import which
    return which(name) is not None


OPENSCAD = find_openscad()
# Bound concurrent openscad renders so the parallel random-search batch can't fork
# hundreds of processes; one per CPU is a good default for CGAL-bound renders.
_RENDER_LIMIT = max(1, os.cpu_count() or 4)


def sh(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


async def _render_png_async(
    model: str, cam: Sequence[float], w: int, h: int, out: str,
    sem: asyncio.Semaphore,
) -> str | None:
    """Render one camera to its OWN PNG concurrently (bounded by `sem`)."""
    cam_arg = ",".join(f"{v:.3f}" for v in cam)
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            OPENSCAD, "--render", "-o", out, f"--camera={cam_arg}",
            f"--imgsize={w},{h}", model,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    return out if os.path.exists(out) else None


async def eval_losses_async(
    model: str, params: list[list[float]], center: list[float],
    w: int, h: int, refm: Any, tmp: str,
) -> list[float]:
    """Render a BATCH of camera params concurrently and return 1-IoU for each.

    This is the parallel core: the random-search samples (and each refine iteration's
    candidate set) are independent renders, so we gather them under a CPU-bound semaphore
    instead of rendering one-at-a-time."""
    sem = asyncio.Semaphore(_RENDER_LIMIT)

    async def one(i: int, p: list[float]) -> float:
        out = os.path.join(tmp, f"cand_{i}.png")
        png = await _render_png_async(model, cam_from_params(p, center), w, h, out, sem)
        if png is None:
            return 1.0
        a = np.asarray(Image.open(png).convert("RGB").resize((w, h)), dtype=np.int16)
        try:
            os.remove(png)
        except OSError:
            pass
        rm = array_to_mask(a)
        # Reject degenerate camera poses DURING search, not only after.
        # A silhouette that fills <8% or >92% of the frame is almost certainly a
        # zoomed-in sliver or a zoomed-out speck — penalise it to max loss so the
        # optimizer never locks onto degenerate poses in the first place.
        frac = float(rm.mean())
        if frac < 0.08 or frac > 0.92:
            return 1.0
        return 1.0 - iou(rm, refm)

    return list(await asyncio.gather(*(one(i, p) for i, p in enumerate(params))))


def eval_losses(
    model: str, params: list[list[float]], center: list[float],
    w: int, h: int, refm: Any, tmp: str,
) -> list[float]:
    """Sync entry to the async batch evaluator (correct path when asyncio is fine; the
    work is genuinely parallel openscad renders)."""
    return asyncio.run(eval_losses_async(model, params, center, w, h, refm, tmp))


# --------------------------------------------------------------------------- #
# Model bounding box: export a temp STL, parse vertices, return centroid+diag. #
# --------------------------------------------------------------------------- #
def model_bbox(
    model: str, tmp: str
) -> tuple[list[float] | None, float | None]:
    """Return (centroid[3], diag) of the model, or (None, None) on failure.

    Forces binary STL and parses it with struct/numpy directly (no trimesh dep).
    """
    stl = os.path.join(tmp, "bbox.stl")
    r = sh([OPENSCAD, "--export-format", "binstl", "-o", stl, model])
    if not os.path.exists(stl) or os.path.getsize(stl) < 84:
        sys.stderr.write("fit-camera: bbox STL export failed:\n%s\n" % (r.stderr or "")[:400])
        return None, None
    with open(stl, "rb") as f:
        f.read(80)  # header
        (ntri,) = struct.unpack("<I", f.read(4))
        if ntri == 0:
            return None, None
        data = f.read(ntri * 50)
    # each triangle: 12 floats (normal+3 verts) + 2-byte attr; verts are floats 3..11
    rec = np.frombuffer(data, dtype=np.uint8).reshape(ntri, 50)
    # bytes 0..47 of each 50-byte record are 12 little-endian float32 (normal + 3
    # verts); the trailing 2 bytes are the attribute count. view the 48 bytes as
    # 12 floats directly (do NOT reshape uint8 to (ntri,12) first — that's a
    # size mismatch: 48 bytes != 12 uint8 elements).
    floats = np.ascontiguousarray(rec[:, :48]).view("<f4")  # (ntri, 12)
    verts = floats[:, 3:12].reshape(-1, 3)
    lo = verts.min(axis=0)
    hi = verts.max(axis=0)
    centroid = (lo + hi) / 2.0
    diag = float(np.linalg.norm(hi - lo))
    return centroid.tolist(), diag


# --------------------------------------------------------------------------- #
# Masks                                                                        #
# --------------------------------------------------------------------------- #
def ref_mask(path: str, w: int, h: int, thresh: int) -> Any:
    im = Image.open(path).convert("L").resize((w, h))
    a = np.asarray(im, dtype=np.uint8)
    # subject = darker than a light background
    return (a < thresh).astype(np.uint8)


def render_to_array(model: str, cam: Sequence[float], w: int, h: int, tmp: str) -> Any:
    out = os.path.join(tmp, "r.png")
    if os.path.exists(out):
        os.remove(out)
    cam_arg = ",".join(f"{v:.3f}" for v in cam)
    sh([OPENSCAD, "--render", "-o", out, f"--camera={cam_arg}",
        f"--imgsize={w},{h}", model])
    if not os.path.exists(out):
        return None
    return np.asarray(Image.open(out).convert("RGB").resize((w, h)), dtype=np.int16)


def array_to_mask(a: Any) -> Any:
    # OpenSCAD default background ~ (255,255,229); subject = anything else.
    bg = np.array([255, 255, 229])
    diff = np.abs(a - bg).sum(axis=2)
    return (diff > 30).astype(np.uint8)


def render_mask(model: str, cam: Sequence[float], w: int, h: int, tmp: str) -> Any:
    a = render_to_array(model, cam, w, h, tmp)
    return None if a is None else array_to_mask(a)


def iou(m1: Any, m2: Any) -> float:
    inter = np.logical_and(m1, m2).sum()
    union = np.logical_or(m1, m2).sum()
    return float(inter) / float(union) if union else 0.0


def ssim_masks(m1: Any, m2: Any) -> float:
    """Global structural similarity between two binary masks (values in [-1, 1]).

    Uses the standard SSIM formula on global statistics. Windowed SSIM would require
    scipy/skimage; global SSIM is a reasonable reporting metric for silhouette quality.
    Unlike IoU (which only counts overlap pixels), SSIM also captures luminance and
    structural contrast — more stable on symmetric subjects where IoU degenerates.
    """
    a = m1.astype(np.float64)
    b = m2.astype(np.float64)
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    mu_a, mu_b = a.mean(), b.mean()
    sigma_a2, sigma_b2 = a.var(), b.var()
    sigma_ab = float(((a - mu_a) * (b - mu_b)).mean())
    num = (2 * mu_a * mu_b + C1) * (2 * sigma_ab + C2)
    den = (mu_a ** 2 + mu_b ** 2 + C1) * (sigma_a2 + sigma_b2 + C2)
    return float(num / den) if den else 0.0


def cam_from_params(p: Sequence[float], center: Sequence[float]) -> list[float]:
    az, el, dist, panx, panz = p
    cx, cy, cz = center[0] + panx, center[1], center[2] + panz
    ar, er = math.radians(az), math.radians(el)
    ex = cx + dist * math.cos(er) * math.cos(ar)
    ey = cy + dist * math.cos(er) * math.sin(ar)
    ez = cz + dist * math.sin(er)
    return [ex, ey, ez, cx, cy, cz]


# --------------------------------------------------------------------------- #
# Diagnostic overlays                                                          #
# --------------------------------------------------------------------------- #
def mask_pca(mask: Any) -> tuple[Any, Any, tuple[int, int, int, int]] | None:
    """Return (centroid_xy, principal_axis_xy_unit, bbox(x0,y0,x1,y1)) or None."""
    ys, xs = np.nonzero(mask)
    if xs.size < 2:
        return None
    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    c = pts.mean(axis=0)
    cov = np.cov((pts - c).T)
    w, v = np.linalg.eigh(cov)
    axis = v[:, int(np.argmax(w))]
    return c, axis, (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def draw_axes_overlay(img: Any, mask: Any, color: tuple[int, int, int]) -> None:
    """Draw PCA principal axis + bbox contour of `mask` onto PIL `img` in `color`."""
    info = mask_pca(mask)
    if info is None:
        return
    c, axis, bbox = info
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = bbox
    d.rectangle([x0, y0, x1, y1], outline=color, width=2)
    L = 0.6 * math.hypot(x1 - x0, y1 - y0)
    a = (c[0] - axis[0] * L, c[1] - axis[1] * L)
    b = (c[0] + axis[0] * L, c[1] + axis[1] * L)
    d.line([a, b], fill=color, width=2)
    r = 4
    d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=color, width=2)


def write_overlay(
    render_arr: Any, ref_path: str, refm: Any, out_path: str, draw_axes: bool
) -> None:
    """render(cyan) over reference(red) ghost; optionally PCA axes/bbox of each."""
    h, w = refm.shape
    rm = array_to_mask(render_arr) if render_arr is not None else np.zeros_like(refm)
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    canvas[..., 0] = (refm * 200).astype(np.uint8)            # ref -> red
    canvas[..., 1] = (rm * 200).astype(np.uint8)              # render -> green+blue (cyan)
    canvas[..., 2] = (rm * 200).astype(np.uint8)
    img = Image.fromarray(canvas, "RGB")
    if draw_axes:
        draw_axes_overlay(img, refm, (255, 80, 80))           # ref axes: light red
        draw_axes_overlay(img, rm, (80, 255, 255))            # render axes: light cyan
    img.save(out_path)


# --------------------------------------------------------------------------- #
def parse_size(
    s: str | None, default_wh: tuple[int, int], ref_aspect: float
) -> tuple[int, int]:
    """'WxH' -> (w,h). 'W' or 'Wx' -> derive H from ref aspect. '' -> default."""
    if not s:
        return default_wh
    s = s.lower()
    if "x" in s:
        a, b = s.split("x", 1)
        if a and b:
            return int(a), int(b)
        if a and not b:  # 'Wx' -> width given, height from aspect
            w = int(a)
            return w, max(1, round(w / ref_aspect))
    w = int(s)
    return w, max(1, round(w / ref_aspect))


def main() -> None:
    ap = argparse.ArgumentParser(prog="3d fit-camera", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="OpenSCAD model (.scad)")
    ap.add_argument("--ref", required=True, help="reference image (light background)")
    ap.add_argument("--out", default="camera.json", help="output JSON (default ./camera.json)")
    ap.add_argument("--center", default=None,
                    help="initial look-at 'x,y,z' (default: model bbox centroid, else origin)")
    ap.add_argument("--opt-size", default=None,
                    help="optimization render size 'WxH' (default ~300px wide @ ref aspect)")
    ap.add_argument("--final-size", default=None,
                    help="final fit render size 'WxH' (default: reference native resolution)")
    ap.add_argument("--thresh", type=int, default=150, help="ref subject darkness threshold (0..255)")
    ap.add_argument("--rand", type=int, default=80, help="random-search samples")
    ap.add_argument("--refine", type=int, default=40, help="coordinate-descent refine steps")
    ap.add_argument("--draw-axes", action="store_true",
                    help="overlay PCA principal axis + bbox contour of both silhouettes")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed (reproducible search)")
    ap.add_argument(
        "--el-range", default="-45,85",
        help="elevation search range 'lo,hi' in degrees (default -45,85). "
             "Restricts the optimizer to physically plausible camera angles: -45 allows "
             "low-angle 'looking-up' shots; 85 allows near-top-down. Use -89,89 to restore "
             "the full sphere. Negative elevation = camera below object centre, looking up.",
    )
    args = ap.parse_args()

    if not os.path.exists(args.model):
        sys.exit(f"fit-camera: model not found: {args.model}")
    if not os.path.exists(args.ref):
        sys.exit(f"fit-camera: reference not found: {args.ref}")

    tmp = tempfile.mkdtemp(prefix="fitcam_")

    # ---- reference aspect (drives render aspect so the ref mask isn't squished) --
    with Image.open(args.ref) as _r:
        rw, rh = _r.size
    ref_aspect = rw / rh if rh else 1.0

    ow, oh = parse_size(args.opt_size, (300, max(1, round(300 / ref_aspect))), ref_aspect)
    fw, fh = parse_size(args.final_size, (rw, rh), ref_aspect)

    # ---- model bbox -> centroid (center) + diagonal (scale) --------------------
    centroid, diag = model_bbox(args.model, tmp)
    if args.center is not None:
        center = [float(x) for x in args.center.split(",")]
    elif centroid is not None:
        center = centroid
    else:
        center = [0.0, 0.0, 0.0]
    if diag is None or diag <= 0:
        diag = 100.0  # fallback scale
        print("fit-camera: WARN bbox unavailable, using fallback scale 100mm", flush=True)
    print(f"model bbox: center={[round(x,2) for x in center]} diag={diag:.2f}mm", flush=True)
    print(f"opt-size={ow}x{oh}  final-size={fw}x{fh}  ref-aspect={ref_aspect:.3f}", flush=True)

    refm = ref_mask(args.ref, ow, oh, args.thresh)
    if refm.sum() == 0:
        print("fit-camera: WARN reference mask is empty (try a higher --thresh)", flush=True)

    def loss(p: list[float]) -> float:
        return eval_losses(args.model, [p], center, ow, oh, refm, tmp)[0]

    # ---- elevation bounds from --el-range (geometric constraint, Tier 1 idea #3) --
    try:
        el_lo_str, el_hi_str = args.el_range.split(",")
        el_lo = float(el_lo_str.strip())
        el_hi = float(el_hi_str.strip())
    except Exception:
        sys.exit("fit-camera: --el-range must be 'lo,hi' floats, e.g. -45,85")
    el_lo = max(-89.0, min(el_lo, 89.0))
    el_hi = max(-89.0, min(el_hi, 89.0))
    if el_lo >= el_hi:
        sys.exit("fit-camera: --el-range lo must be < hi")

    # ---- search space DERIVED FROM bbox diagonal (generic, any scale) ----------
    #  azimuth: full 360°; elevation: constrained by --el-range (avoids underground poses).
    #  distance: 1.2..6x diagonal; pan: +/- one diagonal; centered offsets.
    lo = [-180.0, el_lo, 1.2 * diag, -1.0 * diag, -1.0 * diag]
    hi = [180.0, el_hi, 6.0 * diag, 1.0 * diag, 1.0 * diag]
    rng = np.random.default_rng(args.seed)
    best_p: list[float] | None = None
    best_l = 2.0
    # PARALLEL random search: sample all candidates up front, render the whole batch
    # concurrently (CPU-bound semaphore), then reduce. Same RNG seed => same samples =>
    # reproducible, just faster than one-render-at-a-time.
    print(f"random search ({args.rand} samples, up to {_RENDER_LIMIT} parallel renders)...",
          flush=True)
    samples = [[rng.uniform(lo[k], hi[k]) for k in range(5)] for _ in range(args.rand)]
    losses = eval_losses(args.model, samples, center, ow, oh, refm, tmp)
    for i, (p, loss_val) in enumerate(zip(samples, losses)):
        if loss_val < best_l:
            best_l, best_p = loss_val, p
            print(f"  rand {i:3d}  IoU={1-loss_val:.3f}  {[round(x,1) for x in p]}", flush=True)
    if best_p is None:
        best_p = [(lo[k] + hi[k]) / 2 for k in range(5)]
        best_l = loss(best_p)

    # ---- coordinate-descent refine; steps scale with the diagonal --------------
    # Greedy per-coordinate descent: best_p is updated MID-PASS so a single pass can
    # improve several coordinates (preserves the original sequential algorithm exactly).
    # The only thing parallelized is the TWO independent directions (+step,-step) of the
    # CURRENT coordinate — a 2-way batch — so accuracy is identical, just the per-coord
    # pair renders concurrently.
    print("refine...", flush=True)
    step = [12.0, 6.0, 0.08 * diag, 0.15 * diag, 0.12 * diag]
    min_step = max(0.5, 0.005 * diag)
    for _it in range(args.refine):
        improved = False
        for k in range(5):
            cands: list[list[float]] = []
            for s in (step[k], -step[k]):
                q = list(best_p)
                q[k] = min(max(q[k] + s, lo[k]), hi[k])
                cands.append(q)
            cl = eval_losses(args.model, cands, center, ow, oh, refm, tmp)
            # same fixed order as the sequential version (+step before -step) so ties
            # resolve identically.
            for q, cand_loss in zip(cands, cl):
                if cand_loss < best_l - 1e-4:
                    best_l, best_p, improved = cand_loss, q, True
        if not improved:
            step = [x * 0.5 for x in step]
            if max(step) < min_step:
                break

    iou_best = 1 - best_l
    cam = cam_from_params(best_p, center)
    cam_arg = ",".join(f"{v:.3f}" for v in cam)
    print(f"\nBEST IoU={iou_best:.3f}  camera={cam_arg}", flush=True)

    # ---- final full-res fit render + overlay (render vs reference) -------------
    out_base = os.path.splitext(args.out)[0]
    fit_png = out_base + "_fit.png"
    overlay_png = out_base + "_overlay.png"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    sh([OPENSCAD, "--render", "-o", fit_png, f"--camera={cam_arg}",
        f"--imgsize={fw},{fh}", args.model])

    # overlay at optimization resolution so render & ref masks line up exactly.
    render_arr = render_to_array(args.model, cam, ow, oh, tmp)
    write_overlay(render_arr, args.ref, refm, overlay_png, args.draw_axes)

    # SSIM between final render mask and reference mask (Tier 1 idea #4).
    # More stable than IoU on symmetric subjects where silhouette edges are ambiguous.
    if render_arr is not None:
        rm_final = array_to_mask(render_arr)
        ssim_val = ssim_masks(rm_final, refm)
    else:
        ssim_val = 0.0

    data = {
        "camera_arg": cam_arg,
        "camera": [round(v, 3) for v in cam],
        "params": dict(zip(["azim", "elev", "dist", "panx", "panz"],
                           [round(x, 3) for x in best_p])),
        "center": [round(x, 3) for x in center],
        "iou": round(iou_best, 4),
        "ssim": round(ssim_val, 4),
        "model_diag": round(diag, 3),
        "opt_size": f"{ow}x{oh}",
        "final_size": f"{fw}x{fh}",
        "ref": args.ref,
        "fit_render": fit_png,
        "overlay": overlay_png,
    }
    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"saved {args.out}", flush=True)
    print(f"  fit render: {fit_png}", flush=True)
    print(f"  overlay:    {overlay_png}", flush=True)
    print(f"IoU={iou_best:.4f}  SSIM={ssim_val:.4f}", flush=True)
    print(f"CAMERA_ARG={cam_arg}", flush=True)


if __name__ == "__main__":
    main()
