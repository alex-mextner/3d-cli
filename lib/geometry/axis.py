"""Axis, plane, and OpenSCAD camera-vector validation helpers."""
from __future__ import annotations

import math
from typing import TypeAlias

from errors import InvalidArgument, UsageError

JsonValue: TypeAlias = str | int | float | list[float]
AxisInfo: TypeAlias = dict[str, JsonValue]

_AXIS_VECTORS: dict[str, tuple[str, int, tuple[float, float, float]]] = {
    "+X": ("X", 1, (1.0, 0.0, 0.0)),
    "-X": ("X", -1, (-1.0, 0.0, 0.0)),
    "+Y": ("Y", 1, (0.0, 1.0, 0.0)),
    "-Y": ("Y", -1, (0.0, -1.0, 0.0)),
    "+Z": ("Z", 1, (0.0, 0.0, 1.0)),
    "-Z": ("Z", -1, (0.0, 0.0, -1.0)),
}

_AXIS_ALIASES: dict[str, str] = {
    "X": "+X",
    "+X": "+X",
    "RIGHT": "+X",
    "-X": "-X",
    "LEFT": "-X",
    "Y": "+Y",
    "+Y": "+Y",
    "BACK": "+Y",
    "-Y": "-Y",
    "FRONT": "-Y",
    "Z": "+Z",
    "+Z": "+Z",
    "TOP": "+Z",
    "-Z": "-Z",
    "BOTTOM": "-Z",
}

AXIS_NAMES: list[str] = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]
AXIS_ALIASES: list[str] = sorted(_AXIS_ALIASES)

PLANE_NORMALS: dict[str, tuple[str, tuple[float, float, float]]] = {
    "YZ": ("X", (1.0, 0.0, 0.0)),
    "XZ": ("Y", (0.0, 1.0, 0.0)),
    "XY": ("Z", (0.0, 0.0, 1.0)),
}
PLANE_NAMES: list[str] = list(PLANE_NORMALS)

VIEW_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "iso": (1.0, -1.0, 1.0),
}


def _diag_dir(az_deg: float, el_deg: float) -> tuple[float, float, float]:
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    horiz = math.cos(el)
    return (horiz * math.sin(az), -horiz * math.cos(az), math.sin(el))


VIEW_DIRECTIONS["3-4"] = _diag_dir(45.0, 30.0)
VIEW_DIRECTIONS["front-left"] = _diag_dir(-45.0, 25.0)
VIEW_DIRECTIONS["front-right"] = _diag_dir(45.0, 25.0)
VIEW_DIRECTIONS["rear-left"] = _diag_dir(-135.0, 25.0)
VIEW_DIRECTIONS["rear-right"] = _diag_dir(135.0, 25.0)
VIEW_NAMES: list[str] = list(VIEW_DIRECTIONS)


def _rounded(values: tuple[float, ...]) -> list[float]:
    return [round(v, 6) for v in values]


def validate_axis(value: str, *, command: str = "axis") -> AxisInfo:
    key = value.strip().upper()
    name = _AXIS_ALIASES.get(key)
    if name is None:
        raise InvalidArgument("axis", value, AXIS_ALIASES, command=command)
    axis, sign, vector = _AXIS_VECTORS[name]
    return {
        "kind": "axis",
        "name": name,
        "axis": axis,
        "sign": sign,
        "vector": list(vector),
    }


def validate_plane(value: str, *, command: str = "axis") -> AxisInfo:
    name = value.strip().upper()
    plane = PLANE_NORMALS.get(name)
    if plane is None:
        raise InvalidArgument("plane", value, PLANE_NAMES, command=command)
    normal_axis, normal_vector = plane
    return {
        "kind": "plane",
        "name": name,
        "normal_axis": normal_axis,
        "normal_vector": list(normal_vector),
    }


def validate_view(value: str, *, command: str = "axis") -> AxisInfo:
    name = value.strip().lower()
    direction = VIEW_DIRECTIONS.get(name)
    if direction is None:
        raise InvalidArgument("view", value, VIEW_NAMES, command=command)
    return {
        "kind": "view",
        "name": name,
        "direction": _rounded(direction),
    }


def validate_camera(value: str, *, command: str = "axis") -> AxisInfo:
    raw_parts = [part.strip() for part in value.split(",")]
    if len(raw_parts) != 6 or any(part == "" for part in raw_parts):
        raise UsageError(
            "camera vector must have exactly 6 comma-separated numbers: ex,ey,ez,cx,cy,cz",
            command=command,
            remediation=["Example: 3d axis camera 1,-1,1,0,0,0"],
        )
    try:
        camera = [float(part) for part in raw_parts]
    except ValueError as exc:
        raise UsageError(
            "camera vector must contain only numbers",
            command=command,
            remediation=["Example: 3d axis camera 1,-1,1,0,0,0"],
        ) from exc
    if not all(math.isfinite(part) for part in camera):
        raise UsageError("camera vector values must be finite numbers", command=command)
    eye = camera[:3]
    center = camera[3:]
    direction = [round(center[i] - eye[i], 6) for i in range(3)]
    if all(part == 0.0 for part in direction):
        raise UsageError(
            "camera vector eye and center must differ",
            command=command,
            remediation=["Example: 3d axis camera 1,-1,1,0,0,0"],
        )
    return {
        "kind": "camera",
        "camera": camera,
        "eye": eye,
        "center": center,
        "direction": direction,
    }
