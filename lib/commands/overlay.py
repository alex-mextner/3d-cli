"""3d overlay — difference / 50% ghost / canny edge-overlay diagnostics (ImageMagick).

WHAT: produces three visual comparisons between a render and a reference photo:
  a difference map (red = mismatch), a 50% ghost blend (spot misalignment), and a
  canny edge overlay (red = reference edges, cyan = render edges) so geometry drift
  is visible before numbers are computed.

WHY: numeric scores (IoU, AE, SSIM) can tell you THAT something is wrong, but not WHAT
  is wrong or WHERE. The overlay diagnostics let you see misalignment, missing features,
  or geometry drift at a glance — essential for debugging a match loop or fit-camera run.

Examples:
  3d overlay render.png ref.jpg -o diff/
  3d overlay preview.png photo.jpg           # writes to preview's directory
  3d overlay render.png ref.jpg -o work/    # collect all three outputs in one place

ROADMAP §7 / §13.2: "match/fit-camera → silhouette IoU, overlay-diff (AE / blend / canny).
  The lowest-effort, highest-leverage primitive is a deterministic render → binary mask
  → {IoU, AE} + overlay (red=ref, cyan=render) for the critic."
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from cli.env import find_magick, require_magick
from cli.imaging import compare_ae, magick_identify
from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d overlay <render.png> <reference.{png,jpg}> [-o outdir]
  Difference / 50% ghost / canny edge-overlay diagnostics into outdir.
  Produces three files: overlay.png (difference map), ghost.png (50% blend),
  and edge_overlay.png (canny edges in red + cyan). Use this to visually spot
  misalignment, missing features, or geometry drift that numeric scores alone
  cannot show.

Options:
  -o, --out DIR         output directory (default: the render's directory)
                         Use this to collect all overlay files in one place.

Examples:
  3d overlay render.png ref.jpg -o work/
  3d overlay preview.png photo.jpg          # writes to preview's directory
  3d overlay render.png ref.jpg -o diff/  # collect all three outputs in diff/"""

CANNY = "0x1+10%+30%"  # radius x sigma + lower% + upper% hysteresis (report 7.3d)
GHOST_OPACITY = "50"


def _m(args: list[str], what: str) -> None:
    mgk = find_magick()
    assert mgk is not None
    r = subprocess.run([mgk, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GateFailure(f"overlay: {what} failed: {(r.stderr or r.stdout).strip()}", command="overlay")


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    if len(argv) < 2:
        print(USAGE)
        return 1
    require_magick("overlay")

    render, ref = argv[0], argv[1]
    out_dir = os.path.dirname(render) or "."
    rest = argv[2:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-o", "--out"):
            out_dir = rest[i + 1] if i + 1 < len(rest) else out_dir
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="overlay")

    for f in (render, ref):
        if not os.path.isfile(f):
            raise InputNotFound(f, command="overlay")
    os.makedirs(out_dir, exist_ok=True)

    overlay = os.path.join(out_dir, "overlay.png")
    ghost = os.path.join(out_dir, "ghost.png")
    edge = os.path.join(out_dir, "edge_overlay.png")

    geom = magick_identify(render, "%wx%h")
    print(f"[overlay] render={render} ref={ref} frame={geom}")

    tmp = tempfile.mkdtemp(prefix="3d_overlay.")
    try:
        ref_fit = os.path.join(tmp, "ref_fit.png")
        ren_rgb = os.path.join(tmp, "ren_rgb.png")
        _m([ref, "-alpha", "remove", "-alpha", "off", "-colorspace", "sRGB", "-resize", geom + "!", ref_fit], "ref-fit")
        _m([render, "-alpha", "remove", "-alpha", "off", "-colorspace", "sRGB", "-resize", geom + "!", ren_rgb], "render-fit")

        _m([ref_fit, ren_rgb, "-compose", "Difference", "-composite", "-auto-level", overlay], "difference")
        _m([ref_fit, ren_rgb, "-compose", "blend", "-define", f"compose:args={GHOST_OPACITY}", "-composite", ghost], "ghost")

        r_edge = os.path.join(tmp, "edges_ref.png")
        c_edge = os.path.join(tmp, "edges_render.png")
        r_red = os.path.join(tmp, "r_red.png")
        c_cyan = os.path.join(tmp, "c_cyan.png")
        _m([ref_fit, "-colorspace", "Gray", "-canny", CANNY, r_edge], "canny-ref")
        _m([ren_rgb, "-colorspace", "Gray", "-canny", CANNY, c_edge], "canny-render")
        _m([r_edge, "-colorspace", "sRGB", "-type", "TrueColor", "-fill", "red", "-opaque", "white", r_red], "edge-red")
        _m([c_edge, "-colorspace", "sRGB", "-type", "TrueColor", "-fill", "cyan", "-opaque", "white", c_cyan], "edge-cyan")
        _m([r_red, c_cyan, "-colorspace", "sRGB", "-compose", "Screen", "-composite", "-type", "TrueColor", edge], "edge-overlay")

        ae = compare_ae(ref_fit, ren_rgb, fuzz="5%")
        print("[overlay] wrote:")
        print(f"  (a) difference  : {overlay}")
        print(f"  (b) ghost 50%   : {ghost}")
        print(f"  (c) edge-on-edge: {edge}")
        print(f"[overlay] AE(fuzz5%) mismatched-pixels = {ae}  (diagnostic; 0 = identical)")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


COMMAND = Command(
    name="overlay",
    group="REFERENCE-MATCH PIPELINE",
    summary="difference / ghost / canny edge-overlay diagnostics",
    usage=USAGE,
    run=run,
)
