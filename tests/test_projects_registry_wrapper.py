"""Compatibility tests for the root projects_registry wrapper."""
from __future__ import annotations

import projects_registry
from registries import projects


def test_projects_registry_wrapper_re_exports_public_api() -> None:
    assert projects_registry.__all__ == [
        "REGISTRY_FILENAME",
        "ProjectRegistryError",
        "add",
        "is_registered",
        "list_projects",
        "registry_path",
        "remove",
    ]


def test_projects_registry_wrapper_re_exports_identical_objects() -> None:
    for name in projects_registry.__all__:
        assert getattr(projects_registry, name) is getattr(projects, name)
