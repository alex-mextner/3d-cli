"""Small coloured models for the `3d dimension` part/colour tests (alex-mextner/3d-cli#580).

`write_3mf` hand-writes a 3MF (zipfile + text, no lxml, no trimesh exporter) so the tests pin the
exact structures the renderer must read: one `<object>` per part (`objects=`), or ONE object whose
triangles carry per-triangle `pid`/`p1` colours, the way OpenSCAD exports `color()`. `write_obj`
writes boxes with `o`/`usemtl` and an MTL. Boxes are 10 mm cubes at x = 0, 20, 40, ...
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

RED, GREEN, BLUE = "#CC2222", "#22AA22", "#2222CC"

CUBE_VERTS = np.array(
    [[x, y, z] for x in (0, 10) for y in (0, 10) for z in (0, 10)], dtype=float
)
CUBE_TRIS = np.array(
    [
        [0, 2, 1], [1, 2, 3],  # x = 0
        [4, 5, 6], [5, 7, 6],  # x = 10
        [0, 1, 4], [1, 5, 4],  # y = 0
        [2, 6, 3], [3, 6, 7],  # y = 10
        [0, 4, 2], [2, 4, 6],  # z = 0
        [1, 3, 5], [3, 7, 5],  # z = 10
    ]
)  # fmt: skip


@dataclass
class Box:
    name: str
    x: float  # left edge in mm
    color: str | None = None  # authored "#RRGGBB"
    material: str = ""  # material name ("" -> generic "Color N")
    meta: dict[str, str] = field(
        default_factory=dict
    )  # 3MF object metadata, e.g. {"3d:role": "stand_in"}


def _vertices_xml(box: Box) -> str:
    verts = CUBE_VERTS + [box.x, 0, 0]
    return "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in verts)


def _mesh_xml(box: Box) -> str:
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in CUBE_TRIS)
    return f"<mesh><vertices>{_vertices_xml(box)}</vertices><triangles>{t}</triangles></mesh>"


def _package(path: Path, model_xml: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>',
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        )
        zf.writestr("3D/3dmodel.model", model_xml)
    return path


_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
    ' xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">'
)


def write_3mf(
    path: Path, boxes: list[Box], *, unit: str = "millimeter", transform: str = ""
) -> Path:
    """One `<object>` per box; a coloured box points at its own `basematerials` entry.

    `unit` is the model's unit; `transform` (12 numbers) is put on every build item.
    """
    bases = "".join(
        f'<base name="{b.material or "Color 1"}" displaycolor="{b.color}FF"/>'
        for b in boxes
        if b.color
    )
    objects, items, slot = [], [], 0
    placed = f' transform="{transform}"' if transform else ""
    for i, box in enumerate(boxes, start=2):
        pid = f' pid="1" pindex="{slot}"' if box.color else ""
        slot += 1 if box.color else 0
        meta = "".join(
            f'<metadata name="{k}">{v}</metadata>' for k, v in box.meta.items()
        )
        group = f"<metadatagroup>{meta}</metadatagroup>" if meta else ""
        objects.append(
            f'<object id="{i}" name="{box.name}" type="model"{pid}>{group}{_mesh_xml(box)}</object>'
        )
        items.append(f'<item objectid="{i}"{placed}/>')
    resources = (
        f'<basematerials id="1">{bases}</basematerials>' if bases else ""
    ) + "".join(objects)
    head = _HEAD.replace('unit="millimeter"', f'unit="{unit}"')
    return _package(
        path,
        f"{head}<resources>{resources}</resources><build>{''.join(items)}</build></model>",
    )


def write_3mf_per_triangle(
    path: Path, boxes: list[Box], *, openscad: bool = False
) -> Path:
    """ONE unnamed object; each box's triangles carry their own `p1` (how OpenSCAD exports color()).

    `openscad=True` adds the Application metadata and an uncoloured "Default" slot 0, as OpenSCAD does.
    """
    names = ["Default"] if openscad else []
    colors = ["#F9D72C"] if openscad else []
    for box in boxes:
        if box.color:
            names.append(box.material or f"Color {len(names) + (0 if openscad else 1)}")
            colors.append(box.color)
    bases = "".join(
        f'<base name="{n}" displaycolor="{c}FF"/>'
        for n, c in zip(names, colors, strict=True)
    )
    v, t, offset = "", "", 0
    for box in boxes:
        slot = colors.index(box.color) if box.color else 0
        v += _vertices_xml(box)
        t += "".join(
            f'<triangle v1="{a + offset}" v2="{b + offset}" v3="{c + offset}" pid="1" p1="{slot}"/>'
            for a, b, c in CUBE_TRIS
        )
        offset += len(CUBE_VERTS)
    app = (
        '<metadata name="Application">OpenSCAD (https://www.openscad.org/)</metadata>'
        if openscad
        else ""
    )
    obj = (
        f'<object id="2" name="{"OpenSCAD Model" if openscad else "assembly"}" type="model" pid="1" pindex="0">'
        f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>"
    )
    return _package(
        path,
        f'{_HEAD}{app}<resources><basematerials id="1">{bases}</basematerials>{obj}</resources>'
        '<build><item objectid="2"/></build></model>',
    )


def write_obj(path: Path, boxes: list[Box], *, with_mtl: bool = True) -> Path:
    """An OBJ with one `o` + `usemtl` per box; the MTL (`<stem>.mtl`) gives each material its Kd."""
    lines = [f"mtllib {path.stem}.mtl"] if with_mtl else []
    mtl: list[str] = []
    base = 1
    for box in boxes:
        lines.append(f"o {box.name}")
        lines += [f"v {a + box.x} {b} {c}" for a, b, c in CUBE_VERTS]
        if box.color:
            mat = box.material or f"mat_{box.name}"
            lines.append(f"usemtl {mat}")
            r, g, b = (int(box.color[i : i + 2], 16) / 255 for i in (1, 3, 5))
            mtl.append(f"newmtl {mat}\nKd {r:.4f} {g:.4f} {b:.4f}\n")
        lines += [f"f {a + base} {b + base} {c + base}" for a, b, c in CUBE_TRIS]
        base += len(CUBE_VERTS)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if with_mtl:
        path.with_suffix(".mtl").write_text("\n".join(mtl), encoding="utf-8")
    return path


THREE_BOXES = [Box("lid", 0, RED), Box("base", 20, GREEN), Box("clip", 40, BLUE)]


@functools.cache
def openscad_exports_3mf_color() -> bool:
    """True when the installed openscad writes `color()` into its 3MF export.

    OpenSCAD 2021.01 (Ubuntu 24.04's package, so CI) writes a 3MF with one material and drops every
    `color()`; the snapshot builds that keep colours are what `3d dimension` needs for a .scad input.
    """
    exe = shutil.which("openscad")
    if exe is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "probe.scad"), Path(tmp, "probe.3mf")
        src.write_text(
            'color("red") cube(1);\ncolor("blue") translate([2, 0, 0]) cube(1);\n'
        )
        done = subprocess.run(
            [exe, "-o", str(out), str(src)], capture_output=True, check=False
        )
        if done.returncode != 0 or not out.exists():
            return False
        with zipfile.ZipFile(out) as zf:
            model = next(n for n in zf.namelist() if n.endswith(".model"))
            return zf.read(model).count(b"<base ") > 1


SCAD_COLOR_REASON = (
    "openscad does not export color() to 3MF (needs a 2023+ snapshot, CI has 2021.01)"
)
