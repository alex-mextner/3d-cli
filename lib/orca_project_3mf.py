"""orca_project_3mf.py — write a single OrcaSlicer/Bambu multi-plate .3mf PROJECT file.

WHAT: builds ONE .3mf (an OPC zip) that holds SEVERAL build plates, with each part assigned
  to its plate and positioned on that plate's bed — the format an OrcaSlicer-based slicer
  (incl. Snapmaker U1's) opens as a multi-plate project. This is the single-file counterpart
  to `arrange`'s per-plate output: instead of N separate `_plateN.3mf` files, you get one
  project the user opens once and sees all N plates.

WHY: the user wants "one file with all plates". trimesh's own 3MF export writes a plain,
  plate-less 3MF (no `Metadata/model_settings.config`), so a slicer dumps every object onto
  plate 1. To get real plates we must emit the Bambu/Orca project layout ourselves.

FORMAT (confirmed against the real OrcaSlicer exporter `src/libslic3r/Format/bbs_3mf.cpp`,
  `_BBS_3MF_Exporter`, on `OrcaSlicer/OrcaSlicer@main`):
  - OPC zip with: `[Content_Types].xml`, `_rels/.rels`, `3D/3dmodel.model`,
    `3D/_rels/3dmodel.model.rels`, and `Metadata/model_settings.config`.
  - `[Content_Types].xml`  — exact Defaults from `_add_content_types_file_to_archive` (L6522).
  - `_rels/.rels`          — one Relationship to `/3D/3dmodel.model`, type
    `http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel` (L6654).
  - `3D/3dmodel.model`     — header `<model unit="millimeter" xml:lang="en-US"
    xmlns="…/core/2015/02" xmlns:BambuStudio="…/package/2021">` (L6774). `<resources>` holds
    one `<object id=N type="model"><mesh><vertices/><triangles/></mesh></object>` per part
    (L7220-7307: type is always "model"; `<vertex x= y= z=/>`, `<triangle v1= v2= v3=/>`).
    `<build>` holds one `<item objectid=N transform="…" printable="1" auto_drop="…"/>` per
    part (`_add_build_to_model_stream`, L7389). The transform is written COLUMN-major as 12
    numbers — `add_transformation` (L7379) loops `for c in 0..3: for r in 0..2: emit tr(r,c)`
    — i.e. `m00 m10 m20  m01 m11 m21  m02 m12 m22  m03 m13 m23`; the last three are the
    translation. For a pure translation the first nine are the identity columns
    `1 0 0 0 1 0 0 0 1` and the tail is `tx ty tz`.
  - `3D/_rels/3dmodel.model.rels` — present but empty (no sub-models / production ext); kept
    so the package is well-formed.
  - `Metadata/model_settings.config` — assigns parts to plates
    (`_add_model_config_file_to_archive`, L7784). `<config>` with one `<object id=N>
    <metadata key="name" value="…"/></object>` per part (L7800-7804), then one `<plate>` per
    plate (L7916): `<metadata key="plater_id" value="P"/>` (1-indexed, L7918),
    `<metadata key="plater_name" value=""/>` (L7919), `<metadata key="locked" value="false"/>`
    (L7920), then one `<model_instance>` per part on that plate (L8025) holding
    `<metadata key="object_id" value="N"/>` and `<metadata key="instance_id" value="0"/>`
    (L8049-8050). The importer (L4404) reads plate assignment purely from these blocks.

PLATE LAYOUT IN WORLD COORDS: Orca lays plates out in a virtual grid (`PartPlate.cpp`).
  `compute_colum_count(N) = ceil(sqrt(N))` columns (PartPlate.hpp L38); plate p (0-indexed)
  sits at `col = p % cols`, `row = p // cols`, world origin
  `(col*stride_x, -row*stride_y)`, `stride = bed * (1 + 1/5)` (L4836/L4841, gap = 0.2). So a
  part at in-plate `(lx, ly)` is placed at world `(lx + col*stride, ly - row*stride)`. We set
  BOTH the world transform AND the plate assignment, so each part lands on its own plate at
  its in-plate position regardless of whether the slicer trusts the transform or re-snaps.

This module is PURE STDLIB (zipfile + string building) so it is unit-testable without trimesh.
The caller passes already-extracted mesh arrays (vertices, triangles) per part.

`project_settings.config` / `plate_N.json` are NOT written: they carry print/filament process
settings and per-plate thumbnails, neither required to OPEN the file with the plates correctly
arranged. Keeping the package minimal avoids shipping wrong/garbage process settings.
"""
from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass
from typing import Sequence
from xml.sax.saxutils import quoteattr

# OPC / 3MF entry paths (mirror bbs_3mf.cpp constants).
_CONTENT_TYPES = "[Content_Types].xml"
_ROOT_RELS = "_rels/.rels"
_MODEL_FILE = "3D/3dmodel.model"
_MODEL_RELS = "3D/_rels/3dmodel.model.rels"
_MODEL_SETTINGS = "Metadata/model_settings.config"

# Orca plate grid: stride = bed * (1 + LOGICAL_PART_PLATE_GAP), gap = 1/5.
_PLATE_GAP_FRACTION = 1.0 / 5.0


@dataclass
class MeshPart:
    """One printable body to embed: a name, its vertices and triangle indices.

    `vertices` is a sequence of (x, y, z) already laid flat on z=0 and positioned at the
    part's IN-PLATE lower-left origin (the writer adds the plate's world offset). `triangles`
    is a sequence of (v1, v2, v3) zero-based indices into `vertices`. `plate` is the 0-based
    plate this part belongs to.
    """

    name: str
    vertices: Sequence[tuple[float, float, float]]
    triangles: Sequence[tuple[int, int, int]]
    plate: int


def plate_columns(num_plates: int) -> int:
    """Orca's `compute_colum_count` (PartPlate.hpp): ceil(sqrt(n)) via round-then-bump."""
    if num_plates <= 1:
        return 1
    value = math.sqrt(float(num_plates))
    round_value = round(value)
    return int(round_value + 1) if value > round_value else int(round_value)


def plate_world_origin(plate_index: int, *, bed: float, cols: int) -> tuple[float, float]:
    """World (x, y) origin of plate `plate_index` (0-based) in Orca's virtual grid."""
    stride = bed * (1.0 + _PLATE_GAP_FRACTION)
    row = plate_index // cols
    col = plate_index % cols
    return (col * stride, -row * stride)


def _fmt(value: float) -> str:
    """Compact, round-trip-safe number (matches Orca's no-trailing-zeros intent)."""
    return repr(float(value))


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n'
        ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n'
        "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        "</Relationships>"
    )


def _model_rels_xml() -> str:
    # No sub-model / production-extension targets: a well-formed but empty rels file.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        "</Relationships>"
    )


def _transform_attr(dx: float, dy: float, dz: float = 0.0) -> str:
    """Column-major 4x3 transform for a pure translation (Orca `add_transformation`)."""
    cols = (
        (1.0, 0.0, 0.0),  # column 0 (rows 0,1,2)
        (0.0, 1.0, 0.0),  # column 1
        (0.0, 0.0, 1.0),  # column 2
        (dx, dy, dz),     # column 3 = translation
    )
    return " ".join(_fmt(v) for col in cols for v in col)


def _object_mesh_xml(object_id: int, part: MeshPart) -> str:
    rows: list[str] = []
    rows.append(f'  <object id="{object_id}" type="model">')
    rows.append("   <mesh>")
    rows.append("    <vertices>")
    for x, y, z in part.vertices:
        rows.append(f'     <vertex x="{_fmt(x)}" y="{_fmt(y)}" z="{_fmt(z)}"/>')
    rows.append("    </vertices>")
    rows.append("    <triangles>")
    for v1, v2, v3 in part.triangles:
        rows.append(f'     <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>')
    rows.append("    </triangles>")
    rows.append("   </mesh>")
    rows.append("  </object>")
    return "\n".join(rows)


def _build_3dmodel_xml(parts: Sequence[MeshPart], *, bed: float, cols: int) -> str:
    """`3D/3dmodel.model`: resources (one object per part) + positioned build items."""
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">\n'
        " <resources>\n"
    )
    objects = "\n".join(_object_mesh_xml(i + 1, p) for i, p in enumerate(parts))
    mid = "\n </resources>\n <build>\n"
    items: list[str] = []
    for i, part in enumerate(parts):
        ox, oy = plate_world_origin(part.plate, bed=bed, cols=cols)
        xform = _transform_attr(ox, oy, 0.0)
        items.append(
            f'  <item objectid="{i + 1}" transform="{xform}" printable="1" auto_drop="0"/>'
        )
    tail = "\n </build>\n</model>\n"
    return head + objects + mid + "\n".join(items) + tail


def _model_settings_xml(parts: Sequence[MeshPart], *, num_plates: int) -> str:
    """`Metadata/model_settings.config`: object names + plate -> instance assignment."""
    rows: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for i, part in enumerate(parts):
        rows.append(f'  <object id="{i + 1}">')
        rows.append(f'    <metadata key="name" value={quoteattr(part.name)}/>')
        rows.append("  </object>")
    for plate in range(num_plates):
        rows.append("  <plate>")
        rows.append(f'    <metadata key="plater_id" value="{plate + 1}"/>')
        rows.append('    <metadata key="plater_name" value=""/>')
        rows.append('    <metadata key="locked" value="false"/>')
        for i, part in enumerate(parts):
            if part.plate != plate:
                continue
            rows.append("    <model_instance>")
            rows.append(f'      <metadata key="object_id" value="{i + 1}"/>')
            rows.append('      <metadata key="instance_id" value="0"/>')
            rows.append("    </model_instance>")
        rows.append("  </plate>")
    rows.append("</config>")
    # Part names are emitted via quoteattr (escapes & < > " in the attribute value).
    return "\n".join(rows)


def write_project_3mf(out_path: str, parts: Sequence[MeshPart], *, bed: float) -> int:
    """Write a single multi-plate Orca project .3mf. Returns the number of plates.

    `parts` must be non-empty; each part's `.plate` is a 0-based plate index. Plates are
    numbered densely from 0..num_plates-1 (the caller is expected to pass contiguous indices).
    """
    if not parts:
        raise ValueError("write_project_3mf: no parts given")
    num_plates = max(p.plate for p in parts) + 1
    cols = plate_columns(num_plates)

    model_xml = _build_3dmodel_xml(parts, bed=bed, cols=cols)
    settings_xml = _model_settings_xml(parts, num_plates=num_plates)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml must be the first entry in an OPC package.
        zf.writestr(_CONTENT_TYPES, _content_types_xml())
        zf.writestr(_ROOT_RELS, _root_rels_xml())
        zf.writestr(_MODEL_FILE, model_xml)
        zf.writestr(_MODEL_RELS, _model_rels_xml())
        zf.writestr(_MODEL_SETTINGS, settings_xml)
    return num_plates
