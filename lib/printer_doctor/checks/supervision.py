"""supervision.py — is this printer under ALWAYS-ON AI supervision right now? (#639, #642)

CHECK `printer-supervision` (error): the owner's requirement is that supervision works while a print runs, also
  while the printer is switched off between prints, and that it catches a print start from any route. That
  needs, for every owned fully controlled fleet printer: a `3d-printer-watch@NAME` unit (and, with a camera, a
  `3d-printer-vision@NAME` unit) that is installed, running, enabled, NOT `--dry-run`, with a heartbeat that
  moves (watch/NAME.json < 75 s, vision/NAME.json < max(90 s, 4 cycles), a camera frame < 180 s old while a
  print is supervised), exactly one watcher per printer, and `loginctl enable-linger` so it all runs without a
  login and after a reboot. The verdict logic is `printer_ops.supervise.assess`, shared with
  `3d printer supervise status`.
PASS  every witness agrees (a switched-off printer passes while its units wait for it).
FAIL  (error) the printer ANSWERS (printing or idle) and a unit is missing/stopped/wedged, linger is off, or a
  dry-run unit/drop-in holds the alerts; (warn) the same gap on a printer that does not answer now: nothing is
  being missed, but the next print would be.
SKIP  not an owned `access: full` printer with a watch adapter; `systemctl --user` is unavailable on this host
  (CannotSee: run it where the services run).
FIX   needs-yes, local only (touches the unit directory, never the printer):
  `3d printer supervise install NAME --yes` — writes the shipped templates, retires obsolete intentional-off
  gates (a copy stays), `daemon-reload`, `enable --now`, `enable-linger`. Every applied fix is verified by
  re-running the check.
"""

from __future__ import annotations

import contextlib
import io
import shlex

from printer_doctor.context import Context
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    FixChange,
    JsonValue,
    Verdict,
    command,
)
from printer_ops import supervise as sv
from printer_ops.supervise_units import PrinterPlan


def _plan(ctx: Context) -> PrinterPlan | None:
    from errors import ThreeDError
    from printer_ops.camera import camera_spec
    from printer_ops.supervise_units import WATCH_ADAPTERS

    device = ctx.target.device
    fleet = ctx.target.fleet
    if device is None or fleet is None:
        return None
    if (
        fleet.owner != "own"
        or fleet.access != "full"
        or device.adapter not in WATCH_ADAPTERS
    ):
        return None
    try:
        camera = camera_spec(device).present
    except ThreeDError:
        camera = False
    return PrinterPlan(ctx.name, device.adapter, camera)


def _applies(ctx: Context) -> str | None:
    if _plan(ctx) is None:
        return "not an owned, fully controlled printer with a watch adapter (nothing is supervised for it)"
    return None


def _env(ctx: Context) -> sv.Env:
    src = ctx.services.supervision

    def state(_name: str) -> str | None:
        try:
            return ctx.adapter_status().state
        except CannotSee:
            return None

    plan = _plan(ctx)
    return sv.Env(
        plans=lambda: [plan] if plan is not None else [],
        show=ctx.systemctl_show,
        run=src.runner,
        state_dir=ctx.state_dir(),
        unit_dir=src.unit_dir,
        clock=ctx.now,
        state_probe=state,
        alive=src.alive or sv.pid_alive,
    )


def _linger(ctx: Context, env: sv.Env) -> bool | None:
    src = ctx.services.supervision
    if src.linger is not None:
        return src.linger()
    return sv.linger_enabled(env.runner())


def _assess(ctx: Context) -> sv.Assessment:
    from printer_ops.supervise_units import Layout

    plan = _plan(ctx)
    assert plan is not None  # `applies` filtered
    env = _env(ctx)
    layout = Layout.detect(unit_dir=env.units_dir())
    return sv.gather(plan, env, live=True, layout=layout, linger=_linger(ctx, env))


def _run(ctx: Context) -> Verdict:
    a = _assess(ctx)
    unknown = [u for u in a.units.values() if not u.known]
    if unknown:
        raise CannotSee(
            f"{unknown[0].unit}: `systemctl --user` is not available here: run this on the host of the services"
        )
    evidence: dict[str, JsonValue] = {"assessment": a.to_json()}
    if a.level == sv.OK:
        return Verdict.ok(
            a.findings[-1].text if a.findings else "supervised", **evidence
        )
    worst = next(f for f in a.findings if f.level == a.level)
    extra = sum(1 for f in a.findings if f.level != sv.OK) - 1
    summary = worst.text + (
        f" (+{extra} more finding{'s' if extra > 1 else ''})" if extra > 0 else ""
    )
    return Verdict.fail(summary, "error" if a.level == sv.FAIL else "warn", **evidence)


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    name = shlex.quote(ctx.name)
    steps = [command(f"3d printer supervise install {name} --yes")]
    seen: set[str] = set(steps)
    assessment = result.evidence.get("assessment")
    findings = assessment.get("findings") if isinstance(assessment, dict) else None
    for f in findings if isinstance(findings, list) else []:
        fix = f.get("fix") if isinstance(f, dict) else None
        if isinstance(fix, str) and fix and f.get("level") != sv.OK:
            step = command(fix.split("   #")[0].strip())
            if step not in seen:
                seen.add(step)
                steps.append(step)
    steps.append(command(f"3d printer supervise status {name}"))
    return steps


def _apply(ctx: Context, result: CheckResult) -> FixChange:
    env = _env(ctx)
    before = result.summary
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            sv.cmd_install([ctx.name, "--yes"], env)
        except Exception as exc:  # a structured failure of the install is the fix's failure, with its output
            raise CannotSee(
                f"install failed: {exc}; output: {out.getvalue()[-400:]}", "error"
            ) from exc
    after = _assess(ctx)
    return FixChange(
        before,
        after.findings[0].text if after.findings else "",
        note=out.getvalue().strip()[-600:],
    )


CHECKS = (
    Check(
        id="printer-supervision",
        title="always-on AI supervision: units running, heartbeats fresh, linger on, one watcher",
        severity="error",
        applies_to=(
            "owned (owner own) access-full printers with a watch adapter (moonraker, creality-stock); "
            "needs `systemctl --user` on this host"
        ),
        contacts_printer=True,
        applies=_applies,
        run=_run,
        fix=Fix(
            "needs-yes",
            "install or repair the shipped watch/vision units for this printer (`3d printer supervise install NAME --yes`)",
            _steps,
            apply=_apply,
            touches_printer=False,
        ),
    ),
)
