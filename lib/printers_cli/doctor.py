"""doctor.py — `3d printers doctor`: diagnose (and with --fix, repair) every printer of the fleet.

Thin shell: argument parsing, rendering and the exit code. The engine lives in lib/printer_doctor/
(model, context, engine, registry, journal, render) and the checks in lib/printer_doctor/checks/*.py
(drop-in: one module defining `CHECKS`). Reference: docs/commands/printers-doctor.md.
Also reachable as `3d printer doctor` (commands/printer.py forwards here).
INVARIANTS: stdlib-only at import time (this module is imported by `3d printers --help`); the engine
is imported inside `run`. Default is read-only. Exit 0 / 1 (a finding at or above --fail-on remains
after the fixes: GateFailure silent, the report is already printed) / 2 (usage).
"""

from __future__ import annotations

import datetime
import math
from typing import TYPE_CHECKING

from errors import GateFailure, InvalidArgument, UsageError
from printers_cli import Sub
from printers_cli._common import emit_json, parse, wants_help

if TYPE_CHECKING:
    from printer_doctor.context import Services as ServicesType

CMD_NAME = "printers doctor"
FAIL_ON = ("error", "warn", "never")

USAGE = """3d printers doctor [PRINTER ...|--all] [--fix] [--yes] [--json] [--check ID[,ID]]
                     [--skip ID[,ID]] [--fail-on error|warn|never] [--secs S] [--list-checks]

  Diagnose the printers of the fleet registry (fleet/printers.yaml + printer-devices.yaml) with one
  check per known failure: unreachable printer, Klipper in shutdown, failed Moonraker components,
  too-wide trusted_clients, Spoolman link and CORS, active spool vs PrintOps, camera, watch services
  in --dry-run, stale device overrides, stuck jobs, clock skew, full disk, no web UI, head flags vs
  faults. Read-only by default. `--list-checks` shows every check id.
  An access: offline printer is never contacted (its network checks are skipped).

Arguments:
  PRINTER ...        printer names from `3d printer list`; none and no --all is a usage error
  --all              every printer of the registry                  3d printers doctor --all

Options:
  --fix              apply the `safe` fixes (idempotent, never mid-print), verify each by re-running
                     its check, journal it (settings-change buffer; 3d-api when reachable);
                     `needs-yes` fixes are listed, not run; `manual-only` ones only print their steps
                                                                     3d printers doctor k1se --fix
  --yes              with --fix: also apply `needs-yes` fixes (restarts Klipper / the firmware;
                     refused while a job is printing or paused)      3d printers doctor k1se --fix --yes
  --json             machine-readable report, schema printers.doctor/1   3d printers doctor --all --json
  --check ID[,ID]    run only these checks                           3d printers doctor u1 --check klippy-state,disk-space
  --skip ID[,ID]     run all but these                               3d printers doctor --all --skip camera-snapshot
  --fail-on LEVEL    exit 1 when a finding of this severity or worse remains: error (default),
                     warn, never                                     3d printers doctor --all --fail-on warn
  --secs S           listen window in seconds for a Creality stock printer's WebSocket (default 3)
                                                                     3d printers doctor k1se --secs 5
  --list-checks      list the checks (id, severity, fix risk, applies to); with --json as
                     printers.doctor.checks/1                        3d printers doctor --list-checks --json

Fix risk classes: safe (--fix), needs-yes (--fix --yes), manual-only (never executed).
Exit codes: 0 no finding at/above --fail-on remains; 1 one remains (the report is printed);
2 usage error (unknown printer/check/option, no printer given).

Examples:
  3d printers doctor --all
  3d printers doctor u1 k1se --json
  3d printers doctor k1se --fix
  3d printers doctor u1 --check klippy-state --fix --yes"""


def _ids(raw: str | None, option: str, known: list[str]) -> frozenset[str] | None:
    if raw is None:
        return None
    wanted = [i.strip() for i in raw.split(",") if i.strip()]
    if not wanted:
        raise UsageError(
            f"{option} needs at least one check id",
            command=CMD_NAME,
            remediation=["3d printers doctor --list-checks"],
        )
    for item in wanted:
        if item not in known:
            raise InvalidArgument(
                option,
                item,
                known,
                command=CMD_NAME,
                extra="`3d printers doctor --list-checks` describes each check.",
            )
    return frozenset(wanted)


def _names(pos: list[str], all_flag: bool, valid: list[str]) -> list[str]:
    if all_flag and pos:
        raise UsageError(
            "give printer names or --all, not both",
            command=CMD_NAME,
            remediation=["3d printers doctor --all", "3d printers doctor u1 k1se"],
        )
    if all_flag:
        return valid
    if not pos:
        raise UsageError(
            "no printer given: name one or more printers, or pass --all",
            command=CMD_NAME,
            remediation=[
                f"valid printers: {', '.join(valid) or '(none configured)'}",
                "3d printers doctor --all",
            ],
        )
    by_fold = {n.casefold(): n for n in valid}
    out: list[str] = []
    for name in pos:
        resolved = name if name in valid else by_fold.get(name.casefold())
        if resolved is None:
            raise InvalidArgument(
                "printer",
                name,
                valid,
                command=CMD_NAME,
                extra="`3d printer list` shows every printer of the fleet.",
            )
        if resolved not in out:
            out.append(resolved)
    return out


def run(argv: list[str], services: ServicesType | None = None) -> int:
    """`services` (printer_doctor.context.Services) is the test seam; the CLI passes none."""
    if wants_help(argv):
        print(USAGE)
        return 0
    from printer_doctor import render
    from printer_doctor.context import Context, Services
    from printer_doctor.engine import Options, run_printer, summarize
    from printer_doctor.registry import discover_checks
    from printer_doctor.targets import load_targets

    p = parse(
        argv,
        "doctor",
        values={"--check", "--skip", "--fail-on", "--secs"},
        flags={"--all", "--fix", "--yes", "--json", "--list-checks"},
        n_pos=(0, 1000),
    )
    checks = discover_checks()
    known = [c.id for c in checks]
    as_json = "--json" in p.flags
    if "--list-checks" in p.flags:
        if as_json:
            emit_json(render.checks_json(checks))
        else:
            print(render.checks_text(checks))
        return 0
    fail_on = p.opts.get("--fail-on", "error")
    if fail_on not in FAIL_ON:
        raise InvalidArgument("--fail-on", fail_on, list(FAIL_ON), command=CMD_NAME)
    if "--yes" in p.flags and "--fix" not in p.flags:
        raise UsageError(
            "--yes only applies together with --fix",
            command=CMD_NAME,
            remediation=["3d printers doctor PRINTER --fix --yes"],
        )
    svc = services or Services()
    if "--secs" in p.opts:
        raw = p.opts["--secs"]
        try:
            secs = float(raw)
        except ValueError:
            secs = 0.0
        if not (math.isfinite(secs) and secs > 0):
            raise InvalidArgument(
                "--secs",
                raw,
                ["a positive number of seconds, e.g. 3"],
                command=CMD_NAME,
            )
        svc.listen_s = secs
    options = Options(
        fix="--fix" in p.flags,
        yes="--yes" in p.flags,
        only=_ids(p.opts.get("--check"), "--check", known),
        skip=_ids(p.opts.get("--skip"), "--skip", known) or frozenset(),
    )
    targets = load_targets()
    names = _names(p.pos, "--all" in p.flags, sorted(targets))
    reports = [run_printer(Context(targets[n], svc), checks, options) for n in names]
    summary = summarize(reports, fail_on)
    now = datetime.datetime.fromtimestamp(svc.clock(), datetime.timezone.utc)
    if as_json:
        emit_json(render.to_json(reports, summary, now))
    else:
        print(render.to_text(reports, summary))
    if summary.remaining:
        raise GateFailure(
            f"{summary.remaining} finding(s) at or above --fail-on {fail_on} remain",
            command=CMD_NAME,
            silent=True,
        )
    return 0


SUB = Sub(
    name="doctor",
    summary="diagnose (and with --fix repair) the printers: Klipper, Moonraker, Spoolman, camera, watch, registry",
    usage=USAGE,
    run=run,
)
