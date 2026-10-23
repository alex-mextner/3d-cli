"""printers_cli — `3d printers <sub>` subcommands, one module per sub (self-registering).

CONTRACT: a module `lib/printers_cli/<sub>.py` defines `SUB = Sub(name, summary, usage, run)`
(or several: `SUBS = (Sub(...), Sub(...))` when one module serves related verbs such as
`load`/`unload`/`swap`). `discover()` imports every non-underscore module here and collects
them; adding a sub requires NO edit to `commands/printers.py`, `commands/printer.py` or this
file. Modules stay stdlib-only (+ lazy imports inside `run()`): they are imported for
`3d printers --help`, and the registry contract of `tests/test_imports.py` applies.

`list` and `show` stay in `commands/printers.py` (the model registry and the dossier); every
fleet-wide action on the machines (spools, doctor, firmware) lives here.
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
    aliases: tuple[str, ...] = ()


def discover() -> dict[str, Sub]:
    """name -> Sub for every `printers_cli/<sub>.py` that defines SUB / SUBS (names and aliases
    are unique across the package)."""
    subs: dict[str, Sub] = {}
    taken: set[str] = set()
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        found: list[Sub] = []
        single = getattr(module, "SUB", None)
        if isinstance(single, Sub):
            found.append(single)
        found.extend(s for s in getattr(module, "SUBS", ()) if isinstance(s, Sub))
        for sub in found:
            for word in (sub.name, *sub.aliases):
                if word in taken:
                    raise ValueError(f"duplicate printers sub or alias {word!r}")
                taken.add(word)
            subs[sub.name] = sub
    return dict(sorted(subs.items()))


def resolve(word: str) -> Sub | None:
    """The Sub whose name or alias is `word`, else None."""
    for sub in discover().values():
        if word == sub.name or word in sub.aliases:
            return sub
    return None


def dispatch(argv: list[str]) -> int:
    """Run `argv[0] REST...` as a `3d printers` sub (also what `3d printer doctor` calls)."""
    from errors import UsageError

    sub = resolve(argv[0]) if argv else None
    if sub is None:
        names = ", ".join(discover())
        raise UsageError(
            f"unknown printers subcommand {argv[0] if argv else ''!r}; accepted: {names}",
            command="printers",
            remediation=["Run `3d printers --help`."],
        )
    return sub.run(argv[1:])
