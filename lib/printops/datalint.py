"""datalint.py — PrintOps DATA lint (plan §9.3): rules over the live Spoolman + PrintOps DB state.

ACCESSED VIA: 3d-api `GET /v1/lint` (routes_lint.py) → `3d lint --printops` (which adds these
findings to the fleet-file rules of printops.fleet_lint) and, later, the hourly 3d-api cron.

EXTENSION POINT: every module `lib/printops/lint_<slice>.py` defining
`def lint(ctx: LintContext) -> list[Finding]` is run, in module-name order. A slice adds a
module; it never edits this file. Rules are named `<slice>/<rule>`.

INVARIANTS:
  - Read-only: a rule never writes Spoolman or the DB.
  - Spoolman is read ONCE per run (`ctx.spools`, non-archived) and shared by every rule.
  - A rule that crashes becomes an error finding `<module>/crashed` instead of hiding the others.
  - Same Finding shape as printops.fleet_lint.Finding (rule, level error|warn, subject, message).
"""

from __future__ import annotations

import datetime
import importlib
import pkgutil
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

import printops
from printops.spoolman import JSON, Spoolman

ERROR, WARN = "error", "warn"
DEFAULT_WEIGH_DAYS = 30


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str  # error | warn
    subject: str  # "spool:12", "location:WH:A/S1", …
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class LintContext:
    conn: sqlite3.Connection
    spoolman: Spoolman
    spools: list[JSON]
    now: datetime.datetime
    weigh_days: int = DEFAULT_WEIGH_DAYS
    options: dict[str, Any] = field(default_factory=dict)


def rule_modules() -> list[str]:
    return sorted(
        f"{printops.__name__}.{m.name}"
        for m in pkgutil.iter_modules(printops.__path__)
        if m.name.startswith("lint_")
    )


def run(ctx: LintContext) -> list[Finding]:
    findings: list[Finding] = []
    for name in rule_modules():
        fn = getattr(importlib.import_module(name), "lint", None)
        if fn is None:
            continue
        try:
            findings.extend(fn(ctx))
        except Exception as e:  # noqa: BLE001 — one broken rule must not hide the rest
            findings.append(
                Finding(
                    f"{name.rsplit('.', 1)[-1].removeprefix('lint_')}/crashed",
                    ERROR,
                    name,
                    f"{type(e).__name__}: {e}",
                )
            )
    return findings


def parse_utc(value: str) -> datetime.datetime:
    """ISO-8601 (with Z or offset, or naive = UTC, as Spoolman writes) → aware UTC datetime."""
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
