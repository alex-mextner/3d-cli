"""Tests for the 3d.yaml project model + loader (lib/project.py, ROADMAP §5/§15)."""
from __future__ import annotations

import pytest

import project
from project import ProjectError


def _write(tmp_path, yaml_text: str, *, parts=None):
    (tmp_path / "3d.yaml").write_text(yaml_text, encoding="utf-8")
    for rel in parts or []:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("cube(10);\n", encoding="utf-8")


def test_find_project_walks_up(tmp_path):
    _write(tmp_path, "project:\n  name: demo\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    found = project.find_project(sub)
    assert found == (tmp_path / "3d.yaml").resolve()


def test_find_project_none_when_absent(tmp_path):
    assert project.find_project(tmp_path) is None


def test_load_minimal(tmp_path):
    _write(tmp_path, "project:\n  name: widget\n  units: mm\n")
    p = project.load_project(tmp_path)
    assert p.name == "widget"
    assert p.units == "mm"
    assert p.parts == {}
    assert p.root == tmp_path.resolve()


def test_load_name_defaults_to_dir(tmp_path):
    _write(tmp_path, "project:\n  units: mm\n")
    p = project.load_project(tmp_path)
    assert p.name == tmp_path.resolve().name


def test_part_path_resolved_against_root(tmp_path):
    _write(
        tmp_path,
        "project:\n  name: a\nparts:\n  body:\n    file: parts/body.scad\n    tags: [structural]\n    material: PETG\n",
        parts=["parts/body.scad"],
    )
    p = project.load_project(tmp_path)
    body = p.parts["body"]
    assert body.path == (tmp_path / "parts" / "body.scad").resolve()
    assert body.file == "parts/body.scad"
    assert body.tags == ["structural"]
    assert body.material == "PETG"
    assert body.copies == 1


def test_missing_part_file_raises(tmp_path):
    _write(tmp_path, "project:\n  name: a\nparts:\n  body:\n    file: parts/missing.scad\n")
    with pytest.raises(ProjectError) as exc:
        project.load_project(tmp_path)
    assert "missing.scad" in str(exc.value)


def test_check_files_false_skips_existence(tmp_path):
    _write(tmp_path, "project:\n  name: a\nparts:\n  body:\n    file: parts/missing.scad\n")
    p = project.load_project(tmp_path, check_files=False)
    assert "body" in p.parts


def test_part_without_file_raises(tmp_path):
    _write(tmp_path, "project:\n  name: a\nparts:\n  body:\n    tags: [shell]\n")
    with pytest.raises(ProjectError):
        project.load_project(tmp_path, check_files=False)


def test_no_project_file_raises(tmp_path):
    with pytest.raises(ProjectError):
        project.load_project(tmp_path)


def test_bad_yaml_raises(tmp_path):
    (tmp_path / "3d.yaml").write_text("project:\n  name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProjectError):
        project.load_project(tmp_path)


def test_raw_preserves_unknown_keys(tmp_path):
    _write(tmp_path, "project:\n  name: a\n  custom_future_key: 42\n")
    p = project.load_project(tmp_path)
    assert p.raw["project"]["custom_future_key"] == 42


def test_load_project_config_sections_anchors_loads_and_gates(tmp_path):
    _write(
        tmp_path,
        """project:
  name: fixture
anchors:
  mount:
    pos: [10, 0, 3.5]
    dir: [0, 0, 1]
    area: 12.5
    note: screw boss
sections:
  center:
    preset: mid-z
  mount-cut:
    through: mount
    plane: YZ
    offset: 2
    keep: pos
loads:
  push:
    anchor: mount
    vector: [0, 0, -25]
    note: lid pressure
gates:
  - manifold
  - name: collision
    config: verify/collision.json
    hard: true
parts:
  body:
    file: parts/body.scad
    gates: [manifold, printability]
""",
        parts=["parts/body.scad"],
    )

    p = project.load_project(tmp_path)

    mount = p.anchors["mount"]
    assert mount.pos == [10.0, 0.0, 3.5]
    assert mount.direction == [0.0, 0.0, 1.0]
    assert mount.area == 12.5
    assert mount.note == "screw boss"

    assert p.sections["center"].preset == "mid-z"
    cut = p.sections["mount-cut"]
    assert cut.through == "mount"
    assert cut.plane == "YZ"
    assert cut.offset == 2.0
    assert cut.keep == "pos"

    load = p.loads["push"]
    assert load.anchor == "mount"
    assert load.vector == [0.0, 0.0, -25.0]
    assert load.note == "lid pressure"

    assert [g.name for g in p.gates] == ["manifold", "collision"]
    assert p.gates[1].config == "verify/collision.json"
    assert p.gates[1].hard is True
    assert p.parts["body"].gates == ["manifold", "printability"]


def test_section_shorthand_string_becomes_preset(tmp_path):
    _write(tmp_path, "project:\n  name: a\nsections:\n  z-mid: mid-z\n")
    p = project.load_project(tmp_path)
    assert p.sections["z-mid"].preset == "mid-z"


def test_invalid_anchor_position_raises_project_error(tmp_path):
    _write(tmp_path, "project:\n  name: a\nanchors:\n  bad:\n    pos: [1, 2]\n")
    with pytest.raises(ProjectError) as exc:
        project.load_project(tmp_path)
    assert "anchors.bad.pos" in str(exc.value)


def test_invalid_gate_shape_raises_project_error(tmp_path):
    _write(tmp_path, "project:\n  name: a\ngates:\n  - config: verify/collision.json\n")
    with pytest.raises(ProjectError) as exc:
        project.load_project(tmp_path)
    assert "gates[0]" in str(exc.value)


def test_gate_mapping_rejects_falsey_scalar_values(tmp_path):
    _write(tmp_path, "project:\n  name: a\ngates:\n  collision: false\n")
    with pytest.raises(ProjectError) as exc:
        project.load_project(tmp_path)
    assert "gates.collision" in str(exc.value)


def test_gate_mapping_key_is_authoritative_over_inner_name(tmp_path):
    _write(
        tmp_path,
        "project:\n  name: a\ngates:\n  collision:\n    name: manifold\n    config: verify/collision.json\n",
    )

    loaded = project.load_project(tmp_path)

    assert loaded.gates[0].name == "collision"
    assert loaded.gates[0].config == "verify/collision.json"
