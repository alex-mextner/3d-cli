"""3d import — bring external mesh/model formats into the OpenSCAD workflow."""
from __future__ import annotations

from cli.registry import Command
from errors import InvalidArgument, UsageError
from import_formats import plan_import, render_plan, write_wrapper

USAGE = """3d import <model> [options]
  Import or plan imports for common 3D/model formats in the OpenSCAD workflow.

Direct wrapper formats:
  .stl, .off, .amf, .3mf

Planning-only formats:
  .obj, .ply, .gltf, .glb, .dae, .step, .stp, .iges, .igs, .brep, .fcstd, .usd, .usdc, .usdz

Options:
  -o, --out PATH        output .scad wrapper (default: <model>.import.scad for direct formats)
  --format FMT          override format detection (e.g. --format stl)
  --mode MODE           auto, wrapper, or plan (default: auto)
  --scale N             uniform scale in the generated wrapper (default: 1)
  --convexity N         OpenSCAD import convexity (default: 10)

Examples:
  3d import part.stl
  3d import part.stl -o wrappers/part.scad --scale 25.4 --convexity 12
  3d import scan.obj --mode plan
  3d import asset.mesh --format obj --mode plan"""


def _take_value(argv: list[str], index: int, flag: str) -> tuple[str, int]:
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise UsageError(f"option {flag} needs a value", command="import")
    return argv[index + 1], index + 2


def _parse_float(flag: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise InvalidArgument(flag, raw, ["a number"], command="import") from None


def _parse_int(flag: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise InvalidArgument(flag, raw, ["an integer"], command="import") from None


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    inp = argv[0]
    out = None
    fmt = None
    mode = "auto"
    scale = 1.0
    convexity = 10

    rest = argv[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-o", "--out"):
            out, i = _take_value(rest, i, arg)
        elif arg == "--format":
            fmt, i = _take_value(rest, i, arg)
        elif arg == "--mode":
            mode, i = _take_value(rest, i, arg)
        elif arg == "--scale":
            raw, i = _take_value(rest, i, arg)
            scale = _parse_float("--scale", raw)
        elif arg == "--convexity":
            raw, i = _take_value(rest, i, arg)
            convexity = _parse_int("--convexity", raw)
        else:
            raise UsageError(f"unknown option '{arg}'", command="import")

    plan = plan_import(
        inp,
        out_path=out,
        format_override=fmt,
        mode=mode,
        scale=scale,
        convexity=convexity,
    )
    if plan.action == "wrapper":
        write_wrapper(plan)
        print(render_plan(plan))
    else:
        print(render_plan(plan))
    return 0


COMMAND = Command(
    name="import",
    group="GEOMETRY & EXPORT",
    summary="generate OpenSCAD wrappers or conversion plans for imported model formats",
    usage=USAGE,
    run=run,
)
