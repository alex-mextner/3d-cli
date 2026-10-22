"""Watertight mesh (mm) from a fit: `emit_mesh(fit, 'glb'|'stl'|'obj'|'scad') -> bytes`.

The mesh is the fit's loft at export resolution (24-gon sections): outward-facing, closed, so its extents are exactly
`fit['dims_mm']` (length along X, full width along Y, full thickness along Z in the default OBJECT frame, centred at the origin).
`frame='world'` bakes the fit pose in (the frame of the view cameras). glTF has no unit field: the numbers are millimetres
(1 unit = 1 mm), the same as STL/OBJ/OpenSCAD. Needs `trimesh` for glb/stl/obj (the `fit` extra); `scad` is plain text.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import BadInput, MissingDependency
from .loft import EXPORT_RING, local_mesh
from .render import mesh_from_fit
from .view_check import validate_fit

FORMATS = ("glb", "stl", "obj", "scad")


def export_mesh(
    fit: dict[str, Any], frame: str = "object", ring: int = EXPORT_RING
) -> tuple[np.ndarray, np.ndarray]:
    """(V, F) float64/int64, outward-facing triangles (positive volume), in the object or world frame."""
    validate_fit(fit)
    if frame not in ("object", "world"):
        raise BadInput(
            f"unknown frame {frame!r}", remediation="use 'object' or 'world'"
        )
    V, F = mesh_from_fit(fit, ring) if frame == "world" else local_mesh(fit, ring)
    tri = V[F]
    volume6 = float(
        np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum()
    )
    if (
        volume6 < 0
    ):  # keep the winding convention independent of the template's vertex order
        F = F[:, ::-1].copy()
    return V, F


def _scad(V: np.ndarray, F: np.ndarray) -> bytes:
    pts = ",\n  ".join(f"[{x:.6f}, {y:.6f}, {z:.6f}]" for x, y, z in V)
    # OpenSCAD wants faces listed clockwise seen from outside: reverse our counter-clockwise triangles
    faces = ",\n  ".join(f"[{a}, {c}, {b}]" for a, b, c in F)
    return f"// threed_vision fit, millimetres\npolyhedron(\n points = [\n  {pts}\n ],\n faces = [\n  {faces}\n ],\n convexity = 6\n);\n".encode()


def emit_mesh(fit: dict[str, Any], fmt: str = "glb", *, frame: str = "object") -> bytes:
    """Serialise the fit as `fmt` in {glb, stl (binary), obj, scad}."""
    if fmt not in FORMATS:
        raise BadInput(
            f"unknown mesh format {fmt!r}", remediation=f"use one of {list(FORMATS)}"
        )
    V, F = export_mesh(fit, frame)
    if fmt == "scad":
        return _scad(V, F)
    try:
        import trimesh
    except ImportError as exc:
        raise MissingDependency(
            "trimesh", remediation="python -m pip install 'threed-vision[fit]'"
        ) from exc
    mesh = trimesh.Trimesh(vertices=V, faces=F, process=False)
    out = mesh.export(file_type=fmt)
    return out.encode() if isinstance(out, str) else bytes(out)
