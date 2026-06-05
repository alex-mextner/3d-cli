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
  Render a model to PNG. Default: single view, ISO angle, camera locked from the
  model's bounding box so the whole part fits in frame.

Modes (mutually exclusive):
  (default)            single view render — one PNG, one angle.
  --multi [OUTDIR]     render all standard angles (front/back/left/right/top/iso)
                        into OUTDIR in one batch, async. Use this when you need a
                        set of preview images for docs or a gallery.
                        Default OUTDIR: previews/
                        Example: 3d render bracket.scad --multi previews/
  --section            cut a cross-section to show internal cavities. Use this when
                        you need to verify fit, clearances, or wall thicknesses that
                        are hidden in an external view.
                        Example: 3d render bracket.scad --section --plane YZ

Single-view options:
  --view NAME          preset camera angle computed from the model bbox so the whole
                        part fits; no manual coordinates needed.
                        NAME: front|back|left|right|top|bottom|iso|3-4|front-left|
                        front-right|rear-left|rear-right (default iso)
                        Example: 3d render bracket.scad --view left -o left.png
  --cam ex,ey,ez,cx,cy,cz   manual 6-param VECTOR camera (last resort). Overrides
                        --view. Only use this when the preset angles do not show the
                        feature you need; prefer --view for repeatability.
                        Example: 3d render bracket.scad --cam 100,-200,50,0,0,0
  --ortho              orthographic projection (no perspective). Use this for
                        dimension-accurate technical drawings or when measuring
                        from the render.
                        Example: 3d render bracket.scad --view top --ortho
   --colorscheme NAME   OpenSCAD color theme for the render. Use this to match a
                         dark or light document background.
                         Default: 'Tomorrow Night'
                         Example: 3d render bracket.scad --colorscheme 'Before Dawn'
   --render             force CGAL render mode (slower but exact). Use this when
                         the preview mode produces visual artifacts or when you need
                         the exact geometry for a downstream gate.
                         Example: 3d render bracket.scad --view left --render

   Section options:
  --plane YZ|XZ|XY     cut plane orientation. The plane passes through the model's
                        centroid by default. Use this to show the internal layout
                        on a specific face.
                        Default: YZ
                        Example: 3d render bracket.scad --section --plane XZ
  --keep neg|pos       which half to keep after the cut. 'neg' keeps the side in the
                        negative normal direction; 'pos' keeps the positive side.
                        Default: neg
                        Example: 3d render bracket.scad --section --keep pos
  --color              colored per-part assembly section mode. Use this when the
                        .scad uses -D cut=true and each part is wrapped in a color()
                        so the cut face shows the part color.
                        Example: 3d render assembly.scad --section --color --plane YZ
  --module 'name();'   module to cut, only needed when the generic STL-cut fallback
                        is used instead of the assembly mode. Use this when the section
                        is not a top-level difference().
                        Example: 3d render bracket.scad --section --module 'cutaway();'

Common:
  -o, --out PATH       output PNG path. Default: <file>.png
                        Example: 3d render bracket.scad --view front -o docs/front.png
  --size WxH           render resolution. Default: 1200x900 (single/section),
                        800x600 (multi)
                        Example: 3d render bracket.scad --size 2400x1800
  -D k=v               pass an OpenSCAD variable define. Repeatable. Use this to
                        change a parameter and re-render without editing the .scad.
                        Example: 3d render bracket.scad -D 'depth=40' --view left

Examples:
  3d render bracket.scad --view left -o left.png
  3d render bracket.scad --view 3-4 --ortho
  3d render bracket.scad --multi previews/
  3d render bracket.scad --section --plane YZ -o sec.png
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
