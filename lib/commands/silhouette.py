"""3d silhouette — camera-locked render -> binary silhouette mask."""
from __future__ import annotations

import os
import subprocess
import tempfile

from cli.env import find_magick, require_magick, require_openscad
from cli.imaging import BG, magick_identify
from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

USAGE = """3d silhouette <file.scad> [options]
  Render at a locked camera, then threshold to a binary silhouette mask.

Options:
  -o, --out PATH        output mask PNG (default: <file>_mask.png)
  --cam ex,ey,ez,cx,cy,cz   6-param VECTOR camera (default: auto ISO via viewall)
  --size WxH            image size (default 1200x900)
  --ortho              orthographic projection (recommended for reference overlay)
  -D k=v                pass-through define (repeatable)

Example:
  3d silhouette model.scad -o mask.png --ortho --cam 130,-600,52,130,0,52 --size 1600x700"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    osc = require_openscad("silhouette")
    require_magick("silhouette")

    inp = argv[0]
    rest = argv[1:]
    out = ""
    cam = ""
    size = "1200,900"
    ortho = False
    defs: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            out = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--cam":
            cam = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--size":
            size = (rest[i + 1] if i + 1 < n else size).replace("x", ",")
            i += 2
        elif a == "--ortho":
            ortho = True
            i += 1
        elif a == "-D":
            if i + 1 < n:
                defs += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="silhouette")

    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="silhouette")
    if not out:
        out = (inp[:-5] if inp.endswith(".scad") else inp) + "_mask.png"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    cam_args: list[str]
    if cam:
        ncomma = len(cam.split(","))
        if ncomma != 6:
            raise InvalidArgument(
                "--cam", cam, ["6 comma-separated numbers ex,ey,ez,cx,cy,cz"],
                command="silhouette",
                extra=f"got {ncomma} values; a 7-param gimbal renders an empty frame.",
            )
        cam_args = ["--camera=" + cam]
    else:
        cam_args = ["--camera=1,1,1,0,0,0", "--autocenter", "--viewall"]

    fd, render = tempfile.mkstemp(suffix=".png", prefix="3d_sil.")
    os.close(fd)
    try:
        ocmd = [osc, "--render"]
        if ortho:
            ocmd.append("--projection=ortho")
        ocmd += cam_args + ["--imgsize=" + size, *defs, "-o", render, inp]
        r = subprocess.run(ocmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            raise GateFailure("silhouette: render failed", command="silhouette")

        mgk = find_magick()
        assert mgk is not None
        m = subprocess.run(
            [mgk, render, "-fuzz", "10%", "-fill", "black", "-opaque", BG,
             "-fill", "white", "+opaque", "black", out],
            capture_output=True, text=True,
        )
        if m.returncode != 0:
            raise GateFailure(
                f"silhouette: mask build failed: {(m.stderr or m.stdout).strip()}",
                command="silhouette",
            )
        dims = magick_identify(out, "%wx%h")
        print(f"silhouette: {inp} -> {out}  ({dims})")
        return 0
    finally:
        try:
            os.remove(render)
        except OSError:
            pass


COMMAND = Command(
    name="silhouette",
    group="REFERENCE-MATCH PIPELINE",
    summary="camera-locked render -> binary silhouette mask",
    usage=USAGE,
    run=run,
)
