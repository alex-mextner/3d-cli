"""Unit tests for convert_mesh — the colour-aware worker behind `3d convert`.

The 3MF inputs come from tests/dimension_fixtures (no openscad needed): `write_3mf`
writes one object per box with per-object colours, `write_3mf_per_triangle` writes the
single-object per-triangle form OpenSCAD emits for a colored scad.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from dimension_fixtures import BLUE, Box, GREEN, RED, write_3mf, write_3mf_per_triangle

import convert_mesh


def _run(mode: str, out: Path, *inputs: Path, defines: list[str] | None = None) -> int:
    argv = [mode, str(out), *(str(p) for p in inputs)]
    for define in defines or []:
        argv += ["-D", define]
    return convert_mesh.main(argv)


def _strip_ns(root: ET.Element) -> None:
    """Drop XML namespaces so plain-name findalls work (the writer emits a default ns)."""
    for el in root.iter():
        el.tag = el.tag.rpartition("}")[2]


def _read_project(path: Path) -> tuple[ET.Element, ET.Element]:
    with zipfile.ZipFile(path) as zf:
        model = ET.fromstring(zf.read("3D/3dmodel.model"))
        settings = ET.fromstring(zf.read("Metadata/model_settings.config"))
    _strip_ns(model)
    _strip_ns(settings)
    return model, settings


def _objects(model: ET.Element) -> list[ET.Element]:
    return model.findall("resources/object")


def _object_extruders(settings: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for obj in settings.findall("object"):
        name = ""
        extruder = ""
        for meta in obj.findall("metadata"):
            if meta.get("key") == "name":
                name = meta.get("value") or ""
            if meta.get("key") == "extruder":
                extruder = meta.get("value") or ""
        out[name] = extruder
    return out


def _colored(tmp_path: Path, name: str = "asm.3mf") -> Path:
    """Two coloured cubes 20 mm apart, with real material names for the object names."""
    return write_3mf(
        tmp_path / name,
        [Box("lid", 0, RED, "Lid"), Box("base", 20, GREEN, "Base")],
    )


def test_project_3mf_one_object_per_colour_with_extruders(tmp_path: Path) -> None:
    src = _colored(tmp_path)
    out = tmp_path / "project.3mf"
    assert _run("project3mf", out, src) == 0

    model, settings = _read_project(out)
    objects = _objects(model)
    assert len(objects) == 2  # one object per colour group, 12 triangles each
    triangles = [o.find("mesh/triangles") for o in objects]
    assert all(t is not None and len(t) == 12 for t in triangles)

    extruders = _object_extruders(settings)
    assert sorted(extruders.items()) == [("base", "2"), ("lid", "1")]
    plate = settings.find("plate")
    assert plate is not None and len(plate.findall("model_instance")) == 2

    materials = model.findall("resources/basematerials/base")
    assert sorted((m.get("displaycolor") or "")[:7] for m in materials) == [
        GREEN,
        RED,
    ]


def test_project_3mf_uncoloured_geometry_prints_on_extruder_one(tmp_path: Path) -> None:
    src = write_3mf(
        tmp_path / "asm.3mf",
        [Box("body", 0, None), Box("lid", 20, RED, "Lid")],
    )
    out = tmp_path / "project.3mf"
    assert _run("project3mf", out, src) == 0
    _model, settings = _read_project(out)
    assert _object_extruders(settings) == {"body": "1", "lid": "2"}


def test_project_3mf_equal_colours_share_one_object_across_inputs(
    tmp_path: Path,
) -> None:
    one = write_3mf(tmp_path / "one.3mf", [Box("lid", 0, RED, "Lid")])
    two = write_3mf(
        tmp_path / "two.3mf", [Box("cap", 0, RED, "Cap"), Box("base", 20, BLUE, "Base")]
    )
    out = tmp_path / "project.3mf"
    assert _run("project3mf", out, one, two) == 0

    _model, settings = _read_project(out)
    extruders = _object_extruders(settings)
    # RED groups of both inputs are one extruder slot, BLUE takes the next one.
    assert sorted(extruders.values()) == ["1", "1", "2"]


def test_project_3mf_keeps_authored_relative_positions(tmp_path: Path) -> None:
    """Colour groups 20 mm apart stay 20 mm apart — the overlap is what fuses colours."""
    src = _colored(tmp_path)
    out = tmp_path / "project.3mf"
    assert _run("project3mf", out, src) == 0

    model, _settings = _read_project(out)
    spans = []
    for obj in _objects(model):
        xs = [float(v.get("x") or 0.0) for v in obj.findall("mesh/vertices/vertex")]
        spans.append((min(xs), max(xs)))
    ordered = sorted(spans)
    assert ordered[1][0] - ordered[0][0] == pytest.approx(20.0)


def test_project_3mf_from_an_openscad_style_per_triangle_3mf(tmp_path: Path) -> None:
    """The single-object per-triangle colour form (OpenSCAD's color() export) splits too."""
    src = write_3mf_per_triangle(
        tmp_path / "csg.3mf", [Box("a", 0, RED, "A"), Box("b", 20, BLUE, "B")]
    )
    out = tmp_path / "project.3mf"
    assert _run("project3mf", out, src) == 0
    model, _settings = _read_project(out)
    assert len(_objects(model)) == 2


def test_project_3mf_is_laid_flat_on_the_plate(tmp_path: Path) -> None:
    src = write_3mf(
        tmp_path / "asm.3mf",
        [Box("lid", 0, RED, "Lid")],
        transform="1 0 0 0 1 0 0 0 1 5 6 -7",
    )
    out = tmp_path / "project.3mf"
    assert _run("project3mf", out, src) == 0
    model, _settings = _read_project(out)
    first = _objects(model)[0]
    zs = [float(v.get("z") or 0.0) for v in first.findall("mesh/vertices/vertex")]
    assert min(zs) == pytest.approx(0.0)


def test_stl_mode_merges_every_group(tmp_path: Path) -> None:
    src = _colored(tmp_path)
    out = tmp_path / "merged.stl"
    assert _run("stl", out, src) == 0
    data = out.read_bytes()
    (count,) = struct.unpack("<I", data[80:84])
    assert count == 24  # two cubes
    assert len(data) == 84 + count * 50  # a well-formed binary STL


def test_bad_color_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _colored(tmp_path)
    with pytest.raises(SystemExit) as err:
        convert_mesh.main(
            ["stl", str(tmp_path / "out.stl"), str(src), "--color", "oops"]
        )
    assert err.value.code == 2
    assert "--color" in capsys.readouterr().err


def test_usdz_mode_writes_one_coloured_mesh_per_group(tmp_path: Path) -> None:
    pytest.importorskip("pxr")  # usd-core is an on-demand tier, not a core dep
    src = _colored(tmp_path)
    out = tmp_path / "scene.usdz"
    assert _run("usdz", out, src) == 0

    from pxr import Usd, UsdGeom  # type: ignore[import-untyped]  # usd-core

    stage = Usd.Stage.Open(str(out))
    assert stage is not None
    meshes = [UsdGeom.Mesh(p) for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    assert len(meshes) == 2
    colours = {tuple(mesh.GetDisplayColorAttr().Get()[0]) for mesh in meshes}
    assert len(colours) == 2


def test_usdz_input_round_trips_through_the_project(tmp_path: Path) -> None:
    pytest.importorskip("pxr")
    src = write_3mf(tmp_path / "asm.3mf", [Box("lid", 0, RED, "Lid")])
    usdz = tmp_path / "scene.usdz"
    assert _run("usdz", usdz, src) == 0

    back = tmp_path / "back.3mf"
    assert _run("project3mf", back, usdz) == 0
    model, settings = _read_project(back)
    assert len(_objects(model)) == 1  # the one USDZ mesh, colour group kept
    assert _object_extruders(settings) == {"lid": "1"}
    materials = model.findall("resources/basematerials/base")
    assert len(materials) == 1
    assert (materials[0].get("displaycolor") or "")[:7] == RED
