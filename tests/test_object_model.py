from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from errors import InvalidArgument
from object_model import model_to_dict, parse_scad_annotations, select_nodes

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


SCAD = """
// @id body
// @class structural removable
// @anchor mount pos=[0,1.5,-2] dir=[0,0,1] note="mount face"
// @color #336699
cube([10, 20, 30]);

// @id handle
// @class cosmetic removable
// @color red
translate([0, 0, 30]) sphere(5);
"""


def test_parse_scad_annotations_attach_to_next_code_line() -> None:
    model = parse_scad_annotations(SCAD, source="fixture.scad")

    assert len(model.nodes) == 2
    assert model.nodes[0].id == "body"
    assert model.nodes[0].classes == ("structural", "removable")
    assert model.nodes[0].style == {"color": "#336699"}
    assert model.nodes[0].anchors[0].name == "mount"
    assert model.nodes[0].anchors[0].pos == (0.0, 1.5, -2.0)
    assert model.nodes[0].anchors[0].direction == (0.0, 0.0, 1.0)
    assert model.nodes[0].anchors[0].note == "mount face"
    assert model.nodes[0].code == "cube([10, 20, 30]);"

    doc = model_to_dict(model, [model.nodes[0]])
    assert doc["source"] == "fixture.scad"
    assert doc["nodes"][0]["id"] == "body"
    assert doc["anchors"][0]["node"] == "body"
    assert doc["styles"][0] == {"node": "body", "style": {"color": "#336699"}}


def test_plain_comments_do_not_consume_pending_annotations() -> None:
    model = parse_scad_annotations(
        "// @id body\n"
        "// explanatory OpenSCAD comment\n"
        "cube(1);\n"
    )

    assert len(model.nodes) == 1
    assert model.nodes[0].id == "body"
    assert model.nodes[0].line == 3
    assert model.nodes[0].code == "cube(1);"


def test_annotation_text_inside_string_literal_is_ignored() -> None:
    model = parse_scad_annotations(
        '// @id body\n'
        'text("// @id label");\n'
        'echo("escaped quote \\" // @class bogus");\n'
    )

    assert len(model.nodes) == 1
    assert model.nodes[0].id == "body"
    assert model.nodes[0].code == 'text("// @id label");'


def test_selectors_match_id_class_and_combined_classes() -> None:
    model = parse_scad_annotations(SCAD)

    assert [node.id for node in select_nodes(model, "#body")] == ["body"]
    assert [node.id for node in select_nodes(model, ".removable")] == ["body", "handle"]
    assert [node.id for node in select_nodes(model, ".structural.removable")] == ["body"]


def test_unsupported_descendant_and_operation_expressions_raise_structured_error() -> None:
    model = parse_scad_annotations(SCAD)

    with pytest.raises(InvalidArgument) as descendant:
        select_nodes(model, ".structural .removable")
    assert descendant.value.exit_code == 2
    assert "Descendant selectors" in descendant.value.render()

    with pytest.raises(InvalidArgument) as operation:
        select_nodes(model, ".select(\"#body\")")
    assert operation.value.exit_code == 2
    assert "Transform/query operations" in operation.value.render()


@pytest.mark.parametrize("selector", [".structural.", ".structural..removable", "."])
def test_malformed_class_selectors_raise_structured_error(selector: str) -> None:
    model = parse_scad_annotations(SCAD)

    with pytest.raises(InvalidArgument) as got:
        select_nodes(model, selector)

    assert got.value.exit_code == 2
    assert got.value.flag == "selector"


def test_malformed_anchor_note_raises_structured_error() -> None:
    with pytest.raises(InvalidArgument) as got:
        parse_scad_annotations('// @anchor bad pos=[0,0,0] dir=[0,0,1] note="\\x"\ncube(1);')

    assert got.value.exit_code == 2
    assert "@anchor" in got.value.message
    assert "valid quoted string" in got.value.render()


def test_om_command_prints_matching_json(tmp_path: Path) -> None:
    scad = tmp_path / "annotated.scad"
    scad.write_text(SCAD, encoding="utf-8")

    r = subprocess.run(
        [sys.executable, _THREED, "om", str(scad), ".structural.removable"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert [node["id"] for node in payload["nodes"]] == ["body"]
    assert payload["anchors"][0]["name"] == "mount"
    assert payload["styles"][0]["style"]["color"] == "#336699"


def test_om_command_rejects_unsupported_expressions(tmp_path: Path) -> None:
    scad = tmp_path / "annotated.scad"
    scad.write_text(SCAD, encoding="utf-8")

    r = subprocess.run(
        [sys.executable, _THREED, "om", str(scad), ".structural .removable"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert r.returncode == 2
    assert "Descendant selectors" in r.stderr
