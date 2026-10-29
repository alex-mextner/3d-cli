"""The assembly spec (`3d.assembly/1`) and the vectors it declares: strict, early, with the accepted values named."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from assembly import spec as S
from assembly.vec import format_direction, parse_direction, parse_point, rotation_taking
from errors import InvalidArgument

BASE: dict[str, Any] = {
    "parts": {"a": {"mesh": "a.stl"}, "b": {"mesh": "b.stl", "role": "stand_in"}},
    "mates": [
        {
            "name": "m",
            "a": "a",
            "b": "b",
            "kind": "snap",
            "insertion": {"axis": "-Z", "distance_mm": 5},
        }
    ],
}


def parse(doc: dict[str, Any]) -> S.AssemblySpec:
    return S.parse_spec(doc, base_dir=Path("/tmp"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+X", (1, 0, 0)),
        ("-y", (0, -1, 0)),
        ("Z", (0, 0, 1)),
        ([0, 3, 4], (0, 0.6, 0.8)),
        ("0,0,-2", (0, 0, -1)),
    ],
)
def test_directions_normalise(raw: Any, expected: tuple[float, float, float]) -> None:
    assert parse_direction(raw, "axis") == pytest.approx(expected)


@pytest.mark.parametrize(
    "bad",
    [[0, 0, 0], [1, 2], "up", "1,2", [math.nan, 0, 1], [math.inf, 0, 0], None, True],
)
def test_bad_directions_fail_before_any_geometry_with_the_accepted_forms(
    bad: Any,
) -> None:
    with pytest.raises(InvalidArgument) as err:
        parse_direction(bad, "mates[0].insertion.axis")
    assert "+X" in str(err.value) and "mates[0].insertion.axis" in str(err.value)


def test_points_and_labels() -> None:
    assert parse_point("1, 2, 3", "p") == (1.0, 2.0, 3.0)
    assert format_direction((0.0, -1.0, 0.0)) == "-Y"
    assert format_direction(parse_direction([1, 1, 0], "d")) == "[0.7071, 0.7071, 0]"


def test_rotation_taking_maps_the_source_onto_the_destination() -> None:
    for src in (
        (0.0, 1.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, -1.0),
        (0.6, 0.0, 0.8),
        (0.0, 0.0, 1.0),
    ):
        rows = rotation_taking(src, (0.0, 0.0, 1.0))
        out = [sum(rows[i][j] * src[j] for j in range(3)) for i in range(3)]
        assert out == pytest.approx((0, 0, 1), abs=1e-9)


def test_minimal_spec_parses_roles_mates_and_defaults() -> None:
    spec = parse(BASE)
    assert spec.parts["a"].role == "printed" and spec.parts["a"].printable
    assert spec.parts["b"].role == "stand_in" and not spec.parts["b"].printable
    mate = spec.mate("m")
    assert (mate.kind, mate.fit) == ("snap", "snap")  # fit defaults from the kind
    assert mate.insertion is not None and mate.insertion.axis == (0.0, 0.0, -1.0)
    assert (
        spec.settings.seat_tol_mm == 0.02
    )  # the #140 default: 6 um seated passes, 0.05 mm does not


def test_a_vitamin_without_a_reference_source_is_refused() -> None:
    doc: dict[str, Any] = {"parts": {"screw": {"mesh": "s.stl", "role": "vitamin"}}}
    with pytest.raises(InvalidArgument) as err:
        parse(doc)
    assert "reference_source" in str(err.value)
    doc["parts"]["screw"]["reference_source"] = {
        "url": "https://example.org/m3",
        "license": "CC-BY-4.0",
    }
    assert parse(doc).parts["screw"].part.reference_source is not None


@pytest.mark.parametrize(
    ("patch", "fragment"),
    [
        ({"mates": [{"a": "a", "b": "ghost", "kind": "snap"}]}, "mates[0].b"),
        ({"mates": [{"a": "a", "b": "a", "kind": "snap"}]}, "mates[0].b"),
        ({"mates": [{"a": "a", "b": "b", "kind": "welded"}]}, "mates[0].kind"),
        (
            {
                "mates": [
                    {
                        "a": "a",
                        "b": "b",
                        "kind": "snap",
                        "insertion": {"axis": [0, 0, 0]},
                    }
                ]
            },
            "insertion.axis",
        ),
        (
            {
                "mates": [
                    {
                        "a": "a",
                        "b": "b",
                        "kind": "snap",
                        "insertion": {"axis": "+X", "part": "ghost"},
                    }
                ]
            },
            "insertion.part",
        ),
        (
            {
                "mates": [
                    {
                        "a": "a",
                        "b": "b",
                        "kind": "snap",
                        "zone": {"min": [1, 1, 1], "max": [0, 0, 0]},
                    }
                ]
            },
            "zone",
        ),
        ({"mates": [{"a": "a", "b": "b", "kind": "snap", "surprise": 1}]}, "surprise"),
        ({"settings": {"step_mm": -1}}, "settings.step_mm"),
        ({"schema": "3d.assembly/9"}, "schema"),
        (
            {
                "loads": [
                    {"part": "a", "point": [0, 0, 0], "force_n": 0, "direction": "+Z"}
                ]
            },
            "force_n",
        ),
    ],
)
def test_invalid_specs_name_the_yaml_path(patch: dict[str, Any], fragment: str) -> None:
    doc = {**BASE, **patch}
    with pytest.raises(InvalidArgument) as err:
        parse(doc)
    assert fragment in str(err.value)


def test_duplicate_mate_names_and_missing_parts() -> None:
    dup = {**BASE, "mates": [BASE["mates"][0], BASE["mates"][0]]}
    with pytest.raises(InvalidArgument):
        parse(dup)
    with pytest.raises(InvalidArgument):
        parse({"mates": []})
    with pytest.raises(InvalidArgument):
        parse(BASE).mate("nope")


def test_pose_ops_follow_openscad_order() -> None:
    import numpy as np
    from assembly.scene import pose_matrix

    pose = S.parse_pose({"rotate": [0, 0, 90], "translate": [10, 0, 0]}, "pose")
    moved = pose_matrix(pose) @ np.array([1.0, 0.0, 0.0, 1.0])
    assert moved[:3] == pytest.approx(
        (10.0, 1.0, 0.0), abs=1e-9
    )  # rotate first, then translate
    chain = S.parse_pose([{"translate": [5, 0, 0]}, {"rotate": [0, 0, 90]}], "pose")
    assert (pose_matrix(chain) @ np.array([0.0, 0.0, 0.0, 1.0]))[:3] == pytest.approx(
        (0.0, 5.0, 0.0), abs=1e-9
    )


def test_unset_environment_variable_in_a_path_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASSEMBLY_TEST_ROOT", raising=False)
    with pytest.raises(InvalidArgument):
        S.expand_vars("$ASSEMBLY_TEST_ROOT/x.stl", "parts.a.mesh")
    monkeypatch.setenv("ASSEMBLY_TEST_ROOT", "/data")
    assert S.expand_vars("$ASSEMBLY_TEST_ROOT/x.stl", "parts.a.mesh") == "/data/x.stl"


def test_loading_a_spec_from_a_project_directory(tmp_path: Path) -> None:
    import yaml

    (tmp_path / "assembly.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    assert S.load_spec(tmp_path).name == "assembly"
    with pytest.raises(Exception, match="not found"):
        S.load_spec(tmp_path / "nope")


