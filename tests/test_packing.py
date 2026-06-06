"""Tests for deterministic bed packing plans and the `3d pack` command."""
from __future__ import annotations

import json
import math

import pytest

from commands import pack as pack_cmd
from errors import UsageError
from packing import PartSpec, plan_bed_layout


def test_plan_shelf_layout_is_deterministic() -> None:
    plan = plan_bed_layout(
        [PartSpec("a", 50, 30), PartSpec("b", 40, 20, quantity=2)],
        bed_width=100,
        bed_depth=60,
        gap=5,
    )

    assert [p.to_dict() for p in plan.placements] == [
        {
            "name": "a",
            "index": 1,
            "x": 0.0,
            "y": 0.0,
            "width": 50.0,
            "depth": 30.0,
            "rotated": False,
        },
        {
            "name": "b",
            "index": 1,
            "x": 55.0,
            "y": 0.0,
            "width": 40.0,
            "depth": 20.0,
            "rotated": False,
        },
        {
            "name": "b",
            "index": 2,
            "x": 0.0,
            "y": 35.0,
            "width": 40.0,
            "depth": 20.0,
            "rotated": False,
        },
    ]


def test_plan_rotates_part_when_that_is_the_only_fit() -> None:
    plan = plan_bed_layout([PartSpec("long", 70, 30)], bed_width=40, bed_depth=80)

    assert plan.placements[0].to_dict() == {
        "name": "long",
        "index": 1,
        "x": 0.0,
        "y": 0.0,
        "width": 30.0,
        "depth": 70.0,
        "rotated": True,
    }


def test_plan_allows_decimal_exact_fit_with_float_roundoff() -> None:
    plan = plan_bed_layout([PartSpec("a", 50.1, 10, quantity=2)], bed_width=100.3, bed_depth=10, gap=0.1)

    assert [p.to_dict() for p in plan.placements] == [
        {
            "name": "a",
            "index": 1,
            "x": 0.0,
            "y": 0.0,
            "width": 50.1,
            "depth": 10.0,
            "rotated": False,
        },
        {
            "name": "a",
            "index": 2,
            "x": 50.2,
            "y": 0.0,
            "width": 50.1,
            "depth": 10.0,
            "rotated": False,
        },
    ]


def test_plan_rejects_part_that_cannot_fit_the_bed() -> None:
    with pytest.raises(UsageError) as exc:
        plan_bed_layout([PartSpec("oversize", 120, 40)], bed_width=100, bed_depth=50)

    assert exc.value.exit_code == 2
    assert "does not fit bed" in exc.value.message


def test_part_validation_rejects_bad_dimensions_and_quantity() -> None:
    with pytest.raises(UsageError):
        PartSpec("bad", 0, 20)
    with pytest.raises(UsageError):
        PartSpec("bad", 10, 20, quantity=0)


def test_validation_rejects_non_finite_numbers() -> None:
    with pytest.raises(UsageError):
        PartSpec("bad", math.inf, 20)
    with pytest.raises(UsageError):
        plan_bed_layout([PartSpec("ok", 10, 10)], bed_width=100, bed_depth=math.nan)
    with pytest.raises(UsageError):
        plan_bed_layout([PartSpec("ok", 10, 10)], bed_width=100, bed_depth=100, gap=math.inf)


def test_cmd_help_and_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    assert pack_cmd.run(["--help"]) == 0
    assert "Examples:" in capsys.readouterr().out

    assert pack_cmd.run([]) == 1
    assert "3d pack" in capsys.readouterr().out


def test_cmd_json_plan_is_stable(capsys: pytest.CaptureFixture[str]) -> None:
    assert pack_cmd.run(
        [
            "--bed",
            "100x60",
            "--gap",
            "5",
            "--part",
            "a=50x30",
            "--part",
            "b=40x20:2",
            "--json",
        ]
    ) == 0

    doc = json.loads(capsys.readouterr().out)
    assert doc["bed"] == {"width": 100.0, "depth": 60.0}
    assert doc["gap"] == 5.0
    assert doc["placements"][2]["name"] == "b"
    assert doc["placements"][2]["index"] == 2
    assert doc["placements"][2]["x"] == 0.0
    assert doc["placements"][2]["y"] == 35.0


def test_cmd_rejects_malformed_part() -> None:
    with pytest.raises(UsageError):
        pack_cmd.run(["--bed", "100x60", "--part", "bad"])


def test_cmd_rejects_non_finite_json_inputs() -> None:
    with pytest.raises(UsageError):
        pack_cmd.run(["--bed", "infx100", "--part", "a=10x10", "--json"])
    with pytest.raises(UsageError):
        pack_cmd.run(["--bed", "100x100", "--part", "a=10x10", "--gap", "nan", "--json"])


def test_cmd_rejects_unknown_option() -> None:
    with pytest.raises(UsageError):
        pack_cmd.run(["--bed", "100x60", "--bogus"])
