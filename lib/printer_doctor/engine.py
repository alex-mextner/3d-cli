"""engine.py — runs checks against printers, applies fixes under their risk class, verifies them.

WHAT: `run_printer` evaluates every selected check against one `Context`, then (with
  `Options.fix`) walks the failed ones:
    safe         applied;
    needs-yes    applied only with `Options.yes`, else reported as not applied;
    manual-only  never executed, its manual steps are reported.
  An applied fix is verified by re-running its check on a fresh cache: `verified` is the check
  passing again, otherwise the report says it was not fixed after the apply and the result stays a
  finding at the check's severity (a verifying run that can only skip/error counts as unconfirmed,
  never as a pass). Every applied fix writes a journal record (printer_doctor.journal) whatever
  the verification said.
  Only a printer with `access: full` is ever written to: `safe` / `needs-yes` fixes are refused on
  `read-only` printers (nothing sent) and `manual-only` fixes never run at all.
WHY: the checks only describe a problem and a repair; the policy (what may run unattended, what
  needs a yes, what is never run) lives in this one place.
INVARIANTS: an `access: offline` printer is never contacted (checks that contact the printer are
  skipped with `never contacted`). A crashing check, fix, steps function or journal becomes an
  `error` / per-fix `error` text, never a traceback that hides the other checks or the report of
  a restart that was already sent. Without `fix` the run sends no POST (the Context logs requests,
  so tests prove it). A fix refused for safety (printer busy, access) has sent nothing. Text a
  printer chose (summaries, steps, errors) goes through `clean` so it cannot drive the terminal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from errors import ThreeDError
from printer_doctor.context import Context
from printer_doctor.journal import JournalEntry
from printer_doctor.model import (
    RANK,
    CannotSee,
    Check,
    CheckResult,
    Fix,
    FixRefused,
    FixReport,
    PrinterReport,
    ResultRecord,
    Verdict,
    clean,
)

NEVER_CONTACTED = "never contacted (access: offline)"


@dataclass(frozen=True)
class Options:
    fix: bool = False
    yes: bool = False
    only: frozenset[str] | None = None
    skip: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Summary:
    passed: int
    failed: int
    skipped: int
    errored: int
    fixed: int
    remaining: int  # findings at/above --fail-on that are still there after the fixes


def select(checks: Iterable[Check], options: Options) -> list[Check]:
    return [
        c
        for c in checks
        if (options.only is None or c.id in options.only) and c.id not in options.skip
    ]


def evaluate(check: Check, ctx: Context) -> CheckResult:
    """One check -> one result; never raises."""
    if check.contacts_printer and not ctx.target.contactable:
        verdict = Verdict.skip(NEVER_CONTACTED)
    else:
        try:
            reason = check.applies(ctx)
            verdict = Verdict.skip(reason) if reason else check.run(ctx)
        except CannotSee as exc:
            verdict = Verdict(exc.status, exc.reason)
        except ThreeDError as exc:
            verdict = Verdict("error", exc.message)
        except Exception as exc:  # a broken check must not hide the others
            verdict = Verdict("error", f"check crashed: {type(exc).__name__}: {exc}")
    severity = (
        "error" if verdict.status == "error" else (verdict.severity or check.severity)
    )
    return CheckResult(
        check.id, verdict.status, severity, clean(verdict.summary), verdict.evidence
    )


def _steps(fix: Fix, ctx: Context, result: CheckResult) -> list[str]:
    try:
        return [clean(step) for step in fix.manual_steps(ctx, result)]
    except CannotSee as exc:
        return [f"(manual steps unavailable: {clean(exc.reason)})"]
    except Exception as exc:  # the steps are advice: never lose the report over them
        return [f"(manual steps unavailable: {clean(f'{type(exc).__name__}: {exc}')})"]


def _why(exc: BaseException) -> str:
    return clean(str(getattr(exc, "message", None) or exc))


def _unconfirmed(check: Check, before: CheckResult, after: CheckResult) -> CheckResult:
    """The result after a fix whose verifying run did not pass. A run that still fails is the new
    finding; one that could not look (skip) or broke (error) is an error-status finding at the
    severity the check had before the fix, so the exit code never reads it as healed."""
    if after.status == "fail":
        return after
    return CheckResult(
        check.id,
        "error",
        before.severity,
        f"fix applied but the check could not confirm it: {after.summary}",
        after.evidence,
    )


def _apply(check: Check, fix: Fix, ctx: Context, record: ResultRecord) -> None:
    """Apply `fix` for the failed `record`, verify it, journal it; outcome lands in record.fix."""
    from printer_ops.base import PrinterRefused

    report = record.fix
    assert report is not None and fix.apply is not None
    try:
        change = fix.apply(ctx, record.result)
    except (FixRefused, PrinterRefused) as exc:
        report.error = f"refused: {_why(exc)}"
        return
    except CannotSee as exc:
        report.error = f"failed: {clean(exc.reason)}"
        return
    except Exception as exc:  # incl. ThreeDError/OSError and anything unexpected
        known = isinstance(exc, (ThreeDError, OSError))
        report.error = "failed: " + (
            _why(exc) if known else clean(f"{type(exc).__name__}: {exc}")
        )
        return
    report.applied = True
    report.journal = ctx.journal(
        JournalEntry(
            ctx.name, check.id, fix.risk, change.before, change.after, change.note
        )
    )
    ctx.invalidate()
    after = evaluate(check, ctx)
    report.verified = after.status == "pass"
    if not report.verified:
        report.error = f"not fixed after apply: {after.summary}"
        after = _unconfirmed(check, record.result, after)
    record.result = after


def _refusal(fix: Fix, ctx: Context, options: Options) -> str | None:
    """Why `fix` must not be applied now, before the fix function runs; None = go ahead."""
    access = ctx.target.access
    if access != "full":
        return (
            f"refused: access {access} (3d only writes to printers with access: full)"
        )
    if fix.risk == "needs-yes" and not options.yes:
        return "not applied: this fix needs --yes (it restarts something)"
    return None


def run_printer(
    ctx: Context, checks: Sequence[Check], options: Options
) -> PrinterReport:
    target = ctx.target
    report = PrinterReport(target.name, target.access, target.adapter)
    for check in select(checks, options):
        record = ResultRecord(evaluate(check, ctx))
        fix = check.fix
        if fix is not None and record.result.status == "fail":
            record.fix = FixReport(
                fix.risk, fix.description, _steps(fix, ctx, record.result)
            )
            if options.fix and fix.risk != "manual-only":
                record.fix.error = _refusal(fix, ctx, options)
                if record.fix.error is None:
                    _apply(check, fix, ctx, record)
        report.records.append(record)
    return report


def summarize(reports: Sequence[PrinterReport], fail_on: str) -> Summary:
    """`fail_on` is a severity (error|warn) or `never`; the CLI validates it."""
    counts = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    fixed = remaining = 0
    floor = RANK.get(fail_on)  # None for `never`
    for rep in reports:
        for rec in rep.records:
            counts[rec.result.status] += 1
            if rec.fix is not None and rec.fix.verified:
                fixed += 1
            if (
                floor is not None
                and rec.result.finding
                and RANK[rec.result.severity] >= floor
            ):
                remaining += 1
    return Summary(
        counts["pass"],
        counts["fail"],
        counts["skip"],
        counts["error"],
        fixed,
        remaining,
    )
