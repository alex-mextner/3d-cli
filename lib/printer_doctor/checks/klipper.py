"""klipper.py — Moonraker-family health: Klipper state, Moonraker components, stuck jobs, disk.

CHECKS
  klippy-state          `/server/info` klippy_state must be `ready`. Fix (needs-yes): restart the
                        firmware (`shutdown`) or the Klipper host service (`error`/`disconnected`)
                        through Moonraker, refused while a job is printing or paused, verified by
                        Klipper reporting `ready` within a bounded poll. `startup` is refused:
                        Klipper is still booting, a restart would only restart that.
  moonraker-components  `failed_components` / `warnings` of `/server/info` (a component that did
                        not load: spoolman, update_manager, ...). Manual.
  print-stuck           a job in `paused` for longer than PAUSED_WARN_S (default 2 h). Klipper
                        stamps no pause time, so the pause length is approximated as
                        `total_duration - print_duration` of print_stats (time since the job
                        started minus time spent extruding): it includes heating/prep (minutes),
                        which only makes the number slightly high, never hides a long pause.
                        "printing with no progress" is not cheaply observable and is not checked.
  disk-space            free space of the `gcodes` root from `/server/files/directory`.
                        warn below 5 % or 200 MB free, error below 1 % or 50 MB (decimal MB).
WHY: a printer in `shutdown` accepts nothing until restarted; a failed component silently
  disables a feature (usage booking, updates); a job paused overnight cools down and fails; a full
  disk makes the next upload fail halfway.
"""

from __future__ import annotations

from collections.abc import Mapping

from printer_doctor.checks._common import human_bytes, str_list
from printer_doctor.context import Context
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    FixChange,
    FixRefused,
    JsonValue,
    Verdict,
)

PAUSED_WARN_S = 2 * 3600.0
DISK_WARN_PCT, DISK_WARN_BYTES = 5.0, 200_000_000
DISK_ERROR_PCT, DISK_ERROR_BYTES = 1.0, 50_000_000
RESTART_FOR = {
    "shutdown": "/printer/firmware_restart",
    "error": "/printer/restart",
    "disconnected": "/printer/restart",
}


def _num(value: JsonValue) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


# ---- klippy-state ---------------------------------------------------------------------------


def _klippy_applies(ctx: Context) -> str | None:
    return ctx.moonraker_only()


def _klippy_state(ctx: Context) -> Verdict:
    info = ctx.server_info()
    state = info.get("klippy_state")
    evidence: dict[str, JsonValue] = {
        "klippy_state": state,
        "klippy_connected": info.get("klippy_connected"),
    }
    try:
        message = ctx.result_obj("/printer/info").get("state_message")
    except CannotSee:
        message = None
    if message:
        evidence["state_message"] = message
    if state == "ready":
        return Verdict.ok("Klipper is ready", **evidence)
    return Verdict.fail(f"Klipper is {state!r}, not ready", "error", **evidence)


def _wait_ready(ctx: Context) -> str:
    """Poll `/server/info` until klippy_state is ready or `services.poll_s` has passed; returns the
    last state seen ('unreachable' when Moonraker itself did not answer)."""
    deadline = ctx.now() + ctx.services.poll_s
    state = "unreachable"
    while True:
        try:
            raw = ctx.server_info(fresh=True).get("klippy_state")
            state = raw if isinstance(raw, str) else "unknown"
        except CannotSee:
            state = "unreachable"
        if state == "ready" or ctx.now() >= deadline:
            return state
        ctx.sleep(1.0)


def _restart(ctx: Context, result: CheckResult) -> FixChange:
    before = str(result.evidence.get("klippy_state"))
    if before == "startup":
        raise FixRefused(
            "Klipper is still starting up: wait for it, a restart would only restart the boot"
        )
    path = RESTART_FOR.get(before)
    if path is None:
        raise FixRefused(f"no restart is known for klippy_state {before!r}")
    job = ctx.job_state()
    if job in ("printing", "paused"):
        raise FixRefused(f"a job is {job}: restarting Klipper would kill it")
    ctx.post(path)
    return FixChange(before, _wait_ready(ctx), f"POST {path}")


def _klippy_steps(ctx: Context, result: CheckResult) -> list[str]:
    state = result.evidence.get("klippy_state")
    message = result.evidence.get("state_message")
    path = RESTART_FOR.get(str(state), "/printer/restart")
    verb = "FIRMWARE_RESTART" if path.endswith("firmware_restart") else "RESTART"
    steps = [
        f"Read why: Klipper says {message!r}"
        if message
        else "Read why: the web UI shows Klipper's message.",
        f"In the web UI press {verb} (not while a job runs), or: curl -X POST {ctx.base_url}{path}",
        "If it comes back as `error`, the printer.cfg is wrong: fix the config named in the message, then RESTART.",
        "If it stays `disconnected`, the Klipper service is not running: restart it from the web UI "
        "(Machine tab -> klipper) or on the printer: `systemctl restart klipper`.",
    ]
    return steps


# ---- moonraker-components -------------------------------------------------------------------


def _components(ctx: Context) -> Verdict:
    info = ctx.server_info()
    failed = str_list(info.get("failed_components"))
    warnings = str_list(info.get("warnings"))
    if not failed and not warnings:
        return Verdict.ok("every Moonraker component loaded, no warnings")
    parts = []
    if failed:
        parts.append(f"failed components: {', '.join(failed)}")
    if warnings:
        parts.append(f"{len(warnings)} warning(s)")
    return Verdict.fail(
        "; ".join(parts), "warn", failed_components=failed, warnings=warnings
    )


def _components_steps(ctx: Context, result: CheckResult) -> list[str]:
    failed = result.evidence.get("failed_components")
    names = (
        ", ".join(str(n) for n in failed)
        if isinstance(failed, list) and failed
        else "the named component"
    )
    return [
        f"Read moonraker.log (web UI: Machine tab -> moonraker.log) for the error of {names}.",
        "Fix the matching section of moonraker.conf (the `config` file root; Fluidd/Mainsail "
        "'Configuration' editor), or remove the section when the feature is not wanted.",
        "Restart Moonraker (web UI: Machine tab -> moonraker, or `systemctl restart moonraker`); "
        "the failed list is empty after a clean start.",
        "Warnings are config problems Moonraker named: moonraker docs https://moonraker.readthedocs.io/en/latest/configuration/",
    ]


# ---- print-stuck ----------------------------------------------------------------------------


def _stuck(ctx: Context) -> Verdict:
    body = ctx.result_obj("/printer/objects/query?print_stats")
    status = body.get("status")
    stats = status.get("print_stats") if isinstance(status, Mapping) else None
    if not isinstance(stats, Mapping):
        raise CannotSee("print_stats missing from objects/query", "error")
    state = stats.get("state")
    total, extruding = (
        _num(stats.get("total_duration")),
        _num(stats.get("print_duration")),
    )
    evidence: dict[str, JsonValue] = {
        "state": state,
        "filename": stats.get("filename"),
        "total_duration_s": total,
        "print_duration_s": extruding,
        "threshold_s": PAUSED_WARN_S,
    }
    if state != "paused":
        return Verdict.ok(f"job state {state!r}, nothing paused", **evidence)
    if total is None or extruding is None:
        raise CannotSee(
            "paused, but print_stats carries no durations to measure the pause", "error"
        )
    paused = max(0.0, total - extruding)
    evidence["paused_for_s"] = round(paused)
    if paused > PAUSED_WARN_S:
        return Verdict.fail(
            f"job paused for about {paused / 3600:.1f} h", "warn", **evidence
        )
    return Verdict.ok(
        f"job paused for about {paused / 60:.0f} min (limit {PAUSED_WARN_S / 3600:.0f} h)",
        **evidence,
    )


def _stuck_steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        "Look at the printer: a paused job with a cooled nozzle and bed usually needs the cause "
        "fixed (filament runout, a jam) and RESUME, or CANCEL.",
        f"Resume or cancel from the web UI, or `3d printer cancel {ctx.name}` (3d refuses "
        "to touch a printer with access read-only).",
        "A paused job holds the plate: nothing else can print on this printer until it is resumed or cancelled.",
    ]


# ---- disk-space -----------------------------------------------------------------------------


def _disk(ctx: Context) -> Verdict:
    body = ctx.result_obj("/server/files/directory?path=gcodes")
    usage = body.get("disk_usage")
    if not isinstance(usage, Mapping):
        raise CannotSee("the gcodes root reports no disk_usage")
    total, used, free = (
        _num(usage.get("total")),
        _num(usage.get("used")),
        _num(usage.get("free")),
    )
    if total is None or free is None or total <= 0:
        raise CannotSee(f"unusable disk_usage {dict(usage)!r}", "error")
    pct = 100.0 * free / total
    evidence: dict[str, JsonValue] = {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "free_pct": round(pct, 1),
    }
    text = f"{human_bytes(free)} free of {human_bytes(total)} ({pct:.1f} %)"
    if pct < DISK_ERROR_PCT or free < DISK_ERROR_BYTES:
        return Verdict.fail(f"G-code disk almost full: {text}", "error", **evidence)
    if pct < DISK_WARN_PCT or free < DISK_WARN_BYTES:
        return Verdict.fail(f"G-code disk is filling up: {text}", "warn", **evidence)
    return Verdict.ok(text, **evidence)


def _disk_steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        "Delete old G-code: web UI (Fluidd/Mainsail) G-code files list -> select -> delete, "
        "or `curl -X DELETE " + ctx.base_url + "/server/files/gcodes/<file>.gcode`.",
        "Timelapse videos and `.thumbs` also live in the same partition on most firmwares; clear those too.",
        "Check the number again: `3d printers doctor "
        + ctx.name
        + " --check disk-space`.",
    ]


CHECKS = (
    Check(
        id="klippy-state",
        title="Klipper is ready (not shutdown / error / disconnected)",
        severity="error",
        applies_to="Moonraker-family printers",
        applies=_klippy_applies,
        run=_klippy_state,
        fix=Fix(
            "needs-yes",
            "restart the firmware (shutdown) or the Klipper host (error/disconnected); refused while a job runs",
            _klippy_steps,
            _restart,
        ),
    ),
    Check(
        id="moonraker-components",
        title="no Moonraker component failed to load, no config warnings",
        severity="warn",
        applies_to="Moonraker-family printers",
        applies=lambda ctx: ctx.moonraker_only(),
        run=_components,
        fix=Fix(
            "manual-only",
            "fix the named section of moonraker.conf, restart Moonraker",
            _components_steps,
        ),
    ),
    Check(
        id="print-stuck",
        title="no job paused for longer than 2 h",
        severity="warn",
        applies_to="Moonraker-family printers",
        applies=lambda ctx: ctx.moonraker_only(),
        run=_stuck,
        fix=Fix("manual-only", "resume or cancel the paused job", _stuck_steps),
    ),
    Check(
        id="disk-space",
        title="the G-code disk has room (>= 5 % and 200 MB free)",
        severity="error",
        applies_to="Moonraker-family printers",
        applies=lambda ctx: ctx.moonraker_only(),
        run=_disk,
        fix=Fix("manual-only", "delete old G-code / timelapses", _disk_steps),
    ),
)
