"""Synthetic split-in-two parts for the `3d halves-check` tests.

A "half" here is a flat-cut solid: a 2D cut-face outline (with round dowel holes) extruded
away from the cut plane y = 0 of the assembly frame. Side 1 is material on +y, side 2 on -y,
exactly the convention `halves_check` checks. `write_orca_3mf` lays the halves out the way
an Orca/Bambu project stores them (component objects in 3D/Objects/*.model, a build item per
object, per-object metadata in Metadata/model_settings.config) after turning each half into
its print pose: lying on its cut face on the bed.
"""
from __future__ import annotations

import zipfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from manifold3d import CrossSection, FillRule, Manifold

Mesh = tuple[np.ndarray, np.ndarray]

# a small "spruce" cut face (x, z) in mm: tiers sticking out left and right
TREE: list[tuple[float, float]] = [
    (-2, 0), (2, 0), (2, 6), (16, 6), (8, 18), (13, 18), (6, 30), (10, 30), (0, 46),
    (-10, 30), (-6, 30), (-13, 18), (-8, 18), (-16, 6), (-2, 6),
]
# the same face with the lowest right tier 3 mm longer: what an unmatched pair looks like
TREE_LONG_TIER: list[tuple[float, float]] = [(19.0 if (x, z) == (16, 6) else x, z) for x, z in TREE]
DOWELS: list[tuple[float, float]] = [(0.0, 12.0), (0.0, 26.0)]
DOWEL_D = 3.0


def _face(outline: Sequence[tuple[float, float]], dowels: Sequence[tuple[float, float]], flip: bool) -> CrossSection:
    sz = -1.0 if flip else 1.0
    cs = CrossSection([[(x, sz * z) for x, z in outline]], FillRule.NonZero)
    for x, z in dowels:
        cs = cs - CrossSection.circle(DOWEL_D / 2, 32).translate((x, sz * z))
    return cs


def half_mesh(side: int, outline: Sequence[tuple[float, float]] = TREE,
              dowels: Sequence[tuple[float, float]] = DOWELS, thickness: float = 6.0) -> Mesh:
    """One half in the assembly frame: cut face on y = 0, material on +y (side 1) or -y (side 2)."""
    if side == 1:          # rotate x by -90: (x, y2d, e) -> (x, e, -y2d), so the face is drawn with z negated
        solid = Manifold.extrude(_face(outline, dowels, flip=True), thickness).rotate((-90, 0, 0))
    else:                  # rotate x by +90: (x, y2d, e) -> (x, -e, y2d)
        solid = Manifold.extrude(_face(outline, dowels, flip=False), thickness).rotate((90, 0, 0))
    mesh = solid.to_mesh()
    return np.asarray(mesh.vert_properties[:, :3], float), np.asarray(mesh.tri_verts, np.int64)


def print_pose(side: int, at: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    """assembly -> plate: lay the half on its cut face (material up), footprint moved to `at`."""
    R = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float) if side == 1 else \
        np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float)
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = (at[0], at[1], 0.0)
    return M


def apply(M: np.ndarray, mesh: Mesh) -> Mesh:
    V, F = mesh
    return V @ M[:3, :3].T + M[:3, 3], F


def split_by_height(mesh: Mesh, z: float) -> list[Mesh]:
    """two colour parts of one half sharing a seam plane (they are checked as one body)"""
    V, F = mesh
    from manifold3d import Mesh as MMesh

    m = Manifold(MMesh(vert_properties=np.asarray(V, np.float32), tri_verts=np.asarray(F, np.uint32)))
    lo, hi = m.split_by_plane((0, 0, 1), z)
    out = []
    for part in (hi, lo):
        pm = part.to_mesh()
        out.append((np.asarray(pm.vert_properties[:, :3], float), np.asarray(pm.tri_verts, np.int64)))
    return out


def _fmt(M: np.ndarray) -> str:
    return " ".join(f"{v:.9g}" for v in M.reshape(-1))


def _mesh_xml(oid: int, V: np.ndarray, F: np.ndarray) -> str:
    vs = "".join(f'<vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in V)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F)
    return f'<object id="{oid}" type="model"><mesh><vertices>{vs}</vertices><triangles>{ts}</triangles></mesh></object>'


def _tr(t: Sequence[float]) -> str:
    return f"1 0 0 0 1 0 0 0 1 {t[0]:.6f} {t[1]:.6f} {t[2]:.6f}"


def halves_metadata(pair: str, side: int, local_to_asm: np.ndarray, local_min: Sequence[float],
                    cut: Sequence[float] = (0.0, 1.0, 0.0, 0.0), dowels: int | None = 2) -> dict[str, str]:
    meta = {
        "halves_pair": pair,
        "halves_side": str(side),
        "halves_to_assembly": _fmt(local_to_asm),
        "halves_local_min": " ".join(f"{v:.6f}" for v in local_min),
        "halves_cut": " ".join(f"{v:.9g}" for v in cut),
    }
    if dowels is not None:
        meta["halves_dowels"] = str(dowels)
    return meta


def write_orca_3mf(path: Path, objects: Sequence[dict[str, object]], unit: str = "millimeter") -> Path:
    """objects: {name, parts: [Mesh in plate coords, mm], meta: {key: value} | absent}; `unit` = the
    3MF model unit the coordinates are written in (metadata stays in mm).

    Each object's local frame is its plate coordinates minus the footprint centre (the
    build item translation), as Orca writes it."""
    k = {"millimeter": 1.0, "inch": 25.4}[unit]
    comps, cfg, build, files = [], [], [], {}
    pid = 1
    for oi, obj in enumerate(objects):
        parts = obj["parts"]
        assert isinstance(parts, list)
        allV = np.vstack([V for V, _ in parts])
        lo, hi = allV.min(0), allV.max(0)
        oc = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
        chunks, cc = [], []
        for V, F in parts:
            pc = (V.min(0) + V.max(0)) / 2
            chunks.append(_mesh_xml(pid, (V - pc) / k, F))
            cc.append(f'<component p:path="/3D/Objects/object_{oi + 1}.model" objectid="{pid}" transform="{_tr(((pc - oc) / k).tolist())}"/>')
            pid += 1
        files[f"3D/Objects/object_{oi + 1}.model"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><model unit="{unit}" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
            + "".join(chunks) + "</resources><build/></model>")
        oid = 1000 + oi
        comps.append(f'<object id="{oid}" type="model"><components>{"".join(cc)}</components></object>')
        build.append(f'<item objectid="{oid}" transform="{_tr((oc / k).tolist())}" printable="1"/>')
        meta = obj.get("meta") or {}
        assert isinstance(meta, dict)
        md = "".join(f'<metadata key="{k}" value="{v}"/>' for k, v in meta.items())
        cfg.append(f'<object id="{oid}"><metadata key="name" value="{obj["name"]} — half"/>{md}</object>')
    model = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="{unit}" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
             'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">'
             f'<resources>{"".join(comps)}</resources><build>{"".join(build)}</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", model)
        for name, text in files.items():
            zf.writestr(name, text)
        zf.writestr("Metadata/model_settings.config", f'<?xml version="1.0" encoding="UTF-8"?><config>{"".join(cfg)}</config>')
    return path


def pair_objects(pair: str, mesh1: Mesh, mesh2: Mesh, *, meta: bool = True,
                 colour_split: bool = False) -> list[dict[str, object]]:
    """Both halves in print pose side by side, with (or without) the pair metadata."""
    objs: list[dict[str, object]] = []
    for side, mesh, at in ((1, mesh1, (60.0, 60.0)), (2, mesh2, (140.0, 60.0))):
        P = print_pose(side, at)
        placed = apply(P, mesh)
        parts = split_by_height(placed, 3.0) if colour_split else [placed]
        allV = np.vstack([V for V, _ in parts])
        lo, hi = allV.min(0), allV.max(0)
        oc = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
        T = np.eye(4)
        T[:3, 3] = oc
        local_to_asm = np.linalg.inv(P) @ T          # local -> plate -> assembly
        obj: dict[str, object] = {"name": f"{pair}{side}", "parts": parts}
        if meta:
            obj["meta"] = halves_metadata(pair, side, local_to_asm, allV.min(0) - oc)
        objs.append(obj)
    return objs


def write_stl(path: Path, mesh: Mesh) -> Path:
    import trimesh

    trimesh.Trimesh(mesh[0], mesh[1], process=False).export(path)
    return path

