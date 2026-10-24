"""Synthetic Orca-family project 3MFs with real geometry, for the mix-face check tests.

The layout mirrors what Snapmaker Orca writes: one sub-model per object under 3D/Objects/ holding
the part meshes, a main-model object made of components (one per part, with a transform), build
items with a transform, and Metadata/model_settings.config naming objects and per-part extruders
(part id = the component's object id).
"""

from __future__ import annotations

import json
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
)
# 4 physical filaments; virtual 5 = manual pattern "431"; virtual 6 = ratio (Local-Z) row.
MIXED_ROWS = (
    "1,4,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,431;"
    "1,4,1,1,25,0,g,w,m2,z0,xa0,xb0,d0,o0,u702,cm0"
)

Mesh = tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]
Rot = tuple[
    tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]
]
IDENTITY: Rot = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def rot_x(deg: float) -> Rot:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def _outward(
    v: list[tuple[float, float, float]], f: list[tuple[int, int, int]]
) -> Mesh:
    """Wind every triangle so its normal points away from the centroid (convex meshes)."""
    c = [sum(p[k] for p in v) / len(v) for k in range(3)]
    out = []
    for a, b, t in f:
        pa, pb, pc = v[a], v[b], v[t]
        u = [pb[k] - pa[k] for k in range(3)]
        w = [pc[k] - pa[k] for k in range(3)]
        n = (
            u[1] * w[2] - u[2] * w[1],
            u[2] * w[0] - u[0] * w[2],
            u[0] * w[1] - u[1] * w[0],
        )
        mid = [(pa[k] + pb[k] + pc[k]) / 3 - c[k] for k in range(3)]
        out.append((a, b, t) if sum(n[k] * mid[k] for k in range(3)) > 0 else (a, t, b))
    return v, out


def box(w: float, d: float, h: float) -> Mesh:
    """Closed box centred on the origin, outward-facing triangles."""
    x, y, z = w / 2, d / 2, h / 2
    v = [
        (-x, -y, -z),
        (x, -y, -z),
        (x, y, -z),
        (-x, y, -z),
        (-x, -y, z),
        (x, -y, z),
        (x, y, z),
        (-x, y, z),
    ]
    f = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    return _outward(v, f)


def gable(length: float, width: float, height: float) -> Mesh:
    """Roof prism along X: 45° slopes when height = width / 2."""
    x, y = length / 2, width / 2
    v = [
        (-x, -y, 0.0),
        (-x, y, 0.0),
        (-x, 0.0, height),
        (x, -y, 0.0),
        (x, y, 0.0),
        (x, 0.0, height),
    ]
    f = [
        (0, 1, 2),
        (3, 5, 4),
        (0, 2, 5),
        (0, 5, 3),
        (1, 4, 5),
        (1, 5, 2),
        (0, 3, 4),
        (0, 4, 1),
    ]
    return _outward(v, f)


def transform(
    r: Rot = IDENTITY, t: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> str:
    """3MF transform of p' = R·p + t (3MF multiplies a row vector, so it stores Rᵀ)."""
    m = [
        r[0][0],
        r[1][0],
        r[2][0],
        r[0][1],
        r[1][1],
        r[2][1],
        r[0][2],
        r[1][2],
        r[2][2],
        *t,
    ]
    return " ".join(f"{x:.9g}" for x in m)


@dataclass
class Part:
    name: str
    mesh: Mesh
    extruder: int  # 0 = inherit the object's
    rot: Rot = IDENTITY
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    subtype: str = "normal_part"  # Orca part subtype: modifier_part, support_blocker, …


@dataclass
class Obj:
    name: str
    parts: list[Part]
    extruder: int = 1
    rot: Rot = IDENTITY
    pos: tuple[float, float, float] = (100.0, 100.0, 0.0)
    settings: dict[str, str] = field(default_factory=dict)


def _mesh_xml(oid: int, mesh: Mesh) -> str:
    vs = "".join(
        f'<vertex x="{a:.6f}" y="{b:.6f}" z="{c:.6f}"/>' for a, b, c in mesh[0]
    )
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh[1])
    return f'<object id="{oid}" type="model"><mesh><vertices>{vs}</vertices><triangles>{ts}</triangles></mesh></object>'


def orca_project(
    path: Path,
    objects: list[Obj],
    *,
    mixed: str = MIXED_ROWS,
    n_phys: int = 4,
    settings: dict[str, object] | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    """`settings` are merged into project_settings.config, `files` are extra zip entries."""
    cfg: dict[str, object] = {
        "filament_colour": ["#000000", "#808080", "#E8732A", "#F6C700"][:n_phys]
        + ["#FFFFFF"] * (n_phys - 4),
        "mixed_filament_definitions": mixed,
        "raft_layers": "0",
        "layer_height": "0.12",
        **(settings or {}),
    }
    resources, build, object_settings = [], [], []
    subs: dict[str, str] = {}
    pid = 1
    for i, obj in enumerate(objects):
        sub = f"3D/Objects/object_{i + 1}.model"
        meshes, comps, parts_xml = [], [], []
        for part in obj.parts:
            meshes.append(_mesh_xml(pid, part.mesh))
            comps.append(
                f'<component p:path="/{sub}" objectid="{pid}" transform="{transform(part.rot, part.offset)}"/>'
            )
            parts_xml.append(
                f'<part id="{pid}" subtype="{part.subtype}"><metadata key="name" value="{part.name}"/>'
                f'<metadata key="extruder" value="{part.extruder}"/></part>'
            )
            pid += 1
        subs[sub] = (
            f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {NS}><resources>{"".join(meshes)}</resources><build/></model>'
        )
        oid = 1000 + i
        resources.append(
            f'<object id="{oid}" type="model"><components>{"".join(comps)}</components></object>'
        )
        build.append(
            f'<item objectid="{oid}" transform="{transform(obj.rot, obj.pos)}" printable="1"/>'
        )
        extra = "".join(
            f'<metadata key="{k}" value="{v}"/>' for k, v in obj.settings.items()
        )
        object_settings.append(
            f'<object id="{oid}"><metadata key="name" value="{obj.name}"/>'
            f'<metadata key="extruder" value="{obj.extruder}"/>{extra}{"".join(parts_xml)}</object>'
        )
    main = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {NS}><resources>{"".join(resources)}'
        f"</resources><build>{''.join(build)}</build></model>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", main)
        for name, text in subs.items():
            zf.writestr(name, text)
        zf.writestr("Metadata/project_settings.config", json.dumps(cfg))
        zf.writestr(
            "Metadata/model_settings.config",
            f'<?xml version="1.0" encoding="UTF-8"?><config>{"".join(object_settings)}</config>',
        )
        for name, text in (files or {}).items():
            zf.writestr(name, text)
    return path


def tile(
    name: str, extruder: int, *, standing: bool = False, rot: Rot = IDENTITY
) -> Obj:
    """A 20×20×3 mm tile lying flat, or the same tile standing (20 wide, 3 deep, 20 tall)."""
    mesh = box(20, 3, 20) if standing else box(20, 20, 3)
    return Obj(name, [Part(name, mesh, extruder)], extruder=extruder, rot=rot)


def with_model_colour(path: Path, colour: str) -> Path:
    """Give the project's first object (id 1000) a 3MF basematerials display colour `#RRGGBB`,
    as a CAD export would carry it."""
    with zipfile.ZipFile(path) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    main = entries["3D/3dmodel.model"].decode()
    main = main.replace(
        "<resources>",
        f'<resources><basematerials id="9"><base name="part" displaycolor="{colour}FF"/></basematerials>',
        1,
    ).replace(
        '<object id="1000" type="model">',
        '<object id="1000" pid="9" pindex="0" type="model">',
        1,
    )
    entries["3D/3dmodel.model"] = main.encode()
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path
