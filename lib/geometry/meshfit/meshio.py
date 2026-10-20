"""meshio.py — load a mesh canonically and write byte-reproducible STL / 3MF.

WHAT: `load_mesh` returns welded float64 vertices that are exactly float32-representable (an STL or a
  3MF is float32 anyway, so keep-out vertices survive a save/load round trip bit-for-bit) and int64
  faces. `save_mesh` writes binary STL with a fixed header and a deterministic 3MF (fixed zip
  timestamps, no random ids); .obj / .ply go through trimesh (not byte-guaranteed).

WHY: "same input -> identical SHA-256" has to hold for the file, not only for the arrays; trimesh's
  own exporters stamp headers and zip times, so the two formats a printer pipeline needs are written here.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import InputNotFound, UsageError

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]

STL_HEADER = b"3d-cli mesh deform".ljust(80, b"\0")
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
OUTPUT_SUFFIXES = (".stl", ".3mf", ".obj", ".ply")


def _read_3mf(path: str) -> tuple[Array, IntArray]:
    """Mesh objects of a 3MF's build items (item transforms applied; component trees are not followed)."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".model")]
        if not names:
            raise ValueError("no .model part in the 3MF")
        root = ET.fromstring(
            zf.read(sorted(names, key=lambda n: ("3dmodel" not in n.lower(), n))[0])
        )
    ns = {"m": root.tag.split("}")[0].strip("{")}
    meshes: dict[str, tuple[Array, IntArray]] = {}
    for obj in root.iterfind(".//m:object", ns):
        mesh = obj.find("m:mesh", ns)
        if mesh is None:
            continue
        verts = np.array(
            [
                [float(v.get(k, 0.0)) for k in "xyz"]
                for v in mesh.iterfind("m:vertices/m:vertex", ns)
            ],
            dtype=np.float64,
        ).reshape(-1, 3)
        tris = np.array(
            [
                [int(t.get(k, 0)) for k in ("v1", "v2", "v3")]
                for t in mesh.iterfind("m:triangles/m:triangle", ns)
            ],
            dtype=np.int64,
        ).reshape(-1, 3)
        meshes[str(obj.get("id"))] = (verts, tris)
    parts: list[tuple[Array, IntArray]] = []
    items = list(root.iterfind(".//m:build/m:item", ns)) or []
    for item in items:
        found = meshes.get(str(item.get("objectid")))
        if found is None:
            continue
        verts, tris = found
        if item.get("transform"):
            m = np.array(item.get("transform", "").split(), dtype=np.float64).reshape(
                4, 3
            )
            verts = verts @ m[:3] + m[3]
        parts.append((verts, tris))
    if not items:
        parts = list(meshes.values())
    if not parts:
        raise ValueError("no mesh object in the 3MF")
    offsets = np.cumsum([0] + [len(v) for v, _ in parts[:-1]])
    V = np.concatenate([v for v, _ in parts])
    F = np.concatenate([t + o for (_, t), o in zip(parts, offsets, strict=True)])
    return V, F


def load_mesh(path: str) -> tuple[Array, IntArray]:
    import trimesh

    if not os.path.isfile(path):
        raise InputNotFound(path, command="mesh deform")
    try:
        if path.lower().endswith(".3mf"):
            V0, F0 = _read_3mf(path)
            loaded: Any = trimesh.Trimesh(V0, F0, process=True)
        else:
            loaded = trimesh.load(path, force="mesh", process=True)
    except (ValueError, OSError, zipfile.BadZipFile, KeyError) as err:
        raise UsageError(
            f"cannot read mesh {path}: {err}", command="mesh deform"
        ) from err
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise UsageError(f"{path} contains no triangles", command="mesh deform")
    mesh = loaded
    mesh.remove_unreferenced_vertices()
    V = (
        np.asarray(mesh.vertices, dtype=np.float64)
        .astype(np.float32)
        .astype(np.float64)
    )
    return V, np.asarray(mesh.faces, dtype=np.int64)


def stl_bytes(V: Array, F: IntArray) -> bytes:
    tri = V[F].astype(np.float32)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n = (n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)).astype(
        np.float32
    )
    rec = np.zeros(
        len(F), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    )
    rec["n"], rec["v"] = n, tri
    return STL_HEADER + np.uint32(len(F)).tobytes() + rec.tobytes()


def threemf_bytes(V: Array, F: IntArray) -> bytes:
    verts = "".join(
        f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>'
        for x, y, z in V.astype(np.float32).tolist()
    )
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F.tolist())
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object></resources>'
        '<build><item objectid="1"/></build></model>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", rels),
            ("3D/3dmodel.model", model),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def save_mesh(path: str, V: Array, F: IntArray) -> str:
    """Write the mesh; returns the SHA-256 of the written bytes."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in OUTPUT_SUFFIXES:
        raise UsageError(
            f"unsupported output format {suffix!r}; use one of {', '.join(OUTPUT_SUFFIXES)}",
            command="mesh deform",
        )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if suffix in (".stl", ".3mf"):
        data = stl_bytes(V, F) if suffix == ".stl" else threemf_bytes(V, F)
        with open(path, "wb") as fh:
            fh.write(data)
    else:
        import trimesh

        trimesh.Trimesh(V, F, process=False).export(path)
    return file_sha256(path)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mesh_sha256(V: Array, F: IntArray) -> str:
    """Hash of the in-memory mesh as the STL writer would emit it (format-independent determinism probe)."""
    return hashlib.sha256(stl_bytes(V, F)).hexdigest()


def describe(V: Array, F: IntArray) -> dict[str, Any]:
    from . import meshmath

    return {
        "vertices": int(len(V)),
        "faces": int(len(F)),
        "watertight": meshmath.is_watertight(F),
        "volume_mm3": round(meshmath.signed_volume(V, F), 6),
        "bbox_min": V.min(axis=0).round(6).tolist(),
        "bbox_max": V.max(axis=0).round(6).tolist(),
    }
