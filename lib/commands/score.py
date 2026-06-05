"""3d score — silhouette AE + IoU; prints machine-parseable KEY=VALUE lines."""
from __future__ import annotations

import os
import subprocess

from cli.env import find_magick, require_magick, require_openscad
from cli.imaging import BG, compare_ae, score_metrics
from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

USAGE = """3d score <render.png|file.scad|mask.png> <reference|mask.png> [-o outdir] [options]
  Silhouette AE + IoU. Prints machine-parseable KEY=VALUE lines.

Modes:
  (default)   first arg is a render PNG or .scad; second is the reference image.
              A .scad is rendered at a locked camera first.
  --masks     both args are ready binary masks (white shape, black bg); compared directly.

Options:
  -o DIR                output dir for masks/overlay (default: /tmp/3dscore)
  --cam ex,..,cz        6-param vector camera for the .scad render (default locked side)
  --size WxH            image size for the .scad render (default 1200x900)
  --ortho              orthographic projection for the .scad render
  -D k=v                define passed to the .scad render (repeatable)

Examples:
  3d score model.scad ref.jpg
  3d score render.png ref.jpg -o work/
  3d score mask_a.png mask_b.png --masks"""

BG_FUZZ = "10%"
REF_THRESH = "78%"
DEFAULT_CAM = "125,-330,52,125,28,44"


def _m(args: list[str], what: str) -> str:
    mgk = find_magick()
    assert mgk is not None
    r = subprocess.run([mgk, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GateFailure(f"score: {what} failed: {(r.stderr or r.stdout).strip()}", command="score")
    return r.stdout.strip()


def _identify_int(path: str, fmt: str) -> int:
    mgk = find_magick()
    assert mgk is not None
    r = subprocess.run([mgk, "identify", "-format", fmt, path], capture_output=True, text=True)
    return int(r.stdout.strip())


def run(argv: list[str]) -> int:  # noqa: C901  (faithful port of the bash flow)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    if len(argv) < 2:
        print(USAGE)
        return 1
    require_magick("score")

    a, b = argv[0], argv[1]
    rest = argv[2:]
    out = "/tmp/3dscore"
    masks = False
    cam = ""
    size = "1200,900"
    ortho = False
    defs: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        x = rest[i]
        if x in ("-o", "--out"):
            out = rest[i + 1] if i + 1 < n else out
            i += 2
        elif x == "--masks":
            masks = True
            i += 1
        elif x == "--cam":
            cam = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif x == "--size":
            size = (rest[i + 1] if i + 1 < n else size).replace("x", ",")
            i += 2
        elif x == "--ortho":
            ortho = True
            i += 1
        elif x == "-D":
            if i + 1 < n:
                defs += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{x}'", command="score")

    for f in (a, b):
        if not os.path.isfile(f):
            raise InputNotFound(f, command="score")
    os.makedirs(out, exist_ok=True)

    ext = a.rsplit(".", 1)[-1].lower() if "." in a else ""
    if ext == "scad" and not masks:
        osc = require_openscad("score")
        if not cam:
            cam = DEFAULT_CAM
        if len(cam.split(",")) != 6:
            raise InvalidArgument(
                "--cam", cam, ["6 comma-separated numbers ex,ey,ez,cx,cy,cz"],
                command="score",
            )
        rpng = os.path.join(out, "render.png")
        ocmd = [osc, "--render"]
        if ortho:
            ocmd.append("--projection=ortho")
        ocmd += ["--camera=" + cam, "--imgsize=" + size, *defs, "-o", rpng, a]
        r = subprocess.run(ocmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            raise GateFailure(f"score: render failed for {a}", command="score")
        a = rpng

    if masks:
        mr, mf = a, b
        wa = _identify_int(mr, "%w")
        ha = _identify_int(mr, "%h")
        wb = _identify_int(mf, "%w")
        hb = _identify_int(mf, "%h")
        if (wa, ha) != (wb, hb):
            rs = os.path.join(out, "mask_ref_rs.png")
            _m([mf, "-resize", f"{wa}x{ha}!", rs], "mask-resize")
            mf = rs
        w, h = wa, ha
    else:
        mr = os.path.join(out, "mask_render.png")
        _m([a, "-fuzz", BG_FUZZ, "-fill", "black", "-opaque", BG,
            "-fill", "white", "+opaque", "black", mr], "render-mask")
        w = _identify_int(mr, "%w")
        h = _identify_int(mr, "%h")
        mf = os.path.join(out, "mask_ref.png")
        _m([b, "-resize", f"{w}x{h}!", "-colorspace", "Gray",
            "-threshold", REF_THRESH, "-negate", mf], "ref-mask")

    area = w * h
    if area <= 0:
        raise GateFailure("score: zero-area frame", command="score")

    ae_raw = compare_ae(mr, mf)
    ae_tok = ae_raw.split()[0] if ae_raw.split() else ""
    try:
        ae_int = int(round(float(ae_tok)))
    except ValueError:
        raise GateFailure(f"score: compare AE failed: {ae_raw}", command="score")

    inter = float(_m([mr, "-threshold", "50%", mf, "-threshold", "50%",
                      "-compose", "multiply", "-composite", "-format", "%[fx:mean]", "info:"], "inter"))
    union = float(_m([mr, "-threshold", "50%", mf, "-threshold", "50%",
                      "-compose", "lighten", "-composite", "-format", "%[fx:mean]", "info:"], "union"))

    metrics = score_metrics(inter, union, float(ae_int), area)

    # overlay (best-effort; never fails the score).
    overlay = os.path.join(out, "overlay.png")
    ref_red = os.path.join(out, "_ref_red.png")
    ren_cyan = os.path.join(out, "_ren_cyan.png")
    try:
        _m([mf, "-threshold", "50%", "-fill", "red", "-opaque", "white", ref_red], "ref-red")
        _m([mr, "-threshold", "50%", "-fill", "cyan", "-opaque", "white", ren_cyan], "ren-cyan")
        _m([ref_red, ren_cyan, "-compose", "Screen", "-composite", overlay], "overlay")
    except GateFailure:
        overlay = "(overlay-failed)"
    for f in (ref_red, ren_cyan):
        try:
            os.remove(f)
        except OSError:
            pass

    print(f"AE={ae_int}")
    print(f"AE_NORM={metrics['AE_NORM']:.6f}")
    print(f"IoU={metrics['IoU']:.4f}")
    print(f"CLOSENESS={metrics['CLOSENESS']:.4f}")
    print(f"FRAME={w}x{h}")
    print(f"OVERLAY={overlay}")
    return 0


COMMAND = Command(
    name="score",
    group="REFERENCE-MATCH PIPELINE",
    summary="silhouette AE + IoU (machine-parseable KEY=VALUE lines)",
    usage=USAGE,
    run=run,
)
