"""model.py — the data types of the printer doctor: checks, verdicts, fixes, results.

WHAT: `Check` (one diagnosis: id, severity, `applies`, `run`, optional `Fix`), `Verdict` (what a
  `run` returns), `CheckResult` (a verdict stamped with its check id/severity), `Fix` (risk class +
  how to apply / what to do by hand) and `FixReport` (what happened to a fix in this run).
WHY: the engine, the renderers and every drop-in check module (`checks/*.py`) share one vocabulary,
  so adding a check is writing one `Check(...)` and nothing else.
ACCESSED VIA: `3d printers doctor` (lib/printers_cli/doctor.py) -> printer_doctor.engine.
INVARIANTS: stdlib only. `applies` decides from adapter kind / capabilities / fleet+device data,
  never from a printer's name. A check that cannot see raises `CannotSee` (-> `skip` or `error`
  with the reason): it never reports a pass it could not verify.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from printer_doctor.context import Context

# Sequence/Mapping (covariant): a list[str] or dict[str, int] is a JsonValue without copying.
JsonValue = (
    str | int | float | bool | None | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
)
Evidence = dict[str, JsonValue]

Severity = Literal["info", "warn", "error"]
Risk = Literal["safe", "needs-yes", "manual-only"]
Status = Literal["pass", "fail", "skip", "error"]

SEVERITIES: tuple[Severity, ...] = ("info", "warn", "error")
RISKS: tuple[Risk, ...] = ("safe", "needs-yes", "manual-only")
RANK: dict[str, int] = {s: i for i, s in enumerate(SEVERITIES)}


def clean(text: str) -> str:
    """`text` with control characters (terminal escape sequences, CR, NUL) shown escaped instead of
    interpreted: summaries and steps carry strings a printer chose (component names, messages)."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


COMMAND_PREFIX = "$ "


def command(text: str) -> str:
    """A manual step that is a real shell command (rendered with `$`); other steps are prose."""
    return COMMAND_PREFIX + text


def is_command(step: str) -> bool:
    return step.startswith(COMMAND_PREFIX)


class CannotSee(Exception):
    """The check could not look (endpoint unreachable, tool missing, not configured).

    `status` is `skip` when the thing is simply not observable from here, `error` when it should
    have been and answered something unusable."""

    def __init__(self, reason: str, status: Literal["skip", "error"] = "skip") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class FixRefused(Exception):
    """A safety rule refused to apply the fix (printer busy, ambiguous target); nothing was sent."""


@dataclass(frozen=True)
class Verdict:
    """What `Check.run` returns. `severity` overrides the check's default for this finding."""

    status: Status
    summary: str
    evidence: Evidence = field(default_factory=dict)
    severity: Severity | None = None

    @classmethod
    def ok(cls, summary: str, **evidence: JsonValue) -> Verdict:
        return cls("pass", summary, dict(evidence))

    @classmethod
    def fail(
        cls, summary: str, severity: Severity | None = None, **evidence: JsonValue
    ) -> Verdict:
        return cls("fail", summary, dict(evidence), severity)

    @classmethod
    def skip(cls, reason: str, **evidence: JsonValue) -> Verdict:
        return cls("skip", reason, dict(evidence))


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: Status
    severity: Severity
    summary: str
    evidence: Evidence = field(default_factory=dict)

    @property
    def finding(self) -> bool:
        """A fail, or a check that errored (could not evaluate what it should have)."""
        return self.status in ("fail", "error")


@dataclass(frozen=True)
class FixChange:
    """What an applied fix changed, as read back; journalled."""

    before: JsonValue
    after: JsonValue
    note: str = ""


FixApply = Callable[["Context", CheckResult], FixChange]
FixSteps = Callable[["Context", CheckResult], list[str]]


@dataclass(frozen=True)
class Fix:
    """How a failed check is repaired.

    risk `safe`        --fix applies it (idempotent, never mid-print, verified by the check).
    risk `needs-yes`   --fix applies it only with --yes too (it restarts something).
    risk `manual-only` never executed; `manual_steps` are printed (and put in the JSON).
    `manual_steps` exists for every risk: it is what to do by hand when the fix is refused."""

    risk: Risk
    description: str
    manual_steps: FixSteps
    apply: FixApply | None = None

    def __post_init__(self) -> None:
        if self.risk not in RISKS:
            raise ValueError(f"fix risk {self.risk!r} is not one of {RISKS}")
        if self.risk == "manual-only" and self.apply is not None:
            raise ValueError("a manual-only fix must not carry an apply function")
        if self.risk != "manual-only" and self.apply is None:
            raise ValueError(f"a {self.risk} fix needs an apply function")


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    severity: Severity  # the default (highest usual) severity of a finding
    applies_to: str  # human text for --list-checks
    run: Callable[[Context], Verdict]
    applies: Callable[[Context], str | None] = lambda _ctx: None
    contacts_printer: bool = True  # False: reads config / 3d-api / systemd only
    fix: Fix | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", self.id):
            raise ValueError(f"check id {self.id!r} must be kebab-case")
        if self.severity not in SEVERITIES:
            raise ValueError(f"check {self.id}: severity {self.severity!r}")


@dataclass
class FixReport:
    risk: Risk
    description: str
    manual_steps: list[str]
    applied: bool = False
    verified: bool | None = None
    error: str | None = None
    journal: str | None = None


@dataclass
class ResultRecord:
    result: CheckResult
    fix: FixReport | None = None


@dataclass
class PrinterReport:
    name: str
    access: str
    adapter: str | None
    records: list[ResultRecord] = field(default_factory=list)
