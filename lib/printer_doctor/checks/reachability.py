"""reachability.py — can the doctor talk to the printer, and does it agree on the time?

CHECKS: `reachable` (the adapter reads the printer's status), `time-sync` (the HTTP `Date` header
of the printer against this host's clock).
WHY time-sync: a printer with a wrong clock stamps its G-code metadata, job history and log lines
wrongly, which makes the watcher's "how long ago" and the maintenance counters lie, and TLS or
token expiry (if ever enabled) fail in confusing ways. The `Date` header is the only clock reading
every HTTP printer offers (Moonraker's Tornado sends one on every reply).
THRESHOLDS: |skew| above WARN_SKEW_S (120 s) warns, above ERROR_SKEW_S (1 h) is an error; the
header has 1 s resolution and the request takes milliseconds, so 120 s is far above noise.
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone

from printer_doctor.context import Context
from printer_doctor.model import CannotSee, Check, Fix, JsonValue, Verdict

WARN_SKEW_S = 120.0
ERROR_SKEW_S = 3600.0


def _reachable(ctx: Context) -> Verdict:
    device = ctx.device
    where: dict[str, JsonValue] = {
        "adapter": device.adapter,
        "host": device.host,
        "port": device.port,
    }
    try:
        status = ctx.adapter_status()
    except CannotSee as exc:
        return Verdict.fail(f"no answer: {exc.reason}", "error", **where)
    return Verdict.ok(
        f"{device.adapter} answers: state {status.state} ({status.raw_state})",
        state=status.state,
        raw_state=status.raw_state,
        **where,
    )


def _reachable_steps(ctx: Context, _result: object) -> list[str]:
    d = ctx.device
    return [
        f"Power: is the printer on and on the network? `ping {d.host}`",
        f"Address: host and port of {d.name} come from {d.source} "
        f"(`3d printer list` shows the effective entry): {d.adapter} at {d.host}:{d.port}.",
        "Services: on a Moonraker printer check that Moonraker runs (its web UI shows "
        "'Moonraker not connected' otherwise); a stock Creality firmware answers on its HTTP "
        "port and WebSocket 9999 only while the printer app runs.",
    ]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _time_path(ctx: Context) -> str:
    return "/server/info" if ctx.is_moonraker else "/"


def _time_sync(ctx: Context) -> Verdict:
    got = ctx.get(_time_path(ctx))
    header = got.reply.headers.get("date")
    if not header:
        return Verdict.skip(
            f"{ctx.target.adapter} sends no Date header: its clock cannot be read"
        )
    try:
        printer_ts = email.utils.parsedate_to_datetime(header).timestamp()
    except (TypeError, ValueError):
        raise CannotSee(f"unparseable Date header {header!r}", "error") from None
    skew = printer_ts - got.at
    evidence: dict[str, JsonValue] = {
        "printer_time": _iso(printer_ts),
        "local_time": _iso(got.at),
        "skew_s": round(skew, 1),
        "date_header": header,
    }
    if abs(skew) > ERROR_SKEW_S:
        return Verdict.fail(
            f"clock is {skew:+.0f} s off this host (over 1 h)", "error", **evidence
        )
    if abs(skew) > WARN_SKEW_S:
        return Verdict.fail(f"clock is {skew:+.0f} s off this host", "warn", **evidence)
    return Verdict.ok(f"clock within {abs(skew):.0f} s of this host", **evidence)


def _time_steps(ctx: Context, result: object) -> list[str]:
    return [
        "Check this host first: `timedatectl status` (System clock synchronized: yes).",
        "Then the printer's time source: enable NTP in its system settings (Moonraker: Machine "
        "tab; stock firmware: the touchscreen's time/zone settings), or set the clock by hand.",
        "A Linux-based printer: `date` over ssh, `ntpd`/`chronyd`/`systemd-timesyncd` status.",
        "Re-run `3d printers doctor " + ctx.name + " --check time-sync` to confirm.",
    ]


CHECKS = (
    Check(
        id="reachable",
        title="the printer answers a status read",
        severity="error",
        applies_to="every printer 3d may contact",
        run=_reachable,
        fix=Fix(
            "manual-only",
            "power / network / address (printer-devices.yaml) are not something 3d can repair",
            _reachable_steps,
        ),
    ),
    Check(
        id="time-sync",
        title="the printer's clock agrees with this host (HTTP Date header)",
        severity="error",
        applies_to="every printer 3d may contact whose HTTP replies carry a Date header",
        run=_time_sync,
        fix=Fix("manual-only", "enable NTP / set the printer's clock", _time_steps),
    ),
)
