"""watch_sinks.py — deliver `3d printer watch` events: stdout JSONL, exec hook, Telegram, omp session.

WHAT: `Sink` implementations and the `Dispatcher` that feeds them from one queue:
  stdout  one JSON object per event per line (always unthrottled);
  exec    `sh -c CMD` per event, event JSON on stdin, PRINTER_EVENT_* in the environment;
  tg      EMERGENCY (on unless `watch.tg_fallback: false`): ONE short message per incident (at most
          5 lines: headline, `+N related: kinds`, the pause outcome) through the `tg` CLI, with the
          frame, for events no agent could take — the only case where a printer alert goes straight
          to the user. A printer with a pause policy (auto_pause.py) is paused first for a
          high-confidence critical finding and the message says so. A per-printer cooldown
          (`watch.message_cooldown_minutes`, default 10) folds further alerts of the same or lower
          severity into one `+N more: kinds` line sent when it ends; the pause is never limited;
  steer   one message per batch into a live omp session through `tg-ctl omp-live send` —
          the same bridge `omp-steer` and mem-sentinel use: a busy session gets it as a
          steer, an idle one as an aside that starts a turn.
  subscriptions  agent sessions registered with `3d printer subscribe` (subscriptions.py);
  printer-agent  the temporary per-printer omp agent for events no live subscriber took
          (printer_agent.py). Both are added to every printer automatically.
  The Dispatcher drops events below the printer's min severity, drops a repeated dedup key
  inside the dedup window, coalesces what arrives within `coalesce_s` into one batch (a noodle
  pause is exception + state + gcode lines at once: one message, not four) and rate-limits
  the steer sink (critical events are never rate-limited); tg has its own cooldown limiter.

AGENT FIRST: camera/detector alerts are often false (a U1 "noodle" at p 0.83 on a healthy
  print reached the user), so the watchers message the user themselves only when nobody can
  judge: an agent decides. Sinks run in stages: FIRST (stdout, exec, steer, session
  subscriptions), then LAST_RESORT (the temporary printer agent: only events no agent took),
  then FALLBACK (tg, optional pause): only events no agent took at all — no live subscriber and
  the agent could not be started, or did not acknowledge in time.

DELIVERY IS ACKNOWLEDGED: `OmpLive.send` counts a message as delivered only with the live host's
  `accepted` receipt for that request id and session, and `OmpLive.sessions()` is asked afresh
  before every delivery (`tg-ctl omp-live status` drops a registry entry whose pid is gone and
  probes each socket), so a dead session is never sent to and its events fall straight through.

A SERVICE NEVER HOLDS ALERTS: `--dry-run` makes every sink print instead of send. When stderr is
  the journal (a systemd service) it is refused (`refuse_dry_run_service`): the PAUSED alert of the
  U1 on 2026-10-22 was swallowed by a unit that ran `watch --dry-run`, and nothing said so.

STEER TARGET: a pid, a sessionId (or its prefix), or a substring of the session title, resolved
  on EVERY delivery against `tg-ctl omp-live status --json` — the pid changes when a session
  is resumed, the sessionId does not. More than one match is refused (no broadcast by
  accident); `all` broadcasts on purpose.

INVARIANTS: stdlib only; a failing sink is logged to stderr and counted, never fatal; delivery
  happens on the dispatcher thread so a slow `tg` never stalls a printer stream.
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from printer_ops.watch import CRITICAL, INFO, WARNING, Event, severity_rank

if TYPE_CHECKING:
    from printer_ops.auto_pause import PauseResult
    from printer_ops.devices import Device

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _log(msg: str) -> None:
    print(f"watch: {msg}", file=sys.stderr, flush=True)


def run_real_tg(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """The default runner of TgSink, the user's phone: subprocess.run, except under pytest, where a
    sink built without an injected runner would message the real user (2026-10-23: every test run sent a fake CRITICAL alert)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("refusing to run the real tg from a test: inject runner=")
    return subprocess.run(*args, **kwargs)


_TOOL_DIRS = ("~/.local/bin", "/home/linuxbrew/.linuxbrew/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def find_tool(name: str) -> str | None:
    """PATH first, then the usual install dirs (a systemd user unit has a minimal PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for d in _TOOL_DIRS:
        p = Path(d).expanduser() / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def stderr_is_journal(environ: dict[str, str] | None = None, fd: int = 2) -> bool:
    """True when this process's stderr is a systemd journal stream, i.e. it runs as a service.
    systemd documents the check: `JOURNAL_STREAM=<dev>:<inode>` must match fstat of the descriptor.
    The bare variable is not enough: a terminal started by systemd (GNOME runs apps as units)
    passes it on to every child."""
    env = os.environ if environ is None else environ
    dev, _, ino = env.get("JOURNAL_STREAM", "").partition(":")
    if not (dev.isdigit() and ino.isdigit()):
        return False
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (int(dev), int(ino))


HELD_WARNING_EVERY_S = 3600.0   # a refused service restarts every 15-30 s: tell the user once an hour


def notify_held_service(printer: str, *, runner: Runner | None = None, now: float | None = None) -> bool:
    """Tell the user (`tg`, at most once per HELD_WARNING_EVERY_S, shared by every restart) that a service
    for `printer` refused to start with --dry-run, so its alerts are NOT being delivered. Best effort:
    the refusal itself never depends on it. -> whether a message was sent."""
    run = runner or run_real_tg   # under pytest the default raises: a test must never message the user
    from printer_ops.subscriptions import DeliveryState

    try:
        stamp = time.time() if now is None else now
        if not DeliveryState().claim_warning("held-service", stamp, HELD_WARNING_EVERY_S):
            return False
        r = run([find_tool("tg") or "tg", "--tag", "problem",
                    f"[{printer}] a printer watcher service refused to start: it was started with --dry-run, which "
                    "prints alerts instead of delivering them. Printer alerts are NOT being delivered. Remove "
                    "--dry-run from the unit (ExecStart or WATCH_ARGS), daemon-reload and restart it. This message "
                    "repeats hourly while the unit keeps restarting."],
                   text=True, capture_output=True, timeout=60)
    except Exception as exc:  # best effort: the refusal below is the guarantee
        _log(f"cannot warn the user about the held service: {exc.__class__.__name__}: {exc}")
        return False
    if r.returncode != 0:
        _log(f"tg refused the held-service warning: exit {r.returncode}")
    return r.returncode == 0


def refuse_dry_run_service(printer: str = "?", *, runner: Runner | None = None) -> None:
    """`--dry-run` in a service silently holds every alert (the 2026-10-22 U1 incident): refuse, and tell
    the user (hourly) that the service is not delivering."""
    if stderr_is_journal():
        from errors import UsageError

        notify_held_service(printer, runner=runner)
        raise UsageError(
            "`--dry-run` makes every alert print instead of being delivered, and this process is a service "
            "(its stderr is the journal): nothing would ever reach anyone", command="printer",
            remediation=["Remove --dry-run from the unit's ExecStart / WATCH_ARGS, then:",
                         "  systemctl --user daemon-reload && systemctl --user restart <the unit>",
                         "Dry runs belong in a terminal: `3d printer watch NAME --dry-run --duration 30`."])


@dataclass(frozen=True)
class Admission:
    """The outgoing limiter's verdict for one message of `printer`. `more`/`more_kinds` = alerts folded
    earlier that this message carries as its `+N more` line; `before` restores the state when the send
    fails (a failed send must not use up the cooldown)."""

    send: bool
    more: int = 0
    more_kinds: tuple[str, ...] = ()
    before: dict[str, Any] = field(default_factory=dict)


class Ledger(Protocol):
    """Where the chain counts its failures and keeps the outgoing limiter (subscriptions.DeliveryState)."""

    def bump(self, printer: str, name: str, n: int = 1) -> None: ...

    def admit(self, printer: str, severity: int, kinds: list[str], now: float, cooldown_s: float) -> Admission: ...

    def flush_folded(self, printer: str, now: float, cooldown_s: float) -> Admission: ...

    def restore_limiter(self, printer: str, before: dict[str, Any]) -> None: ...

    def claim_warning(self, key: str, now: float, every_s: float, *, fail_open: bool = False) -> bool: ...

    def release_warning(self, key: str) -> None: ...


class Pauser(Protocol):
    """auto_pause.AutoPauser: pauses the print for a qualifying critical event, once per job."""

    def pause(self, events: list[Event]) -> PauseResult | None: ...


@dataclass
class SinkStats:
    delivered: int = 0
    failed: int = 0
    suppressed: int = 0
    left_to_agent: int = 0   # fallback sinks: events skipped because an agent sink took them
    last_error: str | None = None


FIRST, LAST_RESORT, FALLBACK = 0, 1, 2


class Sink:
    """`on_unreached` is set by the Dispatcher: a sink that accepted events but learns later
    (the printer agent did not come up) that they reached no agent hands them back through it,
    and they go to the FALLBACK sinks."""

    name = "sink"
    rate: tuple[int, float] | None = None   # (max messages, per seconds); None = unlimited
    agent = False      # what this sink delivers reached an AI agent (steer, subscriptions)
    stage = FIRST      # FIRST | LAST_RESORT / FALLBACK: only the events no agent sink took

    def __init__(self) -> None:
        self.stats = SinkStats()
        self.on_unreached: Callable[[list[Event]], None] | None = None

    @property
    def ident(self) -> str:
        return self.name

    def close(self, timeout: float) -> None:
        """Finish background work at shutdown (the printer agent's pending readiness checks)."""

    def maintain(self) -> None:
        """Periodic housekeeping (`Dispatcher.maintain`): the subscription sink prunes dead entries."""

    def deliver(self, events: list[Event], suppressed: int = 0) -> list[Event] | None:
        """Send one batch. `suppressed` = events held back by this sink's rate limit since
        its last delivery (only rate-limited sinks get a non-zero value). Returns the events
        that reached their target when that is a subset; None = all of them."""
        raise NotImplementedError


class StdoutSink(Sink):
    name = "stdout"

    def __init__(self, stream: Any = None) -> None:
        super().__init__()
        self.stream = stream

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        out = self.stream or sys.stdout
        for e in events:
            out.write(json.dumps(e.to_json(), ensure_ascii=False, default=str) + "\n")
        out.flush()


class ExecSink(Sink):
    name = "exec"

    def __init__(self, command: str, *, runner: Runner = subprocess.run, timeout: float = 60.0) -> None:
        super().__init__()
        self.command = command
        self.runner = runner
        self.timeout = timeout

    @property
    def ident(self) -> str:
        return f"exec:{self.command}"

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        for e in events:
            env = {**os.environ, "PRINTER_EVENT_PRINTER": e.printer, "PRINTER_EVENT_KIND": e.kind,
                   "PRINTER_EVENT_SEVERITY": e.severity, "PRINTER_EVENT_TEXT": e.text}
            r = self.runner(["sh", "-c", self.command], input=json.dumps(e.to_json(), ensure_ascii=False, default=str),
                            text=True, capture_output=True, timeout=self.timeout, env=env)
            if r.returncode != 0:
                raise RuntimeError(f"exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:300]}")


def batch_text(events: list[Event], *, suppressed: int = 0, prefix: str = "") -> str:
    """Phone-first text: the most severe event first, one line each."""
    ordered = sorted(events, key=lambda e: -severity_rank(e.severity))
    top = ordered[0].severity.upper()
    lines = [f"{prefix}[{top}] {ordered[0].text}"] + [f"[{e.severity}] {e.text}" for e in ordered[1:]]
    if suppressed:
        lines.append(f"(+{suppressed} lower-severity event(s) held back by the rate limit)")
    return "\n".join(lines)


MAX_TG_FRAMES = 3


def tg_frames(events: list[Event]) -> list[str]:
    """The camera frames of `events` that exist on disk (most severe event first), at most MAX_TG_FRAMES."""
    from printer_ops.subscriptions import frames_of, ordered

    out: list[str] = []
    for e in ordered(events):
        for f in frames_of(e):
            if f not in out and Path(f).is_file():
                out.append(f)
    return out[:MAX_TG_FRAMES]


MAX_HEADLINE = 200
DEFAULT_COOLDOWN_S = 600.0   # `watch.message_cooldown_minutes`
STARTUP_TEST_EVERY_S = 3600.0   # a service that restarts in a loop sends its Telegram test message once an hour


def _first_line(text: str, limit: int = MAX_HEADLINE) -> str:
    line = (text.strip().splitlines() or [""])[0].strip()
    return line if len(line) <= limit else line[:limit - 1] + "…"


def short_text(printer: str, events: list[Event], pause_lines: list[str], more: Admission | None = None) -> str:
    """The whole phone message of one incident, at most 5 lines: `printer: <worst event, first line>`,
    `+N related: kind, kind` (the other events of the batch: kinds only), the pause outcome, and the
    `+N more` line of alerts folded by an earlier cooldown. Raw G-code dumps and event data never go in."""
    ranked = sorted(events, key=lambda e: -severity_rank(e.severity))
    head = _first_line(ranked[0].text)   # watch.py texts already start with `printer: `
    lines = [head if head.startswith(f"{printer}:") else f"{printer}: {head}"]
    others = ranked[1:]
    if others:
        lines.append(f"+{len(others)} related: {', '.join(dict.fromkeys(e.kind for e in others))}")
    lines += pause_lines[:2]
    if more is not None and more.more:
        lines.append(f"+{more.more} more: {', '.join(more.more_kinds)}")
    return "\n".join(lines)


def pause_lines(result: PauseResult) -> list[str]:
    """What the user needs to know about the automatic pause, in one line (nothing for a skipped one)."""
    if result.outcome == "paused":
        where = f" {result.webui}" if result.webui else ""
        return [f"Watcher PAUSED the print ({result.filename or 'current job'}). Resume: printer screen "
                f"or Fluidd/Mainsail{where}; it never resumes or cancels itself."]
    if result.outcome in ("failed", "pause_unverified"):
        return ["Watcher could NOT pause the print — check the printer."]
    return []


class TgSink(Sink):
    """The user's phone, as the emergency fallback: it only gets events that no agent took (no
    live subscriber and the printer agent could not start or did not answer in time), with the
    camera frame, as ONE short message per incident (`short_text`). A printer with a pause policy is
    paused FIRST for a qualifying critical finding (`attach(printer, pauser=...)`) and the message says
    what happened and how to resume; the pause is never limited, only messages are: the per-printer
    cooldown (DeliveryState.admit, shared by every watcher process) folds further alerts of the same or
    lower severity into one `+N more` line sent when it ends (`maintain`) or with the next message, and
    lets a higher severity through once. A refused `--tag`/`--photo` falls back to the plain text: the
    words always go out. The route is shared by every printer of the process, so printer-specific
    parts are attached."""

    name = "tg"
    stage = FALLBACK

    def __init__(self, *, runner: Runner = run_real_tg, binary: str = "tg",
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self.runner = runner
        self.binary = binary
        self.clock = clock
        self.ledger: Ledger | None = None
        self.pausers: dict[str, Pauser] = {}
        self.cooldowns: dict[str, float] = {}
        self.on_pause: Callable[[PauseResult], None] | None = None

    def attach(self, printer: str, *, pauser: Pauser | None = None, cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
        if pauser is not None:
            self.pausers[printer] = pauser
        self.cooldowns[printer] = cooldown_s

    def _count(self, printer: str, name: str, n: int = 1) -> None:
        if self.ledger is not None:
            self.ledger.bump(printer, name, n)

    def _paused(self, events: list[Event]) -> dict[str, list[str]]:
        """Pause where the printer's policy asks for it; -> printer -> the message lines about it."""
        out: dict[str, list[str]] = {}
        for printer in dict.fromkeys(e.printer for e in events):
            pauser = self.pausers.get(printer)
            if pauser is None:
                continue
            try:
                result = pauser.pause([e for e in events if e.printer == printer])
            except Exception as exc:  # the words must still reach the user
                _log(f"{printer}: automatic pause crashed: {exc.__class__.__name__}: {exc}")
                self._count(printer, "auto_pause_failed")
                out[printer] = ["The automatic pause crashed: the print was NOT paused — check the printer."]
                continue
            if result is None:
                continue
            self._count(printer, f"auto_pause_{result.outcome}")
            _log(f"{printer}: automatic pause {result.outcome}: {result.detail}")
            if self.on_pause is not None:
                self.on_pause(result)
            out[printer] = pause_lines(result)
        return out

    def startup_test(self, printers: list[str], *, role: str = "watch", now: float | None = None) -> bool | None:
        """Prove at start that the user can be reached: ONE `[TEST]` message through the same `tg` command line
        a real alert uses, its exit code asserted. A watcher whose tg silently refuses every message looks
        healthy and tells nobody (Chekhov 2026-10-23: `tg --tag answer` without `--reply-to`, the error buried
        in the log, a paused print sat for 6 h). A failure is logged as an ERROR and kept in the sink's stats
        (the heartbeat file shows `failed` / `last_error`); the watcher keeps running because its other sinks
        may still reach someone. `role` (watch, vision) is part of the once-per-STARTUP_TEST_EVERY_S throttle: a
        service has its own environment, so another service's success proves nothing about this one.
        -> True sent, False failed, None skipped (already sent within the hour: restarts in a loop must not spam).
        Never raises: a broken state file only turns the throttle off."""
        key = f"tg-startup-test:{role}:{','.join(sorted(printers))}"
        stamp = self.clock() if now is None else now
        try:
            if self.ledger is not None and not self.ledger.claim_warning(
                key, stamp, STARTUP_TEST_EVERY_S, fail_open=True
            ):
                return None
        except Exception as exc:  # an unreadable state file must not stop the test
            _log(f"startup test throttle unavailable: {exc.__class__.__name__}: {exc}")
        label = ", ".join(printers)
        try:
            self._send(f"[TEST] {label}: the printer watcher has started. This message checks that "
                                    "printer alerts reach you on Telegram; no action is needed.", [])
        except Exception as exc:
            self.stats.failed += 1
            self.stats.last_error = f"startup test: {exc.__class__.__name__}: {exc}"
            _log(f"ERROR: the Telegram test message FAILED ({exc}); alerts that only tg could deliver for "
                 f"{label} will NOT reach you until `tg` works")
            with contextlib.suppress(Exception):   # the next start tries again at once
                if self.ledger is not None:
                    self.ledger.release_warning(key)
            return False
        _log(f"Telegram test message for {label} delivered")
        return True

    def _send(self, text: str, frames: list[str]) -> None:
        rich = [self.binary, "--tag", "problem", *[x for f in frames for x in ("--photo", f)], text]
        r = self.runner(rich, text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            first = f"tg exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:300]}"
            r = self.runner([self.binary, text], text=True, capture_output=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"{first}; plain retry: tg exit {r.returncode}: "
                                   f"{(r.stderr or r.stdout or '').strip()[:300]}")
            _log(f"tg refused the tagged/photo message ({first}); sent the text alone")

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        paused = self._paused(events)   # before any limiting: the safety step is never held back
        now = self.clock()
        errors: list[Exception] = []
        for printer in dict.fromkeys(e.printer for e in events):
            mine = [e for e in events if e.printer == printer]
            cooldown = self.cooldowns.get(printer, DEFAULT_COOLDOWN_S)
            adm = Admission(True)
            if self.ledger is not None:
                adm = self.ledger.admit(printer, max(severity_rank(e.severity) for e in mine),
                                        [e.kind for e in mine], now, cooldown)
            if not adm.send:
                self._count(printer, "fallback_tg_folded", len(mine))
                _log(f"{printer}: tg message folded by the {cooldown / 60:g} min cooldown "
                     f"({', '.join(dict.fromkeys(e.kind for e in mine))})")
                continue
            try:
                self._send(short_text(printer, mine, paused.get(printer, []), adm), tg_frames(mine))
                self._count(printer, "fallback_tg_sent")
            except Exception as exc:  # the other printers of the batch still get theirs
                self._count(printer, "fallback_tg_failed")
                if self.ledger is not None:
                    self.ledger.restore_limiter(printer, adm.before)
                errors.append(exc)
        if errors:
            raise errors[0]

    def maintain(self) -> None:
        """Send the `+N more: kinds` line of every printer whose cooldown has ended."""
        if self.ledger is None:
            return
        now = self.clock()
        for printer, cooldown in list(self.cooldowns.items()):
            adm = self.ledger.flush_folded(printer, now, cooldown)
            if not adm.send:
                continue
            try:
                self._send(f"{printer}: +{adm.more} more: {', '.join(adm.more_kinds)}", [])
                self._count(printer, "fallback_tg_sent")
            except Exception as exc:
                self._count(printer, "fallback_tg_failed")
                self.ledger.restore_limiter(printer, adm.before)
                _log(f"{printer}: the folded-alerts summary was not sent: {exc}")


class OmpLive:
    """The `tg-ctl omp-live` bridge into live omp sessions (status + send), shared by the
    steer and subscription sinks."""

    STATUS_TIMEOUT_S = 10.0   # `status` probes every socket itself: more than this means a stuck bridge
    SEND_TIMEOUT_S = 30.0

    def __init__(self, *, runner: Runner = subprocess.run, tgctl: str = "tg-ctl") -> None:
        self.runner = runner
        self.tgctl = tgctl

    def sessions(self) -> list[dict[str, Any]]:
        """The live omp sessions NOW (never cached): `omp-live status` drops a registry entry whose
        pid is gone and probes each live socket, within STATUS_TIMEOUT_S."""
        try:
            r = self.runner([self.tgctl, "omp-live", "status", "--json"], text=True, capture_output=True,
                            timeout=self.STATUS_TIMEOUT_S)
        except FileNotFoundError as exc:
            raise RuntimeError(f"{self.tgctl} not found: cannot reach omp sessions") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"tg-ctl omp-live status did not answer within {self.STATUS_TIMEOUT_S:g} s") from exc
        if r.returncode != 0:
            raise RuntimeError(f"tg-ctl omp-live status exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
        try:
            hosts = json.loads(r.stdout or "[]")
        except ValueError as exc:
            raise RuntimeError(f"tg-ctl omp-live status printed no JSON: {(r.stdout or '').strip()[:120]!r}") from exc
        return [h for h in hosts if isinstance(h, dict)] if isinstance(hosts, list) else []

    @staticmethod
    def resolve(target: str, hosts: list[dict[str, Any]], *, titles: bool = True) -> list[dict[str, Any]]:
        """pid, sessionId, sessionId prefix, then (with `titles`) a unique title substring;
        `all` = every host. Subscriptions pass titles=False: a dead session id that merely
        appears in another session's title must not reach that session."""
        t = target
        if t == "all":
            return hosts
        matchers: list[Callable[[dict[str, Any]], bool]] = [
            lambda h: str(h.get("pid")) == t,
            lambda h: str(h.get("sessionId", "")) == t,
            lambda h: str(h.get("sessionId", "")).startswith(t),
        ]
        if titles:
            matchers.append(lambda h: t.lower() in str(h.get("title") or "").lower())
        for match in matchers:
            sel = [h for h in hosts if match(h)]
            if sel:
                if len(sel) > 1:
                    names = "; ".join(f"{h.get('pid')} {h.get('title')!r}" for h in sel)
                    raise RuntimeError(f"steer target {t!r} matches {len(sel)} sessions ({names}); "
                                       "use the sessionId (prefix) or pid")
                return sel
        raise RuntimeError(f"no live omp session matches {t!r} (see: omp-steer list)")

    def send(self, host: dict[str, Any], text: str) -> None:
        """Deliver `text` and require the host's receipt: `ok` + `accepted` for THIS request id and
        session. An exit 0 without it (an old bridge, a half answer) counts as NOT delivered."""
        rid = f"3d-printer-watch-{int(time.time() * 1000)}-{host.get('pid')}-{uuid.uuid4().hex[:8]}"
        sid = str(host.get("sessionId"))
        try:
            r = self.runner([self.tgctl, "omp-live", "send", str(host.get("pid")), "--request-id", rid,
                             "--session", sid, text],
                            text=True, capture_output=True, timeout=self.SEND_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"omp-live send to {host.get('pid')}: no acknowledgement within "
                               f"{self.SEND_TIMEOUT_S:g} s") from exc
        out = (r.stdout or "").strip()
        if r.returncode != 0 or '"ok":false' in out.replace(" ", ""):
            raise RuntimeError(f"omp-live send to {host.get('pid')} failed: {(out or r.stderr or '').strip()[:300]}")
        if not self._acknowledged(out, rid, sid):
            raise RuntimeError(f"omp-live send to {host.get('pid')} was not acknowledged (no `accepted` receipt for "
                               f"request {rid}): {out[:200] or '(empty answer)'}")

    @staticmethod
    def _acknowledged(out: str, rid: str, sid: str) -> bool:
        for line in reversed(out.splitlines()):
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict):
                return (reply.get("ok") is True and reply.get("accepted") is True
                        and reply.get("requestId") == rid and str(reply.get("sessionId")) == sid)
        return False


class SteerSink(Sink):
    name = "steer"
    rate = (6, 600.0)
    agent = True

    def __init__(self, target: str, *, runner: Runner = subprocess.run, tgctl: str = "tg-ctl") -> None:
        super().__init__()
        self.target = target
        self.live = OmpLive(runner=runner, tgctl=tgctl)

    @property
    def ident(self) -> str:
        return f"steer:{self.target}"

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        from printer_ops.subscriptions import alert_text  # the same evidence-aware text subscribers get

        footer = [f"(+{suppressed} lower-severity event(s) held back by the rate limit)"] if suppressed else []
        text = alert_text(events, f"[3d printer watch -> steer {self.target}]", footer)
        hosts = OmpLive.resolve(self.target, self.live.sessions())
        if not hosts:  # `all` with no session up: nothing reached an agent
            raise RuntimeError(f"steer target {self.target!r}: no live omp session")
        for h in hosts:
            self.live.send(h, text)


class DryRunSink(Sink):
    """Stands in for a side-effecting sink: prints what it would send to stderr."""

    def __init__(self, inner: Sink) -> None:
        super().__init__()
        self.inner = inner
        self.name = inner.name
        self.rate = inner.rate
        self.agent = inner.agent
        self.stage = inner.stage

    @property
    def ident(self) -> str:
        return self.inner.ident

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        _log(f"dry-run {self.inner.ident}: " + batch_text(events, suppressed=suppressed).replace("\n", " | "))


def sinks_for(spec: dict[str, Any], *, dry_run: bool = False) -> list[Sink]:
    """`watch_run.settings_for(...)["sinks"]` ({stdout: bool, exec: [CMD], tg_fallback: bool,
    steer: [TARGET]}) -> sinks, stdout first."""
    sinks: list[Sink] = [StdoutSink()] if spec.get("stdout", True) else []
    side: list[Sink] = [ExecSink(c) for c in spec.get("exec", [])]
    if spec.get("tg_fallback"):
        side.append(TgSink())
    side += [SteerSink(t) for t in spec.get("steer", [])]
    return sinks + ([DryRunSink(s) for s in side] if dry_run else side)


@dataclass
class _Route:
    sink: Sink
    printers: set[str] = field(default_factory=set)
    sent: deque[float] = field(default_factory=deque)
    held: int = 0   # events suppressed by the rate limit since the last delivery
    last_ok: bool = False   # the last delivery attempt reached its target


class Dispatcher:
    """One queue, many sinks. `submit` is thread-safe; `process` is the synchronous core."""

    def __init__(self, *, dedup_s: float = 600.0, coalesce_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.routes: dict[str, _Route] = {}
        self.min_severity: dict[str, str] = {}
        self.dedup_s: dict[str, float] = {}   # per printer; `default_dedup_s` otherwise
        self.default_dedup_s = dedup_s
        self.coalesce_s = coalesce_s
        self.clock = clock
        self._seen: dict[str, tuple[float, float]] = {}  # key -> (last accepted, its window)
        self._q: queue.Queue[Event | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._startup: threading.Thread | None = None
        self._lock = threading.RLock()   # process() vs late fallbacks from sink threads
        self.last_event: dict[str, dict[str, Any]] = {}

    def add(self, printer: str, sinks: Iterable[Sink], min_severity: str = INFO,
            dedup_s: float | None = None) -> None:
        severity_rank(min_severity)  # validates
        self.min_severity[printer] = min_severity
        if dedup_s is not None:
            self.dedup_s[printer] = dedup_s
        for s in sinks:
            route = self.routes.setdefault(s.ident, _Route(s))
            route.printers.add(printer)
            route.sink.on_unreached = self.fallback

    def fallback(self, events: list[Event]) -> None:
        """Late hand-back from a sink thread: deliver `events` to the FALLBACK sinks only."""
        with self._lock:
            now = self.clock()
            for route in self.routes.values():
                if route.sink.stage == FALLBACK:
                    mine = [e for e in events if e.printer in route.printers]
                    if mine:
                        self._deliver(route, mine, now)

    def add_settings(self, printer: str, settings: dict[str, Any], *, dry_run: bool = False) -> None:
        """Route one printer by its validated `watch_run.settings_for` result, plus the
        `3d printer subscribe` registry (every printer, so watch and vision reach subscribers
        without flags; the registry is re-read when it changes)."""
        from printer_ops.printer_agent import PrinterAgents, PrinterAgentSink, record_event
        from printer_ops.subscriptions import DeliveryState, SubscriptionSink

        if dry_run:
            refuse_dry_run_service(printer)
        state = DeliveryState()
        sinks = [*sinks_for(settings["sinks"], dry_run=dry_run), SubscriptionSink(dry_run=dry_run, state=state)]
        # Managed subscribers may be intentionally read-only. Never replace a missing
        # managed agent with a broader default agent when the operator disables fallback.
        if settings.get("agent_fallback", True):
            agents = PrinterAgents(state=state, ready_timeout_s=settings.get("agent_ack_s", 90.0))
            sinks.append(PrinterAgentSink(agents=agents, dry_run=dry_run))
        self.add(printer, sinks, settings["min_severity"], settings["dedup_s"])
        route = self.routes.get("tg")
        if route is not None and isinstance(route.sink, TgSink) and printer in route.printers:
            tg = route.sink
            tg.ledger = state
            tg.on_pause = lambda result: record_event(result.to_event())
            pauser = None
            if settings.get("pause_policy", "off") != "off":
                from printer_ops.auto_pause import AutoPauser

                pauser = AutoPauser(printer)
            tg.attach(printer, pauser=pauser, cooldown_s=settings.get("message_cooldown_s", DEFAULT_COOLDOWN_S))

    def startup_test(self, printers: list[str], role: str = "watch") -> dict[str, bool | None]:
        """Send each real tg route's `[TEST]` message (`TgSink.startup_test`); a dry-run route is not tg and sends none.
        -> route ident -> sent / failed / skipped."""
        out: dict[str, bool | None] = {}
        for ident, route in self.routes.items():
            if isinstance(route.sink, TgSink):
                mine = [p for p in printers if p in route.printers]
                if mine:
                    out[ident] = route.sink.startup_test(mine, role=role)
        return out

    def start_startup_test(self, printers: list[str], role: str = "watch") -> threading.Thread:
        """`startup_test` on its own thread: a hung `tg` (60 s per call) must not delay the watching. `close()` waits for it."""
        self._startup = threading.Thread(target=self.startup_test, args=(printers, role), name="watch-startup-test",
                                         daemon=True)
        self._startup.start()
        return self._startup

    @classmethod
    def for_devices(cls, devices: Iterable[Device], *, dry_run: bool = False,
                    opts: dict[str, Any] | None = None, **kw: Any) -> Dispatcher:
        """Dispatcher for these devices from their printer-devices.yaml `watch:` (+ parsed
        `printer watch` flags), validated exactly as `3d printer watch` validates them."""
        from printer_ops.watch_run import settings_for

        d = cls(**kw)
        for dev in devices:
            d.add_settings(dev.name, settings_for(dev, opts or {}), dry_run=dry_run)
        return d

    def maintain(self) -> None:
        """Housekeeping of the sinks that have some (the subscription registry prunes dead entries);
        called at start and every few minutes by the watcher. Never raises."""
        for route in list(self.routes.values()):
            self._maintain_one(route.sink)

    @staticmethod
    def _maintain_one(sink: Sink) -> None:
        try:
            sink.maintain()
        except Exception as exc:  # a sink must never stop the watcher
            _log(f"sink {sink.ident} maintenance failed: {exc.__class__.__name__}: {exc}")

    # ---- synchronous core ---------------------------------------------------------------
    def accept(self, e: Event, now: float) -> bool:
        floor = self.min_severity.get(e.printer, INFO)
        if severity_rank(e.severity) < severity_rank(floor):
            return False
        key = e.key or f"{e.printer}:{e.kind}:{e.text}"
        last = self._seen.get(key)
        if last is not None and now - last[0] < last[1]:
            return False
        self._seen[key] = (now, self.dedup_s.get(e.printer, self.default_dedup_s))
        if len(self._seen) > 4096:
            self._seen = {k: (t, w) for k, (t, w) in self._seen.items() if now - t < w}
        return True

    def process(self, batch: list[Event]) -> list[Event]:
        """Filter, then deliver stage by stage. -> the accepted events an agent took."""
        with self._lock:
            return self._process(batch)

    def _process(self, batch: list[Event]) -> list[Event]:
        now = self.clock()
        events = [e for e in batch if self.accept(e, now)]
        for e in events:
            self.last_event[e.printer] = e.to_json()
        taken: set[int] = set()
        for route in sorted(self.routes.values(), key=lambda r: r.sink.stage):
            mine = [e for e in events if e.printer in route.printers]
            if route.sink.stage != FIRST:
                left = [e for e in mine if id(e) not in taken]
                if route.sink.stage == FALLBACK:
                    route.sink.stats.left_to_agent += len(mine) - len(left)
                    # what no agent is meant to take (info) is no emergency
                    left = [e for e in left if severity_rank(e.severity) >= severity_rank(WARNING)]
                mine = left
            if not mine:
                continue
            got = self._deliver(route, mine, now)
            if route.sink.agent:
                taken.update(id(e) for e in got)
        return [e for e in events if id(e) in taken]

    def _deliver(self, route: _Route, events: list[Event], now: float) -> list[Event]:
        """-> the events that reached the sink's target ([] when held back or failed)."""
        sink = route.sink
        if sink.rate is not None:
            limit, per = sink.rate
            while route.sent and now - route.sent[0] > per:
                route.sent.popleft()
            critical = any(e.severity == CRITICAL for e in events)
            if len(route.sent) >= limit and not critical:
                route.held += len(events)
                sink.stats.suppressed += len(events)
                # a --steer agent that is only rate-limited and was reached last time still has
                # them (its next message counts them); a dead one does not
                return events if sink.agent and route.last_ok else []
        try:
            got = sink.deliver(events, suppressed=route.held)
        except Exception as exc:  # a sink must never kill the watcher
            route.last_ok = False
            sink.stats.failed += len(events)
            sink.stats.last_error = f"{exc.__class__.__name__}: {exc}"
            _log(f"sink {sink.ident} failed: {sink.stats.last_error}")
            return []
        route.held = 0
        if sink.rate is not None:
            route.sent.append(now)   # only real sends use the budget: a dead target never "rate-limits"
        route.last_ok = True
        reached = events if got is None else list(got)
        sink.stats.delivered += len(reached)
        return reached

    # ---- threaded front -----------------------------------------------------------------
    def submit(self, event: Event) -> None:
        self._q.put(event)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="watch-dispatch", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 30.0) -> None:
        """Drain the queue, then let sinks finish what they accepted (an agent still starting
        gets its alert or hands it to the fallback) — at most ~`timeout` s in total."""
        deadline = time.monotonic() + timeout
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout)
        if self._startup is not None:
            self._startup.join(max(0.0, deadline - time.monotonic()))
        for route in list(self.routes.values()):
            route.sink.close(max(0.0, deadline - time.monotonic()))

    def _run(self) -> None:
        while True:
            first = self._q.get()
            if first is None:
                return
            batch = [first]
            deadline = time.monotonic() + self.coalesce_s
            done = False
            while (left := deadline - time.monotonic()) > 0:
                try:
                    nxt = self._q.get(timeout=left)
                except queue.Empty:
                    break
                if nxt is None:
                    done = True
                    break
                batch.append(nxt)
            self.process(batch)
            if done:
                return

    def stats(self) -> dict[str, Any]:
        return {ident: {"printers": sorted(r.printers), **r.sink.stats.__dict__} for ident, r in self.routes.items()}


__all__ = ["DEFAULT_COOLDOWN_S", "FALLBACK", "FIRST", "LAST_RESORT", "Admission", "Dispatcher", "DryRunSink", "ExecSink",
           "Ledger", "OmpLive", "Pauser", "Sink", "SteerSink", "StdoutSink", "TgSink", "batch_text", "find_tool",
           "notify_held_service", "pause_lines", "refuse_dry_run_service", "short_text", "sinks_for",
           "stderr_is_journal", "tg_frames"]
