"""Tests for purchase planning from BOM + inventory inputs."""
from __future__ import annotations

import json

import pytest

from commands import procurement as procurement_cmd
from errors import UsageError
from procurement import format_plan_table, load_purchase_plan, plan_purchases


def test_plan_purchases_sorts_and_rounds_to_package_quantity() -> None:
    plan = plan_purchases(
        bom_items=[
            {"sku": "m3-bolt", "description": "M3 bolt", "quantity": 12, "supplier": "BoltCo"},
            {
                "sku": "pla-black",
                "description": "PLA black spool",
                "quantity": 1500,
                "unit": "g",
                "supplier": "FilamentCo",
                "package_qty": 1000,
            },
            {"sku": "m3-bolt", "description": "M3 bolt", "quantity": 8, "supplier": "BoltCo"},
        ],
        inventory_items=[
            {"sku": "m3-bolt", "quantity": 15},
            {"sku": "pla-black", "quantity": 200, "unit": "g"},
        ],
    )

    assert [item.sku for item in plan.items] == ["m3-bolt", "pla-black"]
    assert plan.items[0].needed_qty == 20
    assert plan.items[0].available_qty == 15
    assert plan.items[0].short_qty == 5
    assert plan.items[0].buy_qty == 5
    assert plan.items[1].short_qty == 1300
    assert plan.items[1].buy_qty == 2000


def test_format_plan_table_is_deterministic() -> None:
    plan = plan_purchases(
        bom_items=[
            {"sku": "z-part", "quantity": 2, "supplier": "Zed"},
            {"sku": "a-part", "quantity": 3, "supplier": "Alpha"},
        ],
        inventory_items=[{"sku": "z-part", "quantity": 0}, {"sku": "a-part", "quantity": 1}],
    )

    assert format_plan_table(plan) == (
        "SKU     DESCRIPTION  NEED  HAVE  SHORT  BUY  UNIT  SUPPLIER\n"
        "a-part  -               3     1      2    2  each  Alpha\n"
        "z-part  -               2     0      2    2  each  Zed"
    )


def test_load_purchase_plan_reads_json_files(tmp_path) -> None:
    bom = tmp_path / "bom.json"
    inventory = tmp_path / "inventory.json"
    bom.write_text(
        json.dumps({"items": [{"sku": "heat-set-m3", "quantity": 12, "package_qty": 10}]}),
        encoding="utf-8",
    )
    inventory.write_text(json.dumps({"items": [{"sku": "heat-set-m3", "quantity": 3}]}), encoding="utf-8")

    plan = load_purchase_plan(bom, inventory)

    assert plan.items[0].short_qty == 9
    assert plan.items[0].buy_qty == 10


def test_invalid_quantity_raises_structured_error() -> None:
    with pytest.raises(UsageError) as exc:
        plan_purchases(
            bom_items=[{"sku": "m3", "quantity": "many"}],
            inventory_items=[],
        )

    assert exc.value.exit_code == 2
    assert "quantity" in exc.value.message


@pytest.mark.parametrize("quantity", [float("nan"), float("inf")])
def test_non_finite_quantity_raises_structured_error(quantity: float) -> None:
    with pytest.raises(UsageError) as exc:
        plan_purchases(
            bom_items=[{"sku": "m3", "quantity": quantity, "package_qty": 10}],
            inventory_items=[],
        )

    assert exc.value.exit_code == 2
    assert "finite" in exc.value.message


def test_cmd_help_returns_zero(capsys) -> None:
    assert procurement_cmd.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "plan" in out
    assert "--bom" in out


def test_cmd_no_args_prints_usage_nonzero(capsys) -> None:
    assert procurement_cmd.run([]) == 1
    assert "procurement" in capsys.readouterr().out


def test_cmd_plan_prints_purchase_table(tmp_path, capsys) -> None:
    bom = tmp_path / "bom.json"
    inventory = tmp_path / "inventory.json"
    bom.write_text(json.dumps({"items": [{"sku": "m3", "quantity": 7}]}), encoding="utf-8")
    inventory.write_text(json.dumps({"items": [{"sku": "m3", "quantity": 2}]}), encoding="utf-8")

    assert procurement_cmd.run(["plan", "--bom", str(bom), "--inventory", str(inventory)]) == 0
    out = capsys.readouterr().out
    assert "m3" in out
    assert "SHORT" in out
    assert "  5" in out


def test_cmd_plan_missing_flags_raise_usage_error(tmp_path) -> None:
    bom = tmp_path / "bom.json"
    bom.write_text('{"items": []}', encoding="utf-8")

    with pytest.raises(UsageError):
        procurement_cmd.run(["plan", "--bom", str(bom)])


def test_cmd_plan_unknown_args_raise_usage_error(tmp_path) -> None:
    bom = tmp_path / "bom.json"
    inventory = tmp_path / "inventory.json"
    bom.write_text('{"items": []}', encoding="utf-8")
    inventory.write_text('{"items": []}', encoding="utf-8")

    with pytest.raises(UsageError):
        procurement_cmd.run(["plan", "--bom", str(bom), "--inventory", str(inventory), "--foramt", "json"])


def test_command_metadata() -> None:
    assert procurement_cmd.COMMAND.name == "procurement"
    assert procurement_cmd.COMMAND.group == "LIBRARIES"
