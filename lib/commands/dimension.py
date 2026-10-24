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

USAGE = """3d dimension MODEL.(stl|3mf|obj|scad) -o OUT.png [--bed WxD] [--dim NAME:x,y,z:x,y,z]... [--part SEL=NAME[:ROLE]]... [-D NAME=VALUE]... [--up Z|Y|X] [--overall auto|always|never] [--dims-from SPEC] [options]
3d dimension --check IMAGE.png [--json] [--require-dims]
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
  -D NAME=VALUE        override an OpenSCAD variable of a .scad input (repeatable), e.g. -D standin=true
  --part SEL=NAME[:ROLE]
                       name a part and/or set its role; SEL is a part name or a colour (#RGB/#RRGGBB)
                       (repeatable), e.g. --part "#2a62c9=PCB:stand_in". ROLE is printed|vitamin|
                       reference|stand_in; a stand_in is drawn muted (washed toward white).

Parts and colours: every part of the model is drawn in its OWN colour and a legend under the four views
names them. Parts come from the file: 3MF objects (or colour groups), OBJ o/g groups + usemtl, OpenSCAD
color() groups; geometry without any colour is split into its separate bodies ("body 1", ...). Colours:
the authored one (3MF material, OBJ+MTL Kd, OpenSCAD color()) when it differs clearly from the others,
else the deterministic palette keyed by part NAME (same assembly -> same colours). 3MF objects tagged
3d:role=stand_in|vitamin|reference are drawn muted or per role. A single-part model stays one colour with no legend.

Automatic dimensions: overall X / Y / Z of the bounding box in every view where the edge is
visible, and "span" (the longest distance between two points) when the part's oriented box is more
than 3% longer than its axis-aligned one — a part lying slanted or tilted.

Views and dimensions:
  --up Z|Y|X           the model's vertical axis (default Z). A model drawn Y-up gets a TRUE top view
                       (labels keep the model's own X/Y/Z letters); not with --bed.
                       Example: 3d dimension mole.3mf --up Y -o mole.png
  --overall auto|always|never
                       overall X/Y/Z of the whole model. auto (default) draws them, except on an
                       exploded assembly or a plate of parts that do not touch: there they are noise
                       and every separate part gets its own footprint dims instead. never draws none.
                       always on such a layout is refused (exit 2).
                       Example: 3d dimension plate.3mf --overall never -o plate.png
  --dims-from SPEC     a JSON/YAML list of {name, from: [x,y,z], to: [x,y,z]} measurements that MUST be
                       drawn: the render fails (exit 2) naming any that fits no view, and the sidecar
                       records them for `--check --require-dims`. The mating gap between two parts is
                       declared {name, kind: gap, parts: [A, B], from, to}.
                       Example: 3d dimension box.stl --dims-from dims.yaml -o box.png

Occlusion: a --dim / --dims-from measurement is drawn in a view only if both endpoints are visible there
(a ray toward the eye meets no surface more than 0.5 mm away); otherwise it is listed under `hidden_in_view`
in the sidecar. One hidden in every view fails a --dims-from render. An explicit arrow is anchored on the part
(drawn dashed where it runs over it); name it after the object it measures.

No dimension across separate parts: on an exploded assembly or a plate (parts that do not touch) a
--dim / --dims-from measurement whose two endpoints are on DIFFERENT parts (within 0.5 mm of a part's
bounding box) is refused with exit 2, naming both parts, because the number would only measure the gap
between them. Keep both endpoints on ONE part, or declare a `kind: gap` between two named parts. The
automatic span of a slanted part is never drawn across separated parts either, and --check fails an
image whose sidecar holds such a dimension (rule spans-separated-parts).

Check (--check IMAGE): needs IMAGE.dims.json beside it and verifies
  --require-dims       also FAIL when a measurement declared with --dims-from is missing from the
                       image (or the image declares none). Example: 3d dimension --check box.png --require-dims
  - the sidecar belongs to this exact image (sha256, size) and lists >= 3 dimensions;
  - font >= 14 px at 1280 px width and the drawn text really is that tall;
  - every label box inside the image and its view panel, none overlapping another label,
    none covering the part's silhouette, text/background contrast >= 4.5:1, value shown.
  Exit 0 legible, 1 offenders listed (--json for machine output), 2 usage error.

Examples:
  3d dimension print/spoon.stl -o ~/.cache/3d-cli/spoon_dims.png --bed 220x220
  3d dimension boss.scad -o boss.png --dim "hole d:10,10,0:16,10,0"
  3d dimension assembly.scad -D standin=true -o assembly.png --part "#2a62c9=PCB:stand_in" --part "#c01818=red wire"
  3d dimension box.3mf -o box.png --part "lid=top cover"
  3d dimension --check ~/.cache/3d-cli/spoon_dims.png"""

DEPS = "trimesh,numpy,pillow,scipy,pyyaml"


RENDER_FLAGS = (
    "--bed",
    "--dim",
    "--name",
    "--width",
    "--label-px",
    "--part",
    "--define",
    "--up",
    "--overall",
    "--dims-from",
)
FLAG_ALIASES = {"-D": "--define"}


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
        "--define": (dimension_args.parse_define, "NAME=VALUE, e.g. standin=true"),
        "--up": (dimension_args.parse_up, "Z, Y or X"),
        "--overall": (dimension_args.parse_overall, "auto, always or never"),
        "--dims-from": (
            dimension_args.parse_dims_file,
            "a JSON/YAML file listing {name, from, to} measurements",
        ),
        "--part": (
            dimension_args.parse_part,
            "SELECTOR=NAME[:ROLE], e.g. '#2a62c9=PCB:stand_in'",
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
    require_dims = False
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
        elif arg == "--require-dims":
            require_dims = True
            i += 1
        elif (
            arg.startswith("-D") and len(arg) > 2
        ):  # -Dname=value, as openscad takes it
            _validate("--define", arg[2:])
            render_flags.append(f"--define={arg[2:]}")
            i += 1
        elif (flag := FLAG_ALIASES.get(arg, arg)) in RENDER_FLAGS:
            value = _value(argv, i, arg)
            _validate(flag, value)
            render_flags.append(
                f"{flag}={value}"
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
                "--check takes only IMAGE (and --json, --require-dims)",
                command="dimension",
                remediation=["Render and check in two commands."],
            )
        if not os.path.isfile(check):
            raise InputNotFound(check, command="dimension")
        return exec_tool(
            "numpy,pillow",
            "dimension_check.py",
            [
                check,
                *(["--json"] if as_json else []),
                *(["--require-dims"] if require_dims else []),
            ],
        )
    if require_dims:
        raise UsageError(
            "--require-dims belongs to --check",
            command="dimension",
            remediation=["3d dimension --check IMAGE.png --require-dims"],
        )
    if as_json:
        raise UsageError(
            "--json belongs to --check",
            command="dimension",
            remediation=["3d dimension --check IMAGE.png --json"],
        )
    if (
        model is not None
        and not model.lower().endswith(".scad")
        and any(f.startswith("--define=") for f in render_flags)
    ):
        raise UsageError(
            "-D NAME=VALUE only applies to a .scad input",
            command="dimension",
            remediation=[
                "Pass the .scad file, e.g. 3d dimension box.scad -D standin=true -o box.png"
            ],
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
