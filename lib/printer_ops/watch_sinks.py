"""watch_sinks.py — deliver `3d printer watch` events: stdout JSONL, exec hook, Telegram, omp session.

WHAT: `Sink` implementations and the `Dispatcher` that feeds them from one queue:
  stdout  one JSON object per event per line (always unthrottled);
  exec    `sh -c CMD` per event, event JSON on stdin, PRINTER_EVENT_* in the environment;
  tg      one phone-first message per batch through the `tg` CLI;
  steer   one message per batch into a live omp session through `tg-ctl omp-live send` —
          the same bridge `omp-steer` and mem-sentinel use: a busy session gets it as a
          steer, an idle one as an aside that starts a turn.
  The Dispatcher drops events below the printer's min severity, drops a repeated dedup key
  inside the dedup window, coalesces what arrives within `coalesce_s` into one batch (a noodle
  pause is exception + state + gcode lines at once: one message, not four) and rate-limits
  the chatty sinks (tg, steer); critical events are never rate-limited.

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
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from printer_ops.watch import CRITICAL, INFO, Event, severity_rank

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
    last_error: str | None = None


class Sink:
    name = "sink"
    rate: tuple[int, float] | None = None   # (max messages, per seconds); None = unlimited

    def __init__(self) -> None:
        self.stats = SinkStats()

    @property
    def ident(self) -> str:
        return self.name

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        """Send one batch. `suppressed` = events held back by this sink's rate limit since
        its last delivery (only rate-limited sinks get a non-zero value)."""
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
    name = "tg"
    rate = (6, 600.0)

    def __init__(self, *, runner: Runner = subprocess.run, binary: str = "tg") -> None:
        super().__init__()
        self.runner = runner
        self.binary = binary

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        text = batch_text(events, suppressed=suppressed, prefix="printer ")
        r = self.runner([self.binary, text], text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"tg exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:300]}")


class SteerSink(Sink):
    name = "steer"
    rate = (6, 600.0)

    def __init__(self, target: str, *, runner: Runner = subprocess.run, tgctl: str = "tg-ctl") -> None:
        super().__init__()
        self.target = target
        self.runner = runner
        self.tgctl = tgctl

    @property
    def ident(self) -> str:
        return f"steer:{self.target}"

    def sessions(self) -> list[dict[str, Any]]:
        r = self.runner([self.tgctl, "omp-live", "status", "--json"], text=True, capture_output=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError(f"tg-ctl omp-live status exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
        hosts = json.loads(r.stdout or "[]")
        return hosts if isinstance(hosts, list) else []

    def resolve(self, hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        t = self.target
        if t == "all":
            return hosts
        for match in (
            lambda h: str(h.get("pid")) == t,
            lambda h: str(h.get("sessionId", "")) == t,
            lambda h: str(h.get("sessionId", "")).startswith(t),
            lambda h: t.lower() in str(h.get("title") or "").lower(),
        ):
            sel = [h for h in hosts if match(h)]
            if sel:
                if len(sel) > 1:
                    names = "; ".join(f"{h.get('pid')} {h.get('title')!r}" for h in sel)
                    raise RuntimeError(f"steer target {t!r} matches {len(sel)} sessions ({names}); "
                                       "use the sessionId (prefix) or pid")
                return sel
        raise RuntimeError(f"no live omp session matches {t!r} (see: omp-steer list)")

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        printers = sorted({e.printer for e in events})
        text = batch_text(events, suppressed=suppressed, prefix="[3d printer watch] ")
        text += "\nDetails: " + "; ".join(f"3d printer status {p}" for p in printers)
        for h in self.resolve(self.sessions()):
            rid = f"3d-printer-watch-{int(time.time() * 1000)}-{h.get('pid')}"
            r = self.runner([self.tgctl, "omp-live", "send", str(h.get("pid")), "--request-id", rid,
                             "--session", str(h.get("sessionId")), text],
                            text=True, capture_output=True, timeout=30)
            out = (r.stdout or "").strip()
            if r.returncode != 0 or '"ok":false' in out.replace(" ", ""):
                raise RuntimeError(f"omp-live send to {h.get('pid')} failed: {(out or r.stderr or '').strip()[:300]}")


class DryRunSink(Sink):
    """Stands in for a side-effecting sink: prints what it would send to stderr."""

    def __init__(self, inner: Sink) -> None:
        super().__init__()
        self.inner = inner
        self.name = inner.name
        self.rate = inner.rate

    @property
    def ident(self) -> str:
        return self.inner.ident

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        _log(f"dry-run {self.inner.ident}: " + batch_text(events, suppressed=suppressed).replace("\n", " | "))


def sinks_for(spec: dict[str, Any], *, dry_run: bool = False) -> list[Sink]:
    """`watch_run.settings_for(...)["sinks"]` ({stdout: bool, exec: [CMD], tg: bool,
    steer: [TARGET]}) -> sinks, stdout first."""
    sinks: list[Sink] = [StdoutSink()] if spec.get("stdout", True) else []
    side: list[Sink] = [ExecSink(c) for c in spec.get("exec", [])]
    if spec.get("tg"):
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

    def add_settings(self, printer: str, settings: dict[str, Any], *, dry_run: bool = False) -> None:
        """Route one printer by its validated `watch_run.settings_for` result."""
        self.add(printer, sinks_for(settings["sinks"], dry_run=dry_run), settings["min_severity"],
                 settings["dedup_s"])

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

    def process(self, batch: list[Event]) -> None:
        now = self.clock()
        events = [e for e in batch if self.accept(e, now)]
        for e in events:
            self.last_event[e.printer] = e.to_json()
        if not events:
            return
        for route in self.routes.values():
            mine = [e for e in events if e.printer in route.printers]
            if mine:
                self._deliver(route, mine, now)

    def _deliver(self, route: _Route, events: list[Event], now: float) -> None:
        sink = route.sink
        if sink.rate is not None:
            limit, per = sink.rate
            while route.sent and now - route.sent[0] > per:
                route.sent.popleft()
            critical = any(e.severity == CRITICAL for e in events)
            if len(route.sent) >= limit and not critical:
                route.held += len(events)
                sink.stats.suppressed += len(events)
                return
            route.sent.append(now)
        try:
            sink.deliver(events, suppressed=route.held)
            route.held = 0
            sink.stats.delivered += len(events)
        except Exception as exc:  # a sink must never kill the watcher
            sink.stats.failed += len(events)
            sink.stats.last_error = f"{exc.__class__.__name__}: {exc}"
            _log(f"sink {sink.ident} failed: {sink.stats.last_error}")

    # ---- threaded front -----------------------------------------------------------------
    def submit(self, event: Event) -> None:
        self._q.put(event)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="watch-dispatch", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 30.0) -> None:
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

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


__all__ = ["Dispatcher", "DryRunSink", "ExecSink", "Sink", "SteerSink", "StdoutSink", "TgSink", "batch_text", "sinks_for"]
