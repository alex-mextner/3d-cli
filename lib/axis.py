"""Compatibility wrapper for geometry.axis."""
from __future__ import annotations

from geometry.axis import (
    AXIS_ALIASES,
    AXIS_NAMES,
    PLANE_NAMES,
    PLANE_NORMALS,
    VIEW_DIRECTIONS,
    VIEW_NAMES,
    AxisInfo,
    JsonValue,
    validate_axis,
    validate_camera,
    validate_plane,
    validate_view,
)

__all__ = [
    "AXIS_ALIASES",
    "AXIS_NAMES",
    "PLANE_NAMES",
    "PLANE_NORMALS",
    "VIEW_DIRECTIONS",
    "VIEW_NAMES",
    "AxisInfo",
    "JsonValue",
    "validate_axis",
    "validate_camera",
    "validate_plane",
    "validate_view",
]
