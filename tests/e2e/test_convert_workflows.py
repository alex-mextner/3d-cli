"""`3d convert` e2e: real CLI, real outputs — multicolour scad→3MF, collisions, aliases."""

from __future__ import annotations

import json
import shutil
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from dimension_fixtures import SCAD_COLOR_REASON, openscad_exports_3mf_color
from orca_project_3mf import MeshPart, write_project_3mf

from .workflow_helper import png_colour_counts, png_size, run_cli

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="openscad not installed"
)

COLOURED_SCAD = (
    'color("#CC2222") cube(10);\ncolor("#2222CC") translate([20, 0, 0]) cube(10);\n'
)


def _model_of(project: Path) -> ET.Element:
    """The project's 3D model XML with namespaces stripped (the writer emits a default ns)."""
    with zipfile.ZipFile(project) as zf:
        model = ET.fromstring(zf.read("3D/3dmodel.model"))
    for element in model.iter():
        element.tag = element.tag.rpartition("}")[2]
    return model


def _settings_of(project: Path) -> str:
    with zipfile.ZipFile(project) as zf:
        return zf.read("Metadata/model_settings.config").decode("utf-8")


def _tiny_project(path: Path) -> Path:
    write_project_3mf(
        str(path),
        [
            MeshPart(
                "part",
                [
                    (0, 0, 0),
                    (10, 0, 0),
                    (0, 10, 0),
                    (10, 10, 0),
                    (0, 0, 10),
                    (10, 0, 10),
                    (0, 10, 10),
                    (10, 10, 10),
                ],
                [
                    (0, 2, 1),
                    (1, 2, 3),
                    (4, 5, 6),
                    (5, 7, 6),
                    (0, 1, 5),
                    (0, 5, 4),
                    (2, 7, 3),
                    (2, 6, 7),
                    (0, 4, 2),
                    (2, 4, 6),
                    (1, 3, 7),
                    (1, 7, 5),
                ],
                plate=1,  # a non-zero plate: the STL export must keep its 324 mm x-offset
            )
        ],
        bed=270.0,
    )
    return path


@pytest.mark.skipif(not openscad_exports_3mf_color(), reason=SCAD_COLOR_REASON)
def test_coloured_scad_becomes_a_multicolour_orca_project(tmp_path: Path) -> None:
    """The flagship workflow: color() groups → one object per colour with extruders."""
    scad = tmp_path / "mole.scad"
    scad.write_text(COLOURED_SCAD)
    out = tmp_path / "mole.3mf"
    done = run_cli(tmp_path, "convert", str(scad), str(out))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "extruder" in done.stdout

    model = _model_of(out)
    objects = model.findall("resources/object")
    assert len(objects) == 2  # one object per colour group
    triangles = [o.find("mesh/triangles") for o in objects]
    assert all(t is not None and len(t) == 12 for t in triangles)
    colours = {
        b.get("displaycolor") or ""
        for b in model.findall("resources/basematerials/base")
    }
    assert {c[:7] for c in colours} == {"#CC2222", "#2222CC"}
    settings = _settings_of(out)
    assert 'key="extruder" value="1"' in settings
    assert 'key="extruder" value="2"' in settings
    assert "<plate>" in settings  # a real Orca project, not a bare mesh 3MF


@needs_openscad
def test_scad_to_stl_through_the_real_cli(tmp_path: Path) -> None:
    """A single scad goes straight through OpenSCAD's binary STL export: one 12-triangle cube out."""
    scad = tmp_path / "cube.scad"
    scad.write_text("cube(10);")
    out = tmp_path / "cube.stl"
    done = run_cli(tmp_path, "convert", str(scad), str(out))
    assert done.returncode == 0, done.stdout + done.stderr
    data = out.read_bytes()
    (count,) = struct.unpack("<I", data[80:84])
    assert count == 12


def test_existing_target_refuses_with_the_ready_to_run_command(tmp_path: Path) -> None:
    """An existing out file is never clobbered: exit 2 plus the exact free-name command to copy-paste."""
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "out.stl"
    out.write_bytes(b"in the way")
    done = run_cli(tmp_path, "convert", str(scad), str(out))
    assert done.returncode == 2
    assert "already exists" in done.stderr
    assert f"3d convert {scad} {tmp_path}/out-2.stl" in done.stderr
    assert "--force" in done.stderr
    assert out.read_bytes() == b"in the way"


@needs_openscad
def test_force_overwrites_the_existing_target(tmp_path: Path) -> None:
    """--force is the explicit opt-in that overwrites the existing target."""
    scad = tmp_path / "model.scad"
    scad.write_text("cube(10);")
    out = tmp_path / "out.stl"
    out.write_bytes(b"old")
    done = run_cli(tmp_path, "convert", str(scad), str(out), "--force")
    assert done.returncode == 0, done.stdout + done.stderr
    (count,) = struct.unpack("<I", out.read_bytes()[80:84])
    assert count == 12


def test_output_overwriting_its_input_auto_suffixes(tmp_path: Path) -> None:
    """Converting onto the input's own name auto-suffixes instead of destroying the source."""
    src = tmp_path / "mesh.stl"
    src.write_bytes(b"original mesh")
    done = run_cli(tmp_path, "convert", str(src), str(src))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "instead" in done.stdout
    assert (tmp_path / "mesh-2.stl").read_bytes() == b"original mesh"
    assert src.read_bytes() == b"original mesh"


def test_project_3mf_to_stl_keeps_world_coordinates(tmp_path: Path) -> None:
    """A placed object in a project 3mf lands in the STL where it sat on the plate."""
    src = _tiny_project(tmp_path / "spoon.3mf")
    out = tmp_path / "spoon.stl"
    done = run_cli(tmp_path, "convert", str(src), str(out))
    assert done.returncode == 0, done.stdout + done.stderr
    verts = _stl_vertices(out)
    assert len(verts) == 36  # 12 triangles
    xs = sorted(v[0] for v in verts)
    ys = sorted(v[1] for v in verts)
    zs = sorted(v[2] for v in verts)
    # plate 1 of the 2-plate grid sits at x = 324 (270 mm bed + 1/5 gap); the cube spans 10 mm
    assert xs[0] == pytest.approx(324.0) and xs[-1] == pytest.approx(334.0)
    assert ys[0] == pytest.approx(0.0) and ys[-1] == pytest.approx(10.0)
    assert zs[0] == pytest.approx(0.0) and zs[-1] == pytest.approx(10.0)


def _stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    """Vertices of a binary STL as written (triangle soup, duplicates included)."""
    data = path.read_bytes()
    (count,) = struct.unpack("<I", data[80:84])
    verts: list[tuple[float, float, float]] = []
    for i in range(count):
        base = 84 + i * 50 + 12  # skip the facet normal
        vals = struct.unpack("<9f", data[base : base + 36])
        verts.extend((vals[j], vals[j + 1], vals[j + 2]) for j in (0, 3, 6))
    return verts


def test_unknown_input_format_is_a_structured_error(tmp_path: Path) -> None:
    """An unsupported input format is a structured error that names the accepted ones."""
    src = tmp_path / "model.step"
    src.write_text("ISO-10303-21;")
    done = run_cli(tmp_path, "convert", str(src), str(tmp_path / "out.stl"))
    assert done.returncode == 2
    assert "unknown input format" in done.stderr
    assert ".scad" in done.stderr


def test_3mf_to_3mf_with_to_delegates_to_the_real_retarget(tmp_path: Path) -> None:
    """`3d convert spoon.3mf spoon-k1.3mf --to k1` is `3d retarget` under the hood."""
    from retarget_fixtures import TWO, make_project, write_presets

    src = make_project(tmp_path / "spoon.3mf", TWO)
    presets = write_presets(tmp_path / "presets")
    out = tmp_path / "spoon-k1.3mf"
    done = run_cli(
        tmp_path,
        "convert",
        str(src),
        str(out),
        "--to",
        "testk1",
        "--preset-dir",
        str(presets),
        env_extra={"THREED_PROFILES_DIR": "/nonexistent-profiles"},
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VALIDATION: PASS" in done.stdout
    assert "printer_model: Snapmaker U1 -> Test K1" in done.stdout
    with zipfile.ZipFile(out) as zf:
        settings = json.loads(zf.read("Metadata/project_settings.config"))
    assert settings["printer_settings_id"] == "Test K1 0.4 nozzle"


@pytest.mark.skipif(not openscad_exports_3mf_color(), reason=SCAD_COLOR_REASON)
def test_coloured_scad_to_usdz_is_one_mesh_per_colour(tmp_path: Path) -> None:
    """usdz output keeps the colours too; needs pxr in-process to verify the package."""
    pytest.importorskip("pxr")  # usd-core is an on-demand tier, not a core dep
    scad = tmp_path / "mole.scad"
    scad.write_text(COLOURED_SCAD)
    out = tmp_path / "mole.usdz"
    done = run_cli(tmp_path, "convert", str(scad), str(out), timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr

    from pxr import Usd, UsdGeom  # type: ignore[import-untyped]  # usd-core

    stage = Usd.Stage.Open(str(out))
    assert stage is not None
    meshes = [UsdGeom.Mesh(p) for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    assert len(meshes) == 2
    assert len({tuple(m.GetDisplayColorAttr().Get()[0]) for m in meshes}) == 2


@needs_openscad
def test_png_output_renders_the_model(tmp_path: Path) -> None:
    """A png output is a real rendered picture of the model, not an empty stub."""
    scad = tmp_path / "model.scad"
    scad.write_text("cube(10);")
    out = tmp_path / "shot.png"
    done = run_cli(tmp_path, "convert", str(scad), str(out))
    assert done.returncode == 0, done.stdout + done.stderr
    # The model is on screen: background plus a real share of model pixels (file size
    # can't tell a flat-shaded render from a blank frame).
    colours = png_colour_counts(out)
    background = colours.most_common(1)[0][0]
    assert sum(n for c, n in colours.items() if c != background) >= 0.05 * 1200 * 900
    assert png_size(out) == (1200, 900)
