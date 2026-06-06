from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import axis
from errors import InvalidArgument, UsageError

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO
    return subprocess.run([sys.executable, _THREED, *args], capture_output=True, text=True, env=env)


def test_validate_named_axis_normalizes_signed_alias() -> None:
    info = axis.validate_axis("-z")

    assert info == {
        "kind": "axis",
        "name": "-Z",
        "axis": "Z",
        "sign": -1,
        "vector": [0.0, 0.0, -1.0],
    }


def test_validate_plane_returns_normal_axis() -> None:
    info = axis.validate_plane("yz")

    assert info == {
        "kind": "plane",
        "name": "YZ",
        "normal_axis": "X",
        "normal_vector": [1.0, 0.0, 0.0],
    }


def test_validate_view_exposes_camera_direction() -> None:
    info = axis.validate_view("front-right")

    assert info["kind"] == "view"
    assert info["name"] == "front-right"
    assert info["direction"] == [0.640856, -0.640856, 0.422618]


def test_validate_camera_vector_parses_six_numbers() -> None:
    info = axis.validate_camera("1,-2,3,4.5,0,6")

    assert info == {
        "kind": "camera",
        "camera": [1.0, -2.0, 3.0, 4.5, 0.0, 6.0],
        "eye": [1.0, -2.0, 3.0],
        "center": [4.5, 0.0, 6.0],
        "direction": [3.5, 2.0, 3.0],
    }


def test_invalid_values_raise_structured_errors() -> None:
    with pytest.raises(InvalidArgument):
        axis.validate_axis("north")
    with pytest.raises(InvalidArgument):
        axis.validate_plane("ZZ")
    with pytest.raises(UsageError):
        axis.validate_camera("1,2,3")
    with pytest.raises(UsageError):
        axis.validate_camera("0,0,0,0,0,0")


def test_axis_command_json_output_is_deterministic() -> None:
    r = _run(["axis", "plane", "xy", "--json"])

    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {
        "kind": "plane",
        "name": "XY",
        "normal_axis": "Z",
        "normal_vector": [0.0, 0.0, 1.0],
    }
    assert r.stdout == (
        '{"kind":"plane","name":"XY","normal_axis":"Z",'
        '"normal_vector":[0.0,0.0,1.0]}\n'
    )


def test_axis_command_text_output_is_stable() -> None:
    r = _run(["axis", "camera", "1,-1,1,0,0,0"])

    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "kind: camera\n"
        "camera: 1.0,-1.0,1.0,0.0,0.0,0.0\n"
        "eye: 1.0,-1.0,1.0\n"
        "center: 0.0,0.0,0.0\n"
        "direction: -1.0,1.0,-1.0\n"
    )


def test_axis_command_no_args_and_help_contract() -> None:
    no_args = _run(["axis"])
    help_r = _run(["axis", "--help"])

    assert no_args.returncode == 1
    assert "3d axis" in no_args.stdout
    assert help_r.returncode == 0
    assert "3d axis" in help_r.stdout


def test_axis_command_bad_arity_uses_structured_usage_error() -> None:
    r = _run(["axis", "plane"])

    assert r.returncode == 2
    assert "3d axis" in r.stdout
    assert "axis: expected <axis|plane|view|camera> and <value>" in r.stderr
