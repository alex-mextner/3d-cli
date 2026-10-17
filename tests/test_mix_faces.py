"""Unit tests for the mix-face check (slicing.mix_faces) on synthetic Orca-family project 3MFs."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from head_sequence_fixtures import project_3mf as metadata_only_project
from mix_fixtures import Obj, Part, box, gable, orca_project, rot_x, tile
from slicing.mix_faces import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    ProjectError,
    check,
    classify,
    preflight_check,
)


def _one(path: Path):
    report = check(str(path))
    assert len(report.parts) == 1, report.parts
    return report.parts[0]


def test_flat_tile_in_a_mix_fails_with_its_areas(tmp_path: Path) -> None:
    part = _one(orca_project(tmp_path / "p.3mf", [tile("PTA", 5)]))
    assert part.status == FAIL
    assert part.recipe == "431"
    assert part.top == pytest.approx(400.0)
    assert part.side == pytest.approx(240.0)
    assert "visible face horizontal" in part.detail


def test_standing_tile_in_a_mix_passes(tmp_path: Path) -> None:
    part = _one(orca_project(tmp_path / "p.3mf", [tile("PTA", 5, standing=True)]))
    assert part.status == PASS
    assert part.top == pytest.approx(60.0)
    assert part.side == pytest.approx(920.0)


def test_build_transform_is_applied_flat_tile_rotated_upright_passes(
    tmp_path: Path,
) -> None:
    part = _one(orca_project(tmp_path / "p.3mf", [tile("PTA", 5, rot=rot_x(90))]))
    assert part.status == PASS
    assert part.top == pytest.approx(60.0, abs=1e-6)


def test_component_transform_is_applied(tmp_path: Path) -> None:
    obj = Obj("W", [Part("wall", box(20, 20, 3), 5, rot=rot_x(90))], extruder=5)
    assert _one(orca_project(tmp_path / "p.3mf", [obj])).status == PASS


def test_part_in_a_physical_filament_is_not_checked(tmp_path: Path) -> None:
    report = check(str(orca_project(tmp_path / "p.3mf", [tile("BASE", 1)])))
    assert report.parts == []
    assert report.status == SKIP
    assert preflight_check(str(tmp_path / "p.3mf"))["status"] == SKIP


def test_local_z_ratio_row_is_checked_too(tmp_path: Path) -> None:
    part = _one(orca_project(tmp_path / "p.3mf", [tile("ROOF", 6)]))
    assert part.recipe == "local-z"
    assert part.status == FAIL


def test_45_degree_roof_in_a_mix_warns(tmp_path: Path) -> None:
    obj = Obj("ROOF", [Part("roof", gable(40, 20, 10), 5)], extruder=5)
    part = _one(orca_project(tmp_path / "p.3mf", [obj]))
    assert part.status == WARN
    assert part.top == pytest.approx(0.0)
    assert part.slope == pytest.approx(2 * 40 * 10 * 2**0.5)


def test_part_extruder_zero_inherits_the_mixed_object_filament(tmp_path: Path) -> None:
    obj = Obj("PTB", [Part("PTB", box(20, 20, 3), 0)], extruder=5)
    assert _one(orca_project(tmp_path / "p.3mf", [obj])).status == FAIL


def test_only_the_mixed_part_of_a_multi_part_object_is_checked(tmp_path: Path) -> None:
    base = Part(
        "foot", box(30, 10, 2), 1, offset=(0, 0, -11)
    )  # flat, physical filament
    wall = Part("wall", box(20, 3, 20), 5)  # standing, mixed
    report = check(
        str(orca_project(tmp_path / "p.3mf", [Obj("SW", [base, wall], extruder=1)]))
    )
    assert [(p.part, p.status) for p in report.parts] == [("wall", PASS)]


def test_preflight_check_names_the_failing_part(tmp_path: Path) -> None:
    proj = orca_project(
        tmp_path / "p.3mf", [tile("PTA", 5), tile("OK", 5, standing=True)]
    )
    result = preflight_check(str(proj))
    assert result["status"] == FAIL
    assert (
        result["detail"]
        == "1 of 2 mixed part(s) show their colour on a horizontal face"
    )
    lines = result["lines"]
    assert (
        isinstance(lines, list)
        and len(lines) == 1
        and lines[0].startswith("FAIL PTA / PTA")
    )


def test_not_a_project_is_a_project_error_and_a_preflight_skip(tmp_path: Path) -> None:
    junk = tmp_path / "junk.3mf"
    junk.write_bytes(b"not a zip")
    with pytest.raises(ProjectError):
        check(str(junk))
    assert preflight_check(str(junk))["status"] == SKIP


def test_mixed_project_without_geometry_is_a_project_error(tmp_path: Path) -> None:
    proj = metadata_only_project(tmp_path / "p.3mf", {"A": 5})
    with pytest.raises(ProjectError, match="no build items"):
        check(str(proj))


def test_missing_sub_model_is_reported(tmp_path: Path) -> None:
    proj = orca_project(tmp_path / "p.3mf", [tile("PTA", 5)])
    broken = tmp_path / "broken.3mf"
    with zipfile.ZipFile(proj) as src, zipfile.ZipFile(broken, "w") as dst:
        for item in src.infolist():
            if not item.filename.startswith("3D/Objects/"):
                dst.writestr(item, src.read(item.filename))
    with pytest.raises(ProjectError, match="missing model part"):
        check(str(broken))


@pytest.mark.parametrize(
    ("top", "side", "slope", "status"),
    [
        (100, 201, 0, PASS),
        (100, 200, 0, PASS),
        (101, 200, 0, FAIL),
        (0, 200, 101, WARN),
        (0, 200, 100, PASS),
    ],
)
def test_rule_boundaries(top: float, side: float, slope: float, status: str) -> None:
    assert classify(top, side, slope)[0] == status


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [('x="10.000000"', 'x="nan"', "not finite"), ('v1="0"', 'v1="-1"', "missing vertex")],
)
def test_malformed_geometry_is_a_project_error_not_a_pass(tmp_path: Path, old: str, new: str, message: str) -> None:
    proj = orca_project(tmp_path / "p.3mf", [tile("PTA", 5, standing=True)])
    broken = tmp_path / "broken.3mf"
    with zipfile.ZipFile(proj) as src, zipfile.ZipFile(broken, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("3D/Objects/"):
                data = data.decode().replace(old, new, 1).encode()
            dst.writestr(item, data)
    with pytest.raises(ProjectError, match=message):
        check(str(broken))
    assert preflight_check(str(broken))["status"] == SKIP
