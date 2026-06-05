"""cli — the thin Python dispatcher + shared env/registry for the 3d CLI."""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import paths as paths


def __getattr__(name: str) -> Any:
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"{__name__}.{name}":
            raise AttributeError(name) from e
        raise
