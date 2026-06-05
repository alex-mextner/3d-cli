"""3d preview — fast throwntogether preview PNG (no CGAL render).

Note: preview's default camera is a 7-param gimbal ON PURPOSE (throwntogether wants the
gimbal form); the 6-param vector validation that render/silhouette/score apply does NOT
apply here.
"""
from __future__ import annotations

import os
import subprocess

from cli.env import require_openscad
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d preview <file.scad> [options]
  Fast throwntogether preview PNG (no CGAL render).

Options:
  -o, --out PATH        output PNG (default: <file>.png)
  --cam C               camera (7-param gimbal tx,ty,tz,rx,ry,rz,dist OR 6-param vector)
  --size WxH            image size (default 800x600)
  -D k=v                pass-through define (repeatable)

Examples:
  3d preview model.scad
  3d preview model.scad -o look.png --size 1024x768"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    osc = require_openscad("preview")

    inp = argv[0]
    rest = argv[1:]
    out = ""
    cam = "0,0,0,55,0,25,0"
    size = "800,600"
    defs: list[str] = []
    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a in ("-o", "--out"):
            out = rest[i + 1] if i + 1 < n else ""
            i += 2
        elif a == "--cam":
            cam = rest[i + 1] if i + 1 < n else cam
            i += 2
        elif a == "--size":
            size = (rest[i + 1] if i + 1 < n else "800x800").replace("x", ",")
            i += 2
        elif a == "-D":
            if i + 1 < n:
                defs += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="preview")

    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="preview")
    if not out:
        out = inp[:-5] + ".png" if inp.endswith(".scad") else inp + ".png"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    print(f"preview: {inp} -> {out} (throwntogether)")
    r = subprocess.run(
        [
            osc, "--camera=" + cam, "--imgsize=" + size,
            "--colorscheme=Tomorrow Night", "--autocenter", "--viewall",
            *defs, "-o", out, inp,
        ]
    )
    if r.returncode != 0:
        return r.returncode
    print(f"preview: wrote {out}")
    return 0


COMMAND = Command(
    name="preview",
    group="RENDER & VIEW",
    summary="fast throwntogether preview (no CGAL)",
    usage=USAGE,
    run=run,
)
