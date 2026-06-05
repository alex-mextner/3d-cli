"""3d render — unified view / --multi / --section render (backed by lib/render.py).

Modes (mutually exclusive): single view (default), --multi, --section. The heavy
work (bbox-exact cameras, async multi, STL-cut section) lives in lib/render.py and runs
through pyrun; this module only parses args and picks the per-mode dep set.
"""
from __future__ import annotations

import os

from cli.env import require_openscad
from cli.pyrun import run_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d render <file.scad> [mode + options]
  Default: single CGAL render, ISO view, locked 6-param vector camera.

Modes (mutually exclusive):
  (default)            single view render
  --multi [OUTDIR]     render front/back/left/right/top/iso into OUTDIR (default previews/), async
  --section            true cross-section (generic STL-cut, or --color assembly mode)

Single-view options:
  --view NAME          front|back|left|right|top|bottom|iso|3-4|front-left|front-right|rear-left|rear-right (default iso)
  --cam ex,ey,ez,cx,cy,cz   manual 6-param VECTOR camera (wins over --view)
  --ortho              orthographic projection
  --colorscheme NAME   OpenSCAD colorscheme (default 'Tomorrow Night')

Section options:
  --plane YZ|XZ|XY     cut plane (default YZ)
  --keep neg|pos       which half to keep (default neg)
  --color              coloured per-part ASSEMBLY mode (assembly must honour -D cut=true)
  --module 'name();'   module to cut (only needed in the no-mesh-stack fallback)

Common:
  -o, --out PATH       output PNG (single/section). Default: <file>.png
  --size WxH           image size (single/section 1200x900, multi 800x600)
  -D k=v               pass-through define (repeatable)

Examples:
  3d render model.scad --view left -o left.png
  3d render model.scad --view 3-4 --ortho
  3d render model.scad --multi previews/ --render
  3d render model.scad --section --plane YZ -o sec.png
  3d render assembly.scad --section --color --plane YZ -o sec.png"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    require_openscad("render")

    inp = argv[0]
    rest = argv[1:]

    mode = "single"
    args: list[str] = []  # pass-through to render.py
    out = ""
    size = ""
    multi_outdir = ""

    i = 0
    n = len(rest)
    while i < n:
        a = rest[i]
        if a == "--multi":
            mode = "multi"
            # optional positional outdir (next token if not a flag)
            if i + 1 < n and not rest[i + 1].startswith("-"):
                multi_outdir = rest[i + 1]
                i += 2
            else:
                i += 1
        elif a == "--section":
            mode = "section"
            i += 1
        elif a in ("--view", "--cam", "--colorscheme", "--plane", "--keep", "--module"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="render")
            args += [a, rest[i + 1]]
            i += 2
        elif a in ("--ortho", "--color", "--render"):
            args.append(a)
            i += 1
        elif a in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="render")
            out = rest[i + 1]
            i += 2
        elif a == "--size":
            if i + 1 >= n:
                raise UsageError("option --size needs a value", command="render")
            size = rest[i + 1]
            i += 2
        elif a == "-D":
            if i + 1 >= n:
                raise UsageError("option -D needs a value", command="render")
            args += ["-D", rest[i + 1]]
            i += 2
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="render")

    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="render")

    # Per-mode python deps (see lib/render.py): single/multi need NO deps (mesh stack is
    # a pure enhancement, render.py degrades to --autocenter --viewall offline); the
    # generic section genuinely needs trimesh to read the STL bbox for the cut.
    if mode == "single":
        if out:
            args += ["-o", out]
        if size:
            args += ["--size", size]
        return run_tool("", "render.py", ["single", inp, *args])
    if mode == "multi":
        tool_args = ["multi", inp]
        if multi_outdir:
            tool_args.append(multi_outdir)
        if size:
            args += ["--size", size]
        return run_tool("", "render.py", [*tool_args, *args])
    # section
    if out:
        args += ["-o", out]
    if size:
        args += ["--size", size]
    return run_tool("trimesh,numpy", "render.py", ["section", inp, *args])


COMMAND = Command(
    name="render",
    group="RENDER & VIEW",
    summary="single view / --multi / --section CGAL render (camera from bbox; 6-param vector cuts)",
    usage=USAGE,
    run=run,
)
