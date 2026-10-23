"""render.py — the doctor's output: a human report and the `printers.doctor/1` JSON.

JSON SCHEMA `printers.doctor/1` (docs/commands/printers-doctor.md):
  {schema, generated_at, printers: [{name, access, adapter, results: [{check, status, severity,
   summary, evidence, fix}]}], summary: {pass, fail, skip, error, fixed, remaining}}
  status: pass | fail | skip | error;  severity: info | warn | error;
  fix: null | {risk, description, manual_steps, applied, verified, error, journal}
  `error` of a fix carries why nothing (or not enough) happened: `refused: ...` (a safety rule),
  `not applied: ...` (needs --yes), `failed: ...` (the apply raised), `not fixed after apply: ...`.
  `journal` is the outcome of the settings-change record of an applied fix (imported | buffered |
  failed: ...), null when nothing was applied.
  `--list-checks --json`: {schema: "printers.doctor.checks/1", checks: [{id, title, severity,
  applies_to, contacts_printer, fix: null | {risk, description}}]}.
"""

from __future__ import annotations

import datetime
import shlex
from collections.abc import Sequence

from printer_doctor.engine import Summary
from printer_doctor.model import (
    Check,
    FixReport,
    JsonValue,
    PrinterReport,
    ResultRecord,
    clean,
    is_command,
)

SCHEMA = "printers.doctor/1"
CHECKS_SCHEMA = "printers.doctor.checks/1"
_MARK = {"pass": "ok  ", "fail": "FAIL", "skip": "skip", "error": "ERR "}


def fix_json(fix: FixReport | None) -> JsonValue:
    if fix is None:
        return None
    return {
        "risk": fix.risk,
        "description": fix.description,
        "manual_steps": fix.manual_steps,
        "applied": fix.applied,
        "verified": fix.verified,
        "error": fix.error,
        "journal": fix.journal,
    }


def to_json(
    reports: Sequence[PrinterReport], summary: Summary, now: datetime.datetime
) -> dict[str, JsonValue]:
    return {
        "schema": SCHEMA,
        "generated_at": now.astimezone(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "printers": [
            {
                "name": rep.name,
                "access": rep.access,
                "adapter": rep.adapter,
                "results": [
                    {
                        "check": rec.result.check,
                        "status": rec.result.status,
                        "severity": rec.result.severity,
                        "summary": rec.result.summary,
                        "evidence": rec.result.evidence,
                        "fix": fix_json(rec.fix),
                    }
                    for rec in rep.records
                ],
            }
            for rep in reports
        ],
        "summary": {
            "pass": summary.passed,
            "fail": summary.failed,
            "skip": summary.skipped,
            "error": summary.errored,
            "fixed": summary.fixed,
            "remaining": summary.remaining,
        },
    }


def checks_json(checks: Sequence[Check]) -> dict[str, JsonValue]:
    return {
        "schema": CHECKS_SCHEMA,
        "checks": [
            {
                "id": c.id,
                "title": c.title,
                "severity": c.severity,
                "applies_to": c.applies_to,
                "contacts_printer": c.contacts_printer,
                "fix": None
                if c.fix is None
                else {"risk": c.fix.risk, "description": c.fix.description},
            }
            for c in checks
        ],
    }


def checks_text(checks: Sequence[Check]) -> str:
    width = max((len(c.id) for c in checks), default=0)
    lines = []
    for c in checks:
        risk = c.fix.risk if c.fix else "-"
        lines.append(f"{c.id:<{width}}  {c.severity:<5}  fix: {risk:<11}  {c.title}")
        lines.append(f"{'':<{width}}  applies to: {c.applies_to}")
    return "\n".join(lines)


def _step_line(step: str) -> str:
    """`$ cmd` for a real shell command, `- text` for prose (model.command marks commands)."""
    return f"        {step}" if is_command(step) else f"        - {step}"


def _fix_lines(printer: str, rec: ResultRecord) -> list[str]:
    fix = rec.fix
    if fix is None:
        return []
    lines = [f"        fix [{fix.risk}]: {clean(fix.description)}"]
    if fix.applied and fix.verified:
        return [
            *lines,
            f"        -> fixed, verified by re-running the check (journal: {fix.journal})",
        ]
    if fix.applied:
        lines.append(f"        -> applied but {fix.error} (journal: {fix.journal})")
    elif fix.error:
        lines.append(f"        -> {fix.error}")
    if fix.risk == "manual-only" or fix.error:
        lines += [_step_line(clean(step)) for step in fix.manual_steps]
    elif fix.risk == "safe":
        lines.append("        -> run with --fix to apply it")
    else:
        lines.append("        -> run with --fix --yes to apply it")
    rerun = f"3d printers doctor {shlex.quote(printer)} --check {rec.result.check}"
    return [*lines, f"        re-run: {rerun}"]


def to_text(reports: Sequence[PrinterReport], summary: Summary) -> str:
    out: list[str] = []
    for rep in reports:
        out.append(f"{rep.name} ({rep.adapter or 'no adapter'}, access {rep.access})")
        width = max((len(r.result.check) for r in rep.records), default=0)
        for rec in rep.records:
            r = rec.result
            tag = f" [{r.severity}]" if r.status in ("fail", "error") else ""
            out.append(
                f"  {_MARK[r.status]} {r.check:<{width}}{tag}  {clean(r.summary)}"
            )
            out.extend(_fix_lines(rep.name, rec))
        out.append("")
    out.append(
        f"{summary.passed} pass, {summary.failed} fail, {summary.skipped} skip, "
        f"{summary.errored} error; {summary.fixed} fixed, {summary.remaining} remaining"
    )
    return "\n".join(out)
