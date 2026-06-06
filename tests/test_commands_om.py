"""Unit tests for commands.om — object model inspection."""
from __future__ import annotations

import json
import pathlib

import pytest
from commands.om import run
from errors import InputNotFound, InvalidArgument, UsageError


def _write_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    scad = tmp_path / "camera-bracket.scad"
    scad.write_text(
        """// @id base
// @class structural printed
// @anchor mount-left pos=[0,0,0] dir=[0,0,1] note="left screw"
// @anchor mount-right pos=[40,0,0] dir=[0,0,1]
// @color orange
cube([44, 18, 4]);

// @id cover
// @class cosmetic printed removable
// @color #33ccff
translate([0, 0, 6]) cube([44, 18, 2]);
""",
        encoding="utf-8",
    )
    return scad


def test_om_no_args_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert run([]) == 1
    assert "3d om <file.scad> <selector>" in capsys.readouterr().out


def test_om_help_prints_selector_contract(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--help"]) == 0
    output = capsys.readouterr().out
    assert "Supported annotations" in output
    assert "#id" in output
    assert ".class.other" in output


def test_om_id_selector_prints_selected_node_anchors_and_style(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scad = _write_fixture(tmp_path)

    assert run([str(scad), "#base"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == str(scad)
    assert [node["id"] for node in payload["nodes"]] == ["base"]
    assert payload["nodes"][0]["classes"] == ["structural", "printed"]
    assert payload["nodes"][0]["code"] == "cube([44, 18, 4]);"
    assert [anchor["name"] for anchor in payload["anchors"]] == ["mount-left", "mount-right"]
    assert payload["anchors"][0] == {
        "dir": [0.0, 0.0, 1.0],
        "line": 3,
        "name": "mount-left",
        "node": "base",
        "note": "left screw",
        "pos": [0.0, 0.0, 0.0],
    }
    assert payload["styles"] == [{"node": "base", "style": {"color": "orange"}}]


def test_om_combined_class_selector_filters_intersection(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scad = _write_fixture(tmp_path)

    assert run([str(scad), ".printed.removable"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [node["id"] for node in payload["nodes"]] == ["cover"]
    assert payload["anchors"] == []
    assert payload["styles"] == [{"node": "cover", "style": {"color": "#33ccff"}}]


def test_om_missing_file_is_structured() -> None:
    with pytest.raises(InputNotFound) as excinfo:
        run(["missing.scad", "#base"])

    assert excinfo.value.exit_code == 2
    assert excinfo.value.command == "om"


def test_om_unknown_selector_is_structured(tmp_path: pathlib.Path) -> None:
    scad = _write_fixture(tmp_path)

    with pytest.raises(InvalidArgument):
        run([str(scad), "nope"])


def test_om_reserved_transform_expression_is_structured(tmp_path: pathlib.Path) -> None:
    scad = _write_fixture(tmp_path)

    with pytest.raises(InvalidArgument) as excinfo:
        run([str(scad), ".printed | color(red)"])

    assert excinfo.value.remediation == [
        "Descendant selectors are reserved for the future object-model tree; this slice supports only flat #id, .class, and .a.b selectors."
    ]


def test_om_rejects_extra_args() -> None:
    with pytest.raises(UsageError) as excinfo:
        run(["model.scad", "#base", "--json"])

    assert excinfo.value.exit_code == 2
    assert "expected <file.scad> and <selector>" in str(excinfo.value)
