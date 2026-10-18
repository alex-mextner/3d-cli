"""Synthetic Orca-style project 3MFs for the `3d 3mf` tests: root objects whose components live
in 3D/Objects/object_N.model (production extension), build items with transforms,
Metadata/model_settings.config with names, extruders and plates, and
Metadata/project_settings.config with the printer preset's model and printable area."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

Box = tuple[
    float, float, float, float, float, float
]  # x0, y0, z0, x1, y1, z1 in the object's frame


@dataclass
class Obj:
    name: str
    boxes: list[
        Box
    ]  # one component (part) per box, centred on the object's origin by the caller
    at: tuple[float, float, float] = (50.0, 50.0, 0.0)  # build item translation
    plate: int = 1
    extruder: int = 1
    rotation: tuple[float, ...] = (
        1,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        1,
    )  # 3MF row-major 3x3 of the build item


@dataclass
class Spec:
    objects: list[Obj]
    printer_model: str | None = "Snapmaker U1"
    bed: tuple[float, float, float] = (270.0, 270.0, 270.0)
    extra: dict[str, str] = field(
        default_factory=dict
    )  # more zip members (per-object files)


def tile(w: float, d: float, h: float) -> Box:
    """A box standing on z=0 and centred in X/Y."""
    return (-w / 2, -d / 2, 0.0, w / 2, d / 2, h)


def _mesh(b: Box, oid: int) -> str:
    x0, y0, z0, x1, y1, z1 = b
    v = [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    ]
    # outward-facing (counter-clockwise seen from outside)
    t = [
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
    verts = "\n".join(f'     <vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
    tris = "\n".join(f'     <triangle v1="{a}" v2="{b_}" v3="{c}"/>' for a, b_, c in t)
    return (
        f'  <object id="{oid}" p:UUID="0000{oid:04d}-0000-0000-0000-000000000000" type="model">\n'
        f"   <mesh>\n    <vertices>\n{verts}\n    </vertices>\n    <triangles>\n{tris}\n"
        f"    </triangles>\n   </mesh>\n  </object>\n"
    )


_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
)


def write(path: Path, spec: Spec) -> Path:
    files: dict[str, str] = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n</Types>\n',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n',
    }
    resources, items, rels, settings = [], [], [], []
    part_id = 0
    for k, o in enumerate(spec.objects, 1):
        root_id = 1000 + k - 1
        file = f"3D/Objects/object_{k}.model"
        comps, meshes, parts = [], [], []
        for b in o.boxes:
            part_id += 1
            meshes.append(_mesh(b, part_id))
            comps.append(
                f'    <component p:path="/{file}" objectid="{part_id}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            )
            parts.append(
                f'    <part id="{part_id}" subtype="normal_part">\n'
                f'      <metadata key="name" value="{o.name} part {part_id}"/>\n'
                f'      <metadata key="extruder" value="{o.extruder}"/>\n    </part>'
            )
        files[file] = (
            _HEAD
            + " <resources>\n"
            + "".join(meshes)
            + " </resources>\n <build/>\n</model>\n"
        )
        resources.append(
            f'  <object id="{root_id}" p:UUID="1111{root_id:04d}-0000-0000-0000-000000000000" type="model">\n'
            f"   <components>\n" + "\n".join(comps) + "\n   </components>\n  </object>"
        )
        x, y, z = o.at
        tf = " ".join(str(v) for v in (*o.rotation, x, y, z))
        items.append(f'  <item objectid="{root_id}" transform="{tf}" printable="1"/>')
        rels.append(
            f' <Relationship Target="/{file}" Id="rel-{k + 1}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        )
        settings.append(
            f'  <object id="{root_id}">\n    <metadata key="name" value="{o.name}"/>\n'
            f'    <metadata key="extruder" value="{o.extruder}"/>\n'
            + "\n".join(parts)
            + "\n  </object>"
        )
    files["3D/3dmodel.model"] = (
        _HEAD + ' <metadata name="Application">Snapmaker_Orca-01.10.01.50</metadata>\n'
        " <resources>\n"
        + "\n".join(resources)
        + "\n </resources>\n <build>\n"
        + "\n".join(items)
        + "\n </build>\n</model>\n"
    )
    files["3D/_rels/3dmodel.model.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(rels)
        + "\n</Relationships>\n"
    )
    plates = sorted({o.plate for o in spec.objects})
    plate_xml = []
    for p in plates:
        inst = "".join(
            f'    <model_instance>\n      <metadata key="object_id" value="{1000 + k}"/>\n'
            f'      <metadata key="instance_id" value="0"/>\n    </model_instance>\n'
            for k, o in enumerate(spec.objects)
            if o.plate == p
        )
        plate_xml.append(
            f'  <plate>\n    <metadata key="plater_id" value="{p}"/>\n'
            f'    <metadata key="thumbnail_file" value="Metadata/plate_{p}.png"/>\n{inst}  </plate>'
        )
        files[f"Metadata/plate_{p}.json"] = json.dumps({"plate": p})
    files["Metadata/model_settings.config"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "\n".join(settings)
        + "\n"
        + "\n".join(plate_xml)
        + "\n  <assemble>\n  </assemble>\n</config>\n"
    )
    w, d, h = spec.bed
    proj: dict[str, object] = {
        "printable_area": ["0x0", f"{w:g}x0", f"{w:g}x{d:g}", f"0x{d:g}"],
        "printable_height": f"{h:g}",
        "layer_height": "0.2",
    }
    if spec.printer_model:
        proj["printer_model"] = spec.printer_model
    files["Metadata/project_settings.config"] = json.dumps(proj, indent=4)
    files.update(spec.extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


# Orca's plate grid for two 270 mm plates: plate 2 sits one column right, 270 * 1.2 mm away.
PLATE2_X = 270.0 * 1.2


def three_parts(
    path: Path,
    *,
    printer_model: str | None = "Snapmaker U1",
    extra: dict[str, str] | None = None,
) -> Path:
    """CN (a 20 x 10 x 3 tile, plate 1), SW (a 10 x 10 x 30 post, plate 1), ROOF (a T: stem plus
    a 30 x 30 slab on top, plate 2) — names as in the babushkin-dom projects."""
    spec = Spec(
        [
            Obj("CN — CN", [tile(20, 10, 3)], at=(40.0, 40.0, 0.0), extruder=1),
            Obj("SW — SW", [tile(10, 10, 30)], at=(100.0, 40.0, 0.0), extruder=2),
            Obj(
                "ROOF",
                [tile(4, 4, 10), (-15.0, -15.0, 10.0, 15.0, 15.0, 12.0)],
                at=(PLATE2_X + 60.0, 60.0, 0.0),
                plate=2,
                extruder=3,
            ),
        ],
        printer_model=printer_model,
        extra=extra or {},
    )
    return write(path, spec)
