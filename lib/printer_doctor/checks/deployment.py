"""deployment.py — are the watch/vision user services delivering, or running `--dry-run`?

CHECK `watch-units-dry-run` REUSES `printer_ops.deploy_check` (`check_printer`, `fix_commands`,
  injectable `Show`; the logic of `3d printer vision doctor`): for a printer with a camera, fail when
  `3d-printer-vision@NAME` or `3d-printer-watch@NAME` runs with `--dry-run` (events printed, never
  delivered: the 2026-10-22 incident) or when no vision service runs at all. A host without
  `systemctl --user` (or no user D-Bus) cannot be inspected: the check SKIPs, never passes.
  Manual-only fix: the exact commands of `deploy_check.fix_commands`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from printer_doctor.context import Context
from printer_doctor.model import CannotSee, Check, CheckResult, Fix, JsonValue, Verdict

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

    return check_printer(ctx.name, True, ctx.systemctl_show)


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


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    from printer_ops.deploy_check import fix_commands

    verdict = _units(ctx)
    steps: list[str] = []
    for unit in verdict.units:
        if not unit.ok and unit.state != "unknown":
            steps += fix_commands(ctx.name, unit)
    if not steps:
        steps = [
            f"systemctl --user enable --now 3d-printer-vision@{ctx.name}.service",
            f"systemctl --user enable --now 3d-printer-watch@{ctx.name}.service",
        ]
    return steps


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
