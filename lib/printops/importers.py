"""importers.py — turn project files that list spools on hand into `POST /v1/spools/import` items.

ACCESSED VIA: `3d printops spool import-owned FILE` (the CLI reads the file locally and sends the
items; 3d-api plans/applies them idempotently by `external_key`). Stdlib only.

SOURCE: a babushkin-dom-style `gen/plates.json` (~/xp/garage-band/projects/babushkin-dom):
  owned:  {"<colour name>": "#RRGGBB", …}   PLA spools on hand (its `_doc` says so)
  bought: {"<colour name>": "<Vendor> <product> (<shop>, <price> дин)"}   optional, per spool
Each owned entry becomes one spool, key `<project>:owned:<colour name>`. Material is PLA unless
the bought product names another (PLA+, PETG, …). Vendor and price come only from `bought`;
unknown stays unknown (no guessed vendor, no guessed net weight: those spools show as
"never weighed" until `3d printops spool measure`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_MATERIAL = "PLA"
_MATERIALS = (
    "PLA+",
    "PETG",
    "PCTG",
    "ABS",
    "ASA",
    "TPU",
    "PLA",
)  # longest first: "PLA+" before "PLA"
_BOUGHT = re.compile(
    r"^(?P<vendor>\S+)\s+(?P<product>.+?)\s*\((?P<shop>[^,()]+),\s*(?P<price>\d+(?:[.,]\d+)?)\s*дин\)\s*$"
)
_HEX = re.compile(r"^#?[0-9A-Fa-f]{6}$")


class ImportFormatError(ValueError):
    """The file is not the expected plates.json shape."""


def parse_bought(text: str) -> dict[str, Any]:
    """'Elegoo RAPID PLA+ Green (Game Centar, 1499 дин)' → vendor/product/shop/price/material."""
    m = _BOUGHT.match(text.strip())
    if m is None:
        return {"product": text.strip()}
    product = m["product"]
    material = next(
        (
            mat
            for mat in _MATERIALS
            if re.search(rf"(?<![A-Za-z]){re.escape(mat)}(?![A-Za-z])", product)
        ),
        None,
    )
    return {
        "vendor": m["vendor"],
        "product": product,
        "shop": m["shop"].strip(),
        "price": float(m["price"].replace(",", ".")),
        "material": material,
    }


def plates_owned(
    path: Path, *, project: str | None = None, location: str | None = None
) -> list[dict[str, Any]]:
    """Import items for every `owned` spool of a plates.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ImportFormatError(f"{path}: cannot read JSON: {e}") from e
    owned = data.get("owned") if isinstance(data, dict) else None
    if not isinstance(owned, Mapping) or not owned:
        raise ImportFormatError(f"{path}: no `owned` mapping (colour name → #hex)")
    bought = data.get("bought") or {}
    if not isinstance(bought, Mapping):
        raise ImportFormatError(f"{path}: `bought` must be a mapping")
    proj = project or _project_name(path)
    items = []
    for name, hex_ in owned.items():
        if not isinstance(hex_, str) or not _HEX.match(hex_):
            raise ImportFormatError(
                f"{path}: owned[{name!r}] = {hex_!r} is not a #RRGGBB colour"
            )
        item: dict[str, Any] = {
            "external_key": f"{proj}:owned:{name}",
            "material": DEFAULT_MATERIAL,
            "name": name,
            "color_hex": hex_,
            "comment": f"imported from {proj} gen/plates.json owned[{name!r}]",
        }
        b = bought.get(name)
        if isinstance(b, str):
            info = parse_bought(b)
            item["comment"] += f"; bought: {b}"
            if info.get("vendor"):
                item["vendor"] = info["vendor"]
                item["name"] = info["product"]
            if info.get("material"):
                item["material"] = info["material"]
            if info.get("price") is not None:
                item["price"] = info["price"]
        if location:
            item["location"] = location
        items.append(item)
    return items


def _project_name(path: Path) -> str:
    """…/babushkin-dom/gen/plates.json → babushkin-dom."""
    parent = path.resolve().parent
    return parent.parent.name if parent.name == "gen" else parent.name
