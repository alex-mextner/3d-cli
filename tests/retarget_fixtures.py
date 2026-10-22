"""Synthetic Orca-family project .3mf files and preset dirs for the `3d retarget` tests.

The projects mimic what Snapmaker Orca 2.4 writes for a Snapmaker U1 (270 mm bed, four
extruders, U1-only macros in the layer-change G-code): a main model whose objects are
components pointing at `3D/Objects/*.model` sub-models, per-object settings, a plate with
preview images and a variable layer-height profile. Meshes are plain boxes so the whole
fixture is a few KiB.
"""
from __future__ import annotations

import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path

U1_BED = 270.0


@dataclass(frozen=True)
class Box:
    """One printable object: a box `size` = (x, y, z) mm, spun `rot_z` degrees about Z."""

    name: str
    size: tuple[float, float, float]
    center: tuple[float, float] = (135.0, 135.0)
    rot_z: float = 0.0
    extruder: int = 1
    painted: bool = False
    copies: int = 1
    modifier: tuple[float, float, float] | None = None  # a modifier volume of this size, same origin


TWO = [
    Box("left", (60, 40, 30), center=(200, 200), rot_z=30, extruder=1),
    Box("right", (50, 50, 20), center=(240, 90), extruder=2),
]


def u1_settings(*, extruders: int = 4, filaments: int = 1) -> dict[str, object]:
    """The slice of a U1 `project_settings.config` the retarget logic reads or must not trip on."""
    ext = [str(v) for v in ["0.4"] * extruders]
    return {
        "from": "project",
        "name": "project_settings",
        "version": "2.4.0",
        "printable_area": ["0.5x1", "270.5x1", "270.5x271", "0.5x271"],
        "printable_height": "270.05",
        "printer_model": "Snapmaker U1",
        "printer_settings_id": "Snapmaker U1 (0.4 nozzle)",
        "printer_variant": "0.4",
        "gcode_flavor": "klipper",
        "nozzle_diameter": ext,
        "extruder_colour": ["#FCE94F"] * extruders,
        "extruder_offset": ["0x0"] * extruders,
        "retract_lift_above": ["0"] * extruders,
        "machine_max_speed_x": ["500", "200"],
        "machine_start_gcode": "PRINT_START\nDEFECT_DETECTION_START",
        "machine_end_gcode": "PRINT_END",
        "before_layer_change_gcode": ";BEFORE\nTIMELAPSE_TAKE_FRAME\nDEFECT_DETECTION_DETECT",
        "layer_change_gcode": ";AFTER\nSET_PRINT_STATS_INFO CURRENT_LAYER={layer_num}",
        "change_filament_gcode": "T{next_extruder}",
        "time_lapse_gcode": "U1_ONLY_TIMELAPSE",
        "machine_pause_gcode": "M600",
        "filament_settings_id": ["Snapmaker PLA @U1"] * filaments,
        "filament_colour": ["#080A0D"] * filaments,
        "filament_type": ["PLA"] * filaments,
        "nozzle_temperature": ["220", "220"],
        "filament_flow_ratio": ["0.966", "0.966"],
        "enable_pressure_advance": ["0", "1"],
        "print_settings_id": "0.12mm Standard @Snapmaker U1 (0.4 nozzle)",
        "print_compatible_printers": ["Snapmaker U1 (0.4 nozzle)"],
        "layer_height": "0.08",
        "enable_support": "1",
        "support_top_z_distance": "0.12",
        "support_bottom_z_distance": "0.12",
        "support_interface_top_layers": "2",
        "support_interface_bottom_layers": "2",
        "support_interface_spacing": "0.5",
        "support_interface_pattern": "auto",
        "support_filament": "2" if filaments > 1 else "0",
        "wall_filament": "1",
        "enable_prime_tower": "1",
        "flush_volumes_matrix": ["0"] * (filaments * filaments),
        "flush_volumes_vector": ["140", "140"] * filaments,
        "wipe_tower_x": ["13"],
        "filament_map": ["2"] * filaments,
        "extruder_type": ["Direct Drive"] * extruders,
    }


def _mesh_object(object_id: int, size: tuple[float, float, float], painted: bool) -> str:
    sx, sy, sz = (v / 2 for v in size)
    corners = [(x, y, z) for z in (-sz, sz) for y in (-sy, sy) for x in (-sx, sx)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = [t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))]
    paint = ' paint_color="4"' if painted else ""
    verts = "\n".join(f'    <vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in corners)
    faces = "\n".join(f'    <triangle v1="{a}" v2="{b}" v3="{c}"{paint}/>' for a, b, c in tris)
    return (
        f'  <object id="{object_id}" type="model">\n   <mesh>\n   <vertices>\n{verts}\n   </vertices>\n'
        f"   <triangles>\n{faces}\n   </triangles>\n   </mesh>\n  </object>"
    )


def _box_mesh(object_id: int, size: tuple[float, float, float], painted: bool) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        f" <resources>\n{_mesh_object(object_id, size, painted)}\n </resources>\n</model>\n"
    )


def _item_transform(box: Box) -> str:
    c, s = math.cos(math.radians(box.rot_z)), math.sin(math.radians(box.rot_z))
    # 3MF row-vector layout: m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz; z lifted to sit on the bed.
    values = [c, s, 0, -s, c, 0, 0, 0, 1, box.center[0], box.center[1], box.size[2] / 2]
    return " ".join(repr(round(v, 9)) for v in values)


def make_project(
    path: Path,
    boxes: list[Box],
    *,
    extruders: int = 4,
    filaments: int = 1,
    layer_profile: bool = True,
    plates: tuple[tuple[int, ...], ...] | None = None,
    inline_meshes: bool = False,
    sliced: bool = False,
) -> Path:
    """Write a project 3MF for the U1 with one object per box; returns `path`.

    `plates` groups 1-based box numbers per plate (default: all on plate 1). A box with
    `copies` > 1 becomes that many build items of ONE object (instances 0..copies-1). Items on
    plate N sit `(N - 1) * 324` mm along X, the way the slicer lays plates out. `inline_meshes`
    stores the meshes in the main model instead of `3D/Objects/*.model`; `sliced` adds the
    source printer's plate G-code like a `.gcode.3mf`.
    """
    plate_of = {i: n for n, group in enumerate(plates or (tuple(range(1, len(boxes) + 1)),), start=1) for i in group}
    ids = [(2 * i + 2, 2 * i + 1) for i in range(len(boxes))]  # (main object id, sub-model object id)
    resources, items, settings_objects, assemble, subs = [], [], [], [], {}
    inline: list[str] = []
    plate_instances: dict[int, list[str]] = {}
    for n, (box, (oid, sub_id)) in enumerate(zip(boxes, ids, strict=True), start=1):
        sub_path = f"3D/Objects/box_{n}.model"
        volumes = [(sub_id, box.size, "normal_part", sub_path)]
        if box.modifier:
            volumes.append((sub_id + 100, box.modifier, "modifier_part", f"3D/Objects/box_{n}_mod.model"))
        components, parts_xml = [], []
        for vid, size, subtype, vpath in volumes:
            if inline_meshes:
                inline.append(_mesh_object(vid, size, box.painted))
                components.append(f'    <component objectid="{vid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            else:
                subs[vpath] = _box_mesh(vid, size, box.painted)
                components.append(f'    <component p:path="/{vpath}" objectid="{vid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            parts_xml.append(
                f'    <part id="{vid}" subtype="{subtype}">\n      <metadata key="name" value="{box.name}"/>\n'
                f'      <metadata key="extruder" value="{box.extruder}"/>\n    </part>'
            )
        resources.append(f'  <object id="{oid}" type="model">\n   <components>\n' + "\n".join(components) + "\n   </components>\n  </object>")
        settings_objects.append(
            f'  <object id="{oid}">\n    <metadata key="name" value="{box.name}"/>\n'
            f'    <metadata key="extruder" value="{box.extruder}"/>\n' + "\n".join(parts_xml) + "\n  </object>"
        )
        for copy in range(box.copies):
            shifted = Box(box.name, box.size, (box.center[0] + 70 * copy + 324 * (plate_of[n] - 1), box.center[1]), box.rot_z)
            items.append(f'  <item objectid="{oid}" transform="{_item_transform(shifted)}" printable="1"/>')
            plate_instances.setdefault(plate_of[n], []).append(
                f'    <model_instance>\n      <metadata key="object_id" value="{oid}"/>\n'
                f'      <metadata key="instance_id" value="{copy}"/>\n    </model_instance>'
            )
            assemble.append(
                f'   <assemble_item object_id="{oid}" instance_id="{copy}" transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0" />'
            )

    plate_xml = "".join(
        f'\n  <plate>\n    <metadata key="plater_id" value="{pid}"/>\n'
        '    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
        '    <metadata key="filament_maps" value="1 2"/>\n'
        f'    <metadata key="thumbnail_file" value="Metadata/plate_{pid}.png"/>\n'
        f'    <metadata key="top_file" value="Metadata/top_{pid}.png"/>\n'
        f'    <metadata key="pattern_bbox_file" value="Metadata/plate_{pid}.json"/>\n'
        + (f'    <metadata key="gcode_file" value="Metadata/plate_{pid}.gcode"/>\n' if sliced else "")
        + "\n".join(plate_instances[pid])
        + "\n  </plate>"
        for pid in sorted(plate_instances)
    )
    main = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
        ' <metadata name="Application">Snapmaker_Orca-2.4.0</metadata>\n <resources>\n'
        + "\n".join([*inline, *resources])
        + "\n </resources>\n <build>\n"
        + "\n".join(items)
        + "\n </build>\n</model>\n"
    )
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "\n".join(settings_objects)
        + plate_xml
        + "\n  <assemble>\n"
        + "\n".join(assemble)
        + "\n  </assemble>\n</config>\n"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(
            f' <Relationship Target="/{p}" Id="rel-{i}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            for i, p in enumerate(subs, start=1)
        )
        + "\n</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        ' <Relationship Target="/Metadata/plate_1.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>\n'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n</Types>'
    )
    profile = "".join(
        f"object_id={i}|0.000000;0.{20 + i:02d}0000;0.600000;0.240000;\n" for i in range(1, len(boxes) + 1)
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("Metadata/plate_1.png", b"\x89PNG-fixture")
        zf.writestr("Metadata/top_1.png", b"\x89PNG-fixture")
        zf.writestr("Metadata/plate_1.json", json.dumps({"bbox_all": [0, 0, 1, 1]}))
        zf.writestr("3D/3dmodel.model", main)
        zf.writestr("3D/_rels/3dmodel.model.rels", rels)
        for sub_path, text in subs.items():
            zf.writestr(sub_path, text)
        if layer_profile:
            zf.writestr("Metadata/layer_heights_profile.txt", profile)
        zf.writestr("Metadata/project_settings.config", json.dumps(u1_settings(extruders=extruders, filaments=filaments), indent=4) + "\n")
        zf.writestr("Metadata/model_settings.config", model_settings)
        if sliced:
            zf.writestr("Metadata/plate_1.gcode", "; U1 gcode\nPRINT_START\n")
            zf.writestr("Metadata/plate_1.gcode.md5", "d41d8cd98f00b204e9800998ecf8427e")
            zf.writestr(
                "Metadata/_rels/model_settings.config.rels",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                ' <Relationship Target="/Metadata/plate_1.gcode" Id="rel-1" Type="gcode"/>\n</Relationships>',
            )
    return path


def write_presets(root: Path) -> Path:
    """A tiny Orca-style preset dir: a K1-like machine inheriting an abstract base, a dual
    extruder machine, a process and a filament preset. Returns `root`."""
    def dump(kind: str, name: str, body: dict[str, object]) -> None:
        folder = root / "vendor" / kind
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps({"name": name, **body}, indent=4), encoding="utf-8")

    dump("machine", "fdm_test_base", {"instantiation": "false", "gcode_flavor": "klipper", "machine_end_gcode": "BASE_END",
                                       "machine_max_speed_x": ["800", "800"], "nozzle_diameter": ["0.4"]})
    dump("machine", "Test K1", {"type": "machine_model", "nozzle_diameter": "0.4"})
    dump("machine", "Test K1 0.4 nozzle", {
        "inherits": "fdm_test_base", "instantiation": "true", "printer_model": "Test K1",
        "printer_variant": "0.4", "printable_area": ["0x0", "220x0", "220x220", "0x220"],
        "printable_height": "250", "machine_start_gcode": "START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer]",
        "machine_end_gcode": "END_PRINT", "extruder_colour": ["#FCE94F"], "extruder_offset": ["0x0"],
        "retract_lift_above": ["0"], "before_layer_change_gcode": ";K1 LAYER", "layer_change_gcode": ";K1 DONE",
    })
    dump("machine", "Test Dual 0.4 nozzle", {
        "instantiation": "true", "printable_area": ["0x0", "300x0", "300x300", "0x300"], "printable_height": "300",
        "nozzle_diameter": ["0.4", "0.4"], "machine_start_gcode": "DUAL_START", "machine_end_gcode": "DUAL_END",
    })
    dump("process", "0.20mm Test Process", {"inherits": "fdm_test_process", "layer_height": "0.2", "print_settings_id": "ignored"})
    dump("process", "fdm_test_process", {"instantiation": "false", "enable_support": "0", "support_top_z_distance": "0.2"})
    dump("filament", "Test PLA @K1", {"filament_settings_id": ["Test PLA @K1"], "filament_flow_ratio": ["0.93"],
                                        "nozzle_temperature": ["210"], "enable_pressure_advance": ["1"]})
    return root
