"""Compatibility wrapper for registries.projects."""
from __future__ import annotations

from registries.projects import (
    REGISTRY_FILENAME,
    ProjectRegistryError,
    add,
    is_registered,
    list_projects,
    registry_path,
    remove,
)

__all__ = [
    "REGISTRY_FILENAME",
    "ProjectRegistryError",
    "add",
    "is_registered",
    "list_projects",
    "registry_path",
    "remove",
]
