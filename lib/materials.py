"""Compatibility wrapper for registries.materials."""
from __future__ import annotations

from registries.materials import (
    FINISHES,
    OVERRIDE_FILENAME,
    Material,
    MaterialError,
    get_material,
    load_materials,
)

__all__ = [
    "FINISHES",
    "OVERRIDE_FILENAME",
    "Material",
    "MaterialError",
    "get_material",
    "load_materials",
]
