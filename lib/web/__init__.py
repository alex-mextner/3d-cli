#!/usr/bin/env python3
"""lib/web — the `3d web` local dashboard (FastAPI + SSE + three.js SPA)."""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import constants_io as constants_io
    from . import render_service as render_service
    from . import scan as scan
    from . import webconfig as webconfig


def __getattr__(name: str) -> Any:
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"{__name__}.{name}":
            raise AttributeError(name) from e
        raise
