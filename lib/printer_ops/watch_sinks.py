"""watch_sinks.py — deliver `3d printer watch` events: stdout JSONL, exec hook, Telegram, omp session.

WHAT: `Sink` implementations and the `Dispatcher` that feeds them from one queue:
  stdout  one JSON object per event per line (always unthrottled);
  exec    `sh -c CMD` per event, event JSON on stdin, PRINTER_EVENT_* in the environment;
  tg      EMERGENCY only (`watch.tg_fallback: true`): one phone message per batch through the
          `tg` CLI, marked «агент недоступен», for events no agent could take;
  steer   one message per batch into a live omp session through `tg-ctl omp-live send` —
          the same bridge `omp-steer` and mem-sentinel use: a busy session gets it as a
          steer, an idle one as an aside that starts a turn.
  subscriptions  agent sessions registered with `3d printer subscribe` (subscriptions.py);
  printer-agent  the temporary per-printer omp agent for events no live subscriber took
          (printer_agent.py). Both are added to every printer automatically.
  The Dispatcher drops events below the printer's min severity, drops a repeated dedup key
  inside the dedup window, coalesces what arrives within `coalesce_s` into one batch (a noodle
  pause is exception + state + gcode lines at once: one message, not four) and rate-limits
  the chatty sinks (tg, steer); critical events are never rate-limited.

AGENT FIRST: camera/detector alerts are often false (a U1 "noodle" at p 0.83 on a healthy
  print reached the user), so the watchers never message the user themselves: an agent
  decides. Sinks run in stages: FIRST (stdout, exec, steer, session subscriptions), then
  LAST_RESORT (the temporary printer agent: only events no agent took), then FALLBACK (tg):
  only events no agent took at all — the agent could not be started.

STEER TARGET: a pid, a sessionId (or its prefix), or a substring of the session title, resolved
  on EVERY delivery against `tg-ctl omp-live status --json` — the pid changes when a session
  is resumed, the sessionId does not. More than one match is refused (no broadcast by
  accident); `all` broadcasts on purpose.

INVARIANTS: stdlib only; a failing sink is logged to stderr and counted, never fatal; delivery
  happens on the dispatcher thread so a slow `tg` never stalls a printer stream.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from printer_ops.watch import CRITICAL, INFO, WARNING, Event, severity_rank

if TYPE_CHECKING:
    from printer_ops.devices import Device

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _log(msg: str) -> None:
    print(f"watch: {msg}", file=sys.stderr, flush=True)


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


class TgSink(Sink):
    """The user's phone, as the emergency fallback: it only gets events that no agent took
    (the printer agent could not start), marked as such."""

    name = "tg"
    rate = (6, 600.0)
    stage = FALLBACK

    def __init__(self, *, runner: Runner = subprocess.run, binary: str = "tg") -> None:
        super().__init__()
        self.runner = runner
        self.binary = binary

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        text = batch_text(events, suppressed=suppressed, prefix="[агент недоступен] printer ")
        r = self.runner([self.binary, text], text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"tg exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:300]}")


class OmpLive:
    """The `tg-ctl omp-live` bridge into live omp sessions (status + send), shared by the
    steer and subscription sinks."""

    def __init__(self, *, runner: Runner = subprocess.run, tgctl: str = "tg-ctl") -> None:
        self.runner = runner
        self.tgctl = tgctl

    def sessions(self) -> list[dict[str, Any]]:
        try:
            r = self.runner([self.tgctl, "omp-live", "status", "--json"], text=True, capture_output=True, timeout=20)
        except FileNotFoundError as exc:
            raise RuntimeError(f"{self.tgctl} not found: cannot reach omp sessions") from exc
        if r.returncode != 0:
            raise RuntimeError(f"tg-ctl omp-live status exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
        hosts = json.loads(r.stdout or "[]")
        return hosts if isinstance(hosts, list) else []

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
        rid = f"3d-printer-watch-{int(time.time() * 1000)}-{host.get('pid')}-{uuid.uuid4().hex[:8]}"
        r = self.runner([self.tgctl, "omp-live", "send", str(host.get("pid")), "--request-id", rid,
                         "--session", str(host.get("sessionId")), text],
                        text=True, capture_output=True, timeout=30)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or '"ok":false' in out.replace(" ", ""):
            raise RuntimeError(f"omp-live send to {host.get('pid')} failed: {(out or r.stderr or '').strip()[:300]}")


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
        from printer_ops.printer_agent import PrinterAgentSink
        from printer_ops.subscriptions import SubscriptionSink

        self.add(printer, [*sinks_for(settings["sinks"], dry_run=dry_run), SubscriptionSink(dry_run=dry_run),
                           PrinterAgentSink(dry_run=dry_run)], settings["min_severity"], settings["dedup_s"])

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
                if route.sink.stage == FALLBACK:  # what no agent is meant to take (info) is no emergency
                    left = [e for e in left if severity_rank(e.severity) >= severity_rank(WARNING)]
                if route.sink.stage == FALLBACK:
                    route.sink.stats.left_to_agent += len(mine) - len(left)
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
                # a live --steer agent that is only rate-limited still has them (its next message
                # counts them): no temporary agent for a printer whose agent is fine
                return events if sink.agent else []
            route.sent.append(now)
        try:
            got = sink.deliver(events, suppressed=route.held)
        except Exception as exc:  # a sink must never kill the watcher
            sink.stats.failed += len(events)
            sink.stats.last_error = f"{exc.__class__.__name__}: {exc}"
            _log(f"sink {sink.ident} failed: {sink.stats.last_error}")
            return []
        route.held = 0
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


__all__ = ["FALLBACK", "FIRST", "LAST_RESORT", "Dispatcher", "DryRunSink", "ExecSink", "OmpLive", "Sink", "SteerSink", "StdoutSink", "TgSink",
           "batch_text", "sinks_for"]
