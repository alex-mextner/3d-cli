"""klipper.py — Moonraker-family health: Klipper state, Moonraker components, stuck jobs, disk.

CHECKS
  klippy-state          `/server/info` klippy_state must be `ready`. Fix (needs-yes), through
                        Moonraker: `shutdown` -> `POST /printer/firmware_restart`, `error` ->
                        `POST /printer/restart` (both are Klippy requests relayed over its socket),
                        `disconnected` -> `POST /machine/services/restart {"service": "klipper"}`
                        (Klippy is not connected, so only the host service can be restarted; needs
                        a Moonraker with a service provider and `klipper` in allowed_services,
                        https://moonraker.readthedocs.io/en/latest/external_api/machine/). An HTTP
                        answer >= 400 is a failed fix (nothing journalled: nothing happened).
                        Refused while a job is printing or paused; a print state that cannot be
                        read counts as "Klipper is not answering" here (its job was halted).
                        Verified by Klipper reporting `ready` within a bounded poll. `startup` is
                        refused: Klipper is still booting, a restart would only restart that.
  moonraker-components  `failed_components` / `warnings` of `/server/info` (a component that did
                        not load: spoolman, update_manager, ...). Manual.
  print-stuck           a job that is `paused` NOW and has spent more than PAUSED_WARN_S (2 h) not
                        extruding. Klipper stamps no pause time, so the figure is
                        `total_duration - print_duration` of print_stats: CUMULATIVE time without
                        extrusion since the job started (every pause that was resumed, plus the
                        heating before the first extrusion), not the length of the current pause.
                        A job with several long colour-change pauses can therefore warn right after
                        a short new pause; the evidence says which number it is
                        (`not_extruding_s`). "printing with no progress" is not cheaply observable
                        and is not checked.
  disk-space            free space of the `gcodes` root from `/server/files/directory`.
                        warn below 5 % or 200 MB free, error below 1 % or 50 MB (decimal MB).
WHY: a printer in `shutdown` accepts nothing until restarted; a failed component silently
  disables a feature (usage booking, updates); a job paused overnight cools down and fails; a full
  disk makes the next upload fail halfway.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from typing import NamedTuple

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
    command,
)

PAUSED_WARN_S = 2 * 3600.0
DISK_WARN_PCT, DISK_WARN_BYTES = 5.0, 200_000_000
DISK_ERROR_PCT, DISK_ERROR_BYTES = 1.0, 50_000_000


class Restart(NamedTuple):
    path: str
    payload: Mapping[str, JsonValue] | None = None


RESTART_FOR = {
    "shutdown": Restart("/printer/firmware_restart"),
    "error": Restart("/printer/restart"),
    "disconnected": Restart("/machine/services/restart", {"service": "klipper"}),
}


def _num(value: JsonValue) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


# ---- klippy-state ---------------------------------------------------------------------------


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
    restart = RESTART_FOR.get(before)
    if restart is None:
        raise FixRefused(f"no restart is known for klippy_state {before!r}")
    try:
        job = ctx.job_state()
    except CannotSee:
        job = None  # Klipper is down, so its job was halted: nothing to protect
    if job in ("printing", "paused"):
        raise FixRefused(f"a job is {job}: restarting Klipper would end it")
    got = ctx.post(restart.path, restart.payload)
    if got.reply.status >= 400:
        detail = got.reply.body[:160].decode("utf-8", "replace")
        raise CannotSee(
            f"POST {restart.path}: HTTP {got.reply.status} {detail}", "error"
        )
    return FixChange(before, _wait_ready(ctx), f"POST {restart.path}")


def _klippy_steps(ctx: Context, result: CheckResult) -> list[str]:
    state = str(result.evidence.get("klippy_state"))
    message = result.evidence.get("state_message")
    restart = RESTART_FOR.get(state, RESTART_FOR["error"])
    url = shlex.quote(f"{ctx.base_url}{restart.path}")
    curl = f"curl -X POST {url}"
    if restart.payload is not None:
        body = shlex.quote(json.dumps(restart.payload))
        curl = f"curl -X POST -H 'Content-Type: application/json' -d {body} {url}"
    verb = (
        "FIRMWARE_RESTART" if restart.path.endswith("firmware_restart") else "RESTART"
    )
    return [
        f"Read why: Klipper says {message!r}"
        if message
        else "Read why: the web UI shows Klipper's message.",
        f"When no job runs, press {verb} in the web UI (for `disconnected`: Machine tab -> klipper -> restart), or:",
        command(curl),
        "If it comes back as `error`, the printer.cfg is wrong: fix the config named in the message, then RESTART.",
        "A Moonraker without a service provider cannot restart `klipper` itself: restart the service on the printer "
        "(e.g. `systemctl restart klipper`, or the firmware's own init script).",
    ]


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
            "paused, but print_stats carries no durations to measure the time without extrusion",
            "error",
        )
    idle = max(
        0.0, total - extruding
    )  # cumulative: every pause so far + the heating before it
    evidence["not_extruding_s"] = round(idle)
    if idle > PAUSED_WARN_S:
        return Verdict.fail(
            f"job is paused and has spent about {idle / 3600:.1f} h not extruding in total "
            "(all pauses and heating so far, not only this pause)",
            "warn",
            **evidence,
        )
    return Verdict.ok(
        f"job is paused, {idle / 60:.0f} min without extrusion in total (limit {PAUSED_WARN_S / 3600:.0f} h)",
        **evidence,
    )


def _stuck_steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        "Look at the printer: a paused job with a cooled nozzle and bed usually needs the cause "
        "fixed (filament runout, a jam) and RESUME, or CANCEL. The figure is cumulative: if the "
        "pauses so far were intended (colour changes), this one may be fine.",
        "Resume or cancel from the web UI, or:",
        command(f"3d printer cancel {shlex.quote(ctx.name)}"),
        "(3d refuses to touch a printer with access read-only.)",
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
        "Delete old G-code: web UI (Fluidd/Mainsail) G-code files list -> select -> delete "
        "(API: DELETE /server/files/gcodes/FILE).",
        "Timelapse videos and `.thumbs` also live in the same partition on most firmwares; clear those too.",
    ]


CHECKS = (
    Check(
        id="klippy-state",
        title="Klipper is ready (not shutdown / error / disconnected)",
        severity="error",
        applies_to="Moonraker-family printers",
        applies=lambda ctx: ctx.moonraker_only(),
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
        title="no paused job that has spent over 2 h not extruding in total",
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
