"""Focused tests for the export format registry and dry-run planning."""
from __future__ import annotations

import pytest

from errors import InvalidArgument
from export_formats import build_export_plan, get_format, list_export_formats, selector_map


def test_export_registry_lists_supported_and_planned_formats() -> None:
    formats = {fmt.key: fmt for fmt in list_export_formats()}

    assert formats["stl"].status == "supported"
    assert formats["usdz"].route == "usdz"
    assert formats["glb"].status == "planned"
    assert formats["step"].extensions == ("step", "stp")
    assert selector_map()["--usdz"] == "usdz"


def test_get_format_accepts_keys_extensions_and_selectors() -> None:
    assert get_format("3mf").key == "3mf"
    assert get_format(".stp").key == "step"
    assert get_format("--glb").key == "glb"


def test_get_format_rejects_unknown_format() -> None:
    with pytest.raises(InvalidArgument):
        get_format("fbx")


def test_build_export_plan_defaults_to_binary_stl() -> None:
    plan = build_export_plan("part.scad", "", "", ascii_stl=False)

    assert plan.format.key == "stl"
    assert plan.output_path == "part.stl"
    assert "binstl" in plan.steps[0]
    assert any("mesh_check.py" in step for step in plan.steps)


def test_build_export_plan_respects_ascii_stl() -> None:
    plan = build_export_plan("part.scad", "part.stl", "", ascii_stl=True)

    assert plan.format.key == "stl"
    assert "asciistl" in plan.steps[0]
    assert not any("mesh_check.py" in step for step in plan.steps)


def test_build_export_plan_describes_integrated_usdz_path() -> None:
    plan = build_export_plan("part.scad", "", "usdz", ascii_stl=False)

    assert plan.format.key == "usdz"
    assert plan.output_path == "part.usdz"
    assert "OpenSCAD intermediate STL export" in plan.steps[0]
    assert "lib/usdz.py" in plan.steps[-1]


def test_build_export_plan_rejects_mismatched_output_extension() -> None:
    with pytest.raises(InvalidArgument):
        build_export_plan("part.scad", "part.glb", "stl", ascii_stl=False)


def test_build_export_plan_rejects_extensionless_output_without_explicit_format() -> None:
    with pytest.raises(InvalidArgument):
        build_export_plan("part.scad", "part", "", ascii_stl=False)


def test_build_export_plan_allows_extensionless_output_with_explicit_format() -> None:
    plan = build_export_plan("part.scad", "part", "stl", ascii_stl=False)

    assert plan.format.key == "stl"
    assert plan.output_path == "part"


def test_build_export_plan_uses_explicit_openscad_format_for_extensionless_off() -> None:
    plan = build_export_plan("part.scad", "part", "off", ascii_stl=False)

    assert plan.format.key == "off"
    assert plan.output_path == "part"
    assert "--export-format off" in plan.steps[0]
