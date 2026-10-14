"""Synthetic standing parts for the `3d props` tests.

Boxes and a ribbed panel built with manifold3d in the print frame (z up, thickness along
y), plus writers for the inputs a user has: a binary STL, and an Orca/Bambu-style 3MF whose
object keeps its colour parts as components in 3D/Objects/*.model (production extension)
with per-part extruders in Metadata/model_settings.config.
"""
from __future__ import annotations

import struct
import zipfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from manifold3d import Manifold
from standing_props import mesh_arrays

Mesh = tuple[np.ndarray, np.ndarray]


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


arrays = mesh_arrays


def ribbed_panel() -> Manifold:
    """a 100 x 2 x 60 front plate with two 4 mm deep horizontal ribs on its +y back
    (z 10-12 and 30-32): hollow cells between them, hindmost back point at y = 6"""
    plate = box(0, 0, 0, 100, 2, 60)
    return plate + box(0, 2, 10, 100, 6, 12) + box(0, 2, 30, 100, 6, 32)


def write_stl(path: Path, m: Manifold) -> None:
    V, F = arrays(m)
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(F)))
        for tri in F:
            a, b, c = V[tri]
            n = np.cross(b - a, c - a)
            n = n / (np.linalg.norm(n) or 1.0)
            f.write(struct.pack("<12fH", *n, *a, *b, *c, 0))


_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _mesh_xml(oid: int, mesh: Mesh) -> str:
    V, F = mesh
    vs = "".join(f'<vertex x="{float(x)!r}" y="{float(y)!r}" z="{float(z)!r}"/>' for x, y, z in V)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F)
    return f'<object id="{oid}" type="model"><mesh><vertices>{vs}</vertices><triangles>{ts}</triangles></mesh></object>'


def write_orca_3mf(path: Path, objects: Sequence[tuple[str, Sequence[tuple[str, Manifold, int]]]],
                   offsets: Sequence[tuple[float, float]] | None = None, *, sub_unit: str = "millimeter",
                   unprintable: Sequence[str] = ()) -> None:
    """objects: (object name, [(part name, mesh, extruder), ...]); each object goes to its own
    3D/Objects/object_N.model, placed by a build-item translation (offsets, default 0, in mm).
    sub_unit: the unit of the object files (meshes are scaled into it; the root stays mm);
    unprintable: object names whose build item has printable="0"."""
    offsets = offsets or [(0.0, 0.0)] * len(objects)
    per_mm = 1.0 / {"millimeter": 1.0, "centimeter": 10.0, "inch": 25.4}[sub_unit]
    root_res, items, cfg = [], [], ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    files: dict[str, str] = {}
    pid = 1
    for n, ((name, parts), (dx, dy)) in enumerate(zip(objects, offsets), start=1):
        sub = f"3D/Objects/object_{n}.model"
        meshes, comps, part_cfg = [], [], []
        for pname, m, ext in parts:
            meshes.append(_mesh_xml(pid, arrays(m.scale((per_mm, per_mm, per_mm)))))
            comps.append(f'<component p:path="/{sub}" objectid="{pid}"/>')
            part_cfg.append(f'<part id="{pid}" subtype="normal_part"><metadata key="name" value="{pname}"/>'
                            f'<metadata key="extruder" value="{ext}"/></part>')
            pid += 1
        files[sub] = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="{sub_unit}" xmlns="{_CORE}">'
                      f'<resources>{"".join(meshes)}</resources><build/></model>')
        oid = 1000 + n
        root_res.append(f'<object id="{oid}" type="model"><components>{"".join(comps)}</components></object>')
        printable = 0 if name in unprintable else 1
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {dx} {dy} 0" printable="{printable}"/>')
        cfg.append(f'<object id="{oid}"><metadata key="name" value="{name}"/>'
                   f'<metadata key="extruder" value="{parts[0][2]}"/>{"".join(part_cfg)}</object>')
    cfg.append("</config>")
    root = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{_CORE}" xmlns:p="{_PROD}">'
            f'<resources>{"".join(root_res)}</resources><build>{"".join(items)}</build></model>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        zf.writestr("3D/3dmodel.model", root)
        for p, text in files.items():
            zf.writestr(p, text)
        zf.writestr("Metadata/model_settings.config", "\n".join(cfg))
