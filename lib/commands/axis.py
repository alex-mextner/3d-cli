"""3d axis — validate named axes, section planes, views, and camera vectors."""
from __future__ import annotations

import json

from axis import AxisInfo, validate_axis, validate_camera, validate_plane, validate_view
from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d axis <axis|plane|view|camera> <value> [--json]
  Validate and normalize CLI axis/camera inputs without rendering.

Kinds:
  axis    X|Y|Z, signed axes +X|-X|+Y|-Y|+Z|-Z, or aliases left/right/front/back/top/bottom
  plane   YZ|XZ|XY section plane names
  view    front|back|left|right|top|bottom|iso|3-4|front-left|front-right|rear-left|rear-right
  camera  ex,ey,ez,cx,cy,cz OpenSCAD vector camera

Options:
  --json  print compact deterministic JSON instead of text

Examples:
  3d axis axis -Z
  3d axis plane YZ --json
  3d axis view front-right
  3d axis camera 1,-1,1,0,0,0"""

_KINDS = ("axis", "plane", "view", "camera")


def _format_value(value: str | int | float | list[float]) -> str:
    if isinstance(value, list):
        return ",".join(str(float(v)) for v in value)
    return str(value)


def _print_text(info: AxisInfo) -> None:
    for key, value in info.items():
        print(f"{key}: {_format_value(value)}")


def _validate(kind: str, value: str) -> AxisInfo:
    if kind == "axis":
        return validate_axis(value)
    if kind == "plane":
        return validate_plane(value)
    if kind == "view":
        return validate_view(value)
    if kind == "camera":
        return validate_camera(value)
    raise InvalidArgument("kind", kind, _KINDS, command="axis")


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    json_out = False
    args: list[str] = []
    for arg in argv:
        if arg == "--json":
            json_out = True
        else:
            args.append(arg)
    if len(args) != 2:
        print(USAGE)
        raise UsageError("expected <axis|plane|view|camera> and <value>", command="axis")

    info = _validate(args[0], args[1])
    if json_out:
        print(json.dumps(info, separators=(",", ":")))
    else:
        _print_text(info)
    return 0


COMMAND = Command(
    name="axis",
    group="RENDER & VIEW",
    summary="validate named axes, planes, views, and OpenSCAD camera vectors",
    usage=USAGE,
    run=run,
)
