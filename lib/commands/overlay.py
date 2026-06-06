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

import debug_overlay
from cli.env import require_magick
from cli.imaging import compare_ae, magick_identify
from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d overlay <render.png> <reference.{png,jpg}> [-o outdir] [options]
  Structured debug overlays and advisory diagnostics for rendered/model artifacts.

Artifacts:
  overlay.png       auto-leveled difference image
  ghost.png         50% reference/render blend
  edge_overlay.png  red reference edges over cyan render edges

Options:
  -o, --out DIR       output dir (default: render image directory)
  --mode MODE         difference|ghost|edge|all (repeatable; comma lists accepted)
  --json              print machine-readable JSON summary
  --advice-only       print the planned artifacts without running ImageMagick

Examples:
  3d overlay render.png ref.jpg -o work/
  3d overlay preview.png photo.jpg          # writes to preview's directory
  3d overlay render.png ref.jpg --mode edge
  3d overlay render.png ref.jpg --mode edge --json"""

CANNY = "0x1+10%+30%"  # radius x sigma + lower% + upper% hysteresis (report 7.3d)
GHOST_OPACITY = "50"


def _m(magick: str, args: list[str], what: str) -> None:
    r = subprocess.run([magick, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GateFailure(f"overlay: {what} failed: {(r.stderr or r.stdout).strip()}", command="overlay")


def _frame_pixels(geom: str) -> int:
    try:
        width, height = geom.lower().split("x", 1)
        return int(width) * int(height)
    except ValueError:
        raise GateFailure(f"overlay: could not parse frame geometry: {geom}", command="overlay") from None


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    if len(argv) < 2:
        print(USAGE)
        return 1

    render, ref = argv[0], argv[1]
    out_dir = os.path.dirname(render) or "."
    modes: list[str] = []
    json_out = False
    advice_only = False
    rest = argv[2:]
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="overlay")
            out_dir = rest[i + 1]
            i += 2
        elif a == "--mode":
            if i + 1 >= n:
                raise UsageError("option --mode needs a value", command="overlay")
            modes.append(rest[i + 1])
            i += 2
        elif a == "--json":
            json_out = True
            i += 1
        elif a == "--advice-only":
            advice_only = True
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="overlay")

    for f in (render, ref):
        if not os.path.isfile(f):
            raise InputNotFound(f, command="overlay")

    plan = debug_overlay.build_plan(render, ref, out_dir=out_dir, modes=modes)
    if advice_only:
        print(debug_overlay.plan_to_json(plan) if json_out else debug_overlay.format_plan(plan))
        return 0

    magick = require_magick("overlay")
    os.makedirs(plan.out_dir, exist_ok=True)

    artifact_paths = {artifact.kind: artifact.path for artifact in plan.artifacts}

    geom = magick_identify(render, "%wx%h")
    if not json_out:
        print(f"[overlay] render={render} ref={ref} frame={geom}")

    tmp = tempfile.mkdtemp(prefix="3d_overlay.")
    try:
        ref_fit = os.path.join(tmp, "ref_fit.png")
        ren_rgb = os.path.join(tmp, "ren_rgb.png")
        _m(
            magick,
            [ref, "-alpha", "remove", "-alpha", "off", "-colorspace", "sRGB", "-resize", geom + "!", ref_fit],
            "ref-fit",
        )
        _m(
            magick,
            [render, "-alpha", "remove", "-alpha", "off", "-colorspace", "sRGB", "-resize", geom + "!", ren_rgb],
            "render-fit",
        )

        if "difference" in artifact_paths:
            _m(
                magick,
                [ref_fit, ren_rgb, "-compose", "Difference", "-composite", "-auto-level", artifact_paths["difference"]],
                "difference",
            )
        if "ghost" in artifact_paths:
            _m(
                magick,
                [
                    ref_fit,
                    ren_rgb,
                    "-compose",
                    "blend",
                    "-define",
                    f"compose:args={GHOST_OPACITY}",
                    "-composite",
                    artifact_paths["ghost"],
                ],
                "ghost",
            )

        if "edge" in artifact_paths:
            r_edge = os.path.join(tmp, "edges_ref.png")
            c_edge = os.path.join(tmp, "edges_render.png")
            r_red = os.path.join(tmp, "r_red.png")
            c_cyan = os.path.join(tmp, "c_cyan.png")
            _m(magick, [ref_fit, "-colorspace", "Gray", "-canny", CANNY, r_edge], "canny-ref")
            _m(magick, [ren_rgb, "-colorspace", "Gray", "-canny", CANNY, c_edge], "canny-render")
            _m(
                magick,
                [r_edge, "-colorspace", "sRGB", "-type", "TrueColor", "-fill", "red", "-opaque", "white", r_red],
                "edge-red",
            )
            _m(
                magick,
                [c_edge, "-colorspace", "sRGB", "-type", "TrueColor", "-fill", "cyan", "-opaque", "white", c_cyan],
                "edge-cyan",
            )
            _m(
                magick,
                [
                    r_red,
                    c_cyan,
                    "-colorspace",
                    "sRGB",
                    "-compose",
                    "Screen",
                    "-composite",
                    "-type",
                    "TrueColor",
                    artifact_paths["edge"],
                ],
                "edge-overlay",
            )

        ae = compare_ae(ref_fit, ren_rgb, fuzz="5%")
        advice = debug_overlay.summarize_advice(ae, frame_pixels=_frame_pixels(geom))
        if json_out:
            print(debug_overlay.plan_to_json(plan, advice))
            return 0
        print("[overlay] wrote:")
        for artifact in plan.artifacts:
            print(f"  {artifact.kind:<10}: {artifact.path}")
        print(f"[overlay] AE(fuzz5%) mismatched-pixels = {ae}  (diagnostic; 0 = identical)")
        print(debug_overlay.format_advice(advice))
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


COMMAND = Command(
    name="overlay",
    group="REFERENCE-MATCH PIPELINE",
    summary="structured debug overlays and advisory diagnostics",
    usage=USAGE,
    run=run,
)
