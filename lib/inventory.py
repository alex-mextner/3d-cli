"""inventory.py — local materials/parts inventory JSON store.

ACCESSED VIA: `3d inventory list|add|show` (lib/commands/inventory.py). This module is the
headless core: it owns the on-disk JSON shape, validation, and structured errors; callers print.

INVARIANTS:
  - The store lives at `cli.paths.config_dir()/inventory.json`, resolved at call time so
    XDG_CONFIG_HOME overrides and tests are honored.
  - Missing store means empty inventory. A present-but-malformed store is a UsageError
    because the user should fix or move the bad JSON instead of silently losing records.
  - Public kind inputs accept singular or plural (`material`/`materials`, `part`/`parts`),
    but the JSON stores plural top-level keys.
"""
from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass
from typing import Any, overload

from cli import paths
from errors import InvalidArgument, UsageError

STORE_FILENAME = "inventory.json"
KINDS = ("materials", "parts")


@dataclass(slots=True, frozen=True)
class InventoryItem:
    """One locally stocked material or part."""

    kind: str
    name: str
    quantity: float
    unit: str
    location: str | None = None
    material: str | None = None
    notes: str | None = None


def store_path() -> pathlib.Path:
    """Absolute path to the local inventory JSON store."""
    return paths.config_dir() / STORE_FILENAME


def _normalize_kind(kind: str) -> str:
    if kind in ("material", "materials"):
        return "materials"
    if kind in ("part", "parts"):
        return "parts"
    raise InvalidArgument(
        "kind",
        kind,
        list(KINDS),
        command="inventory",
        extra="Use `materials` or `parts`.",
    )


def _singular(kind: str) -> str:
    return "material" if kind == "materials" else "part"


def _clean_text(field: str, value: str | None, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise InvalidArgument(
                field,
                "(missing)",
                [f"non-empty {field}"],
                command="inventory",
            )
        return None
    cleaned = value.strip()
    if not cleaned:
        raise InvalidArgument(
            field,
            value,
            [f"non-empty {field}"],
            command="inventory",
        )
    return cleaned


def _clean_quantity(quantity: float) -> float:
    qty = float(quantity)
    if not math.isfinite(qty) or qty <= 0:
        raise InvalidArgument(
            "quantity",
            str(quantity),
            ["positive finite number"],
            command="inventory",
        )
    return qty


def _item_from_raw(kind: str, raw: Any) -> InventoryItem:
    if not isinstance(raw, dict):
        raise UsageError(
            f"inventory entry under `{kind}` must be an object, got {type(raw).__name__}",
            command="inventory",
            remediation=[f"Fix {store_path()} so every `{kind}` entry is a JSON object."],
        )
    name_raw = raw.get("name")
    unit_raw = raw.get("unit")
    if not isinstance(name_raw, str):
        raise UsageError(
            f"inventory entry under `{kind}` is missing string `name`",
            command="inventory",
            remediation=[f"Fix {store_path()} and give every `{kind}` entry a string `name`."],
        )
    if not isinstance(unit_raw, str):
        raise UsageError(
            f"inventory entry {name_raw!r} is missing string `unit`",
            command="inventory",
            remediation=[f"Fix {store_path()} and give {name_raw!r} a string `unit`."],
        )
    quantity_raw = raw.get("quantity")
    if not isinstance(quantity_raw, (int, float, str)) or isinstance(quantity_raw, bool):
        raise UsageError(
            f"inventory entry {name_raw!r} has invalid `quantity`",
            command="inventory",
            remediation=[f"Fix {store_path()} and set `quantity` to a positive number."],
        )
    try:
        quantity = _clean_quantity(float(quantity_raw))
    except (TypeError, ValueError):
        raise UsageError(
            f"inventory entry {name_raw!r} has invalid `quantity`",
            command="inventory",
            remediation=[f"Fix {store_path()} and set `quantity` to a positive number."],
        ) from None

    def optional_string(field: str) -> str | None:
        value = raw.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise UsageError(
                f"inventory entry {name_raw!r}: `{field}` must be a string",
                command="inventory",
                remediation=[f"Fix {store_path()} and make `{field}` a JSON string."],
            )
        return _clean_text(field, value)

    clean_name = _clean_text("name", name_raw, required=True)
    clean_unit = _clean_text("unit", unit_raw, required=True)
    if clean_name is None or clean_unit is None:  # pragma: no cover - required=True raises instead
        raise AssertionError("unreachable")
    return InventoryItem(
        kind=_singular(kind),
        name=clean_name,
        quantity=quantity,
        unit=clean_unit,
        location=optional_string("location"),
        material=optional_string("material") if kind == "parts" else None,
        notes=optional_string("notes"),
    )


def _item_to_raw(item: InventoryItem) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": item.name,
        "quantity": item.quantity,
        "unit": item.unit,
    }
    if item.location is not None:
        raw["location"] = item.location
    if item.material is not None:
        raw["material"] = item.material
    if item.notes is not None:
        raw["notes"] = item.notes
    return raw


def _empty_store() -> dict[str, list[InventoryItem]]:
    return {"materials": [], "parts": []}


def _load() -> dict[str, list[InventoryItem]]:
    p = store_path()
    if not p.is_file():
        return _empty_store()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(
            f"could not parse {p}: {exc}",
            command="inventory",
            remediation=["Fix the JSON syntax or move the file aside and add items again."],
        ) from exc
    if not isinstance(doc, dict):
        raise UsageError(
            f"{p} must be a JSON object with `materials` and `parts` arrays",
            command="inventory",
            remediation=['Use a shape like: {"materials": [], "parts": []}.'],
        )

    store = _empty_store()
    for kind in KINDS:
        entries = doc.get(kind, [])
        if not isinstance(entries, list):
            raise UsageError(
                f"{p}: `{kind}` must be an array",
                command="inventory",
                remediation=[f"Set `{kind}` to a JSON array."],
            )
        store[kind] = [_item_from_raw(kind, entry) for entry in entries]
    return store


def _save(store: dict[str, list[InventoryItem]]) -> None:
    p = store_path()
    doc = {kind: [_item_to_raw(item) for item in store[kind]] for kind in KINDS}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise UsageError(
            f"could not write {p}: {exc}",
            command="inventory",
            remediation=[
                "Check that the config directory exists and is writable, "
                "or set XDG_CONFIG_HOME to a writable location.",
            ],
        ) from exc


@overload
def list_items(kind: None = None) -> dict[str, list[InventoryItem]]:
    ...


@overload
def list_items(kind: str) -> list[InventoryItem]:
    ...


def list_items(kind: str | None = None) -> dict[str, list[InventoryItem]] | list[InventoryItem]:
    """List inventory items, either all grouped by kind or one kind only."""
    store = _load()
    if kind is None:
        return store
    return list(store[_normalize_kind(kind)])


def get_item(kind: str, name: str) -> InventoryItem:
    """Find one inventory item by kind/name, case-insensitive."""
    normalized = _normalize_kind(kind)
    clean_name = _clean_text("name", name, required=True)
    if clean_name is None:  # pragma: no cover - required=True raises instead
        raise AssertionError("unreachable")
    wanted = clean_name.casefold()
    for item in _load()[normalized]:
        if item.name.casefold() == wanted:
            return item
    raise UsageError(
        f"{_singular(normalized)} not found: {name}",
        command="inventory",
        remediation=[f"Run `3d inventory list {normalized}` to see known names."],
    )


def add_item(
    kind: str,
    name: str,
    *,
    quantity: float,
    unit: str | None = None,
    location: str | None = None,
    material: str | None = None,
    notes: str | None = None,
) -> InventoryItem:
    """Add a material or part to the local inventory store."""
    normalized = _normalize_kind(kind)
    clean_name = _clean_text("name", name, required=True)
    if clean_name is None:  # pragma: no cover - required=True raises instead
        raise AssertionError("unreachable")
    clean_unit = _clean_text("unit", unit or ("pcs" if normalized == "parts" else None), required=True)
    if clean_unit is None:  # pragma: no cover - required=True raises instead
        raise AssertionError("unreachable")
    item = InventoryItem(
        kind=_singular(normalized),
        name=clean_name,
        quantity=_clean_quantity(quantity),
        unit=clean_unit,
        location=_clean_text("location", location),
        material=_clean_text("material", material) if normalized == "parts" else None,
        notes=_clean_text("notes", notes),
    )

    store = _load()
    wanted = item.name.casefold()
    if any(existing.name.casefold() == wanted for existing in store[normalized]):
        raise UsageError(
            f"{item.kind} already exists: {item.name}",
            command="inventory",
            remediation=[
                f"Run `3d inventory show {item.kind} \"{item.name}\"` to inspect it. "
                "Remove or edit inventory.json before adding a duplicate."
            ],
        )
    store[normalized].append(item)
    store[normalized].sort(key=lambda i: i.name.casefold())
    _save(store)
    return item
