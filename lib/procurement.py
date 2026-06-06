"""procurement.py — deterministic purchase planning from BOM + inventory inputs.

This headless core takes local BOM and inventory records, computes shortages, rounds buy
quantities to optional package sizes, and returns a stable purchase plan. It deliberately
does not contact suppliers, fetch prices, or perform any network lookups.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from errors import InputNotFound, MissingDependency, UsageError


@dataclass(slots=True, frozen=True)
class Requirement:
    sku: str
    description: str
    quantity: float
    unit: str
    supplier: str
    package_qty: float | None


@dataclass(slots=True, frozen=True)
class Stock:
    sku: str
    quantity: float
    unit: str


@dataclass(slots=True, frozen=True)
class PurchasePlanItem:
    sku: str
    description: str
    needed_qty: float
    available_qty: float
    short_qty: float
    buy_qty: float
    unit: str
    supplier: str
    package_qty: float | None


@dataclass(slots=True, frozen=True)
class PurchasePlan:
    items: list[PurchasePlanItem]


def _require_yaml() -> Any:
    try:
        import yaml  # lazy: procurement accepts YAML files, but imports stay light
    except ImportError as exc:  # pragma: no cover - exercised only without project deps
        raise MissingDependency(
            "pyyaml",
            install="uv sync  (pyyaml is a core dependency)  # or: pip install pyyaml",
            degrades="YAML BOM/inventory parsing; JSON inputs still work",
            command="procurement",
        ) from exc
    return yaml


def _coerce_quantity(value: Any, *, field: str, sku: str) -> float:
    if value is None:
        raise UsageError(
            f"item {sku!r}: missing `{field}`",
            command="procurement",
            remediation=[f"Add a numeric `{field}:` value for {sku}."],
        )
    try:
        qty = float(value)
    except (TypeError, ValueError):
        raise UsageError(
            f"item {sku!r}: `{field}` must be a number, got {value!r}",
            command="procurement",
            remediation=[f"Set `{field}:` to a non-negative number for {sku}."],
        ) from None
    if not math.isfinite(qty):
        raise UsageError(
            f"item {sku!r}: `{field}` must be a finite number, got {value!r}",
            command="procurement",
            remediation=[f"Set `{field}:` to a finite non-negative number for {sku}."],
        )
    if qty < 0:
        raise UsageError(
            f"item {sku!r}: `{field}` must be non-negative, got {value!r}",
            command="procurement",
            remediation=[f"Set `{field}:` to zero or greater for {sku}."],
        )
    return qty


def _sku(spec: Mapping[str, Any]) -> str:
    raw = spec.get("sku") or spec.get("name") or spec.get("item")
    if raw is None or str(raw).strip() == "":
        raise UsageError(
            "every procurement item needs `sku:` (or `name:`)",
            command="procurement",
            remediation=["Add a stable SKU/name to each BOM and inventory item."],
        )
    return str(raw).strip()


def _require_mapping(value: Any, *, source: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UsageError(
            f"{source} item must be a mapping, got {type(value).__name__}",
            command="procurement",
            remediation=["Write each item as `{sku: ..., quantity: ...}`."],
        )
    return value


def _requirement(spec: Mapping[str, Any]) -> Requirement:
    sku = _sku(spec)
    package_qty = None
    if spec.get("package_qty") is not None:
        package_qty = _coerce_quantity(spec.get("package_qty"), field="package_qty", sku=sku)
        if package_qty == 0:
            raise UsageError(
                f"item {sku!r}: `package_qty` must be greater than zero",
                command="procurement",
                remediation=[f"Remove `package_qty` or set it above zero for {sku}."],
            )
    return Requirement(
        sku=sku,
        description=str(spec.get("description") or "-"),
        quantity=_coerce_quantity(spec.get("quantity"), field="quantity", sku=sku),
        unit=str(spec.get("unit") or "each"),
        supplier=str(spec.get("supplier") or "-"),
        package_qty=package_qty,
    )


def _stock(spec: Mapping[str, Any]) -> Stock:
    sku = _sku(spec)
    return Stock(
        sku=sku,
        quantity=_coerce_quantity(spec.get("quantity"), field="quantity", sku=sku),
        unit=str(spec.get("unit") or "each"),
    )


def _merge_requirement(existing: Requirement | None, item: Requirement) -> Requirement:
    if existing is None:
        return item
    if existing.unit != item.unit:
        raise UsageError(
            f"item {item.sku!r}: conflicting BOM units {existing.unit!r} and {item.unit!r}",
            command="procurement",
            remediation=["Use one unit per SKU before planning purchases."],
        )
    package_qty = existing.package_qty if existing.package_qty is not None else item.package_qty
    return Requirement(
        sku=item.sku,
        description=existing.description if existing.description != "-" else item.description,
        quantity=existing.quantity + item.quantity,
        unit=existing.unit,
        supplier=existing.supplier if existing.supplier != "-" else item.supplier,
        package_qty=package_qty,
    )


def _merge_stock(existing: Stock | None, item: Stock) -> Stock:
    if existing is None:
        return item
    if existing.unit != item.unit:
        raise UsageError(
            f"item {item.sku!r}: conflicting inventory units {existing.unit!r} and {item.unit!r}",
            command="procurement",
            remediation=["Use one inventory unit per SKU before planning purchases."],
        )
    return Stock(sku=item.sku, quantity=existing.quantity + item.quantity, unit=existing.unit)


def _round_to_package(short_qty: float, package_qty: float | None) -> float:
    if package_qty is None:
        return short_qty
    return math.ceil(short_qty / package_qty) * package_qty


def plan_purchases(
    *,
    bom_items: Iterable[Mapping[str, Any]],
    inventory_items: Iterable[Mapping[str, Any]],
) -> PurchasePlan:
    """Compute shortages in a stable supplier/name order.

    Only positive shortages appear in the returned plan. Duplicate SKUs are combined before
    comparing against inventory.
    """
    required: dict[str, Requirement] = {}
    for raw in bom_items:
        req_item = _requirement(_require_mapping(raw, source="BOM"))
        required[req_item.sku] = _merge_requirement(required.get(req_item.sku), req_item)

    stocked: dict[str, Stock] = {}
    for raw in inventory_items:
        stock_item = _stock(_require_mapping(raw, source="inventory"))
        stocked[stock_item.sku] = _merge_stock(stocked.get(stock_item.sku), stock_item)

    plan_items: list[PurchasePlanItem] = []
    for sku, req in required.items():
        stock = stocked.get(sku)
        available = stock.quantity if stock is not None else 0.0
        if stock is not None and stock.unit != req.unit:
            raise UsageError(
                f"item {sku!r}: BOM unit {req.unit!r} conflicts with inventory unit {stock.unit!r}",
                command="procurement",
                remediation=["Use matching units for the same SKU in BOM and inventory."],
            )
        short = max(req.quantity - available, 0.0)
        if short <= 0:
            continue
        plan_items.append(
            PurchasePlanItem(
                sku=sku,
                description=req.description,
                needed_qty=req.quantity,
                available_qty=available,
                short_qty=short,
                buy_qty=_round_to_package(short, req.package_qty),
                unit=req.unit,
                supplier=req.supplier,
                package_qty=req.package_qty,
            )
        )

    return PurchasePlan(items=sorted(plan_items, key=lambda i: (i.supplier, i.sku)))


def _load_doc(path: pathlib.Path) -> Any:
    if not path.is_file():
        raise InputNotFound(str(path), command="procurement")
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return json.loads(text)
        yaml = _require_yaml()
        return yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise UsageError(
            f"could not parse {path}: {exc}",
            command="procurement",
            remediation=[f"Fix the JSON syntax in {path}."],
        ) from exc
    except OSError as exc:
        raise UsageError(
            f"could not read {path}: {exc}",
            command="procurement",
            remediation=["Check file permissions and try again."],
        ) from exc
    except Exception as exc:
        if exc.__class__.__module__.startswith("yaml"):
            raise UsageError(
                f"could not parse {path}: {exc}",
                command="procurement",
                remediation=[f"Fix the YAML syntax in {path}."],
            ) from exc
        raise


def _items_from_doc(doc: Any, *, path: pathlib.Path) -> list[Mapping[str, Any]]:
    if doc is None:
        return []
    if isinstance(doc, list):
        values = doc
    elif isinstance(doc, Mapping):
        raw_items = doc.get("items")
        if raw_items is None:
            values = [
                {"sku": key, **value} if isinstance(value, Mapping) else {"sku": key, "quantity": value}
                for key, value in doc.items()
            ]
        elif isinstance(raw_items, list):
            values = raw_items
        elif isinstance(raw_items, Mapping):
            values = [
                {"sku": key, **value} if isinstance(value, Mapping) else {"sku": key, "quantity": value}
                for key, value in raw_items.items()
            ]
        else:
            raise UsageError(
                f"{path}: `items` must be a list or mapping, got {type(raw_items).__name__}",
                command="procurement",
                remediation=["Use `items: [{sku: ..., quantity: ...}]`."],
            )
    else:
        raise UsageError(
            f"{path} must contain a list or mapping, got {type(doc).__name__}",
            command="procurement",
            remediation=["Use a top-level `items:` list for BOM and inventory files."],
        )

    return [_require_mapping(item, source=str(path)) for item in values]


def load_purchase_plan(
    bom_path: str | os.PathLike[str],
    inventory_path: str | os.PathLike[str],
) -> PurchasePlan:
    """Load local JSON/YAML BOM + inventory files and compute a purchase plan."""
    bom = pathlib.Path(bom_path)
    inventory = pathlib.Path(inventory_path)
    return plan_purchases(
        bom_items=_items_from_doc(_load_doc(bom), path=bom),
        inventory_items=_items_from_doc(_load_doc(inventory), path=inventory),
    )


def _fmt_num(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def format_plan_table(plan: PurchasePlan) -> str:
    """Render a deterministic plain-text purchase plan."""
    if not plan.items:
        return "No purchases needed."
    rows = [
        [
            item.sku,
            item.description,
            _fmt_num(item.needed_qty),
            _fmt_num(item.available_qty),
            _fmt_num(item.short_qty),
            _fmt_num(item.buy_qty),
            item.unit,
            item.supplier,
        ]
        for item in plan.items
    ]
    headers = ["SKU", "DESCRIPTION", "NEED", "HAVE", "SHORT", "BUY", "UNIT", "SUPPLIER"]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]
    right = {2, 3, 4, 5}

    def fmt_row(row: list[str]) -> str:
        cells = [
            row[i].rjust(widths[i]) if i in right else row[i].ljust(widths[i])
            for i in range(len(row))
        ]
        return "  ".join(cells).rstrip()

    return "\n".join([fmt_row(headers), *(fmt_row(row) for row in rows)])


def plan_to_json(plan: PurchasePlan) -> str:
    """Render a stable JSON representation for scripts."""
    data = {
        "items": [
            {
                "sku": item.sku,
                "description": item.description,
                "needed_qty": item.needed_qty,
                "available_qty": item.available_qty,
                "short_qty": item.short_qty,
                "buy_qty": item.buy_qty,
                "unit": item.unit,
                "supplier": item.supplier,
                "package_qty": item.package_qty,
            }
            for item in plan.items
        ]
    }
    return json.dumps(data, allow_nan=False, indent=2, sort_keys=True)
