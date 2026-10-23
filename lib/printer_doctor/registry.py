"""registry.py — drop-in discovery of the doctor's checks.

CONTRACT: a module `lib/printer_doctor/checks/<name>.py` defines `CHECKS: tuple[Check, ...]`.
`discover_checks()` imports every non-underscore module of that package and collects them, so
adding a check is dropping one file: no edit to the engine, the CLI, the docs index or this file
(the same contract as `lib/printers_cli/__init__.py`). A duplicate check id, or a `CHECKS` entry
that is not a `Check`, is a programming error and raises at discovery, never at diagnosis time.
"""

from __future__ import annotations

import importlib
import pkgutil

from printer_doctor.model import Check


def discover_checks() -> tuple[Check, ...]:
    """Every check of `printer_doctor.checks`, sorted by id."""
    from printer_doctor import checks

    found: dict[str, tuple[str, Check]] = {}
    for info in pkgutil.iter_modules(checks.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{checks.__name__}.{info.name}")
        for check in getattr(module, "CHECKS", ()):
            if not isinstance(check, Check):
                raise TypeError(f"{info.name}.CHECKS holds {check!r}, not a Check")
            if check.id in found:
                raise ValueError(
                    f"duplicate check id {check.id!r} in {info.name} and {found[check.id][0]}"
                )
            found[check.id] = (info.name, check)
    return tuple(c for _, c in sorted(found.values(), key=lambda item: item[1].id))
