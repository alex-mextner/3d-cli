"""Readable e2e workflows for `3d scad` (#560): the real bin/3d driving OpenSCAD on tiny models."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .workflow_helper import run_cli

pytestmark = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="openscad not installed"
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_export_stl_of_a_cube_passes_the_geometry_check(tmp_path: Path) -> None:
    """A modeller exports a cube and sees PASSED with a real binary STL."""
    cube = _write(tmp_path, "cube.scad", "cube(10);\n")
    out = tmp_path / "out" / "cube.stl"

    result = run_cli(tmp_path, "scad", "export-stl", str(cube), str(out))

    assert result.returncode == 0, result.stderr
    assert "STATUS: PASSED" in result.stdout
    assert "Triangles: 12" in result.stdout
    assert out.stat().st_size == 84 + 12 * 50


def test_export_stl_defines_reach_the_model(tmp_path: Path) -> None:
    """-D overrides change the exported geometry (ASCII STL shows the bigger cube)."""
    model = _write(tmp_path, "box.scad", "w = 5;\ncube(w);\n")
    out = tmp_path / "box.stl"

    result = run_cli(
        tmp_path, "scad", "export-stl", str(model), str(out), "-D", "w=7", "--ascii"
    )

    assert result.returncode == 0, result.stderr
    assert "vertex 7 7 7" in out.read_text(encoding="utf-8")


def test_export_stl_fails_when_assert_fails_even_though_openscad_exits_zero(
    tmp_path: Path,
) -> None:
    """A failed assert() must not produce a green export."""
    model = _write(tmp_path, "bad.scad", 'assert(false, "wall too thin");\ncube(10);\n')
    out = tmp_path / "bad.stl"

    result = run_cli(tmp_path, "scad", "export-stl", str(model), str(out))

    assert result.returncode == 1
    assert "STATUS: FAILED" in result.stdout
    assert "wall too thin" in result.stdout


def test_export_stl_fails_on_non_manifold_geometry(tmp_path: Path) -> None:
    """An open pyramid (a missing face) is not a closed solid: export reports FAILED."""
    model = _write(
        tmp_path,
        "open.scad",
        "union() {\n"
        "  polyhedron(points=[[0,0,0],[10,0,0],[10,10,0],[0,10,0],[5,5,10]],\n"
        "             faces=[[0,1,4],[1,2,4],[2,3,4]]);\n"
        "  translate([20,0,0]) cube(3);\n"
        "}\n",
    )
    out = tmp_path / "open.stl"

    result = run_cli(tmp_path, "scad", "export-stl", str(model), str(out))

    assert result.returncode == 1
    assert "STATUS: FAILED" in result.stdout
    assert "manifold" in result.stdout.lower()


def test_export_stl_missing_input_is_a_usage_error(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "scad",
        "export-stl",
        str(tmp_path / "nope.scad"),
        str(tmp_path / "o.stl"),
    )

    assert result.returncode == 2
    assert "file not found" in result.stderr


def test_validate_reports_syntax_ok_and_failure(tmp_path: Path) -> None:
    good = _write(tmp_path, "good.scad", "echo(hello=1);\ncube(1);\n")
    bad = _write(tmp_path, "bad.scad", 'assert(false, "nope");\ncube(1);\n')

    ok = run_cli(tmp_path, "scad", "validate", str(good))
    fail = run_cli(tmp_path, "scad", "validate", str(bad))

    assert ok.returncode == 0 and "Syntax OK" in ok.stdout and "hello" in ok.stdout
    assert fail.returncode == 1 and "Validation failed" in fail.stdout


def test_params_lists_customizer_parameters_as_json(tmp_path: Path) -> None:
    model = _write(
        tmp_path,
        "p.scad",
        "width = 40; // [10:100] Box width\n"
        'mode = "a"; // [a, b] Mode\n'
        "module m() { inner = 3; }\n",
    )

    result = run_cli(tmp_path, "scad", "params", str(model), "--json")

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [r["name"] for r in rows] == ["width", "mode"]
    assert rows[0] == {
        "name": "width",
        "value": "40",
        "type": "integer",
        "range": "10:100",
        "description": "Box width",
    }
    assert rows[1]["options"] == "a, b"


def test_next_version_counts_from_existing_files(tmp_path: Path) -> None:
    (tmp_path / "bracket_001.scad").write_text("", encoding="utf-8")
    (tmp_path / "bracket_002.scad").write_text("", encoding="utf-8")

    result = run_cli(tmp_path, "scad", "next-version", "bracket")
    fresh = run_cli(tmp_path, "scad", "next-version", "other")

    assert "Create: bracket_003.scad" in result.stdout
    assert "No existing versions for 'other'" in fresh.stdout


def test_section_writes_png_and_removes_its_temp_file(tmp_path: Path) -> None:
    """The temp section file sits next to the model (use<> resolves) and is gone afterwards."""
    model = _write(
        tmp_path, "part.scad", "module shaft() { cylinder(h=40, r=10, $fn=24); }\n"
    )
    out = tmp_path / "img" / "cut.png"

    result = run_cli(
        tmp_path,
        "scad",
        "section",
        str(model),
        str(out),
        "--module=shaft()",
        "--plane=XZ",
        "--size=200x150",
        timeout=240,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_file() and out.stat().st_size > 0
    assert not list(tmp_path.glob(".section_tmp_*.scad"))


def test_section_requires_module(tmp_path: Path) -> None:
    model = _write(tmp_path, "part.scad", "cube(1);\n")

    result = run_cli(tmp_path, "scad", "section", str(model), str(tmp_path / "c.png"))

    assert result.returncode == 2
    assert "--module" in result.stderr


def test_section_color_renders_an_assembly_honouring_the_cut_contract(
    tmp_path: Path,
) -> None:
    model = _write(
        tmp_path,
        "asm.scad",
        "cut = false;\n"
        'section_plane = "YZ";\nsection_keep = "neg";\n'
        "module half() { if (cut) difference() { children(); translate([0,-50,-50]) cube(100); } "
        "else children(); }\n"
        'color("red") half() cube(20, center=true);\n',
    )
    out = tmp_path / "sc.png"

    result = run_cli(
        tmp_path,
        "scad",
        "section-color",
        str(model),
        str(out),
        "--dist=80",
        "--size=200x150",
        timeout=240,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Camera (eye+center): 80.0000,23.2000,40.0000,0.0,0.0,0.0" in result.stdout
    assert out.is_file()


def test_preview_and_multi_preview_write_pngs(tmp_path: Path) -> None:
    model = _write(tmp_path, "cube.scad", "cube(10);\n")
    single = tmp_path / "one.png"
    gallery = tmp_path / "gallery"

    one = run_cli(
        tmp_path,
        "scad",
        "preview",
        str(model),
        str(single),
        "--size=200x150",
        timeout=240,
    )
    many = run_cli(
        tmp_path, "scad", "multi-preview", str(model), str(gallery), timeout=480
    )

    assert one.returncode == 0, one.stderr
    assert single.is_file()
    assert many.returncode == 0, many.stderr
    assert sorted(p.name for p in gallery.glob("*.png")) == [
        f"cube_{a}.png" for a in ("back", "front", "iso", "left", "right", "top")
    ]


def test_help_lists_every_subcommand(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "scad", "--help")

    assert result.returncode == 0
    for sub in (
        "validate",
        "preview",
        "multi-preview",
        "export-stl",
        "params",
        "section",
        "section-color",
        "next-version",
    ):
        assert sub in result.stdout
