"""ondemand.py — `3d printer ondemand NAME`: the tiny always-on supervisor that runs the watch and vision
workers of one printer only while a print runs (#747).

WHAT: one process per printer (systemd user unit `3d-printer-ondemand@NAME`, shipped by supervise_units.py).
  Every `poll` seconds (default 30) it asks the printer ONE small question and decides "a print is active"
  (printing or paused):
    moonraker       GET /printer/objects/query?print_stats&webhooks  (331 bytes) over a raw socket
    creality-stock  one short read of the printer's ws:9999 state push
  A print starts      -> `systemctl --user start 3d-printer-watch@NAME [3d-printer-vision@NAME]`
  A print ends        -> the workers keep running for `grace` seconds (default 300: the end-of-print events,
                         the bed check, a print that starts again), then `systemctl --user stop` removes them
                         COMPLETELY: no models in memory, no sockets, no timers, no heartbeat files.
  The workers are the unchanged `3d printer watch` / `3d printer vision` services: this module never reads a
  frame, never sends anything to the printer but that one GET, and never pauses or cancels.

WHY (#747): the always-on workers held 1.7 GiB (two ONNX vision processes) and 4.6 % of a core while nothing
  printed. The requirement of #640/#641 stays: a print started from the touchscreen, Fluidd, a slicer or Home
  Assistant is supervised, here within one poll interval (the first layer is minutes after the start: heating).

WHY POLLING and not a websocket subscription: Moonraker pushes `notify_proc_stat_update` to every websocket
  client every second, so a subscription costs a 1 Hz wake-up and a permanently open connection on the
  printers' small boards, against one 331-byte request per 30 s. systemd path or socket activation cannot
  observe a remote printer.

THE SUPERVISOR IS TINY ON PURPOSE: stdlib only, no `printer_ops` import on the hot path except the unit-name
  helper (no yaml, no ssl, no http.client: ~13 MB RSS). The printer's address comes from a one-shot child
  (`3d printer ondemand NAME --resolve`, the heavy config loader) that exits at once; it runs at start, on
  SIGHUP and when a config file's mtime changes.

NO DISK WHILE IDLE: no heartbeat or state file. The supervisor reports to systemd with sd_notify
  (READY, WATCHDOG, STATUS: `systemctl --user show -p StatusText`), which is memory only; it logs one line
  per transition (never per poll), so an idle printer writes nothing to the journal either.

RESTART SAFETY: stopping or restarting the supervisor never touches the workers (they are separate units).
  At start it reconciles: a print already running starts the workers; an idle printer whose workers are still
  running (a restart inside the grace period, or the workers were started by hand) gets one fresh grace
  period. A printer that does not answer is retried with backoff (poll, 2x, 4x, ... up to 2 min) and never
  starts workers; while workers run, an unreachable printer counts like an idle one (grace applies).

INVARIANTS: stdlib only at import time; every systemctl call goes through `Workers.run` (tests inject it);
  the module never writes a file.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printer_ops.supervise_units import RUNNING, Runner, real_runner, unit_name

DEFAULT_POLL_S = 30.0
DEFAULT_GRACE_S = 300.0
ENV_POLL, ENV_GRACE = (
    "THREED_ONDEMAND_POLL_S",
    "THREED_ONDEMAND_GRACE_S",
)  # a systemd drop-in sets these
MIN_POLL_S = 5.0
BACKOFF_MAX_S = 120.0  # a switched-off printer: a heated start takes minutes, so 2 min is still early
PROBE_TIMEOUT_S = 8.0  # connect + reply, one overall deadline
CREALITY_LISTEN_S = (
    3.0  # how long the K1 stock firmware gets to push its state after connect
)
MAX_REPLY = (
    64 * 1024
)  # the query answer is ~330 bytes; anything bigger is not Moonraker
RESOLVE_TIMEOUT_S = 120.0
RESOLVE_RETRY_MAX_S = 300.0
# While idle, look for workers somebody started (by hand, a mount-retry timer) every this many polls: one
# `systemctl is-active` per 10 minutes at the default poll, so an idle printer still costs ~nothing.
STRAY_CHECK_EVERY_POLLS = 20
ACTIVE_PRINT_STATES = frozenset({"printing", "paused"})  # Klipper print_stats.state
CREALITY_ACTIVE = frozenset(
    {1, 5, 7}
)  # creality.parse_status: printing, paused, a cancel still finishing

IDLE, ACTIVE, GRACE = "idle", "active", "grace"
START, STOP = "start", "stop"
WORKER_KINDS = ("watch", "vision")

USAGE = """3d printer ondemand NAME [--poll S] [--grace S] [--once]

The always-on part of the printer supervision (#747): a tiny process (about 13 MB, no disk) that asks
the printer ONE small question every --poll seconds (Moonraker: GET /printer/objects/query?print_stats&
webhooks; creality-stock: one ws:9999 read) and runs `systemctl --user start|stop` on the heavy workers
`3d-printer-watch@NAME` and `3d-printer-vision@NAME` only while a print runs.

  - a print starts (printing or paused, started from anywhere)   -> both workers are started
  - the print ends                                               -> they keep running --grace seconds, then
                                                                    are stopped COMPLETELY (no models, no files)
  - the printer does not answer                                  -> retried with backoff (poll, 2x, 4x .. 2 min);
                                                                    workers are never started for it
  - the supervisor restarts                                      -> the workers are left alone; a running print
                                                                    starts them, an idle printer with running
                                                                    workers gets one fresh grace period

Options:
  --poll S     seconds between questions (default 30, at least 5)
  --grace S    seconds the workers keep running after the print ended (default 300; 0 = stop at once)
  --once       ask now, print one JSON line (what the printer says, which workers run, what would happen) and
               exit: 0 when the printer answered, 1 when not. Changes nothing.
  --resolve    print the resolved configuration as JSON and exit (what the supervisor's helper child runs)

Timing without flags (a systemd drop-in is the place: `systemctl --user edit 3d-printer-ondemand@NAME`):
  [Service]
  Environment=THREED_ONDEMAND_GRACE_S=900 THREED_ONDEMAND_POLL_S=60      # both optional; the flags win

The units are installed by `3d printer supervise install`; `systemctl --user show -p StatusText
3d-printer-ondemand@NAME` says what the supervisor sees right now.

Examples:
  3d printer ondemand u1 --once
  3d printer ondemand k1se --grace 900"""


class Unreachable(Exception):
    """The printer gave no usable answer (down, timeout, not Moonraker, an error status)."""


# ---- the question ------------------------------------------------------------------------------


def _dechunk(body: bytes) -> bytes:
    """Body of a `Transfer-Encoding: chunked` reply (a proxy in front of Moonraker may chunk it)."""
    out = bytearray()
    while body:
        size_line, sep, rest = body.partition(b"\r\n")
        try:
            size = int(size_line.split(b";", 1)[0], 16)
        except ValueError as exc:
            raise Unreachable("malformed chunked reply") from exc
        if not sep or size == 0:
            break
        out += rest[:size]
        body = rest[size + 2 :]
    return bytes(out)


def http_get(
    host: str, port: int, path: str, timeout: float = PROBE_TIMEOUT_S
) -> tuple[int, bytes]:
    """(status, body) of one plain HTTP GET on a raw socket. HTTP/1.0 + `Connection: close`: the server
    answers without chunking and hangs up, so the reply ends at EOF. One overall deadline."""
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise Unreachable(f"{host}:{port}: {exc.strerror or exc}") from exc
    buf = bytearray()
    with sock:
        try:
            sock.sendall(
                f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\nAccept: application/json\r\n"
                "User-Agent: 3d-printer-ondemand\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
            )
            while len(buf) <= MAX_REPLY:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Unreachable(
                        f"{host}:{port}: no complete answer in {timeout:g} s"
                    )
                sock.settimeout(left)
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
        except OSError as exc:
            raise Unreachable(f"{host}:{port}: {exc.strerror or exc}") from exc
    head, sep, body = bytes(buf).partition(b"\r\n\r\n")
    parts = head.split(b"\r\n", 1)[0].split()
    if not sep or len(parts) < 2 or not parts[1].isdigit():
        raise Unreachable(f"{host}:{port}: not an HTTP answer")
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return int(parts[1]), body


def moonraker_active(status: Mapping[str, Any]) -> bool:
    """Is a print running in a Moonraker `objects/query` status? Klipper not ready means no print can run
    (moonraker.parse_status maps it to `unknown`, which is neither printing nor paused)."""
    if (status.get("webhooks") or {}).get("state", "ready") != "ready":
        return False
    return (status.get("print_stats") or {}).get("state") in ACTIVE_PRINT_STATES


def probe_moonraker(target: Target, timeout: float = PROBE_TIMEOUT_S) -> bool:
    status_code, body = http_get(
        target.host, target.port, "/printer/objects/query?print_stats&webhooks", timeout
    )
    if status_code == 503:  # Moonraker is up, Klipper is not: nothing can be printing
        return False
    if status_code != 200:
        raise Unreachable(f"{target.host}:{target.port}: HTTP {status_code}")
    try:
        status = json.loads(body)["result"]["status"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unreachable(
            f"{target.host}:{target.port}: no status in the answer"
        ) from exc
    if not isinstance(status, dict):
        raise Unreachable(f"{target.host}:{target.port}: no status in the answer")
    return moonraker_active(status)


def creality_active(pushed: Mapping[str, Any]) -> bool | None:
    """Is a print running in the merged state push of a K1 stock firmware (creality.parse_status codes)?
    None: the push carried no `state` at all, which says nothing."""
    try:
        return int(pushed["state"]) in CREALITY_ACTIVE
    except (KeyError, TypeError, ValueError):
        return None


def probe_creality(target: Target, timeout: float = PROBE_TIMEOUT_S) -> bool:
    from printer_ops.ws import (
        WebSocket,
        WebSocketClosed,
        WebSocketError,
    )  # lazy: ssl-free but not free

    pushed: dict[str, Any] = {}
    try:
        with WebSocket.connect(
            target.host, target.ws_port or 9999, timeout=timeout
        ) as ws:
            deadline = time.monotonic() + CREALITY_LISTEN_S
            while (left := deadline - time.monotonic()) > 0 and "state" not in pushed:
                message = ws.recv(left)
                if message is None:
                    break
                if "heart_beat" in message:
                    ws.send_text(
                        "ok"
                    )  # the firmware drops a client that does not answer its heartbeat
                    continue
                try:
                    obj = json.loads(message)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    pushed.update(obj)
    except WebSocketClosed:
        pass
    except (WebSocketError, OSError) as exc:
        raise Unreachable(f"{target.host}:{target.ws_port or 9999}: {exc}") from exc
    verdict = creality_active(pushed)
    if verdict is None:
        raise Unreachable(
            f"{target.host}: the printer pushed no state within {CREALITY_LISTEN_S:g} s"
        )
    return verdict


PROBES: dict[str, Callable[[Target], bool]] = {
    "moonraker": probe_moonraker,
    "creality-stock": probe_creality,
}


# ---- what to supervise -------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One printer as the supervisor needs it: where to ask, which workers to run, the two timings."""

    name: str
    adapter: str
    host: str
    port: int
    ws_port: int | None = None
    workers: tuple[str, ...] = WORKER_KINDS
    poll_s: float = DEFAULT_POLL_S
    grace_s: float = DEFAULT_GRACE_S
    files: tuple[
        str, ...
    ] = ()  # config files the target came from: a changed mtime means re-resolve

    def units(self) -> tuple[str, ...]:
        return tuple(unit_name(kind, self.name) for kind in self.workers)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "adapter": self.adapter,
            "host": self.host,
            "port": self.port,
            "ws_port": self.ws_port,
            "workers": list(self.workers),
            "poll_s": self.poll_s,
            "grace_s": self.grace_s,
            "files": list(self.files),
        }

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> Target:
        return cls(
            str(doc["name"]),
            str(doc["adapter"]),
            str(doc["host"]),
            int(doc["port"]),
            int(doc["ws_port"]) if doc.get("ws_port") is not None else None,
            tuple(str(w) for w in doc["workers"]),
            float(doc["poll_s"]),
            float(doc["grace_s"]),
            tuple(str(f) for f in doc.get("files", ())),
        )


def _seconds(value: Any, what: str, minimum: float) -> float:
    from errors import UsageError

    ok = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= minimum
    )
    if not ok:
        raise UsageError(
            f"{what} must be a number of seconds >= {minimum:g}, got {value!r}",
            command="printer ondemand",
        )
    return float(value)


def _timing(flag: float | None, env: str, default: float, minimum: float) -> float:
    """A timing: the flag, else the environment variable (what a systemd drop-in sets), else the default."""
    from errors import UsageError

    if flag is not None:
        return flag
    raw = os.environ.get(env, "").strip()
    if not raw:
        return default
    try:
        return _seconds(float(raw), env, minimum)
    except ValueError as exc:
        raise UsageError(
            f"{env} must be a number of seconds, got {raw!r}",
            command="printer ondemand",
        ) from exc


def resolve(
    name: str, *, poll_s: float | None = None, grace_s: float | None = None
) -> Target:
    """The printer's Target from the fleet registry and printer-devices.yaml (heavy imports: only the one-shot
    child and `--once` call this, never the supervisor loop). Refuses a printer 3d does not supervise."""
    from errors import UsageError
    from printer_ops.devices import load_devices, search_paths
    from printer_ops.supervise_units import plan_printers
    from printops.fleet import load_fleet

    fleet = load_fleet()
    devices = load_devices()
    plans = {p.name: p for p in plan_printers(fleet, dict(devices))}
    if name not in plans:
        raise UsageError(
            f"{name}: not a supervised printer (owner own + access full + a watch adapter)",
            command="printer ondemand",
            remediation=[f"supervised printers: {', '.join(sorted(plans)) or 'none'}"],
        )
    device = devices[name]
    if device.adapter not in PROBES:
        raise UsageError(
            f"{name}: no on-demand probe for adapter {device.adapter}",
            command="printer ondemand",
        )
    poll = _timing(poll_s, ENV_POLL, DEFAULT_POLL_S, MIN_POLL_S)
    grace = _timing(grace_s, ENV_GRACE, DEFAULT_GRACE_S, 0.0)
    files = [str(p) for p in search_paths() if p.is_file()]
    if fleet is not None:
        files.append(str(fleet.path))
    return Target(
        name,
        device.adapter,
        device.host,
        device.port,
        device.ws_port,
        plans[name].kinds,
        poll,
        grace,
        tuple(files),
    )


# ---- the workers -------------------------------------------------------------------------------


@dataclass
class Workers:
    """`systemctl --user` on the worker units of one printer. `run` is injected by the tests."""

    units: tuple[str, ...]
    run: Runner = real_runner

    def _systemctl(
        self, *args: str, timeout_ok: bool = False
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(["systemctl", "--user", *args])
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                list(exc.cmd), 124, "", f"timed out after {exc.timeout:g} s"
            )

    def states(self) -> dict[str, str]:
        """unit -> ActiveState (`is-active` prints one line per unit, in the order given)."""
        out = self._systemctl("is-active", *self.units).stdout.split()
        return {
            u: (out[i] if i < len(out) else "unknown") for i, u in enumerate(self.units)
        }

    def stopped(self) -> list[str]:
        return [u for u, s in self.states().items() if s not in RUNNING]

    def any_running(self) -> bool:
        return len(self.stopped()) < len(self.units)

    def start(self, units: Sequence[str]) -> str | None:
        """Start the units; the error text when systemctl refused, else None."""
        r = self._systemctl("start", *units)
        return (
            None
            if r.returncode == 0
            else (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200]
        )

    def stop(self) -> str | None:
        r = self._systemctl("stop", *self.units)
        return (
            None
            if r.returncode == 0
            else (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200]
        )


# ---- the decision ------------------------------------------------------------------------------


@dataclass
class Machine:
    """idle -> active (a print runs) -> grace (it ended; workers stay) -> idle. Pure: the clock is `now`.

    `observe` takes what the poll saw (True a print runs, False it does not, None no answer) and returns the
    action the caller must run: START (idempotent, returned on EVERY poll while a print runs, so a worker that
    died or was stopped by hand comes back) or STOP (the grace period is over). The phase becomes `idle` only
    when the caller confirms the stop with `stopped()`: a failed `systemctl stop` is retried at the next poll."""

    grace_s: float
    phase: str = IDLE
    deadline: float = 0.0

    def adopt(self, now: float) -> None:
        """Workers already run at supervisor start with no print known: give them one fresh grace period."""
        self.phase, self.deadline = GRACE, now + self.grace_s

    def observe(self, now: float, printing: bool | None) -> str | None:
        if printing:
            self.phase = ACTIVE
            return START
        if self.phase == ACTIVE:
            self.phase, self.deadline = GRACE, now + self.grace_s
        if self.phase == GRACE and now >= self.deadline:
            return STOP
        return None

    def stopped(self) -> None:
        self.phase = IDLE


def sd_notify(message: str) -> None:
    """systemd's notify protocol (READY=1, WATCHDOG=1, STATUS=...): one datagram to $NOTIFY_SOCKET, memory
    only. A no-op outside a Type=notify unit."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s,
        contextlib.suppress(OSError),  # systemd going away: nothing to tell
    ):
        s.sendto(message.encode(), "\0" + path[1:] if path.startswith("@") else path)


def _log(message: str) -> None:
    print(message, flush=True)


@dataclass
class Supervisor:
    """The poll loop of one printer. Everything it touches is injected: the question (`probe`), the workers,
    the clocks (monotonic for timing, wall for the status text), the notifier and the logger."""

    target: Target
    probe: Callable[[Target], bool]
    workers: Workers
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    notify: Callable[[str], None] = sd_notify
    log: Callable[[str], None] = _log
    machine: Machine = field(init=False)
    fails: int = 0
    last_error: str = ""
    action_error: str = ""
    idle_polls: int = 0

    def __post_init__(self) -> None:
        self.machine = Machine(self.target.grace_s)

    def retarget(self, target: Target) -> None:
        """New settings after a config change; the phase and the grace deadline carry over."""
        self.target = target
        self.workers = Workers(target.units(), self.workers.run)
        self.machine.grace_s = target.grace_s

    def reconcile(self) -> None:
        """At supervisor start: workers that already run (a restart inside the grace period, or started by hand)
        get one fresh grace period, so they are stopped when no print needs them."""
        if self.workers.any_running():
            self.machine.adopt(self.clock())
            self.log(
                f"{self.target.name}: workers already running at start: kept for a fresh grace period"
            )

    def _check_strays(self, printing: bool | None) -> None:
        """Idle and nothing printing: every STRAY_CHECK_EVERY_POLLS polls, adopt workers that run anyway (started
        by hand or by a retry timer) so they stop after one grace period instead of running for ever."""
        if self.machine.phase != IDLE or printing:
            self.idle_polls = 0
            return
        self.idle_polls += 1
        if (
            self.idle_polls % STRAY_CHECK_EVERY_POLLS == 0
            and self.workers.any_running()
        ):
            self.machine.adopt(self.clock())
            self.log(
                f"{self.target.name}: workers run with no print (started by hand?): they stop after "
                f"{self.target.grace_s:g} s"
            )

    def tick(self) -> float:
        """One poll: ask, decide, act, publish. Returns the seconds to wait before the next one."""
        printing: bool | None
        try:
            printing = self.probe(self.target)
            self._answered()
        except Unreachable as exc:
            printing = None
            self._unreachable(str(exc))
        self._check_strays(printing)
        before = self.machine.phase
        action = self.machine.observe(self.clock(), printing)
        if action == START:
            self._ensure_started(before)
        elif action == STOP:
            self._stop()
        elif before == ACTIVE and self.machine.phase == GRACE:
            why = "print ended" if printing is False else "printer lost during a print"
            self.log(
                f"{self.target.name}: {why}: workers stay {self.target.grace_s:g} s, then stop"
            )
        self._publish(printing)
        return self._interval(printing)

    def _answered(self) -> None:
        if self.last_error:
            self.log(f"{self.target.name}: printer answers again")
        self.fails, self.last_error = 0, ""

    def _unreachable(self, why: str) -> None:
        self.fails += 1
        if why != self.last_error:  # one line per change of reason, not one per poll
            self.log(
                f"{self.target.name}: printer does not answer ({why}); retrying with backoff"
            )
            self.last_error = why

    def _ensure_started(self, before: str) -> None:
        down = self.workers.stopped()
        if not down:
            return
        if before != ACTIVE:
            what = (
                "print resumed inside the grace period"
                if before == GRACE
                else "print started"
            )
            self.log(f"{self.target.name}: {what}: starting {', '.join(down)}")
        error = self.workers.start(down) or self._still_down()
        if (
            error and error != self.action_error
        ):  # a persistent failure is logged once, not every poll
            self.log(f"{self.target.name}: cannot start workers: {error}")
        self.action_error = error or ""

    def _still_down(self) -> str | None:
        """`systemctl start` exits 0 for a unit whose Condition failed (it stays inactive): say so."""
        down = self.workers.stopped()
        return (
            f"{', '.join(down)} not running after start (a Condition= or drop-in holds it?)"
            if down
            else None
        )

    def _stop(self) -> None:
        error = self.workers.stop()
        if error:
            if error != self.action_error:
                self.log(f"{self.target.name}: cannot stop workers: {error}")
            self.action_error = error
            return  # still in grace: the next poll retries
        self.action_error = ""
        self.machine.stopped()
        self.log(f"{self.target.name}: grace over: workers stopped")

    def _interval(self, printing: bool | None) -> float:
        poll = self.target.poll_s
        if printing is None and self.machine.phase == IDLE and self.fails > 1:
            return min(poll * 2 ** (self.fails - 1), max(BACKOFF_MAX_S, poll))
        return poll

    def _publish(self, printing: bool | None) -> None:
        seen = (
            "unreachable" if printing is None else ("printing" if printing else "idle")
        )
        self.notify(
            f"STATUS=printer={seen} phase={self.machine.phase} polled={int(self.wall())}"
        )
        self.notify("WATCHDOG=1")


# ---- config resolution and the loop ------------------------------------------------------------


def resolve_in_child(name: str, flags: Sequence[str]) -> Target:
    """Run `--resolve` in a one-shot python child (it imports yaml and the fleet registry; the supervisor does
    not, so its RSS stays small). Raises RuntimeError with the child's stderr when it fails."""
    lib = str(Path(__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, [lib, os.environ.get("PYTHONPATH", "")])
        ),
    }
    r = subprocess.run(
        [sys.executable, "-m", "printer_ops.ondemand", name, "--resolve", *flags],
        env=env,
        capture_output=True,
        text=True,
        timeout=RESOLVE_TIMEOUT_S,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            (r.stderr or r.stdout or f"exit {r.returncode}").strip()[-300:]
        )
    return Target.from_json(json.loads(r.stdout))


def _mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def mtimes(target: Target) -> dict[str, int | None]:
    return {f: _mtime(f) for f in target.files}


def config_changed(target: Target, seen: Mapping[str, int | None]) -> bool:
    """Did any config file the target came from change (mtime, appearing or vanishing) since `seen`?"""
    return any(_mtime(f) != seen.get(f) for f in target.files)


_STOP_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT})


def _wait(seconds: float) -> signal.Signals | None:
    """Sleep up to `seconds`, woken by SIGTERM/SIGINT/SIGHUP (blocked in `serve`, so none is lost between
    polls). No thread, no timer."""
    info = signal.sigtimedwait({signal.SIGTERM, signal.SIGINT, signal.SIGHUP}, seconds)
    return signal.Signals(info.si_signo) if info is not None else None


def serve(name: str, flags: Sequence[str]) -> int:
    """The supervisor process: resolve, reconcile, then poll until SIGTERM. Never exits on a printer or
    config problem (a crash loop would respawn the heavy resolver every RestartSec)."""
    signal.pthread_sigmask(
        signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    )
    target = _resolve_until_ok(name, flags)
    if target is None:
        return 0
    sup = Supervisor(target, PROBES[target.adapter], Workers(target.units()))
    sup.reconcile()
    seen = mtimes(target)
    _log(
        f"{name}: supervising {target.adapter} {target.host}:{target.port} every {target.poll_s:g} s, "
        f"grace {target.grace_s:g} s, workers {', '.join(target.workers)}"
    )
    sd_notify("READY=1")
    while True:
        sig = _wait(sup.tick())
        if sig in _STOP_SIGNALS:
            _log(f"{name}: stopping (workers are left as they are)")
            return 0
        if sig is signal.SIGHUP or config_changed(sup.target, seen):
            sup.retarget(_reresolve(name, flags, sup.target))
            seen = mtimes(
                sup.target
            )  # also after a failed reload: one log line, not one retry per poll


def _reresolve(name: str, flags: Sequence[str], current: Target) -> Target:
    try:
        target = resolve_in_child(name, flags)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        _log(f"{name}: config changed but cannot be read, keeping the old one: {exc}")
        return current
    _log(f"{name}: config reloaded")
    return target


def _resolve_until_ok(name: str, flags: Sequence[str]) -> Target | None:
    """The first resolution, retried with backoff while the config is broken. None: stopped by a signal."""
    delay = 10.0
    while True:
        try:
            return resolve_in_child(name, flags)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            _log(
                f"{name}: cannot read the printer configuration (retry in {delay:g} s): {exc}"
            )
        # READY now: a Type=notify unit that never says so is killed after TimeoutStartSec and restarted
        # every RestartSec, which would respawn the heavy resolver forever on a broken config
        sd_notify("READY=1")
        sd_notify("STATUS=waiting for a readable configuration")
        sd_notify("WATCHDOG=1")
        if _wait(delay) in _STOP_SIGNALS:
            return None
        delay = min(delay * 2, RESOLVE_RETRY_MAX_S)


# ---- command line ------------------------------------------------------------------------------


def _parse(argv: list[str]) -> tuple[str, dict[str, Any]]:
    from errors import UsageError

    opts: dict[str, Any] = {}
    names: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--once", "--resolve"):
            opts[a] = True
        elif a in ("--poll", "--grace"):
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="printer ondemand")
            opts[a] = argv[i + 1]
            i += 1
        elif a.startswith("-"):
            raise UsageError(
                f"unknown option {a}", command="printer ondemand", remediation=[USAGE]
            )
        else:
            names.append(a)
        i += 1
    if len(names) != 1:
        raise UsageError(
            "give exactly one printer NAME",
            command="printer ondemand",
            remediation=[USAGE],
        )
    return names[0], opts


def _flag_seconds(opts: Mapping[str, Any], flag: str, minimum: float) -> float | None:
    from errors import UsageError

    if flag not in opts:
        return None
    try:
        return _seconds(float(opts[flag]), flag, minimum)
    except ValueError as exc:
        raise UsageError(
            f"{flag} must be a number of seconds, got {opts[flag]!r}",
            command="printer ondemand",
        ) from exc


def _would(printing: bool | None, running: int, total: int) -> str:
    if printing is None:
        return "retry with backoff"
    if printing:
        return "nothing" if running == total else "start the workers"
    return "stop them after the grace period" if running else "nothing"


def _once(target: Target) -> int:
    printing: bool | None
    try:
        printing, error = PROBES[target.adapter](target), None
    except Unreachable as exc:
        printing, error = None, str(exc)
    states = Workers(target.units()).states()
    running = sum(1 for s in states.values() if s in RUNNING)
    print(
        json.dumps(
            {
                "printer": target.name,
                "answered": printing is not None,
                "print_active": printing,
                "error": error,
                "workers": states,
                "would": _would(printing, running, len(states)),
                "poll_s": target.poll_s,
                "grace_s": target.grace_s,
            }
        )
    )
    return 0 if printing is not None else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    name, opts = _parse(argv)
    poll = _flag_seconds(opts, "--poll", MIN_POLL_S)
    grace = _flag_seconds(opts, "--grace", 0.0)
    if opts.get("--resolve"):
        print(json.dumps(resolve(name, poll_s=poll, grace_s=grace).to_json()))
        return 0
    if opts.get("--once"):
        return _once(resolve(name, poll_s=poll, grace_s=grace))
    flags = [
        part for k in ("--poll", "--grace") if k in opts for part in (k, str(opts[k]))
    ]
    return serve(name, flags)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
