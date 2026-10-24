"""fit_coupon_export.py — write the fit coupon as OpenSCAD source, STL, 3MF and a JSON map, positioned on the bed.

WHAT: `export(layout, out)` renders every object of a `fit_coupon.Layout` with OpenSCAD (the same renderer the
  calibration plates use, `slicing.calibration_plates.dense.openscad_stl`) and writes, next to each other:
    NAME.scad   all objects as one OpenSCAD source (edit-and-re-render friendly)
    NAME.stl    one mesh of the whole coupon, in bed coordinates
    NAME.3mf    one 3MF model with one build item per object (ladders and spares), millimetres, bed coordinates,
                the printer/material in the metadata - ready for `3d prep` / `3d arrange` / `3d retarget`
    NAME.json   the map: ladders, gaps, object boxes and the readout instruction per ladder
  The extension of `out` picks the main file; the other three are written too (`formats`).
WHY: the S2 acceptance asks for an STL/3MF coupon a slicer project can take; the plate machinery only emits
  G-code, so this path needs no Orca and no presets.
INVARIANTS: the 3MF writer is stdlib (zipfile); a mesh is the ASCII STL OpenSCAD returns; no file is written unless
  every object rendered.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import fit_coupon
from errors import MissingDependency, ThreeDError

FORMATS = ("scad", "stl", "3mf", "json")
_VERTEX = re.compile(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)")

Mesh = tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]


def render(scad: str) -> str:
    """ASCII STL of OpenSCAD source; a missing OpenSCAD is a structured error."""
    from slicing.calibration_gen import GenError
    from slicing.calibration_plates.dense import openscad_stl

    try:
        return openscad_stl(scad)
    except GenError as e:
        if "needs OpenSCAD" in str(e):
            raise MissingDependency(
                "OpenSCAD",
                install="brew install --cask openscad  |  apt install openscad",
                degrades="the fit coupon cannot be rendered to STL/3MF",
                command="calibrate fit gen",
            ) from e
        raise ThreeDError(str(e), command="calibrate fit gen") from e


def parse_stl(text: str) -> Mesh:
    """Vertices (deduplicated) and triangles of an ASCII STL."""
    index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    corner: list[int] = []
    for m in _VERTEX.finditer(text):
        v = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
        i = index.get(v)
        if i is None:
            i = index[v] = len(vertices)
            vertices.append(v)
        corner.append(i)
        if len(corner) == 3:
            if len(set(corner)) == 3:  # a degenerate sliver would break manifold checks downstream
                tris.append((corner[0], corner[1], corner[2]))
            corner = []
    if not tris:
        raise ThreeDError("OpenSCAD returned an empty mesh", command="calibrate fit gen")
    return vertices, tris


def merge(meshes: Sequence[Mesh]) -> Mesh:
    vertices: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    for vs, ts in meshes:
        base = len(vertices)
        vertices.extend(vs)
        tris.extend((a + base, b + base, c + base) for a, b, c in ts)
    return vertices, tris


def stl_text(mesh: Mesh, name: str) -> str:
    vertices, tris = mesh
    out = [f"solid {name}"]
    for a, b, c in tris:
        out.append("facet normal 0 0 0\n outer loop")
        out += [f"  vertex {vertices[i][0]:g} {vertices[i][1]:g} {vertices[i][2]:g}" for i in (a, b, c)]
        out.append(" endloop\nendfacet")
    out.append(f"endsolid {name}\n")
    return "\n".join(out)


def model_3mf(
    objects: Sequence[tuple[str, Mesh]], title: str, metadata: dict[str, str]
) -> str:
    """The 3D/3dmodel.model XML: one object + build item per mesh (already in bed coordinates)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
        f' <metadata name="Title">{escape(title)}</metadata>',
        ' <metadata name="Application">3d calibrate fit gen</metadata>',
    ]
    lines += [f' <metadata name="{escape(k)}">{escape(v)}</metadata>' for k, v in metadata.items()]
    lines.append(" <resources>")
    for n, (name, (vertices, tris)) in enumerate(objects, start=1):
        lines.append(f'  <object id="{n}" type="model" name="{escape(name)}">')
        lines.append("   <mesh>")
        lines.append("    <vertices>")
        lines += [f'     <vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in vertices]
        lines.append("    </vertices>")
        lines.append("    <triangles>")
        lines += [f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris]
        lines.append("    </triangles>")
        lines.append("   </mesh>")
        lines.append("  </object>")
    lines.append(" </resources>")
    lines.append(" <build>")
    lines += [f'  <item objectid="{n}"/>' for n in range(1, len(objects) + 1)]
    lines.append(" </build>")
    lines.append("</model>")
    return "\n".join(lines) + "\n"


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


def write_3mf(path: Path, model: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)


def scad_source(lay: fit_coupon.Layout, title: str) -> str:
    parts = [f"// {title}\n// every object is in bed coordinates (mm); `$fn` is set per primitive\n"]
    parts.extend(f"// {o.name}\n{o.scad}" for o in lay.objects)
    return "\n".join(parts)


def export(
    lay: fit_coupon.Layout,
    out: Path,
    *,
    title: str,
    metadata: dict[str, str],
    doc: dict[str, Any],
    formats: Sequence[str] = FORMATS,
) -> tuple[list[Path], dict[str, Mesh]]:
    """Render and write the coupon files named after `out` (its suffix is one of FORMATS); returns the paths
    and the mesh of each object by name (for the caller's checks)."""
    suffix = out.suffix.lower().lstrip(".")
    if suffix not in FORMATS:
        raise ThreeDError(
            f"--out {out}: the coupon is written as {', '.join('.' + f for f in FORMATS)}",
            command="calibrate fit gen",
            remediation=["Name the output coupon.3mf (or .stl / .scad)."],
        )
    meshes = {o.name: parse_stl(render(o.scad)) for o in lay.objects}
    out.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    stem = out.with_suffix("")
    for fmt in formats:
        path = stem.with_suffix(f".{fmt}")
        if fmt == "scad":
            path.write_text(scad_source(lay, title), encoding="utf-8")
        elif fmt == "stl":
            path.write_text(stl_text(merge(list(meshes.values())), "fit_coupon"), encoding="utf-8")
        elif fmt == "3mf":
            write_3mf(path, model_3mf(list(meshes.items()), title, metadata))
        else:
            path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written, meshes
