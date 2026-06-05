"""commands — self-registering 3d subcommands (one COMMAND per module)."""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import init as init
    from . import materials as materials
    from . import printers as printers


def __getattr__(name: str) -> Any:
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"{__name__}.{name}":
            raise AttributeError(name) from e
        raise
