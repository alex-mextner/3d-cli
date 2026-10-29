"""3d render — unified view / --multi / --section render (backed by lib/render.py).

Modes (mutually exclusive): single view (default), --multi, --section. The heavy
work (bbox-exact cameras, async multi, STL-cut section) lives in lib/render.py and runs
through pyrun; this module only parses args and picks the per-mode dep set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import dimension_args
from cli.env import require_openscad
from cli.pyrun import run_tool
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

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
  Engineering-drawing convention: the CUT FACES are violet with 45-degree hatch lines, every
  uncut surface keeps the colour of its PART, a legend names the parts and the cut face; colours
  come from .scad color(), 3MF and OBJ+MTL files, see `3d dimension --help`.
  --plane YZ|XZ|XY     cut plane orientation. The plane sits at the middle of the
                        model's bounding box on that axis by default (--at moves it). Use this to show the internal layout
                        on a specific face.
                        Default: YZ
                        Example: 3d render bracket.scad --section --plane XZ
  --keep neg|pos       which half to keep after the cut. 'neg' keeps the side in the
                        negative normal direction; 'pos' keeps the positive side.
                        Default: neg
                        Example: 3d render bracket.scad --section --keep pos
  --at MM              position of the cut plane along its axis, in mm (a model coordinate).
                        Default: the middle of the model.
                        Example: 3d render bracket.scad --section --plane XZ --at 12.5
  --part SEL=NAME[:ROLE]  name a part and/or set its role (SEL is a part name or a colour,
                        e.g. '#2a62c9'; ROLE printed|vitamin|reference|stand_in; a stand_in is
                        drawn muted). Repeatable.
                        Example: 3d render assembly.scad --section --part '#2a62c9=PCB:stand_in'
  --color              accepted for old scripts; a section is always per-part coloured.
                        Example: 3d render assembly.scad --section --color --plane YZ

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


SECTION_DEPS = "trimesh,numpy,pillow,scipy,manifold3d"


def _validate_part(spec: str) -> None:
    """Reject a malformed --part here, with the shared grammar, as a structured error."""
    try:
        dimension_args.parse_part(spec)
    except ValueError as exc:
        raise InvalidArgument(
            "--part",
            spec,
            ["SELECTOR=NAME[:ROLE], e.g. '#2a62c9=PCB:stand_in'"],
            command="render",
            extra=str(exc),
        ) from exc


@dataclass
class _Options:
    mode: str = "single"
    args: list[str] = field(default_factory=list)  # pass-through to render.py
    # section-only flags, already in section_render.py's own spelling
    section: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    out: str = ""
    size: str = ""
    multi_outdir: str = ""


def _value(rest: list[str], i: int) -> str:
    if i + 1 >= len(rest):
        raise UsageError(f"option {rest[i]} needs a value", command="render")
    return rest[i + 1]


def _opt_multi(o: _Options, rest: list[str], i: int) -> int:
    o.mode = "multi"
    # optional positional outdir (next token if not a flag)
    if i + 1 < len(rest) and not rest[i + 1].startswith("-"):
        o.multi_outdir = rest[i + 1]
        return i + 2
    return i + 1


def _opt_section(o: _Options, rest: list[str], i: int) -> int:
    o.mode = "section"
    return i + 1


def _opt_camera(o: _Options, rest: list[str], i: int) -> int:
    o.args += [rest[i], _value(rest, i)]
    return i + 2


def _opt_section_value(o: _Options, rest: list[str], i: int) -> int:
    value = _value(rest, i)
    if rest[i] == "--part":
        _validate_part(value)
    o.section.append(f"{rest[i]}={value}")  # `=` form: a value may start with '-'
    return i + 2


def _opt_module(o: _Options, rest: list[str], i: int) -> int:
    raise UsageError(
        "option --module was removed: a section now cuts the model mesh itself",
        command="render",
        remediation=["Drop --module; use --part and --at to choose what to show."],
    )


def _opt_color(o: _Options, rest: list[str], i: int) -> int:
    o.section.append(
        rest[i]
    )  # accepted for old scripts; a section is always per-part coloured
    return i + 1


def _opt_flag(o: _Options, rest: list[str], i: int) -> int:
    o.args.append(rest[i])
    return i + 1


def _opt_out(o: _Options, rest: list[str], i: int) -> int:
    o.out = _value(rest, i)
    return i + 2


def _opt_size(o: _Options, rest: list[str], i: int) -> int:
    o.size = _value(rest, i)
    return i + 2


def _opt_define(o: _Options, rest: list[str], i: int) -> int:
    o.defines.append(_value(rest, i))
    return i + 2


_OPTION_HANDLERS = {
    "--multi": _opt_multi,
    "--section": _opt_section,
    "--view": _opt_camera,
    "--cam": _opt_camera,
    "--colorscheme": _opt_camera,
    "--plane": _opt_section_value,
    "--keep": _opt_section_value,
    "--at": _opt_section_value,
    "--part": _opt_section_value,
    "--module": _opt_module,
    "--color": _opt_color,
    "--ortho": _opt_flag,
    "--render": _opt_flag,
    "-o": _opt_out,
    "--out": _opt_out,
    "--size": _opt_size,
    "-D": _opt_define,
}


def _parse_options(rest: list[str]) -> _Options:
    o = _Options()
    i = 0
    while i < len(rest):
        handler = _OPTION_HANDLERS.get(rest[i])
        if handler is None:
            print(USAGE)
            raise UsageError(f"unknown option '{rest[i]}'", command="render")
        i = handler(o, rest, i)
    return o


def _check_inputs(inp: str, o: _Options) -> None:
    if o.section and o.mode != "section":
        raise UsageError(
            f"{o.section[0].split('=')[0]} belongs to --section",
            command="render",
            remediation=["3d render model.scad --section --plane XZ -o sec.png"],
        )
    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="render")
    # openscad renders single/multi views and exports a .scad for the section; a section of a
    # mesh file (STL/3MF/OBJ) needs no openscad at all.
    if o.mode != "section" or inp.lower().endswith(".scad"):
        require_openscad("render")


def _run_section(inp: str, o: _Options) -> int:
    target = o.out or os.path.splitext(inp)[0] + ".png"
    section_args = [inp, f"--out={target}", *(f"--define={d}" for d in o.defines)]
    if o.size:
        section_args.append(f"--size={o.size}")
    return run_tool(
        SECTION_DEPS,
        "section_render.py",
        [*section_args, *(s for s in o.section if s != "--color")],
    )


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    inp = argv[0]
    o = _parse_options(argv[1:])
    _check_inputs(inp, o)
    args = o.args
    for define in o.defines:
        args += ["-D", define]

    # Per-mode python deps (see lib/render.py): single/multi need NO deps (mesh stack is
    # a pure enhancement, render.py degrades to --autocenter --viewall offline); the
    # section (lib/section_render.py) cuts the mesh itself.
    if o.mode == "single":
        if o.out:
            args += ["-o", o.out]
        if o.size:
            args += ["--size", o.size]
        return run_tool("", "render.py", ["single", inp, *args])
    if o.mode == "multi":
        tool_args = ["multi", inp]
        if o.multi_outdir:
            tool_args.append(o.multi_outdir)
        if o.size:
            args += ["--size", o.size]
        return run_tool("", "render.py", [*tool_args, *args])
    return _run_section(inp, o)


COMMAND = Command(
    name="render",
    group="RENDER & VIEW",
    summary="single view / --multi / --section CGAL render (camera from bbox; 6-param vector cuts)",
    usage=USAGE,
    run=run,
)
