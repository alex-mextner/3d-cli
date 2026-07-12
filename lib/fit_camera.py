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

from fit_camera_math import cam_from_params, fit_status_from_spatial_metrics, stratified_samples

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
MAX_PROOF_ANCHOR_SAMPLES = 720


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
    w: int, h: int, refm: Any, tmp: str, objective: str,
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
            return 9.0
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
            return 9.0
        return objective_loss(rm, refm, objective)

    return list(await asyncio.gather(*(one(i, p) for i, p in enumerate(params))))


def eval_losses(
    model: str, params: list[list[float]], center: list[float],
    w: int, h: int, refm: Any, tmp: str, objective: str,
) -> list[float]:
    """Sync entry to the async batch evaluator (correct path when asyncio is fine; the
    work is genuinely parallel openscad renders)."""
    return asyncio.run(eval_losses_async(model, params, center, w, h, refm, tmp, objective))


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
def ref_mask(path: str, w: int, h: int, thresh: int, polarity: str = "dark") -> Any:
    im = Image.open(path).convert("L").resize((w, h))
    a = np.asarray(im, dtype=np.uint8)
    dark = a < thresh
    if polarity == "light":
        return np.logical_not(dark).astype(np.uint8)
    return dark.astype(np.uint8)


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


def image_looks_like_binary_mask(path: str) -> bool:
    """Return true for proof references that are probably just copied masks."""
    try:
        arr = np.asarray(Image.open(path).convert("L"), dtype=np.uint8)
    except Exception:
        return False
    unique = np.unique(arr)
    if unique.size <= 4:
        return True
    extreme_fraction = float(((arr <= 8) | (arr >= 247)).mean())
    mid_fraction = float(((arr > 16) & (arr < 239)).mean())
    return extreme_fraction > 0.995 and mid_fraction < 0.005


def render_mask(model: str, cam: Sequence[float], w: int, h: int, tmp: str) -> Any:
    a = render_to_array(model, cam, w, h, tmp)
    return None if a is None else array_to_mask(a)


def iou(m1: Any, m2: Any) -> float:
    inter = np.logical_and(m1, m2).sum()
    union = np.logical_or(m1, m2).sum()
    return float(inter) / float(union) if union else 0.0


def objective_loss(render_mask_arr: Any, refm: Any, objective: str) -> float:
    if objective == "area-iou":
        return 1.0 - iou(render_mask_arr, refm)
    from spatial_fit_metrics import spatial_fit_metrics

    metrics = spatial_fit_metrics(render_mask_arr, refm)
    diag = max(1.0, math.hypot(*render_mask_arr.shape))
    contour_loss = 1.0 - metrics.edge_f1_at_4
    contour_loss += 0.5 * min(1.0, metrics.edge_chamfer_px / diag)
    contour_loss += 0.5 * min(1.0, metrics.boundary_sdf_loss_px / diag)
    contour_loss += 0.25 * min(1.0, metrics.hausdorff_p95_px / diag)
    contour_loss += 0.15 * min(1.0, abs(metrics.coverage_ratio - 1.0))
    if metrics.render_touches_border and not metrics.reference_touches_border:
        contour_loss += 0.25
    return float(contour_loss)


def require_spatial_metrics() -> None:
    try:
        import spatial_fit_metrics  # noqa: F401
    except Exception as exc:
        sys.stderr.write(
            "fit-camera: missing python deps for contour/spatial report (scipy): %s\n"
            "  Bootstrap the worktree: uv sync --extra dev --extra preprocess\n"
            "  or install scipy in the active Python environment.\n" % exc
        )
        sys.exit(127)


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


def _mask_bbox_xywh(mask: Any) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def _range_values(lower: float, upper: float, step: float) -> list[float]:
    values: list[float] = []
    value = lower
    while value <= upper + 1e-9:
        values.append(round(value, 6))
        value += step
    if not values or not math.isclose(values[-1], upper, abs_tol=1e-6):
        values.append(upper)
    return values


def anchor_camera_samples(
    lo: Sequence[float],
    hi: Sequence[float],
    diag: float,
    refm: Any,
    render_size: tuple[int, int],
    *,
    proof: bool,
) -> list[list[float]]:
    """Deterministic coarse camera anchors before random search."""
    w, h = render_size
    bbox = _mask_bbox_xywh(refm)
    if bbox is None:
        ref_cx, ref_cy = w / 2.0, h / 2.0
    else:
        x, y, bw, bh = bbox
        ref_cx, ref_cy = x + bw / 2.0, y + bh / 2.0
    panx_hint = ((ref_cx / max(1.0, w)) - 0.5) * diag
    panz_hint = (0.5 - (ref_cy / max(1.0, h))) * diag
    az_values = _range_values(float(lo[0]), float(hi[0]), 30.0)
    el_seed = (-15.0, 0.0, 15.0, 30.0, 45.0, 60.0) if proof else (-10.0, 20.0, 40.0)
    el_values = list(dict.fromkeys(max(lo[1], min(hi[1], v)) for v in el_seed))
    dist_mults = (0.75, 1.0, 1.25, 1.6, 2.2, 3.2, 4.6) if proof else (1.6, 2.7, 4.2)
    dist_values = list(dict.fromkeys(max(lo[2], min(hi[2], diag * m)) for m in dist_mults))
    pan_candidates = [
        (0.0, 0.0, 0.0),
        (panx_hint, 0.0, panz_hint),
        (panx_hint - 0.18 * diag, 0.0, panz_hint),
        (panx_hint + 0.18 * diag, 0.0, panz_hint),
        (panx_hint, 0.0, panz_hint - 0.14 * diag),
        (panx_hint, 0.0, panz_hint + 0.14 * diag),
    ]
    out: list[list[float]] = []
    seen: set[tuple[float, ...]] = set()
    for dist in dist_values:
        for el in el_values:
            for az in az_values:
                for panx, pany, panz in pan_candidates:
                    sample = [
                        max(lo[0], min(hi[0], az)),
                        max(lo[1], min(hi[1], el)),
                        dist,
                        max(lo[3], min(hi[3], panx)),
                    ]
                    if len(lo) == 6:
                        sample.extend([
                            max(lo[4], min(hi[4], pany)),
                            max(lo[5], min(hi[5], panz)),
                        ])
                    else:
                        sample.append(max(lo[4], min(hi[4], panz)))
                    key = tuple(round(x, 3) for x in sample)
                    if key not in seen:
                        seen.add(key)
                        out.append(sample)
    return out


def viewbank_anchor_samples(
    lo: Sequence[float],
    hi: Sequence[float],
    diag: float,
    refm: Any,
    render_size: tuple[int, int],
    *,
    az_step: int = 30,
    elevations: Sequence[float] = (0.0, 10.0, 20.0, 30.0),
    dist_mult: float = 2.0,
) -> list[list[float]]:
    """Coarse azimuth/elevation VIEW-BANK seeds for the random search (opt-in).

    Ported from tools/spatial_fit_experiment.py::evaluate_view_bank_retrieval: sweep a
    coarse az/el grid at a fixed bbox-derived framing and let the normal loss ranking
    surface the best pose. The harness proved this recovers the right azimuth basin but
    only ever lived in the experiment; wiring it here as `--seed-from-viewbank` stops the
    free search from landing in a wrong basin on near-symmetric subjects. The look-at pan
    is seeded from the reference-mask centroid, matching anchor_camera_samples."""
    if not 5 <= len(lo) <= 6:
        raise ValueError(
            f"viewbank_anchor_samples expects a 5- or 6-parameter search space, got {len(lo)}")
    w, h = render_size
    bbox = _mask_bbox_xywh(refm)
    if bbox is None:
        ref_cx, ref_cy = w / 2.0, h / 2.0
    else:
        x, y, bw, bh = bbox
        ref_cx, ref_cy = x + bw / 2.0, y + bh / 2.0
    panx_hint = ((ref_cx / max(1.0, w)) - 0.5) * diag
    panz_hint = (0.5 - (ref_cy / max(1.0, h))) * diag
    dist = max(lo[2], min(hi[2], diag * dist_mult))
    out: list[list[float]] = []
    for az in range(-180, 180, az_step):
        for el in elevations:
            sample = [
                max(lo[0], min(hi[0], float(az))),
                max(lo[1], min(hi[1], float(el))),
                dist,
                max(lo[3], min(hi[3], panx_hint)),
            ]
            if len(lo) == 6:
                sample.extend([max(lo[4], min(hi[4], 0.0)), max(lo[5], min(hi[5], panz_hint))])
            else:
                sample.append(max(lo[4], min(hi[4], panz_hint)))
            out.append(sample)
    return out


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


def write_edge_overlay(render_mask_arr: Any, refm: Any, out_path: str) -> None:
    from spatial_fit_metrics import binary_contour

    render_edge = binary_contour(render_mask_arr)
    ref_edge = binary_contour(refm)
    canvas = np.zeros((*refm.shape, 3), dtype=np.uint8)
    canvas[..., 0] = (ref_edge * 255).astype(np.uint8)
    canvas[..., 1] = (render_edge * 255).astype(np.uint8)
    canvas[..., 2] = (render_edge * 255).astype(np.uint8)
    Image.fromarray(canvas, "RGB").save(out_path)


def write_spatial_panel(
    ref_path: str,
    refm: Any,
    fit_png: str,
    edge_overlay_png: str,
    panel_png: str,
) -> None:
    h, w = refm.shape
    ref = Image.open(ref_path).convert("RGB").resize((w, h))
    mask = Image.fromarray((refm * 255).astype(np.uint8), "L").convert("RGB")
    fit = Image.open(fit_png).convert("RGB").resize((w, h))
    edge = Image.open(edge_overlay_png).convert("RGB").resize((w, h))
    header_h = 24
    panel = Image.new("RGB", (w * 4, h + header_h), "white")
    draw = ImageDraw.Draw(panel)
    labels = ("reference", "mask", "fitted render", "contour overlay")
    for idx, (img, label) in enumerate(zip((ref, mask, fit, edge), labels)):
        x = idx * w
        draw.text((x + 8, 6), label, fill=(20, 20, 20))
        panel.paste(img, (x, header_h))
    panel.save(panel_png)


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
    ap.add_argument(
        "--mask-polarity",
        choices=("dark", "light"),
        default="dark",
        help="which reference pixels are subject: dark raw photo (default) or light binary mask",
    )
    ap.add_argument(
        "--backplate",
        default=None,
        help="original/reference photo to show in spatial proof panels when --ref is a mask",
    )
    ap.add_argument("--rand", type=int, default=80, help="random-search samples")
    ap.add_argument("--refine", type=int, default=40, help="coordinate-descent refine steps")
    ap.add_argument(
        "--search-mode",
        choices=("normal", "proof"),
        default="normal",
        help="normal random search or slower proof search with broader near-camera distance",
    )
    ap.add_argument("--draw-axes", action="store_true",
                    help="overlay PCA principal axis + bbox contour of both silhouettes")
    ap.add_argument(
        "--seed-from-viewbank", action="store_true",
        help="prepend a coarse az/el view-bank pose grid to the search (opt-in; helps "
             "the search avoid wrong azimuth basins on near-symmetric subjects). OFF by default.",
    )
    ap.add_argument(
        "--spatial-report",
        default=None,
        help="write contour-first metrics and proof panel into this directory",
    )
    ap.add_argument(
        "--trace",
        default=None,
        help="write best-candidate trace JSONL for candidate-evolution demos",
    )
    ap.add_argument(
        "--objective",
        choices=("area-iou", "contour"),
        default="area-iou",
        help="search objective: area-iou (default) or contour edge F1/SDF/Chamfer/p95",
    )
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
    if args.backplate and not os.path.exists(args.backplate):
        sys.exit(f"fit-camera: backplate not found: {args.backplate}")
    if args.search_mode == "proof":
        args.objective = "contour"
    if args.objective == "contour" or args.spatial_report:
        require_spatial_metrics()

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

    refm = ref_mask(args.ref, ow, oh, args.thresh, args.mask_polarity)
    if refm.sum() == 0:
        print("fit-camera: WARN reference mask is empty (try a higher --thresh)", flush=True)

    def loss(p: list[float]) -> float:
        return eval_losses(args.model, [p], center, ow, oh, refm, tmp, args.objective)[0]

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
    dist_min = 0.65 * diag if args.search_mode == "proof" else 1.2 * diag
    if args.search_mode == "proof":
        lo = [-180.0, el_lo, dist_min, -1.0 * diag, -1.0 * diag, -1.0 * diag]
        hi = [180.0, el_hi, 6.0 * diag, 1.0 * diag, 1.0 * diag, 1.0 * diag]
    else:
        lo = [-180.0, el_lo, dist_min, -1.0 * diag, -1.0 * diag]
        hi = [180.0, el_hi, 6.0 * diag, 1.0 * diag, 1.0 * diag]
    rng = np.random.default_rng(args.seed)
    best_p: list[float] | None = None
    best_l = float("inf")
    trace_rows: list[dict[str, object]] = []
    # PARALLEL random search: sample all candidates up front, render the whole batch
    # concurrently (CPU-bound semaphore), then reduce. Same RNG seed => same samples =>
    # reproducible, just faster than one-render-at-a-time.
    effective_rand = max(args.rand, 240) if args.search_mode == "proof" else args.rand
    print(f"random search ({effective_rand} samples, mode={args.search_mode}, up to {_RENDER_LIMIT} parallel renders)...",
          flush=True)
    n_params = len(lo)
    anchor_pool = (
        anchor_camera_samples(lo, hi, diag, refm, (ow, oh), proof=True)
        if args.search_mode == "proof"
        else []
    )
    max_anchor_budget = effective_rand
    if args.search_mode == "proof":
        max_anchor_budget = max(1, int(effective_rand * 0.75))
    anchor_budget = min(MAX_PROOF_ANCHOR_SAMPLES, len(anchor_pool), max_anchor_budget)
    anchors = stratified_samples(anchor_pool, anchor_budget)
    random_count = max(0, effective_rand - len(anchors))
    random_samples = [[rng.uniform(lo[k], hi[k]) for k in range(n_params)] for _ in range(random_count)]
    viewbank_seeds = (
        viewbank_anchor_samples(lo, hi, diag, refm, (ow, oh))
        if args.seed_from_viewbank
        else []
    )
    samples = viewbank_seeds + anchors + random_samples
    if viewbank_seeds:
        print(f"  view-bank seeds={len(viewbank_seeds)} "
              "(tools/spatial_fit_experiment.evaluate_view_bank_retrieval)", flush=True)
    print(f"  anchors={len(anchors)} random={len(random_samples)}", flush=True)
    losses = eval_losses(args.model, samples, center, ow, oh, refm, tmp, args.objective)
    for i, (p, loss_val) in enumerate(zip(samples, losses)):
        if loss_val < best_l:
            best_l, best_p = loss_val, p
            if args.objective == "area-iou":
                score = max(0.0, 1.0 - loss_val)
            elif args.trace:
                score_mask = render_mask(args.model, cam_from_params(p, center), ow, oh, tmp)
                score = iou(score_mask, refm) if score_mask is not None else 0.0
            else:
                score = None
            if args.trace:
                trace_rows.append(
                    {
                        "phase": "random",
                        "iteration": i,
                        "loss": round(loss_val, 6),
                        "iou": round(score or 0.0, 6),
                        "params": [round(float(x), 6) for x in p],
                    }
                )
            if score is None:
                print(f"  rand {i:3d}  loss={loss_val:.3f}  {[round(x,1) for x in p]}", flush=True)
            else:
                print(f"  rand {i:3d}  loss={loss_val:.3f}  IoU={score:.3f}  {[round(x,1) for x in p]}", flush=True)
    if best_p is None:
        best_p = [(lo[k] + hi[k]) / 2 for k in range(n_params)]
        best_l = loss(best_p)

    # ---- coordinate-descent refine; steps scale with the diagonal --------------
    # Greedy per-coordinate descent: best_p is updated MID-PASS so a single pass can
    # improve several coordinates (preserves the original sequential algorithm exactly).
    # The only thing parallelized is the TWO independent directions (+step,-step) of the
    # CURRENT coordinate — a 2-way batch — so accuracy is identical, just the per-coord
    # pair renders concurrently.
    print("refine...", flush=True)
    step = (
        [12.0, 6.0, 0.08 * diag, 0.15 * diag, 0.15 * diag, 0.12 * diag]
        if n_params == 6
        else [12.0, 6.0, 0.08 * diag, 0.15 * diag, 0.12 * diag]
    )
    min_step = max(0.5, 0.005 * diag)
    for _it in range(args.refine):
        improved = False
        for k in range(n_params):
            cands: list[list[float]] = []
            for s in (step[k], -step[k]):
                q = list(best_p)
                q[k] = min(max(q[k] + s, lo[k]), hi[k])
                cands.append(q)
            cl = eval_losses(args.model, cands, center, ow, oh, refm, tmp, args.objective)
            # same fixed order as the sequential version (+step before -step) so ties
            # resolve identically.
            for q, cand_loss in zip(cands, cl):
                if cand_loss < best_l - 1e-4:
                    best_l, best_p, improved = cand_loss, q, True
                    if args.trace:
                        score_mask = render_mask(args.model, cam_from_params(q, center), ow, oh, tmp)
                        score = iou(score_mask, refm) if score_mask is not None else 0.0
                        trace_rows.append(
                            {
                                "phase": "refine",
                                "iteration": _it,
                                "coord": k,
                                "loss": round(cand_loss, 6),
                                "iou": round(score, 6),
                                "params": [round(float(x), 6) for x in q],
                            }
                        )
        if not improved:
            step = [x * 0.5 for x in step]
            if max(step) < min_step:
                break

    cam = cam_from_params(best_p, center)
    best_mask = render_mask(args.model, cam, ow, oh, tmp)
    iou_best = iou(best_mask, refm) if best_mask is not None else 0.0
    cam_arg = ",".join(f"{v:.3f}" for v in cam)
    print(f"\nBEST IoU={iou_best:.3f}  camera={cam_arg}", flush=True)

    # ---- final full-res fit render + overlay (render vs reference) -------------
    out_base = os.path.splitext(args.out)[0]
    fit_png = out_base + "_fit.png"
    overlay_png = out_base + "_overlay.png"
    if args.spatial_report == "":
        args.spatial_report = None
    if (args.search_mode == "proof" or args.objective == "contour") and args.spatial_report is None:
        args.spatial_report = out_base + "_spatial"
    spatial_metrics: dict[str, float | bool | str | None] = {}
    spatial_panel_png: str | None = None
    edge_overlay_png: str | None = None
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    sh([OPENSCAD, "--render", "-o", fit_png, f"--camera={cam_arg}",
        f"--imgsize={fw},{fh}", args.model])

    # overlay at optimization resolution so render & ref masks line up exactly.
    render_arr = render_to_array(args.model, cam, ow, oh, tmp)
    write_overlay(render_arr, args.ref, refm, overlay_png, args.draw_axes)

    # SSIM between final render mask and reference mask (Tier 1 idea #4).
    # More stable than IoU on symmetric subjects where silhouette edges are ambiguous.
    proof_status = "diagnostic-only"
    proof_warnings = ["contour spatial metrics were not requested; run with --objective contour --spatial-report DIR"]
    if render_arr is not None:
        rm_final = array_to_mask(render_arr)
        ssim_val = ssim_masks(rm_final, refm)
        if args.objective == "contour" or args.spatial_report:
            from spatial_fit_metrics import spatial_fit_metrics

            spatial_metrics = spatial_fit_metrics(rm_final, refm).as_dict()
            proof_status, proof_warnings = fit_status_from_spatial_metrics(spatial_metrics)
            proof_reference_is_mask = (
                args.mask_polarity == "light"
                and (
                    not args.backplate
                    or os.path.abspath(args.backplate) == os.path.abspath(args.ref)
                    or image_looks_like_binary_mask(args.backplate)
                )
            )
            if proof_reference_is_mask:
                mask_proof_warning = (
                    "mask-polarity light requires a distinct non-mask --backplate/--proof-reference "
                    "original image before fit_status can be ok"
                )
                if proof_status == "ok":
                    proof_status = "warning"
                if mask_proof_warning not in proof_warnings:
                    proof_warnings.append(mask_proof_warning)
            if args.spatial_report:
                os.makedirs(args.spatial_report, exist_ok=True)
                edge_overlay_png = os.path.join(args.spatial_report, "edge_overlay.png")
                spatial_panel_png = os.path.join(args.spatial_report, "proof_panel.png")
                write_edge_overlay(rm_final, refm, edge_overlay_png)
                write_spatial_panel(args.backplate or args.ref, refm, fit_png, edge_overlay_png, spatial_panel_png)
                metrics_path = os.path.join(args.spatial_report, "spatial_metrics.json")
                with open(metrics_path, "w") as f:
                    json.dump(spatial_metrics, f, indent=2)
    else:
        ssim_val = 0.0
        proof_status = "failed"
        proof_warnings = ["final render failed; no fitted render mask was available"]

    if args.trace:
        os.makedirs(os.path.dirname(os.path.abspath(args.trace)) or ".", exist_ok=True)
        with open(args.trace, "w") as f:
            for row in trace_rows:
                f.write(json.dumps(row) + "\n")

    data = {
        "camera_arg": cam_arg,
        "camera": [round(v, 3) for v in cam],
        "params": dict(zip(
            ["azim", "elev", "dist", "panx", "pany", "panz"] if len(best_p) == 6
            else ["azim", "elev", "dist", "panx", "panz"],
            [round(x, 3) for x in best_p],
        )),
        "center": [round(x, 3) for x in center],
        "iou": round(iou_best, 4),
        "objective": args.objective,
        "objective_loss": round(best_l, 6),
        "fit_status": proof_status,
        "diagnostic_only": proof_status != "ok",
        "warnings": proof_warnings,
        "ssim": round(ssim_val, 4),
        "model_diag": round(diag, 3),
        "opt_size": f"{ow}x{oh}",
        "final_size": f"{fw}x{fh}",
        "ref": args.ref,
        "backplate": args.backplate,
        "proof_reference": args.backplate or args.ref,
        "mask_polarity": args.mask_polarity,
        "fit_render": fit_png,
        "overlay": overlay_png,
        "spatial_metrics": spatial_metrics,
        "spatial_panel": spatial_panel_png,
        "proof_panel": spatial_panel_png,
        "edge_overlay": edge_overlay_png,
        "trace": args.trace,
    }
    for warning in proof_warnings:
        print(f"fit-camera: WARN {warning}", flush=True)
    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"saved {args.out}", flush=True)
    print(f"  fit render: {fit_png}", flush=True)
    print(f"  overlay:    {overlay_png}", flush=True)
    if spatial_panel_png:
        print(f"  proof:      {spatial_panel_png}", flush=True)
    print(f"STATUS={proof_status}", flush=True)
    print(f"IoU={iou_best:.4f}  SSIM={ssim_val:.4f}", flush=True)
    print(f"CAMERA_ARG={cam_arg}", flush=True)


if __name__ == "__main__":
    main()
