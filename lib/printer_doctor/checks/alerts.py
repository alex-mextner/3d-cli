"""alerts.py — does anybody actually receive this printer's alerts?

CHECK `alert-subscribers` (issue #526). A printer with a watcher (a camera, hence a vision unit, or
  `watch:` settings, hence a watch unit) delivers its events to the omp sessions subscribed with
  `3d printer subscribe`; a session that is not live counts as unsubscribed and its events fall
  through to the temporary printer agent (or nowhere). On 2026-10-23 three subscriptions of a
  session that had been dead for five days held 89 / 41 / 36 failed deliveries ("no live omp session
  matches") and `3d printer subscriptions` listed only live entries: every alert went nowhere.
  FAILS (error) when no subscription that covers the printer is both at warning level or lower and
  held by a live session. Entries judged by `printer_ops.subscriptions.healths` (the liveness and
  delivery-state logic of `3d printer subscriptions`): evidence per covering entry is its id, session,
  liveness, consecutive / total failed deliveries, last success and last error. When liveness cannot be
  read at all (`tg-ctl` missing) and no entry is known live, the check SKIPs: it cannot see.
  Applies to printers with a watcher (camera or `watch:` settings), never to offline ones.
FIX (`safe`, touches no printer): prune the entries that cover the printer, whose session is not live
  now AND whose last STALE_AFTER_FAILURES deliveries failed — the code path of
  `3d printer unsubscribe --id`, liveness re-read right before, so an entry of a live session is
  never removed and a session that is merely away (few failures: `omp -r` keeps the session id)
  stays. Idempotent: with nothing to prune the fix is refused with that reason. Verified when the stale
  entries are gone; the finding REMAINS ("no live subscriber") until somebody subscribes: that is
  manual-only (the exact `3d printer subscribe --session SESSION_ID --printer NAME` is printed).
"""

from __future__ import annotations

import shlex

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


def _applies(ctx: Context) -> str | None:
    from printer_ops.subscriptions import has_watcher

    device = ctx.target.device
    if device is None:
        return "no device (offline printer): nothing watches it"
    if not has_watcher(device):
        return "no watch or vision unit configured (no camera, no `watch:` settings)"
    return None


def _run(ctx: Context) -> Verdict:
    from printer_ops.subscriptions import STALE_AFTER_FAILURES

    covering = [h for h in ctx.subscription_health() if h.sub.covers(ctx.name)]
    evidence: dict[str, JsonValue] = {
        "entries": [h.to_dict() for h in covering],
        "prunable": [h.sub.id for h in covering if h.prunable],
        "stale_after_failures": STALE_AFTER_FAILURES,
    }
    live = [h for h in covering if h.takes_alerts_for(ctx.name)]
    if live:
        ids = ", ".join(h.sub.id for h in live)
        return Verdict.ok(
            f"{len(live)} live subscriber(s) at warning level: {ids}", **evidence
        )
    unknown = [h for h in covering if h.live is None]
    if unknown:
        raise CannotSee(
            f"cannot tell whether the subscribed sessions are live ({unknown[0].live_detail})"
        )
    if not covering:
        return Verdict.fail(
            "no subscription covers this printer: its alerts go to the temporary printer agent or nowhere",
            "error",
            **evidence,
        )
    stale = sum(h.stale for h in covering)
    return Verdict.fail(
        f"no live subscriber at warning level: {len(covering)} entr{'y' if len(covering) == 1 else 'ies'} "
        f"cover this printer, {stale} stale (session gone or deliveries failing)",
        "error",
        **evidence,
    )


def _prune(ctx: Context, result: CheckResult) -> FixChange:
    from printer_ops.subscriptions import STALE_AFTER_FAILURES

    health = ctx.subscription_health(
        fresh=True
    )  # act on what is true now, not on what the check saw
    doomed = {h.sub.id: h for h in health if h.sub.covers(ctx.name) and h.prunable}
    if not doomed:
        raise FixRefused(
            "nothing to prune: no entry covering this printer is both not live and past "
            f"{STALE_AFTER_FAILURES} failed deliveries in a row"
        )
    gone = ctx.subscription_registry().remove(lambda s: s.id in doomed)
    removed = sorted(s.id for s in gone)
    return FixChange(
        [
            f"{h.sub.id} session {h.sub.session} ({h.consecutive_failed} failed in a row)"
            for h in doomed.values()
        ],
        [],
        f"removed {', '.join(removed) or 'nothing (already gone)'} from the subscription registry",
    )


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    name = shlex.quote(ctx.name)
    prunable = result.evidence.get("prunable")
    steps = [
        command(f"3d printer unsubscribe --id {shlex.quote(str(sid))}")
        for sid in (prunable if isinstance(prunable, list) else [])
    ]
    steps += [
        "Subscribe a LIVE omp session to this printer (list the live sessions first):",
        command("omp-steer list"),
        f"then: 3d printer subscribe --session SESSION_ID --printer {name}",
        f"Check the routing: 3d printer subscriptions test {name}",
    ]
    return steps


def _stale_entries_gone(after: CheckResult) -> bool:
    return not after.evidence.get("prunable")


CHECKS = (
    Check(
        id="alert-subscribers",
        title="a live omp session is subscribed to the printer's alerts (warning level)",
        severity="error",
        applies_to="printers with a watcher (a camera, or `watch:` settings); reads the subscription registry and tg-ctl, not the printer",
        contacts_printer=False,
        applies=_applies,
        run=_run,
        fix=Fix(
            "safe",
            "remove the subscriptions of dead sessions whose deliveries keep failing "
            "(same as `3d printer unsubscribe --id`); subscribing a live session stays manual",
            _steps,
            _prune,
            touches_printer=False,
            goal=_stale_entries_gone,
        ),
    ),
)
