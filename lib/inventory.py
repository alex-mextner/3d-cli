"""Compatibility wrapper for registries.inventory."""
from __future__ import annotations

from registries.inventory import (
    KINDS,
    STORE_FILENAME,
    InventoryItem,
    add_item,
    get_item,
    list_items,
    store_path,
)

__all__ = [
    "KINDS",
    "STORE_FILENAME",
    "InventoryItem",
    "add_item",
    "get_item",
    "list_items",
    "store_path",
]
