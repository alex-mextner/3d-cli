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
import argparse
import json
import math
import os
import struct
import subprocess
import sys
import tempfile

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


def find_openscad():
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


def _on_path(name):
    from shutil import which
    return which(name) is not None


OPENSCAD = find_openscad()


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# Model bounding box: export a temp STL, parse vertices, return centroid+diag. #
# --------------------------------------------------------------------------- #
def model_bbox(model, tmp):
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
def ref_mask(path, w, h, thresh):
    im = Image.open(path).convert("L").resize((w, h))
    a = np.asarray(im, dtype=np.uint8)
    # subject = darker than a light background
    return (a < thresh).astype(np.uint8)


def render_to_array(model, cam, w, h, tmp):
    out = os.path.join(tmp, "r.png")
    if os.path.exists(out):
        os.remove(out)
    cam_arg = ",".join(f"{v:.3f}" for v in cam)
    sh([OPENSCAD, "--render", "-o", out, f"--camera={cam_arg}",
        f"--imgsize={w},{h}", model])
    if not os.path.exists(out):
        return None
    return np.asarray(Image.open(out).convert("RGB").resize((w, h)), dtype=np.int16)


def array_to_mask(a):
    # OpenSCAD default background ~ (255,255,229); subject = anything else.
    bg = np.array([255, 255, 229])
    diff = np.abs(a - bg).sum(axis=2)
    return (diff > 30).astype(np.uint8)


def render_mask(model, cam, w, h, tmp):
    a = render_to_array(model, cam, w, h, tmp)
    return None if a is None else array_to_mask(a)


def iou(m1, m2):
    inter = np.logical_and(m1, m2).sum()
    union = np.logical_or(m1, m2).sum()
    return float(inter) / float(union) if union else 0.0


def cam_from_params(p, center):
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
def mask_pca(mask):
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


def draw_axes_overlay(img, mask, color):
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


def write_overlay(render_arr, ref_path, refm, out_path, draw_axes):
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
def parse_size(s, default_wh, ref_aspect):
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


def main():
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

    def loss(p):
        m = render_mask(args.model, cam_from_params(p, center), ow, oh, tmp)
        if m is None:
            return 1.0
        return 1.0 - iou(m, refm)

    # ---- search space DERIVED FROM bbox diagonal (generic, any scale) ----------
    #  azimuth/elevation: full generic view sphere (no project view-prior).
    #  distance: 1.2..6x diagonal; pan: +/- one diagonal; centered offsets.
    lo = [-180.0, -89.0, 1.2 * diag, -1.0 * diag, -1.0 * diag]
    hi = [180.0, 89.0, 6.0 * diag, 1.0 * diag, 1.0 * diag]
    rng = np.random.default_rng(args.seed)
    best_p, best_l = None, 2.0
    print("random search...", flush=True)
    for i in range(args.rand):
        p = [rng.uniform(lo[k], hi[k]) for k in range(5)]
        l = loss(p)
        if l < best_l:
            best_l, best_p = l, p
            print(f"  rand {i:3d}  IoU={1-l:.3f}  {[round(x,1) for x in p]}", flush=True)
    if best_p is None:
        best_p = [(lo[k] + hi[k]) / 2 for k in range(5)]
        best_l = loss(best_p)

    # ---- coordinate-descent refine; steps scale with the diagonal --------------
    print("refine...", flush=True)
    step = [12.0, 6.0, 0.08 * diag, 0.15 * diag, 0.12 * diag]
    min_step = max(0.5, 0.005 * diag)
    for _it in range(args.refine):
        improved = False
        for k in range(5):
            for s in (step[k], -step[k]):
                q = list(best_p)
                q[k] += s
                q[k] = min(max(q[k], lo[k]), hi[k])
                l = loss(q)
                if l < best_l - 1e-4:
                    best_l, best_p, improved = l, q, True
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

    data = {
        "camera_arg": cam_arg,
        "camera": [round(v, 3) for v in cam],
        "params": dict(zip(["azim", "elev", "dist", "panx", "panz"],
                           [round(x, 3) for x in best_p])),
        "center": [round(x, 3) for x in center],
        "iou": round(iou_best, 4),
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
    print(f"IoU={iou_best:.4f}", flush=True)
    print(f"CAMERA_ARG={cam_arg}", flush=True)


if __name__ == "__main__":
    main()
