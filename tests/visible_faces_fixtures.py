"""Synthetic parts and G-code for `3d visible-faces` tests.

Standing slabs 40 (x) x 5 (y) x 30 (z) mm in print pose, built from (y, z) profiles extruded
along x with manifold3d; the visible side is y < 2.5 (the outer half). Hand-written G-code
plates print one rectangular object over a one-layer support raft.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

import numpy as np

SLAB_X, SLAB_Y, SLAB_Z = 40.0, 5.0, 30.0
VISIBLE = "y<2.5"


def _prism(profile: list[tuple[float, float]], x0: float = 0.0, length: float = SLAB_X) -> Any:
    """solid from a (y, z) profile extruded along x from x0"""
    from manifold3d import CrossSection, Manifold
    pts = np.asarray(profile, float)
    area = 0.5 * float(np.sum(pts[:, 0] * np.roll(pts[:, 1], -1) - np.roll(pts[:, 0], -1) * pts[:, 1]))
    if area < 0:
        pts = pts[::-1]
    solid = Manifold.extrude(CrossSection([pts.tolist()]), length)
    # (u, v, w) = (y, z, x) -> (x, y, z): a cyclic permutation (det +1)
    perm = np.array([[0, 0, 1, x0], [1, 0, 0, 0], [0, 1, 0, 0]], float)
    return solid.transform(perm)


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> Any:
    from manifold3d import Manifold
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def slab() -> Any:
    return box(0, 0, 0, SLAB_X, SLAB_Y, SLAB_Z)


def ledge_slab(angle_deg: float, side: str = "visible", depth: float = 2.0, z_base: float = 10.0) -> Any:
    """slab with a ledge sticking out `depth` mm; its underside overhangs `angle_deg` from vertical"""
    rise = depth / math.tan(math.radians(angle_deg)) if angle_deg < 89.99 else 0.0
    top = z_base + rise + 3.0
    if side == "visible":   # out of the y = 0 face
        prof = [(0, 0), (SLAB_Y, 0), (SLAB_Y, SLAB_Z), (0, SLAB_Z), (0, top), (-depth, top),
                (-depth, z_base + rise), (0, z_base)]
    else:                   # out of the y = SLAB_Y face
        prof = [(0, 0), (SLAB_Y, 0), (SLAB_Y, z_base), (SLAB_Y + depth, z_base + rise), (SLAB_Y + depth, top),
                (SLAB_Y, top), (SLAB_Y, SLAB_Z), (0, SLAB_Z)]
    return _prism(prof)


def window_slab(width: float, z0: float = 10.0, z1: float = 20.0, bar: tuple[float, float] = (14.0, 15.0)) -> Any:
    """slab with a through window `width` mm wide centred in x, crossed by one horizontal bar"""
    xa = (SLAB_X - width) / 2
    hole = box(xa, -1, z0, xa + width, SLAB_Y + 1, z1)
    return (slab() - hole) + box(xa - 0.01, 0, bar[0], xa + width + 0.01, SLAB_Y, bar[1])


def window_opening(width: float, z0: float = 10.0, z1: float = 20.0) -> list[float]:
    xa = (SLAB_X - width) / 2
    return [xa, xa + width, z0, z1]


def mesh_arrays(solid: Any) -> tuple[np.ndarray, np.ndarray]:
    m = solid.to_mesh()
    return np.asarray(m.vert_properties, float)[:, :3], np.asarray(m.tri_verts, np.int64)


def write_stl(path: Path, solid: Any) -> Path:
    V, F = mesh_arrays(solid)
    tri = V[F]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    out = bytearray(b"\0" * 80) + struct.pack("<I", len(F))
    for k in range(len(F)):
        out += struct.pack("<12fH", *n[k], *tri[k].ravel(), 0)
    path.write_bytes(bytes(out))
    return path


IDENTITY_AT_100 = "1 0 0 0 1 0 0 0 1 100 100 0"
ROT90_AT_100 = "0 1 0 -1 0 0 0 0 1 100 100 0"      # 3MF row-vector form: (x, y) -> (100 - y, 100 + x)


def write_3mf(path: Path, objects: list[tuple[str, list[Any]]], transforms: list[str] | None = None,
              unit: str = "millimeter", per_mm: float = 1.0) -> Path:
    """minimal 3MF: one object per entry, each a set of mesh components (colour volumes);
    build items at `transforms` (default: moved to 100, 100), coordinates written as mm x per_mm"""
    import zipfile
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    res, build, oid = [], [], 1
    for k, (name, volumes) in enumerate(objects):
        comp_ids = []
        for vol in volumes:
            V, F = mesh_arrays(vol)
            V = V * per_mm
            vs = "".join(f'<vertex x="{a:.6f}" y="{b:.6f}" z="{c:.6f}"/>' for a, b, c in V)
            ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F)
            res.append(f'<object id="{oid}" type="model"><mesh><vertices>{vs}</vertices>'
                       f"<triangles>{ts}</triangles></mesh></object>")
            comp_ids.append(oid)
            oid += 1
        comps = "".join(f'<component objectid="{c}"/>' for c in comp_ids)
        res.append(f'<object id="{oid}" name="{name}" type="model"><components>{comps}</components></object>')
        tf = [float(t) for t in (transforms[k] if transforms else IDENTITY_AT_100).split()]
        tf[9:] = [t * per_mm for t in tf[9:]]
        build.append(f'<item objectid="{oid}" transform="{" ".join(f"{t:g}" for t in tf)}"/>')
        oid += 1
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="{unit}" xmlns="{core}">'
           f'<resources>{"".join(res)}</resources><build>{"".join(build)}</build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", xml)
    return path


Rect = tuple[float, float, float, float]


def _loop(r: Rect) -> list[str]:
    x0, y0, x1, y1 = r
    return [f"G1 X{x0:.3f} Y{y0:.3f} F9000", f"G1 X{x1:.3f} Y{y0:.3f} E0.5", f"G1 X{x1:.3f} Y{y1:.3f} E0.1",
            f"G1 X{x0:.3f} Y{y1:.3f} E0.5", f"G1 X{x0:.3f} Y{y0:.3f} E0.1"]


def gcode(layers: list[Rect], name: str = "SLAB", layer: float = 0.12,
          support: dict[int, tuple[float, float, float, float]] | None = None) -> str:
    """an Orca-style plate: one raft layer of support, then one perimeter loop per layer;
    `support[i]` adds a support line (x0, y0, x1, y1) on object layer i"""
    support = support or {}
    label = f"{name}_id_0_copy_0"
    out = ["; generated by visible_faces_fixtures", "M83", ";TYPE:Custom", "G1 X-5 Y-5 E2 ; purge"]
    z = 0.2
    out += [f";Z:{z:.3f}", f"G1 Z{z:.3f}", f"EXCLUDE_OBJECT_START NAME={label}", ";TYPE:Support",
            "G1 X-1 Y-1 F9000", "G1 X41 Y-1 E1", "G1 X41 Y6 E0.3", "G1 X-1 Y6 E1", "EXCLUDE_OBJECT_END NAME=" + label]
    for i, r in enumerate(layers):
        z += layer
        out += [f";Z:{z:.3f}", f"G1 Z{z:.3f}", f"EXCLUDE_OBJECT_START NAME={label}", ";TYPE:Outer wall", *_loop(r)]
        if i in support:
            sx0, sy0, sx1, sy1 = support[i]
            out += [";TYPE:Support", f"G1 X{sx0} Y{sy0} F9000", f"G1 X{sx1} Y{sy1} E0.3"]
        out.append(f"EXCLUDE_OBJECT_END NAME={label}")
    return "\n".join(out) + "\n"


def slab_layers(n: int = 3, step: float = 0.0, side: str = "visible") -> list[Rect]:
    """n footprints of the slab; each layer steps `step` mm further out on one side"""
    rects = []
    for i in range(n):
        d = step * i
        rects.append((0.0, -d, SLAB_X, SLAB_Y) if side == "visible" else (0.0, 0.0, SLAB_X, SLAB_Y + d))
    return rects
