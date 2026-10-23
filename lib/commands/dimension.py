"""3d dimension — shaded multi-view renders WITH dimensions, and the legibility gate for them.

WHAT: `3d dimension MODEL -o out.png` draws top / front / side / iso views of the real mesh with
  double-headed mm arrows (overall X/Y/Z, the longest span of a slanted part, any `--dim`), an
  optional bed outline and the part name, and writes `out.png.dims.json` next to it.
  `3d dimension --check out.png` verifies from that sidecar AND the pixels that every label is
  readable (exit 1 + offenders otherwise). tg-cli's send gate calls the same check.

WHY: a plain render does not let the owner confirm a model before a print — sizes must be
  visible on the picture (alex-mextner/3d-cli#477).

INVARIANTS: stdlib-only at import time; rendering and checking live in lib/dimension_render.py
  and lib/dimension_check.py, run through pyrun with their deps.
"""

from __future__ import annotations

import os

import dimension_args
from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d dimension MODEL.(stl|3mf|obj|scad) -o OUT.png [--bed WxD] [--dim NAME:x,y,z:x,y,z]... [options]
3d dimension --check IMAGE.png [--json]
  Render the part from top / front / side / iso with dimension arrows and values in mm, or
  verify that such a render is legible. The render also writes OUT.png.dims.json (every
  dimension: label, value, view, text box in px; per-view panel and silhouette; image sha256).

Render options:
  -o, --out OUT.png    output image (required)
  --bed WxD            bed outline in the top view, e.g. 220x220 (print-pose models: model
                       coordinates are bed coordinates; flags a part sticking out of the bed)
  --dim NAME:P1:P2     extra dimension between two points, P = x,y,z in model mm, e.g.
                       "boss length:60,40,0:60,65,0" (repeatable). Shown only in views where
                       the arrow keeps >= 90% of its true length.
  --name TEXT          part name in the header (default: the file name)
  --width PX           image width 640..8192 (default 1280, the width the legibility rules are set at)
  --label-px PX        label font size at 1280 px width (default 20; below 14 the check fails)

Automatic dimensions: overall X / Y / Z of the bounding box in every view where the edge is
visible, and "span" (the longest distance between two points) when the part's oriented box is more
than 3% longer than its axis-aligned one — a part lying slanted or tilted.

Check (--check IMAGE): needs IMAGE.dims.json beside it and verifies
  - the sidecar belongs to this exact image (sha256, size) and lists >= 3 dimensions;
  - font >= 14 px at 1280 px width and the drawn text really is that tall;
  - every label box inside the image and its view panel, none overlapping another label,
    none covering the part's silhouette, text/background contrast >= 4.5:1, value shown.
  Exit 0 legible, 1 offenders listed (--json for machine output), 2 usage error.

Examples:
  3d dimension print/spoon.stl -o ~/.cache/3d-cli/spoon_dims.png --bed 220x220
  3d dimension boss.scad -o boss.png --dim "hole d:10,10,0:16,10,0"
  3d dimension --check ~/.cache/3d-cli/spoon_dims.png"""

DEPS = "trimesh,numpy,pillow,scipy"


RENDER_FLAGS = ("--bed", "--dim", "--name", "--width", "--label-px")


def _value(argv: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(argv):
        raise UsageError(
            f"{flag} needs a value",
            command="dimension",
            remediation=["See `3d dimension --help`."],
        )
    return argv[index + 1]


def _validate(flag: str, value: str) -> None:
    """Reject a malformed value here with the shared grammar, so the error is a structured one."""
    parsers = {
        "--bed": (dimension_args.parse_bed, "WIDTHxDEPTH in mm, e.g. 220x220"),
        "--dim": (
            dimension_args.parse_dim,
            "NAME:x,y,z:x,y,z (three finite numbers per point)",
        ),
        "--width": (
            dimension_args.parse_width,
            f"an integer >= {dimension_args.MIN_WIDTH_PX}",
        ),
        "--label-px": (
            lambda raw: dimension_args.positive(raw, flag),
            "a positive finite number",
        ),
    }
    parse, accepted = parsers.get(flag, (None, ""))
    if parse is None:
        return
    try:
        parse(value)
    except ValueError as exc:
        raise InvalidArgument(
            flag, value, [accepted], command="dimension", extra=str(exc)
        ) from exc


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    model: str | None = None
    check: str | None = None
    out: str | None = None
    as_json = False
    render_flags: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--check":
            check = _value(argv, i, arg)
            i += 2
        elif arg in ("-o", "--out"):
            out = _value(argv, i, arg)
            i += 2
        elif arg == "--json":
            as_json = True
            i += 1
        elif arg in RENDER_FLAGS:
            value = _value(argv, i, arg)
            _validate(arg, value)
            render_flags.append(
                f"{arg}={value}"
            )  # `=` form: a value may start with '-'
            i += 2
        elif arg.startswith("-"):
            raise UsageError(
                f"unknown option {arg}",
                command="dimension",
                remediation=["See `3d dimension --help`."],
            )
        elif model is None:
            model = arg
            i += 1
        else:
            raise UsageError(
                f"unexpected argument {arg!r}",
                command="dimension",
                remediation=["One MODEL only; see `3d dimension --help`."],
            )
    if check is not None:
        if model or out or render_flags:
            raise UsageError(
                "--check takes only IMAGE (and --json)",
                command="dimension",
                remediation=["Render and check in two commands."],
            )
        if not os.path.isfile(check):
            raise InputNotFound(check, command="dimension")
        return exec_tool(
            "numpy,pillow",
            "dimension_check.py",
            [check, *(["--json"] if as_json else [])],
        )
    if as_json:
        raise UsageError(
            "--json belongs to --check",
            command="dimension",
            remediation=["3d dimension --check IMAGE.png --json"],
        )
    if model is None or out is None:
        raise UsageError(
            "MODEL and -o OUT.png are required",
            command="dimension",
            remediation=["3d dimension part.stl -o part_dims.png"],
        )
    if not os.path.isfile(model):
        raise InputNotFound(model, command="dimension")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    return exec_tool(
        DEPS, "dimension_render.py", [model, f"--out={out}", *render_flags]
    )


COMMAND = Command(
    name="dimension",
    group="RENDER & VIEW",
    summary="shaded top/front/side/iso render with mm dimension arrows + legibility --check",
    usage=USAGE,
    run=run,
    aliases=("dims",),
)
