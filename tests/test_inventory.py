"""Tests for the local inventory JSON store and `3d inventory` command."""
from __future__ import annotations

import importlib
import json
from typing import Any, cast

import pytest

import inventory
import registries.inventory as inventory_impl
from errors import InvalidArgument, UsageError

inventory_cmd = cast(Any, importlib.import_module("commands.inventory"))


@pytest.fixture
def isolated_inventory(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    return cfg / "3d-cli" / "inventory.json"


def test_root_wrapper_re_exports_inventory_public_api_identities():
    public_names = [
        "KINDS",
        "STORE_FILENAME",
        "InventoryItem",
        "add_item",
        "get_item",
        "list_items",
        "store_path",
    ]

    assert inventory.__all__ == public_names
    for name in public_names:
        assert getattr(inventory, name) is getattr(inventory_impl, name)


def test_empty_inventory_lists_no_items(isolated_inventory):
    assert inventory.list_items() == {"materials": [], "parts": []}
    assert not isolated_inventory.exists()


def test_add_and_show_material(isolated_inventory):
    item = inventory.add_item(
        "material",
        "PETG clear",
        quantity=2.5,
        unit="kg",
        location="shelf A",
        notes="sealed",
    )

    assert item.kind == "material"
    assert item.name == "PETG clear"
    assert item.quantity == 2.5
    assert item.unit == "kg"
    assert inventory.get_item("material", "PETG clear") == item
    stored = json.loads(isolated_inventory.read_text(encoding="utf-8"))
    assert stored["materials"][0]["name"] == "PETG clear"


def test_add_and_show_part_defaults_to_pieces(isolated_inventory):
    item = inventory.add_item("part", "M3x8 screw", quantity=120)

    assert item.kind == "part"
    assert item.unit == "pcs"
    assert inventory.get_item("part", "M3x8 screw").quantity == 120


def test_list_filters_by_kind(isolated_inventory):
    inventory.add_item("material", "PLA", quantity=1, unit="spool")
    inventory.add_item("part", "608 bearing", quantity=4)

    assert [i.name for i in inventory.list_items("material")] == ["PLA"]
    assert [i.name for i in inventory.list_items("part")] == ["608 bearing"]


def test_duplicate_name_in_same_kind_raises(isolated_inventory):
    inventory.add_item("material", "PLA", quantity=1, unit="spool")

    with pytest.raises(UsageError) as exc:
        inventory.add_item("material", "PLA", quantity=1, unit="spool")

    assert exc.value.exit_code == 2
    assert exc.value.command == "inventory"


def test_unknown_kind_raises_invalid_argument(isolated_inventory):
    with pytest.raises(InvalidArgument) as exc:
        inventory.list_items("tools")

    assert exc.value.flag == "kind"
    assert exc.value.accepted == ["materials", "parts"]


def test_invalid_quantity_raises(isolated_inventory):
    with pytest.raises(InvalidArgument):
        inventory.add_item("part", "bad", quantity=0)


def test_malformed_store_raises(isolated_inventory):
    isolated_inventory.parent.mkdir(parents=True)
    isolated_inventory.write_text("{not json", encoding="utf-8")

    with pytest.raises(UsageError) as exc:
        inventory.list_items()

    assert "could not parse" in str(exc.value)


def test_unwritable_store_path_raises_structured_error(isolated_inventory):
    isolated_inventory.parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(UsageError) as exc:
        inventory.add_item("part", "bolt", quantity=1)

    assert "could not write" in str(exc.value)
    assert exc.value.command == "inventory"


def test_command_help_returns_zero(capsys):
    rc = inventory_cmd.run(["--help"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "inventory" in out
    assert "Examples" in out


def test_command_no_args_prints_usage(capsys):
    rc = inventory_cmd.run([])

    assert rc == 1
    assert "inventory" in capsys.readouterr().out


def test_command_add_material_and_list(isolated_inventory, capsys):
    rc = inventory_cmd.run(
        ["add", "material", "PLA", "--qty", "1", "--unit", "spool", "--location", "bin 2"]
    )
    add_out = capsys.readouterr().out

    assert rc == 0
    assert "Added material: PLA" in add_out

    rc = inventory_cmd.run(["list", "materials"])
    list_out = capsys.readouterr().out

    assert rc == 0
    assert "PLA" in list_out
    assert "spool" in list_out


def test_command_add_part_and_show(isolated_inventory, capsys):
    assert inventory_cmd.run(["add", "part", "M3 nut", "--qty", "25", "--material", "steel"]) == 0
    capsys.readouterr()

    rc = inventory_cmd.run(["show", "part", "M3 nut"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "M3 nut" in out
    assert "steel" in out


def test_command_add_material_rejects_part_material_option(isolated_inventory):
    with pytest.raises(UsageError):
        inventory_cmd.run(["add", "material", "PLA", "--qty", "1", "--unit", "spool", "--material", "PLA"])


def test_command_add_duplicate_option_raises(isolated_inventory):
    with pytest.raises(UsageError):
        inventory_cmd.run(["add", "part", "M3 nut", "--qty", "1", "--qty", "2"])


def test_command_show_missing_name_raises():
    with pytest.raises(UsageError):
        inventory_cmd.run(["show", "part"])


def test_command_unknown_subcommand_raises():
    with pytest.raises(UsageError):
        inventory_cmd.run(["count"])
