"""alert_fallback.py — when nobody is subscribed, does the last-resort temporary agent really work?

CHECK `alert-fallback-agent` (issue #526). Warning+ alerts that no live subscribed omp session takes go to
  the printer's TEMPORARY agent (printer_ops/printer_agent.py): an omp session started in tmux on the
  first such alert. On 2026-10-22 the agents of two printers sat `stopped, not in omp-live`, the last
  alert of one was from 2026-10-21 16:08, and the PAUSED alert of 2026-10-22 20:22:59 reached nothing
  although the watcher journal printed `[3d printer alert -> temporary <printer> agent]`: that header is
  only logged by the `--dry-run` branch of the agent sink, which reports the alert as delivered, starts
  nothing and holds the tg fallback back.
PASS  a live subscriber at warning level exists (the check is not needed: see `alert-subscribers`), or the
  agent can take the alerts: tmux and omp are installed, the last start did not fail, and it took the
  newest watcher alert.
FAIL  (error) no live subscriber AND the agent cannot be relied on:
  - `tmux` or `omp` is not installed (the same lookup the watcher uses: PATH, then ~/.local/bin, ...);
  - the agent's last start / delivery failed and no alert reached an agent since (state `last_error`,
    `consecutive_errors` in `printer-subscriptions.json`);
  - a watcher alert (warning+) is newer than the agent's last alert by more than GRACE_S, i.e. the agent
    did not get it; when a `--dry-run` watcher swallowed alerts after the agent's last one (state
    `dry_run_ts`, `dry_run_events`) that is named as the cause.
  Not failures: an agent that is not running (it starts on demand), the 10-minute restart limit.
SKIP  3d-api-independent, but the subscriptions cannot be judged (tg-ctl missing: nobody known live, nobody
  known dead); applies only to printers with a watcher (camera or `watch:` settings), like `alert-subscribers`.
WHERE the times come from: the agent's `last_ts` and its events.jsonl (printer_ops/printer_agent.py); the
  newest watcher alert from the watcher's own files: `<state>/watch/*.json` (heartbeat: the last event the
  dispatcher accepted per printer) and `<state>/vision/NAME.json` (the vision loop's last event). Only
  warning+ events count (the agent never gets info ones); each file keeps just the LAST event of a
  printer, so a newer info event can hide an older warning: the check then sees nothing to compare and
  does not fail on it.
FIX   manual-only (nothing 3d can start for you reliably): the exact `tmux -L SOCKET ...` commands to look
  at / clear the agent, `3d printer subscribe --session SESSION_ID --printer NAME`, and, for the dry-run
  case, the command that shows the unit's arguments.
"""

from __future__ import annotations

import datetime
import glob
import json
import shlex
from collections.abc import Mapping
from pathlib import Path

from printer_doctor.checks.alerts import watched_printer
from printer_doctor.context import Context
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    JsonValue,
    Verdict,
    command,
)

GRACE_S = 150.0  # an agent needs up to READY_TIMEOUT_S (90 s) to come up before it gets the alert


def _epoch(value: JsonValue) -> float | None:
    """Seconds since the epoch of an ISO time (state, heartbeat) or an epoch number (vision status)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def _warning_or_worse(severity: JsonValue) -> bool:
    return severity in ("warning", "critical")


def _read_json(path: Path) -> Mapping[str, JsonValue] | None:
    try:
        data: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def newest_watcher_alert(state_dir: Path, printer: str) -> dict[str, JsonValue] | None:
    """The newest warning+ event the watchers recorded for `printer`: heartbeat files
    (`watch/*.json`, `printers.<printer>.last_event`) and the vision status (`vision/<printer>.json`,
    `last_event`). None when no file names one."""
    found: list[tuple[float, dict[str, JsonValue]]] = []

    def consider(event: JsonValue, source: Path) -> None:
        if not isinstance(event, Mapping) or not _warning_or_worse(
            event.get("severity")
        ):
            return
        ts = _epoch(event.get("ts"))
        if ts is not None:
            found.append((ts, {"ts": _iso(ts), "kind": event.get("kind"), "severity": event.get("severity"),
                               "source": str(source)}))  # fmt: skip

    for path in sorted(glob.glob(str(state_dir / "watch" / "*.json"))):
        beat = _read_json(Path(path))
        printers = beat.get("printers") if beat else None
        row = printers.get(printer) if isinstance(printers, Mapping) else None
        if isinstance(row, Mapping):
            consider(row.get("last_event"), Path(path))
    vision = _read_json(state_dir / "vision" / f"{printer}.json")
    if vision:
        consider(vision.get("last_event"), state_dir / "vision" / f"{printer}.json")
    return max(found, key=lambda item: item[0])[1] if found else None


def _events_mtime(state_dir: Path, printer: str) -> float | None:
    try:
        return (state_dir / "printer-agents" / printer / "events.jsonl").stat().st_mtime
    except OSError:
        return None


def _run(ctx: Context) -> Verdict:
    from printer_ops.printer_agent import session_name, spawn_refusal, tmux_socket

    covering = [h for h in ctx.subscription_health() if h.sub.covers(ctx.name)]
    live = [h for h in covering if h.takes_alerts_for(ctx.name)]
    if live:
        return Verdict.ok(
            "a live subscriber takes the alerts, the temporary agent is not needed: "
            + ", ".join(h.sub.id for h in live),
            subscribers=[h.sub.id for h in live],
        )
    unknown = [h for h in covering if h.live is None]
    if unknown:
        raise CannotSee(
            f"cannot tell whether the subscribed sessions are live ({unknown[0].live_detail})"
        )

    tmux, omp = ctx.tool("tmux"), ctx.tool("omp")
    agents = ctx.printer_agents()
    row = agents.state.agent(ctx.name)
    state_dir = ctx.state_dir()
    refused = ctx.services.agents.runner is None and spawn_refusal()
    running: bool | None = (
        None if refused else (agents.running(ctx.name) if tmux else False)
    )
    last_ok = _epoch(row.get("last_ts"))
    last_bad = _epoch(row.get("last_error_ts"))
    dry_ts = _epoch(row.get("dry_run_ts"))
    watcher = newest_watcher_alert(state_dir, ctx.name)
    watcher_ts = _epoch(watcher["ts"]) if watcher else None
    errors = row.get("consecutive_errors")
    problems: list[str] = []
    if not tmux:
        problems.append(
            "tmux is not installed: the temporary agent runs in a tmux session"
        )
    if not omp:
        problems.append("omp is not installed: the temporary agent is an omp session")
    unresolved = (
        row.get("last_error")
        and last_bad is not None
        and (last_ok is None or last_bad > last_ok)
    )
    if unresolved:
        streak = (
            f" ({errors} in a row)" if isinstance(errors, int) and errors > 1 else ""
        )
        problems.append(
            f"the last start / delivery failed and no alert reached an agent since{streak}: {row.get('last_error')}"
        )
    lag: float | None = None
    if watcher_ts is not None and (last_ok is None or watcher_ts - last_ok > GRACE_S):
        lag = watcher_ts - (last_ok if last_ok is not None else 0.0)
        since = "ever" if last_ok is None else f"since {_iso(last_ok)}"
        swallowed = (
            dry_ts is not None
            and dry_ts >= watcher_ts - GRACE_S
            and (last_ok is None or dry_ts > last_ok)
        )
        if swallowed:
            problems.append(
                f"a watcher running with --dry-run swallowed {row.get('dry_run_events', '?')} alert(s) (last "
                f"{row.get('dry_run_ts')}): it prints them and starts no agent; the watcher's newest warning+ alert "
                f"({watcher.get('kind') if watcher else None}) is {_iso(watcher_ts)}, the agent took none {since}"
            )
        else:
            problems.append(
                f"the watcher's newest warning+ alert ({watcher.get('kind') if watcher else None}, {_iso(watcher_ts)}) "
                f"never reached the agent: its last alert was {since if last_ok is None else _iso(last_ok)}"
            )
    evidence: dict[str, JsonValue] = {
        "tmux": tmux,
        "omp": omp,
        "tmux_socket": tmux_socket(),
        "session": session_name(ctx.name),
        "agent": {
            "running": running,
            "started": row.get("started"),
            "stopped": row.get("stopped"),
            "last_alert": _iso(last_ok),
            "last_kinds": row.get("last_kinds"),
            "last_error": row.get("last_error"),
            "last_error_ts": row.get("last_error_ts"),
            "consecutive_errors": errors,
            "start_claimed": _iso(_epoch(row.get("start_claimed"))),
            "dry_run_ts": row.get("dry_run_ts"),
            "dry_run_events": row.get("dry_run_events"),
            "events_jsonl_mtime": _iso(_events_mtime(state_dir, ctx.name)),
        },
        "watcher_newest_alert": watcher,
        "agent_lag_s": None if lag is None else round(lag),
        "problems": problems,
    }
    if problems:
        return Verdict.fail(
            "no live subscriber and the temporary agent cannot be relied on: "
            + "; ".join(problems),
            "error",
            **evidence,
        )
    state = (
        "running"
        if running
        else "startable on the next alert"
        if running is False
        else "not probed"
    )
    seen = f", last alert {_iso(last_ok)}" if last_ok else ", no alert yet"
    return Verdict.ok(
        f"no live subscriber, but the temporary agent can take the alerts (tmux and omp found; {state}{seen})",
        **evidence,
    )


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    from printer_ops.printer_agent import attach_hint, session_name, tmux_socket

    name, sock = shlex.quote(ctx.name), shlex.quote(tmux_socket())
    steps: list[str] = []
    if not ctx.tool("tmux"):
        steps.append(
            "Install tmux with your package manager: the temporary agent runs in a tmux session."
        )
    if not ctx.tool("omp"):
        steps.append(
            "Install omp, or put it where the watcher finds it: its systemd user unit has a minimal PATH, so "
            "~/.local/bin, /usr/local/bin and /usr/bin are searched too."
        )
    agent = result.evidence.get("agent")
    dry = isinstance(agent, Mapping) and agent.get("dry_run_ts")
    if dry:
        steps += [
            "A watcher runs with --dry-run: see the unit's arguments, then remove the flag and restart it:",
            command(
                f"systemctl --user cat 3d-printer-watch@{name}.service 3d-printer-vision@{name}.service"
            ),
            command(f"3d printer vision doctor {name}"),
        ]
    steps += [
        "The agent's tmux server (is the session there? did omp exit at once?):",
        command(f"tmux -L {sock} ls"),
        command(attach_hint(ctx.name)),
        "A stuck or crashed agent: clear it and its restart-limit slot, the next alert starts a fresh one:",
        command(
            f"tmux -L {sock} kill-session -t ={shlex.quote(session_name(ctx.name))}"
        ),
        command(f"3d printer agent stop {name}"),
        command("3d printer agent list"),
        "Or take the pressure off it: subscribe a LIVE omp session (list the live sessions first):",
        command("omp-steer list"),
        f"then: 3d printer subscribe --session SESSION_ID --printer {name}",
    ]
    return steps


CHECKS = (
    Check(
        id="alert-fallback-agent",
        title="with no live subscriber, the temporary printer agent can take the alerts",
        severity="error",
        applies_to="printers with a watcher (a camera, or `watch:` settings); reads tmux availability, the agent state and the watcher's state files, not the printer",
        contacts_printer=False,
        applies=watched_printer,
        run=_run,
        fix=Fix(
            "manual-only",
            "install tmux/omp, remove --dry-run from the watcher unit, clear a stuck agent, or subscribe a live session",
            _steps,
        ),
    ),
)

__all__ = ["CHECKS", "GRACE_S", "newest_watcher_alert"]
