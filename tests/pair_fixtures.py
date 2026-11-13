"""Synthetic threaded and ribbed parts (and the G-code that prints them) for the `3d assembly pair` tests.

A thread here is a surface of revolution whose radius follows a triangle wave of the helical phase
`u = z/pitch - hand * theta/2pi` (so a perfect single-start thread: one radius per phase), built as an exact watertight
triangle mesh. The numbers a test asserts are read off the construction: a bore profile that is the boss profile plus
`gap` at the same phase has radial gap `gap` at the aligned phase and a free fraction of `gap / amplitude` of a turn.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
import numpy.typing as npt
import shapely
import trimesh
from manifold3d import Manifold

Radius = Callable[
    [npt.NDArray[np.float64], npt.NDArray[np.float64]], npt.NDArray[np.float64]
]


def triangle_thread(
    base: float, amplitude: float, pitch: float, hand: int = 1, phase: float = 0.0
) -> Radius:
    """r(theta, z) = base + amplitude * tri(u), u = z/pitch - hand*theta/2pi + phase; tri: 0 at u=0, 1 at u=1/2."""

    def r(
        theta: npt.NDArray[np.float64], z: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.float64]:
        u = (z / pitch - hand * theta / (2 * math.pi) + phase) % 1.0
        return base + amplitude * (1.0 - np.abs(2.0 * u - 1.0))

    return r


def _grid(
    radius: Radius, z0: float, z1: float, n_theta: int, n_z: int
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    theta = np.linspace(0.0, 2 * math.pi, n_theta, endpoint=False)
    z = np.linspace(z0, z1, n_z)
    tt, zz = np.meshgrid(theta, z)  # rows: z, columns: theta
    r = radius(tt, zz)
    verts = np.column_stack(
        [(r * np.cos(tt)).ravel(), (r * np.sin(tt)).ravel(), zz.ravel()]
    )
    idx = np.arange(n_z * n_theta).reshape(n_z, n_theta)
    a, b = idx[:-1], idx[1:]
    a2, b2 = np.roll(a, -1, axis=1), np.roll(b, -1, axis=1)
    faces = np.concatenate(
        [
            np.column_stack([a.ravel(), a2.ravel(), b2.ravel()]),
            np.column_stack([a.ravel(), b2.ravel(), b.ravel()]),
        ]
    )
    return verts, faces


def threaded_boss(
    radius: Radius,
    z0: float = 0.0,
    z1: float = 9.0,
    *,
    n_theta: int = 240,
    steps_per_pitch: int = 12,
    pitch: float = 3.0,
) -> trimesh.Trimesh:
    """A solid with a threaded outer surface and flat ends."""
    n_z = max(2, round((z1 - z0) / pitch * steps_per_pitch) + 1)
    verts, faces = _grid(radius, z0, z1, n_theta, n_z)
    bottom, top = len(verts), len(verts) + 1
    verts = np.vstack([verts, [0, 0, z0], [0, 0, z1]])
    ring0 = np.arange(n_theta)
    ring1 = (n_z - 1) * n_theta + ring0
    cap0 = np.column_stack([np.full(n_theta, bottom), np.roll(ring0, -1), ring0])
    cap1 = np.column_stack([np.full(n_theta, top), ring1, np.roll(ring1, -1)])
    mesh = trimesh.Trimesh(verts, np.vstack([faces, cap0, cap1]), process=True)
    mesh.fix_normals()
    return mesh


def threaded_bore(
    radius: Radius,
    z0: float = 0.0,
    z1: float = 9.0,
    *,
    outer: float = 14.0,
    n_theta: int = 240,
    steps_per_pitch: int = 12,
    pitch: float = 3.0,
) -> trimesh.Trimesh:
    """A tube (outer radius `outer`, flat ends) whose bore wall follows `radius`."""
    n_z = max(2, round((z1 - z0) / pitch * steps_per_pitch) + 1)
    inner, faces = _grid(radius, z0, z1, n_theta, n_z)
    theta = np.linspace(0.0, 2 * math.pi, n_theta, endpoint=False)
    z = np.linspace(z0, z1, n_z)
    tt, zz = np.meshgrid(theta, z)
    shell = np.column_stack(
        [(outer * np.cos(tt)).ravel(), (outer * np.sin(tt)).ravel(), zz.ravel()]
    )
    verts = np.vstack([inner, shell])
    off = len(inner)
    ring0 = np.arange(n_theta)
    ring1 = (n_z - 1) * n_theta + ring0
    nxt = np.roll(ring0, -1)
    caps = [
        np.column_stack([ring0, nxt + off, ring0 + off]),
        np.column_stack([ring0, nxt, nxt + off]),
        np.column_stack([ring1, ring1 + off, np.roll(ring1, -1) + off]),
        np.column_stack([ring1, np.roll(ring1, -1) + off, np.roll(ring1, -1)]),
    ]
    mesh = trimesh.Trimesh(
        verts,
        np.vstack([faces, *caps, _grid_outer(n_theta, n_z, off)]),
        process=True,
    )
    mesh.fix_normals()
    return mesh


def _grid_outer(n_theta: int, n_z: int, off: int) -> npt.NDArray[np.int64]:
    idx = np.arange(n_z * n_theta).reshape(n_z, n_theta) + off
    a, b = idx[:-1], idx[1:]
    a2, b2 = np.roll(a, -1, axis=1), np.roll(b, -1, axis=1)
    return np.concatenate(
        [
            np.column_stack([a.ravel(), a2.ravel(), b2.ravel()]),
            np.column_stack([a.ravel(), b2.ravel(), b.ravel()]),
        ]
    )


def load(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load(path, force="mesh")
    assert isinstance(mesh, trimesh.Trimesh)
    return mesh


def write(mesh: trimesh.Trimesh, path: Path) -> Path:
    assert mesh.is_watertight and mesh.volume > 0, "fixture mesh must be a closed solid"
    mesh.export(path)
    return path


def thread_pair(
    tmp: Path,
    *,
    gap: float = 0.5,
    amplitude: float = 0.8,
    pitch: float = 3.0,
    boss_hand: int = 1,
    bore_hand: int = 1,
    bore_extra_phase: float = 0.0,
) -> tuple[Path, Path]:
    """(bore.stl, boss.stl): the bore is the boss's profile plus `gap` at the same phase (aligned = best phase)."""
    boss_r = triangle_thread(8.0, amplitude, pitch, boss_hand)
    bore_r = triangle_thread(8.0 + gap, amplitude, pitch, bore_hand, bore_extra_phase)
    bore = write(threaded_bore(bore_r, pitch=pitch), tmp / "bore.stl")
    boss = write(threaded_boss(boss_r, pitch=pitch), tmp / "boss.stl")
    return bore, boss


def write_stl(path: Path, solid: Manifold) -> Path:
    mesh = solid.to_mesh()
    trimesh.Trimesh(mesh.vert_properties[:, :3], mesh.tri_verts, process=False).export(
        path
    )
    return path


def ribbed_pair(
    tmp: Path, *, n: int = 6, rib_width: float = 2.0, groove_width: float = 2.6
) -> tuple[Path, Path]:
    """(bore.stl, boss.stl): a boss (r 10) with `n` radial ribs out to r 11 in a bore (r 10.8) with `n` grooves out to r 12."""
    height = 8.0
    boss = Manifold.cylinder(height, 10.0, 10.0, 96)
    groove_cut = Manifold.cylinder(height + 2, 10.8, 10.8, 96).translate((0, 0, -1))
    bore = Manifold.cylinder(height, 14.0, 14.0, 96) - groove_cut
    for k in range(n):
        ang = 360.0 * k / n
        rib = Manifold.cube((11.0, rib_width, height), False).translate(
            (0, -rib_width / 2, 0)
        )
        groove = Manifold.cube((12.0, groove_width, height + 2), False).translate(
            (0, -groove_width / 2, -1)
        )
        boss = boss + rib.rotate((0, 0, ang))
        bore = bore - groove.rotate((0, 0, ang))
    return write_stl(tmp / "rib_bore.stl", bore), write_stl(tmp / "rib_boss.stl", boss)


def _layer_polygons(mesh: trimesh.Trimesh, layer_h: float) -> list[list[shapely.Polygon]]:
    """The model's section polygons at the mid height of every layer (mesh z = 0 on the bed)."""
    from trimesh import intersections

    n_layers = round(float(mesh.bounds[1][2]) / layer_h)
    heights = (np.arange(n_layers) + 0.5) * layer_h
    lines, _to3d, _ = intersections.mesh_multiplane(mesh, np.zeros(3), np.array([0, 0, 1.0]), heights)
    return [list(trimesh.load_path(np.asarray(seg)).polygons_full) if len(seg) else [] for seg in lines]


def _object_block(
    name: str, polys: list[shapely.Polygon], center: tuple[float, float], grow: float, width: float
) -> list[str]:
    """One object's outer-wall lines of one layer, between its EXCLUDE_OBJECT markers."""
    slug = name.replace(" ", "_")
    cx, cy = center
    out = [f"EXCLUDE_OBJECT_START NAME={slug}_id_0_copy_0", ";TYPE:Outer wall", f";WIDTH:{width}"]
    for poly in polys:
        for ring in _wall_rings(poly, grow, width):
            pts = np.asarray(ring.coords)
            out.append(f"G1 X{pts[0][0] + cx:.4f} Y{pts[0][1] + cy:.4f} F3000")
            out += [f"G1 X{x + cx:.4f} Y{y + cy:.4f} E0.05" for x, y in pts[1:]]
    out.append(f"EXCLUDE_OBJECT_END NAME={slug}_id_0_copy_0")
    return out


def write_wall_gcode(
    path: Path,
    objects: list[tuple[str, trimesh.Trimesh, tuple[float, float]]],
    *,
    layer_h: float = 0.2,
    width: float = 0.42,
    grow: float = 0.0,
) -> Path:
    """An Orca-style G-code holding only the OUTER WALL lines of each `(name, mesh, (cx, cy))` object.

    The mesh is placed with its z = 0 on the bed and its axis at (cx, cy); each layer's wall centre line is the model
    section inset by half the line width. `grow` offsets the section outward first (a slicer that prints oversize).
    """
    out = ["; generated by tests/pair_fixtures.py", "M83"]
    per_object = [_layer_polygons(mesh, layer_h) for _n, mesh, _c in objects]
    for name, _m, (cx, cy) in objects:
        out.append(f"EXCLUDE_OBJECT_DEFINE NAME={name.replace(' ', '_')}_id_0_copy_0 CENTER={cx},{cy}")
    for layer in range(max(len(layers) for layers in per_object)):
        out += [";LAYER_CHANGE", f";Z:{(layer + 1) * layer_h:.3f}", f";HEIGHT:{layer_h}"]
        for (name, _m, center), layers in zip(objects, per_object, strict=True):
            if layer < len(layers):
                out += _object_block(name, layers[layer], center, grow, width)
    path.write_text("\n".join(out) + "\n")
    return path


def _wall_rings(poly: object, grow: float, width: float) -> list[shapely.LinearRing]:
    assert isinstance(poly, shapely.Polygon)
    inset = (
        poly.buffer(grow - width / 2.0, join_style="round")
        if grow - width / 2.0
        else poly
    )
    geoms = list(inset.geoms) if hasattr(inset, "geoms") else [inset]
    rings: list[shapely.LinearRing] = []
    for g in geoms:
        if g.is_empty:
            continue
        rings.append(g.exterior)
        rings.extend(g.interiors)
    return rings


def write_3mf(path: Path, objects: list[tuple[int, trimesh.Trimesh]]) -> Path:
    """A minimal 3MF (core spec, one build item per object, identity transform) the stdlib reader accepts."""
    import zipfile

    rows = []
    for oid, mesh in objects:
        verts = "".join(f'<vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in mesh.vertices)
        tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
        rows.append(
            f'<object id="{oid}" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
        )
    items = "".join(f'<item objectid="{oid}"/>' for oid, _ in objects)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{''.join(rows)}</resources><build>{items}</build></model>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", xml)
    return path
