"""printops.cli — `3d printops <sub>` subcommands, one module per sub (self-registering).

CONTRACT: a module `lib/printops/cli/<sub>.py` defines `SUB = Sub(name, summary, usage, run)`;
`discover()` imports every non-underscore module here and collects them. Adding a sub requires
no edit to `lib/commands/printops.py` or this file. Modules stay stdlib + pyyaml at import
time (they are imported for `3d printops --help`); FastAPI/uvicorn only lazily inside run().

An ACTION of an existing sub (`3d printops spool drift`) is the same idea one level down: a module
defines `SUB_ACTION = SubAction(parent="spool", name="drift", …)`; `discover_actions()` collects
them, `lib/commands/printops.py` routes `<parent> <name> …` to it and lists it under the parent's
`--help`. The parent's own module is not edited.
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


@dataclass(frozen=True)
class SubAction:
    parent: str
    name: str
    summary: str
    usage: str
    run: Callable[[list[str]], int]


def _modules() -> list[object]:
    return [
        importlib.import_module(f"{__name__}.{i.name}")
        for i in pkgutil.iter_modules(__path__)
        if not i.name.startswith("_")
    ]


def discover() -> dict[str, Sub]:
    """name -> Sub for every `printops/cli/<sub>.py` that defines SUB."""
    subs: dict[str, Sub] = {}
    for module in _modules():
        sub = getattr(module, "SUB", None)
        if isinstance(sub, Sub):
            if sub.name in subs:
                raise ValueError(f"duplicate printops sub {sub.name!r}")
            subs[sub.name] = sub
    return dict(sorted(subs.items()))


def discover_actions() -> dict[str, dict[str, SubAction]]:
    """parent sub name -> {action name -> SubAction} for every module that defines SUB_ACTION."""
    out: dict[str, dict[str, SubAction]] = {}
    for module in _modules():
        action = getattr(module, "SUB_ACTION", None)
        if isinstance(action, SubAction):
            if action.name in out.setdefault(action.parent, {}):
                raise ValueError(
                    f"duplicate printops action {action.parent} {action.name!r}"
                )
            out[action.parent][action.name] = action
    return out


def actions_help(actions: dict[str, SubAction]) -> str:
    """The block `3d printops <parent> --help` appends for the actions other modules added."""
    width = max(len(n) for n in actions)
    lines = ["", "More actions (each has its own --help):"]
    lines += [f"  {a.name:<{width}}  {a.summary}" for a in actions.values()]
    return "\n".join(lines)
