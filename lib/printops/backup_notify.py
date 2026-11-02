"""backup_notify.py — what a FAILED (or recovered) backup run tells people.

ACCESSED VIA: `printops.backup_run.run_and_report` (so `3d printops backup run` and 3d-api's
`POST /v1/backup/run` both report exactly once).

THREE SIGNALS on failure, each independent (one failing never hides another):
  1. the notification payload is always printed (stderr) — what Home Assistant gets;
  2. a Home Assistant persistent notification `printops_backup_failed`, when PRINTOPS_HA_URL and a
     token are configured (the same variables 3d-api uses for humidity, printops.homeassistant);
     the HA package raises the same notification id when 3d-api itself is unreachable, so the two
     replace each other instead of piling up;
  3. a PrintOps anomaly `backup_failed` (scope `backup:<target>`, subject `backup:<target>`) in the PrintOps DB,
     listed by `3d printops anomalies` and resolved by the next good run.
A good run dismisses the notification and resolves the anomaly.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from printops import reconcile
from printops.db import open_db, transaction, utc_now
from printops.ha_rest import HaRest, HaRestError
from printops.homeassistant import HaError, HaStates

NOTIFICATION_ID = "printops_backup_failed"
ANOMALY_KIND = "backup_failed"
ANOMALY_SCOPE = "backup"  # one reconcile scope PER TARGET: `backup:<target>` (see `anomaly_scope`)


@dataclass(frozen=True)
class Context:
    target: str
    db_path: Path | None  # the PrintOps DB; None = no anomaly can be recorded
    env: Mapping[str, str]


def anomaly_scope(target: str) -> str:
    """reconcile.sync resolves every open anomaly of a scope that its findings do not repeat, so targets
    must not share one: target A's good run would otherwise close target B's still-open failure."""
    return f"{ANOMALY_SCOPE}:{target}"


def notification_payload(target: str, error: str) -> dict[str, Any]:
    return {
        "notification_id": NOTIFICATION_ID,
        "title": "PrintOps backup FAILED",
        "message": (
            f"The scheduled Spoolman/PrintOps backup (target `{target}`) failed:\n\n{error}\n\n"
            "Nothing was pruned. See `3d printops backup ls` and the PrintOps anomaly `backup_failed`."
        ),
    }


def _ha(env: Mapping[str, str]) -> HaRest | None:
    states = HaStates.from_env(env)
    return (
        None if states is None else HaRest(states.base_url, states.token, timeout=8.0)
    )


def _record_anomaly(ctx: Context, error: str | None) -> None:
    """Open (error) or resolve (None) the `backup_failed` anomaly of this target."""
    if ctx.db_path is None or not ctx.db_path.is_file():
        return
    finding = (
        []
        if error is None
        else [
            reconcile.Finding(
                kind=ANOMALY_KIND,
                scope=anomaly_scope(ctx.target),
                subject=f"backup:{ctx.target}",
                evidence={"target": ctx.target, "error": error[:500]},
            )
        ]
    )
    conn = open_db(ctx.db_path)
    try:
        with transaction(conn):
            reconcile.sync(conn, anomaly_scope(ctx.target), finding, live_ok=True, now=utc_now())
    finally:
        conn.close()


def _guard(label: str, fn: Callable[[], None], say: Callable[[str], None]) -> None:
    try:
        fn()
    except (HaRestError, HaError, OSError, ValueError) as e:
        say(f"{label}: {e}")
    except Exception as e:  # noqa: BLE001 — a failing signal must never hide the backup error or the others
        say(f"{label}: {type(e).__name__}: {e}")


def report_failure(
    ctx: Context, error: str, say: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Emit all three signals; returns the notification payload."""
    say = say or (lambda line: print(line, file=sys.stderr))
    payload = notification_payload(ctx.target, error)
    say("notification: " + json.dumps(payload, sort_keys=True))

    def post() -> None:
        ha = _ha(ctx.env)
        if ha is not None:
            ha.call_service("persistent_notification", "create", payload)
        else:
            say(
                "home assistant: PRINTOPS_HA_URL is not set; not posted (the HA package notifies on its own)"
            )

    _guard("home assistant notification", post, say)
    _guard("anomaly", lambda: _record_anomaly(ctx, error), say)
    return payload


def report_recovered(ctx: Context, say: Callable[[str], None] | None = None) -> None:
    """A good run: resolve the anomaly and dismiss the notification (best effort)."""
    say = say or (lambda line: print(line, file=sys.stderr))

    def dismiss() -> None:
        ha = _ha(ctx.env)
        if ha is not None:
            ha.call_service(
                "persistent_notification",
                "dismiss",
                {"notification_id": NOTIFICATION_ID},
            )

    _guard("home assistant dismiss", dismiss, say)
    _guard("anomaly", lambda: _record_anomaly(ctx, None), say)
