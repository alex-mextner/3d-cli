"""printops.cli — `3d printops <sub>` subcommands, one module per sub (self-registering).

CONTRACT: a module `lib/printops/cli/<sub>.py` defines `SUB = Sub(name, summary, usage, run)`;
`discover()` imports every non-underscore module here and collects them. Adding a sub requires
no edit to `lib/commands/printops.py` or this file. Modules stay stdlib + pyyaml at import
time (they are imported for `3d printops --help`); FastAPI/uvicorn only lazily inside run().
"""
from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Sub:
    name: str
    summary: str
    usage: str
    run: Callable[[list[str]], int]


def discover() -> dict[str, Sub]:
    """name -> Sub for every `printops/cli/<sub>.py` that defines SUB."""
    subs: dict[str, Sub] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        sub = getattr(importlib.import_module(f"{__name__}.{info.name}"), "SUB", None)
        if isinstance(sub, Sub):
            if sub.name in subs:
                raise ValueError(f"duplicate printops sub {sub.name!r}")
            subs[sub.name] = sub
    return dict(sorted(subs.items()))
