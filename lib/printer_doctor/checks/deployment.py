"""deployment.py — are the watch/vision user services delivering, or running `--dry-run`?

CHECK `watch-units-dry-run` REUSES `printer_ops.deploy_check` (`check_printer`, `fix_commands`,
  injectable `Show`; the logic of `3d printer vision doctor`): for a printer with a camera, fail when
  `3d-printer-vision@NAME` or `3d-printer-watch@NAME` runs with `--dry-run` (events printed, never
  delivered: the 2026-10-22 incident) or when no vision service runs at all. A host without
  `systemctl --user` (or no user D-Bus) cannot be inspected: the check SKIPs, never passes.
  Manual-only fix: the exact commands of `deploy_check.fix_commands`.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import TYPE_CHECKING

from printer_doctor.context import Context
from printer_ops.unit_names import CHECK, SUPERVISOR, unit_name
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    JsonValue,
    Verdict,
    command,
)

if TYPE_CHECKING:
    from printer_ops.deploy_check import PrinterVerdict


def _applies(ctx: Context) -> str | None:
    from errors import ThreeDError
    from printer_ops.camera import camera_spec

    if ctx.target.device is None:
        return "no device (offline printer)"
    try:
        spec = camera_spec(ctx.device)
    except ThreeDError:
        return "camera config is invalid (see check camera-config)"
    return None if spec.present else f"no camera configured ({spec.source})"


def _units(ctx: Context) -> PrinterVerdict:
    from printer_ops.deploy_check import check_printer

    return check_printer(ctx.name, has_camera=True, show=ctx.systemctl_show)


def _run(ctx: Context) -> Verdict:
    verdict = _units(ctx)
    unknown = [u for u in verdict.units if u.state == "unknown"]
    if unknown:
        raise CannotSee(
            f"{unknown[0].note or 'cannot inspect'}: run this on the host of the services"
        )
    units: list[JsonValue] = [
        {"unit": u.unit, "state": u.state, "dry_run": u.dry_run, "source": u.source}
        for u in verdict.units
    ]
    dry = [u for u in verdict.units if not u.ok]
    if dry:
        return Verdict.fail(
            "running with --dry-run (events are printed, never delivered): "
            + ", ".join(u.unit for u in dry),
            "error",
            units=units,
            warnings=verdict.warnings,
        )
    if verdict.warnings:
        return Verdict.fail(
            verdict.warnings[0], "warn", units=units, warnings=verdict.warnings
        )
    return Verdict.ok("vision and watch services run without --dry-run", units=units)


def _unit_fix_steps(ctx: Context, row: object) -> list[str]:
    """The fix commands for one evidence row, none for a healthy or an uninspectable unit."""
    from printer_ops.deploy_check import UnitVerdict, fix_commands

    if not isinstance(row, Mapping):
        return []
    unit = UnitVerdict(
        str(row.get("unit")),
        str(row.get("state")),
        row.get("dry_run") is True,
        str(row.get("source") or ""),
    )
    if unit.ok or unit.state == "unknown":
        return []
    return [
        command(c) if c.split(None, 1)[0] in ("systemctl", "3d") else c
        for c in fix_commands(ctx.name, unit)
    ]


def _supervisor_steps(ctx: Context, result: CheckResult) -> list[str]:
    """No worker unit to fix: the supervisor timer or its one-shot check is what the warning names."""
    name = shlex.quote(ctx.name)
    warnings = result.evidence.get("warnings")
    text = " ".join(str(w) for w in warnings) if isinstance(warnings, list) else ""
    timer, check = unit_name(SUPERVISOR, ctx.name), unit_name(CHECK, ctx.name)
    if f"{timer} is not-found" in text or f"{check} is not-found" in text:
        return [
            command(f"3d printer supervise install {name} --yes")
        ]  # nothing to enable: no unit files
    if f"{check} is failed" in text:
        # the timer runs: enabling it again fixes nothing, the check itself fails
        return [f"read the journal of the check: journalctl --user -u {shlex.quote(check)} -n 20"]
    # #747: only the supervisor timer is enabled; its one-shot check starts the vision and watch workers while a print runs
    return [command(f"systemctl --user enable --now {shlex.quote(timer)}")]


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    """The fix commands for the units the run itself found failing (rebuilt from its evidence, so
    systemd is asked once, not again for the steps)."""
    units = result.evidence.get("units")
    steps = [
        step
        for row in (units if isinstance(units, list) else [])
        for step in _unit_fix_steps(ctx, row)
    ]
    return steps or _supervisor_steps(ctx, result)


CHECKS = (
    Check(
        id="watch-units-dry-run",
        title="3d-printer-vision/watch services run for real (no --dry-run)",
        severity="error",
        applies_to="printers with a camera; needs `systemctl --user` on this host",
        contacts_printer=False,
        applies=_applies,
        run=_run,
        fix=Fix(
            "manual-only",
            "remove --dry-run from the unit, daemon-reload, restart",
            _steps,
        ),
    ),
)
