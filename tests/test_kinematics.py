"""Tests for the kinematics project slice and `3d kinematics` command."""
from __future__ import annotations

import json

import pytest

from errors import UsageError


def _write_project(tmp_path, yaml_text: str) -> None:
    (tmp_path / "3d.yaml").write_text(yaml_text, encoding="utf-8")
    for rel in ("parts/base.scad", "parts/arm.scad", "parts/gripper.scad"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("cube(1);\n", encoding="utf-8")


VALID_PROJECT = """project:
  name: robot
  units: mm
parts:
  base:
    file: parts/base.scad
  arm:
    file: parts/arm.scad
  gripper:
    file: parts/gripper.scad
kinematics:
  joints:
    slide:
      type: prismatic
      parent: arm
      child: gripper
      axis: [2, 0, 0]
      limits: [0, 25]
    shoulder:
      type: revolute
      parent: base
      child: arm
      axis: [0, 0, 2]
      origin: [1, 2, 3]
      limits: [-90, 90]
"""


def test_summary_normalizes_and_sorts_joints(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, VALID_PROJECT)
    summary = kinematics.summarize_project(tmp_path)

    assert summary == {
        "joint_count": 2,
        "joints": [
            {
                "axis": [0.0, 0.0, 1.0],
                "child": "arm",
                "limits": {"max": 90.0, "min": -90.0, "units": "deg"},
                "name": "shoulder",
                "origin": [1.0, 2.0, 3.0],
                "parent": "base",
                "type": "revolute",
            },
            {
                "axis": [1.0, 0.0, 0.0],
                "child": "gripper",
                "limits": {"max": 25.0, "min": 0.0, "units": "mm"},
                "name": "slide",
                "origin": [0.0, 0.0, 0.0],
                "parent": "arm",
                "type": "prismatic",
            },
        ],
        "parts": ["arm", "base", "gripper"],
        "project": "robot",
        "units": "mm",
    }


def test_summary_json_is_deterministic(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, VALID_PROJECT)
    text = kinematics.summary_json(kinematics.summarize_project(tmp_path))

    assert json.loads(text)["joint_count"] == 2
    assert text == kinematics.summary_json(kinematics.summarize_project(tmp_path))
    assert text.endswith("\n")


def test_missing_kinematics_joints_raises_structured_error(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, "project:\n  name: robot\n")

    with pytest.raises(UsageError) as exc:
        kinematics.summarize_project(tmp_path)

    assert exc.value.command == "kinematics"
    assert "kinematics.joints" in str(exc.value)


def test_unknown_child_part_raises(tmp_path) -> None:
    import kinematics

    _write_project(
        tmp_path,
        VALID_PROJECT.replace("child: gripper", "child: missing"),
    )

    with pytest.raises(UsageError) as exc:
        kinematics.summarize_project(tmp_path)

    assert "unknown child part" in str(exc.value)
    assert "missing" in str(exc.value)


def test_zero_axis_raises(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, VALID_PROJECT.replace("axis: [0, 0, 2]", "axis: [0, 0, 0]"))

    with pytest.raises(UsageError) as exc:
        kinematics.summarize_project(tmp_path)

    assert "axis must not be the zero vector" in str(exc.value)


def test_reversed_limits_raise(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, VALID_PROJECT.replace("limits: [-90, 90]", "limits: [90, -90]"))

    with pytest.raises(UsageError) as exc:
        kinematics.summarize_project(tmp_path)

    assert "limits min must be <= max" in str(exc.value)


def test_non_finite_axis_value_raises(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, VALID_PROJECT.replace("axis: [0, 0, 2]", "axis: [.nan, 0, 2]"))

    with pytest.raises(UsageError) as exc:
        kinematics.summarize_project(tmp_path)

    assert "must be finite" in str(exc.value)


def test_non_finite_limit_value_raises(tmp_path) -> None:
    import kinematics

    _write_project(tmp_path, VALID_PROJECT.replace("limits: [-90, 90]", "limits: [-90, .inf]"))

    with pytest.raises(UsageError) as exc:
        kinematics.summarize_project(tmp_path)

    assert "must be finite" in str(exc.value)


def test_command_prints_json(tmp_path, capsys) -> None:
    from commands import kinematics as cmd

    _write_project(tmp_path, VALID_PROJECT)
    rc = cmd.run([str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert json.loads(out)["project"] == "robot"
    assert '"joint_count": 2' in out


def test_command_help_and_no_args(capsys) -> None:
    from commands import kinematics as cmd

    assert cmd.run(["--help"]) == 0
    assert "kinematics" in capsys.readouterr().out

    assert cmd.run([]) == 1
    assert "kinematics" in capsys.readouterr().out


def test_command_unknown_option_raises() -> None:
    from commands import kinematics as cmd

    with pytest.raises(UsageError):
        cmd.run(["--bogus"])
