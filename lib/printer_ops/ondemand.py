"""ondemand.py — `3d printer ondemand NAME`: the one-shot check, run by a systemd timer every 30 s, that starts
and stops the watch and vision workers of one printer so they run only while a print runs (#747).

WHAT: NO process lives between two checks. The timer `3d-printer-ondemand@NAME.timer` starts the one-shot
  service `3d-printer-ondemand@NAME.service` every 30 s; the service runs this module, asks the printer ONE
  small question, acts and exits (~20 ms, ~15 MB while it runs, nothing resident afterwards). "A print is
  active" means printing or paused:
    moonraker       GET /printer/objects/query?print_stats&webhooks  (331 bytes) over a raw socket
    creality-stock  one short read of the printer's ws:9999 state push
  A print starts      -> `systemctl --user start 3d-printer-watch@NAME [3d-printer-vision@NAME]`
  A print ends        -> the workers keep running for `grace` seconds (default 300: the end-of-print events,
                         the bed check, a print that starts again), then `systemctl --user stop` removes them
                         COMPLETELY: no models in memory, no sockets, no timers, no heartbeat files.
  The workers are the unchanged `3d printer watch` / `3d printer vision` services: this module never reads a
  frame, never sends anything to the printer but that one GET, and never pauses or cancels.

WHY (#747): the always-on workers held 1.7 GiB (two ONNX vision processes) and 4.6 % of a core while nothing
  printed; the first fix kept one tiny always-on supervisor process per printer (18 MB each, a wake-up every
  30 s, a resident python). A timer-driven one-shot removes even that. The requirement of #640/#641 stays: a
  print started from the touchscreen, Fluidd, a slicer or Home Assistant is supervised, here within one timer
  interval (the first layer is minutes after the start: heating).

WHY POLLING and not a websocket subscription or a push: Moonraker pushes `notify_proc_stat_update` to every
  websocket client every second, so a subscription costs a 1 Hz wake-up and a permanently open connection on the
  printers' small boards, against one 331-byte request per 30 s. systemd path or socket activation cannot
  observe a remote printer, and the printers do not push a print start to us.

STATE BETWEEN CHECKS lives in ONE small file per printer in `$XDG_RUNTIME_DIR/3d-cli/ondemand/NAME.json`
  (tmpfs: RAM, never the disk, gone at reboot, when the workers are gone too): the resolved printer address and
  the mtimes of the config files it came from (so the heavy yaml/fleet loader runs only when the config
  changed), the phase (idle | active | grace), the grace deadline, the backoff step and the last error. The file
  is rewritten ONLY when its content changed (a transition, a backoff step, a config change), so an idle printer
  writes nothing: no file, no journal line (the unit sets `LogLevelMax=notice`, which also silences systemd's own
  Starting/Finished lines), no disk.

RESTART SAFETY: workers are separate units that only this check starts and stops. After a reboot (state file
  gone, workers gone) a print already running starts them at the first check. A state file that is missing while
  workers run (the runtime directory was cleaned, or they were started by hand) gives them one fresh grace
  period; the same stray look repeats about every 10 minutes while idle. A printer that does not answer is retried
  with backoff (the next tick once, then every 2 min) and never starts workers; while workers run, an unreachable
  printer counts like an ended print (grace applies).

INVARIANTS: stdlib only and import-light at module level (no yaml, ssl, http.client, pathlib, subprocess,
  dataclasses; `tests/e2e/test_printer_ondemand_workflows.py` pins the startup cost); the heavy config loader
  (yaml, the fleet registry) runs only in a one-shot child (`--resolve`) after a config change; every systemctl
  call goes through `Workers.run` (tests inject it); the only file written is the state file above, only on
  change.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from printer_ops.unit_names import RUNNING, unit_name

TYPE_CHECKING = False  # PEP 484: not imported from typing, which costs ~4 ms per check

if TYPE_CHECKING:
    import subprocess
    from typing import Any

    Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

DOWN = frozenset({"inactive", "failed"})  # `is-active` answers that leave nothing to stop
DEFAULT_GRACE_S = 300.0
ENV_GRACE = "THREED_ONDEMAND_GRACE_S"  # a drop-in on the service sets it
# A printer (or a config) that failed twice in a row is asked again only after BACKOFF_S: the timer keeps firing every
# 30 s and the check exits at once in between (2 min is still early for a heated start). The deadline is in the state
# file, so it holds for any timer interval; the file is rewritten once per ask while the printer stays off.
BACKOFF_S = 120.0
BACKOFF_SLACK_S = 5.0  # a tick a little early must not slip a whole interval
MAX_FAILS = 2  # 0 = answered, 1 = one miss (retried at the next tick), 2 = backing off
RESOLVE_TIMEOUT_S = 60.0  # below the unit's TimeoutStartSec: a hung resolver must end in our own backoff, not in a kill
PROBE_TIMEOUT_S = 8.0  # connect + reply, one overall deadline
CREALITY_LISTEN_S = (
    3.0  # how long the K1 stock firmware gets to push its state after connect
)
MAX_REPLY = (
    64 * 1024
)  # the query answer is ~330 bytes; anything bigger is not Moonraker
# While idle, look for workers somebody started (by hand, a mount-retry timer) in about one check of STRAY_ODDS: one
# `systemctl is-active` per ~10 minutes at the 30 s interval, so an idle printer costs ~nothing. Chosen at random
# (os.urandom: no import, no state to write) so it works for any timer interval: a fixed time window can fall
# between the ticks of some intervals for ever.
STRAY_ODDS = 20
ACTIVE_PRINT_STATES = frozenset({"printing", "paused"})  # Klipper print_stats.state
CREALITY_ACTIVE = frozenset(
    {1, 5, 7}
)  # creality.parse_status: printing, paused, a cancel still finishing

IDLE, ACTIVE, GRACE = "idle", "active", "grace"
START, STOP = "start", "stop"
WORKER_KINDS = ("watch", "vision")
STATE_VERSION = 1
EX_TEMPFAIL = 75  # sysexits: another check of this printer holds the lock; a failed run, so status says so
EX_CONFIG = 78  # sysexits: `--resolve` of a printer 3d does not supervise (not an unreadable file)

USAGE = """3d printer ondemand NAME [--grace S] [--once]

The on-demand part of the printer supervision (#747): a one-shot check that a systemd timer runs every 30 s
(`3d-printer-ondemand@NAME.timer`; no process lives between two checks). It asks the printer ONE small question
(Moonraker: GET /printer/objects/query?print_stats&webhooks; creality-stock: one ws:9999 read) and runs
`systemctl --user start|stop` on the heavy workers `3d-printer-watch@NAME` and `3d-printer-vision@NAME` only
while a print runs.

  - a print starts (printing or paused, started from anywhere)   -> both workers are started
  - the print ends                                               -> they keep running --grace seconds, then
                                                                    are stopped COMPLETELY (no models, no files)
  - the printer does not answer                                  -> retried at the next tick, then every 2 min;
                                                                    workers are never started for it
  - after a reboot or a restart of the timer                     -> a running print starts the workers; workers
                                                                    that run with no print get one fresh grace
                                                                    period

With no option it does ONE scheduled check (what the unit runs). Run by hand it acts with your shell's environment
(another working directory, no drop-in grace period) on the same state: to just look, use --once. Options:
  --grace S    seconds the workers keep running after the print ended (default 300; 0 = stop at once)
  --resolve    print the resolved configuration as JSON and exit (what the check's helper child runs)
  --once       ask now, print one JSON line (what the printer says, which workers run, what would happen) and
               exit: 0 when the printer answered, 1 when not. Changes nothing, writes no state.

Grace without a flag: a systemd drop-in is the place (`systemctl --user edit 3d-printer-ondemand@NAME`):
  [Service]
  Environment=THREED_ONDEMAND_GRACE_S=900
The check interval is the timer's (`systemctl --user edit 3d-printer-ondemand@NAME.timer`); an empty assignment
resets every timer setting, so a drop-in repeats both:
  [Timer]
  OnActiveSec=
  OnActiveSec=5s
  OnUnitInactiveSec=10s
(a longer interval delays the workers by as much; `supervise status` judges with the shipped 30 s)

The units are installed by `3d printer supervise install`; `3d printer supervise status` says what the last check
saw. The state of the last check is in $XDG_RUNTIME_DIR/3d-cli/ondemand/NAME.json (RAM).

Examples:
  3d printer ondemand u1 --once
  3d printer ondemand k1se --grace 900 --once"""


class Unreachable(Exception):
    """The printer gave no usable answer (down, timeout, not Moonraker, an error status)."""


class NotSupervised(Exception):
    """The configuration was read and says 3d does not supervise this printer (not `owner: own` + `access: full`,
    or no probe for its adapter). Unlike an unreadable file that is final until a config file changes: `files` are
    the config files whose change can make it supervised again."""

    def __init__(self, message: str, files: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.files = tuple(files)


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


class Target:
    """One printer as the check needs it: where to ask, which workers to run, the config files it came from."""

    __slots__ = ("adapter", "files", "host", "name", "port", "workers", "ws_port")

    def __init__(
        self,
        name: str,
        adapter: str,
        host: str,
        port: int,
        ws_port: int | None = None,
        workers: tuple[str, ...] = WORKER_KINDS,
        files: tuple[str, ...] = (),  # a changed mtime of one of them means re-resolve
    ) -> None:
        self.name, self.adapter, self.host, self.port = name, adapter, host, port
        self.ws_port, self.workers, self.files = ws_port, workers, files

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
            tuple(str(f) for f in doc.get("files", ())),
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Target) and self.to_json() == other.to_json()

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return f"Target({self.to_json()!r})"


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


def grace_seconds(flag: float | None = None) -> float:
    """The grace period: the flag, else the environment variable (what a systemd drop-in sets), else the default.
    Read at every check, so a drop-in applies at the next one."""
    from errors import UsageError

    if flag is not None:
        return flag
    raw = os.environ.get(ENV_GRACE, "").strip()
    if not raw:
        return DEFAULT_GRACE_S
    try:
        return _seconds(float(raw), ENV_GRACE, 0.0)
    except ValueError as exc:
        raise UsageError(
            f"{ENV_GRACE} must be a number of seconds, got {raw!r}",
            command="printer ondemand",
        ) from exc


def resolve(name: str) -> Target:
    """The printer's Target from the fleet registry and printer-devices.yaml (heavy imports: yaml and the fleet
    loader; the check calls this only when the cached target is missing or a config file changed). Raises
    NotSupervised for a printer 3d does not supervise."""
    from printer_ops.camera import MODELS_FILE
    from printer_ops.devices import load_devices, search_paths
    from printer_ops.supervise_units import plan_printers
    from printops.fleet import fleet_path, load_fleet

    # every place a config file could be read from, present or not: a file that appears later (a
    # printer-devices.yaml with a corrected address) must invalidate the cached target too. The shipped camera
    # defaults decide whether a printer gets a vision worker, so a `git pull` that changes them counts too.
    files = [str(p) for p in search_paths()]
    fleet_file = fleet_path()
    if fleet_file is not None:
        files.append(str(fleet_file))
    files.append(str(MODELS_FILE))
    fleet = load_fleet()
    devices = load_devices()
    plans = {p.name: p for p in plan_printers(fleet, dict(devices))}
    if name not in plans:
        raise NotSupervised(
            f"{name}: not a supervised printer (owner own + access full + a watch adapter; supervised printers: "
            f"{', '.join(sorted(plans)) or 'none'})",
            files,
        )
    device = devices[name]
    if device.adapter not in PROBES:
        raise NotSupervised(
            f"{name}: no on-demand probe for adapter {device.adapter}", files
        )
    return Target(
        name,
        device.adapter,
        device.host,
        device.port,
        device.ws_port,
        plans[name].kinds,
        tuple(files),
    )


def resolve_in_child(name: str) -> Target:
    """`resolve` in a python child WITH site-packages (the check itself runs `-S`, which has no yaml): the heavy
    loader runs only here, after a config change, and its memory is gone when the child exits. Raises
    RuntimeError with the child's message when it fails."""
    import subprocess  # lazy

    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, [lib, os.environ.get("PYTHONPATH", "")])
        ),
    }
    r = subprocess.run(
        [sys.executable, "-m", "printer_ops.ondemand", name, "--resolve"],
        env=env,
        capture_output=True,
        text=True,
        timeout=RESOLVE_TIMEOUT_S,
        check=False,
    )
    if r.returncode == EX_CONFIG:
        refusal = json.loads(r.stdout)
        raise NotSupervised(str(refusal["message"]), refusal["files"])
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


def mtimes(files: Sequence[str]) -> dict[str, int | None]:
    return {f: _mtime(f) for f in files}


def config_changed(files: Sequence[str], seen: Mapping[str, int | None]) -> bool:
    """Did any of the config `files` change (mtime, appearing or vanishing) since `seen`?"""
    return any(_mtime(f) != seen.get(f) for f in files)


# ---- the workers -------------------------------------------------------------------------------


def _real_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    import subprocess  # lazy: only a check that acts or looks for strays runs systemctl

    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(f"refusing to run {argv[0]} from a test: inject a runner")
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=60, check=False
    )


class Workers:
    """`systemctl --user` on the worker units of one printer. `run` is injected by the tests."""

    def __init__(
        self, units: tuple[str, ...], name: str, run: Runner | None = None
    ) -> None:
        self.units = units  # what the printer's configuration wants running
        # every worker kind of the printer: one a config change dropped (the camera was removed while it ran)
        # must still be stopped, and found as a stray
        self.every = tuple(unit_name(kind, name) for kind in WORKER_KINDS)
        self.run = run or _real_runner

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        import subprocess

        try:
            return self.run(["systemctl", "--user", *args])
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                list(exc.cmd), 124, "", f"timed out after {exc.timeout:g} s"
            )

    def _states(self, units: tuple[str, ...]) -> dict[str, str]:
        """unit -> ActiveState (`is-active` prints one line per unit, in the order given)."""
        out = self._systemctl("is-active", *units).stdout.split()
        return {u: (out[i] if i < len(out) else "unknown") for i, u in enumerate(units)}

    def states(self) -> dict[str, str]:
        return self._states(self.units)

    def stopped(self) -> list[str]:
        return [u for u, s in self.states().items() if s not in RUNNING]

    def any_running(self) -> bool:
        return any(s in RUNNING for s in self._states(self.every).values())

    def start(self, units: Sequence[str]) -> str | None:
        """Start the units; the error text when systemctl refused, else None."""
        r = self._systemctl("start", *units)
        return (
            None
            if r.returncode == 0
            else (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200]
        )

    def stop(self) -> str | None:
        """Stop the worker units of the printer that are not DEFINITELY down. A unit that reports `inactive` is left
        out: its template may not be installed (a printer without a camera, a camera added after the install),
        and systemctl answers a stop of an unloaded unit with an error that would keep the printer in its grace
        period for ever. A unit whose state is unknown (no answer from `is-active`: bus briefly gone, timeout) or
        in transition (`deactivating`) is stopped, or its stop fails and the next check retries: never mistake
        'could not ask' for 'stopped'."""
        up = [u for u, s in self._states(self.every).items() if s not in DOWN]
        if not up:
            return None
        r = self._systemctl("stop", *up)
        return (
            None
            if r.returncode == 0
            else (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200]
        )


# ---- the decision ------------------------------------------------------------------------------


class Machine:
    """idle -> active (a print runs) -> grace (it ended; workers stay) -> idle. Pure: the clock is `now`.

    `observe` takes what the check saw (True a print runs, False it does not, None no answer) and returns the
    action the caller must run: START (idempotent, returned on EVERY check while a print runs, so a worker that
    died or was stopped by hand comes back) or STOP (the grace period is over). The phase becomes `idle` only
    when the caller confirms the stop with `stopped()`: a failed `systemctl stop` is retried at the next check."""

    def __init__(self, grace_s: float, phase: str = IDLE, started: float = 0.0) -> None:
        self.grace_s, self.phase, self.started = grace_s, phase, started

    @property
    def deadline(self) -> float:
        """When the grace period ends: its START plus the CURRENT grace, so `--grace` or a drop-in changed during a
        grace period applies to it at once (shortened or extended)."""
        return self.started + self.grace_s

    def adopt(self, now: float) -> None:
        """Workers already run with no print known: give them one fresh grace period."""
        self.phase, self.started = GRACE, now

    def observe(self, now: float, printing: bool | None) -> str | None:
        if printing:
            self.phase = ACTIVE
            return START
        if self.phase == ACTIVE:
            self.phase, self.started = GRACE, now
        if self.phase == GRACE and now >= self.deadline:
            return STOP
        return None

    def stopped(self) -> None:
        self.phase = IDLE


def _log(message: str) -> None:
    print(message, flush=True)


class Check:
    """One check of one printer: ask, decide, act. Everything it touches is injected: the question (`probe`),
    the workers, the clock and the logger. It starts from the persisted `state` of the previous check and
    `snapshot()` is what to persist for the next one."""

    def __init__(
        self,
        target: Target,
        probe: Callable[[Target], bool],
        workers: Workers,
        grace_s: float,
        state: Mapping[str, Any],
        now: float,
        log: Callable[[str], None] = _log,
    ) -> None:
        self.target, self.probe, self.workers, self.now, self.log = (
            target,
            probe,
            workers,
            now,
            log,
        )
        self.machine = Machine(
            grace_s, str(state.get("phase", IDLE)), float(state.get("grace_start", 0.0))
        )
        self.fails = int(state.get("fails", 0))
        self.last_error = str(state.get("error", ""))
        self.action_error = str(state.get("action_error", ""))
        self.seen = ""
        self.retry_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.machine.phase,
            "grace_start": self.machine.started,
            "seen": self.seen,
            "fails": self.fails,
            "error": self.last_error,
            "action_error": self.action_error,
            "retry_at": self.retry_at,
        }

    def run(self, *, first: bool, stray_due: bool) -> None:
        """`first`: no state from a previous check (a boot, or the runtime directory was cleaned); `stray_due`:
        this check is the one that looks for workers somebody started."""
        printing: bool | None
        try:
            printing = self.probe(self.target)
            self._answered()
        except Unreachable as exc:
            printing = None
            self._unreachable(str(exc))
        self.seen = (
            "unreachable" if printing is None else ("printing" if printing else "idle")
        )
        self._adopt_strays(printing, first=first, stray_due=stray_due)
        before = self.machine.phase
        action = self.machine.observe(self.now, printing)
        if action == START:
            self._ensure_started(before)
        elif action == STOP:
            self._stop()
        elif before == ACTIVE and self.machine.phase == GRACE:
            why = "print ended" if printing is False else "printer lost during a print"
            self.log(
                f"{self.target.name}: {why}: workers stay {self.machine.grace_s:g} s, then stop"
            )
        if printing is None and self.machine.phase == IDLE and self.fails >= MAX_FAILS:
            self.retry_at = self.now + BACKOFF_S - BACKOFF_SLACK_S

    def _adopt_strays(
        self, printing: bool | None, *, first: bool, stray_due: bool
    ) -> None:
        """Idle and nothing printing: on the first check and then every ~10 minutes, adopt workers that run
        anyway (started by hand or by a retry timer) so they stop after one grace period instead of running for
        ever."""
        if self.machine.phase != IDLE or printing or not (first or stray_due):
            return
        if not self.workers.any_running():
            return
        self.machine.adopt(self.now)
        if first:
            self.log(
                f"{self.target.name}: workers already running at the first check: kept for a fresh grace period"
            )
        else:
            self.log(
                f"{self.target.name}: workers run with no print (started by hand?): they stop after "
                f"{self.machine.grace_s:g} s"
            )

    def _answered(self) -> None:
        if self.last_error:
            self.log(f"{self.target.name}: printer answers again")
        self.fails, self.last_error = 0, ""

    def _unreachable(self, why: str) -> None:
        self.fails = min(self.fails + 1, MAX_FAILS)
        if why != self.last_error:  # one line per change of reason, not one per check
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
        ):  # a persistent failure is logged once, not every check
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
            return  # still in grace: the next check retries
        self.action_error = ""
        self.machine.stopped()
        self.log(f"{self.target.name}: grace over: workers stopped")


# ---- state between checks ----------------------------------------------------------------------


def state_dir() -> str:
    """`$XDG_RUNTIME_DIR/3d-cli/ondemand` (tmpfs: RAM, not disk, cleared at reboot)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(base, "3d-cli", "ondemand")


def state_path(name: str) -> str:
    return os.path.join(state_dir(), f"{name}.json")


def load_state(path: str) -> dict[str, Any]:
    """The state of the previous check; {} when there is none or it is unreadable (a fresh start)."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) and doc.get("v") == STATE_VERSION else {}


def save_state(path: str, doc: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True)
    os.replace(tmp, path)


def read_status(name: str, path: str | None = None) -> dict[str, Any]:
    """What the last check saw, for `3d printer supervise status`: {printer: idle|printing|unreachable, phase,
    error, config_error, config_kept, refused, last_probe_mono}; {} when no check has run since boot. A check that never got past an unreadable
    configuration has no phase, but its config_error must not vanish."""
    doc = load_state(path or state_path(name))
    if "phase" not in doc and not doc.get("config_error"):
        return {}
    return {
        "printer": doc.get("seen", ""),
        "phase": doc.get("phase", ""),
        "error": doc.get("error", ""),
        "config_error": doc.get("config_error", ""),
        "config_kept": bool(doc.get("config_kept")),
        "refused": bool(doc.get("refused")),
        "last_probe_mono": _last_probe_mono(doc),
    }


def _last_probe_mono(doc: Mapping[str, Any]) -> float | None:
    """CLOCK_MONOTONIC seconds of the last check that really asked the printer, when the state says it is
    backing off (a later check then exits without asking, and its finish time says nothing about the
    observation); None otherwise. The probe that set the deadline ran `BACKOFF_S - BACKOFF_SLACK_S` before it."""
    retry_at = doc.get("retry_at")
    if (
        isinstance(retry_at, bool)
        or not isinstance(retry_at, (int, float))
        or retry_at <= 0
    ):
        return None
    return float(retry_at) - (BACKOFF_S - BACKOFF_SLACK_S)


# ---- the scheduled check -----------------------------------------------------------------------


def _context() -> str:
    """What the resolution depends on besides the config files' mtimes: which fleet file is selected, the config
    directory, the working directory (a project-local printer-devices.yaml) and the checkout this code runs from. A change invalidates the cached target."""
    here = os.path.dirname(os.path.abspath(__file__))
    env = [  # unset and empty differ: THREED_FLEET="" disables the fleet layer, unset reads the default
        "<unset>" if os.environ.get(name) is None else os.environ[name]
        for name in ("THREED_FLEET", "XDG_CONFIG_HOME")
    ]
    # bin/3d sets REPO_ROOT to its checkout and the unit does not: unset means the checkout this code runs from, so a
    # hand-run `3d printer ondemand` is not another context than the timer's check
    repo = os.environ.get("REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.realpath(here)))
    return "\n".join([*env, repo, os.getcwd(), here])


def _unreadable(
    name: str,
    doc: dict[str, Any],
    cached: Target | None,
    exc: Exception,
    now: float,
    log: Callable[[str], None],
    context: str,
) -> Target | None:
    """The configuration cannot be read: log it once per change of the message. With a `cached` target of the
    same context (a file edited into a state that does not parse) it carries on with that one and tries again
    after one backoff period. Without one (another context, or none yet) the target is not usable: back off, and
    forget the context so the next attempt resolves afresh. Returns the target to carry on with."""
    message = str(getattr(exc, "message", "") or exc)[:300]
    if doc.get("config_error") != message:
        keeping = ", keeping the old one" if cached is not None else ""
        log(f"{name}: cannot read the printer configuration{keeping}: {message}")
    doc["config_error"] = message
    if cached is not None:
        doc["config_kept"] = (
            True  # status: a warning (the old configuration still works), not "cannot start"
        )
        doc["resolve_retry_at"] = now + BACKOFF_S - BACKOFF_SLACK_S
        return cached
    # the stale `target` stays: a print that is still in its grace period needs its worker names to be cleaned up
    for key in ("mtimes", "context", "resolve_retry_at", "config_kept", "refused"):
        doc.pop(key, None)
    doc["config_fails"] = min(int(doc.get("config_fails", 0)) + 1, MAX_FAILS)
    if doc["config_fails"] >= MAX_FAILS:
        doc["retry_at"] = now + BACKOFF_S - BACKOFF_SLACK_S
        doc["backoff_context"] = context
    return None


def _stamps(files: Sequence[str], before: Mapping[str, int | None], started_ns: int) -> dict[str, int | None]:
    """The file stamps to keep after a resolution that began at `started_ns`: those taken BEFORE it, else (the
    first resolution knows no file names yet) the current ones, except a file modified since the loader began:
    the loader may have read its old content, so its stamp is one that never matches and the next check
    resolves again (that one stamps first)."""
    out: dict[str, int | None] = {}
    for f in files:
        m = before[f] if f in before else _mtime(f)
        out[f] = -1 if f not in before and m is not None and m >= started_ns else m
    return out


def _refused(
    name: str,
    doc: dict[str, Any],
    exc: NotSupervised,
    before: Mapping[str, int | None],
    started_ns: int,
    context: str,
    log: Callable[[str], None],
) -> None:
    """The configuration says 3d does not supervise this printer (any more): no target, nothing to ask. Final until
    one of the config files changes, so the heavy loader does not run again before that. A target kept in the
    state still names the workers of a print in its grace period, which then run out and are stopped."""
    message = str(exc)[:300]
    if doc.get("config_error") != message:
        log(f"{name}: not supervised any more, no probe and no new workers: {message}")
    doc["config_error"] = message
    doc["refused"] = list(exc.files)
    for key in ("retry_at", "backoff_context"):  # a printer that is never asked has no backoff to report
        doc.pop(key, None)
    doc["mtimes"] = _stamps(exc.files, before, started_ns)
    doc["context"] = context
    for key in ("config_kept", "config_fails", "resolve_retry_at"):
        doc.pop(key, None)


def _stored(
    name: str,
    doc: dict[str, Any],
    target: Target,
    cached: Target | None,
    before: Mapping[str, int | None],
    started_ns: int,
    context: str,
    log: Callable[[str], None],
) -> None:
    """A fresh resolution succeeded: log what changed and keep it, with the file stamps of BEFORE the loader ran."""
    if cached is None:
        log(
            f"{name}: supervising {target.adapter} {target.host}:{target.port}, workers {', '.join(target.workers)}"
        )
    elif cached != target:
        log(f"{name}: config reloaded")
    doc["target"], doc["mtimes"], doc["context"] = (
        target.to_json(),
        _stamps(target.files, before, started_ns),
        context,
    )
    for key in (
        "config_error",
        "config_fails",
        "config_kept",
        "refused",
        "resolve_retry_at",
    ):
        doc.pop(key, None)


def _cache_answer(
    doc: dict[str, Any],
    watched: Sequence[str],
    cached: Target | None,
    refused: bool,
    now: float,
) -> tuple[bool, Target | None]:
    """(True, target) when the cache answers without a resolution: no watched file changed, or the last
    re-resolution failed and its backoff period is not over."""
    if not config_changed(watched, doc.get("mtimes", {})):
        # files match the last good resolution again (e.g. the original restored with its old mtime): no kept-config warning
        if not refused and doc.get("config_kept"):
            for key in ("config_error", "config_kept", "resolve_retry_at"):
                doc.pop(key, None)
        return True, None if refused else cached
    if now < float(doc.get("resolve_retry_at", 0.0)):
        return True, cached
    return False, None


def _target(
    name: str,
    doc: dict[str, Any],
    resolver: Callable[[str], Target],
    now: float,
    log: Callable[[str], None],
) -> Target | None:
    """The cached target while no config file changed, else a fresh resolution (heavy, rare). None: no usable
    configuration (logged once per change of the message; retried with backoff), or a printer 3d does not
    supervise (final until a config file changes). A file edited into an unreadable state keeps the old target
    and is retried after one backoff period; a changed context drops it."""
    cached = Target.from_json(doc["target"]) if "target" in doc else None
    refused = [str(f) for f in doc.get("refused", ())]
    context = _context()
    same_context = doc.get("context") == context
    watched = refused or (cached.files if cached is not None else None)
    before: Mapping[str, int | None] = {}
    if watched is not None and same_context:
        answered, target = _cache_answer(doc, watched, cached, bool(refused), now)
        if answered:
            return target
        # the stamps BEFORE the resolution: a file saved while the loader runs is then seen as changed next time
        before = mtimes(watched)
    started_ns = time.time_ns()  # the clock the file times are on
    try:
        target = resolver(name)
    except NotSupervised as exc:
        _refused(name, doc, exc, before, started_ns, context, log)
        return None
    except Exception as exc:  # any loader failure (yaml, fleet, devices): the config is unreadable, not a crash
        # a CHANGED CONTEXT (another fleet, config directory or checkout) is another configuration: the old
        # target does not belong to it; a refused printer has no target to keep
        keep = None if refused or not same_context else cached
        return _unreadable(name, doc, keep, exc, now, log, context)
    _stored(name, doc, target, cached, before, started_ns, context, log)
    return target


def _stray_due() -> bool:
    return os.urandom(1)[0] * STRAY_ODDS < 256


def _lock(path: str) -> int | None:
    """An exclusive, non-blocking flock on `path`.lock: the fd, or None when another check of this printer holds
    it. The timer never overlaps its own one-shot, but a hand-run `3d printer ondemand NAME` can; two checks
    interleaving would let an older one stop workers a newer one just started. The lock file is created once and
    afterwards opened read-only, so an idle check writes nothing for it."""
    import fcntl  # lazy: a C module, but only the scheduled check needs it

    lock_path = path + ".lock"
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
        os.close(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600))
        fd = os.open(lock_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def run_check(name: str, *, path: str, **kwargs: Any) -> int:
    """The scheduled check of printer `name` (see `_run_check`), serialized per printer: a check that finds
    another one running exits at once, with EX_TEMPFAIL, and leaves the work to it. It is a FAILED run on purpose:
    a skipped check must not refresh the finish time `supervise status` reads as "the printer was just asked"
    (a hand-run check hanging on the lock would otherwise hide a stale snapshot)."""
    fd = _lock(path)
    if fd is None:
        print(
            f"{name}: another check of this printer is running; this one leaves the work to it",
            file=sys.stderr,
        )
        return EX_TEMPFAIL
    try:
        return _run_check(name, path=path, **kwargs)
    finally:
        os.close(fd)


def _run_check(
    name: str,
    *,
    path: str,
    grace_s: float | None = None,
    resolver: Callable[[str], Target] = resolve_in_child,
    probe: Callable[[Target], bool] | None = None,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
    stray_due: Callable[[], bool] = _stray_due,
    log: Callable[[str], None] = _log,
) -> int:
    """The body of one check, run under the per-printer lock: load the state, ask, act, save the state when it
    changed. Exit code 0 also for an unreachable printer or a broken config (both are logged and retried with
    backoff)."""
    now = clock()
    before = load_state(path)
    doc: dict[str, Any] = {**before, "v": STATE_VERSION}
    context = _context()
    idle = doc.get("phase", IDLE) == IDLE
    # a backoff holds only for the context it was set in: another configuration is asked at once
    resting = (
        now < float(doc.get("retry_at", 0.0)) and doc.get("backoff_context") == context
    )
    if resting and idle:
        return 0  # backing off: nothing to ask, nothing to write
    grace = grace_seconds(
        grace_s
    )  # before the (heavy) resolution: a bad drop-in value must not respawn the loader
    target = None if resting else _target(name, doc, resolver, now, log)
    degraded = target is None
    if target is None:
        if idle or "target" not in doc:
            _save_if_changed(path, before, doc)
            return 0
        # no usable configuration, yet workers run for a print: the old target still names them, and a printer
        # that cannot be asked is a printer that was lost, so the grace period runs out and they are stopped
        target = Target.from_json(doc["target"])
    chk = Check(
        target,
        _no_configuration if degraded else (probe or PROBES[target.adapter]),
        Workers(target.units(), name, runner),
        grace,
        doc,
        now,
        log,
    )
    chk.run(first="phase" not in doc, stray_due=stray_due())
    snap = chk.snapshot()
    if degraded:
        snap.pop("retry_at")  # the backoff of the failed configuration stays as it is
    elif snap["retry_at"]:
        doc["backoff_context"] = context
    doc.update(snap)
    _save_if_changed(path, before, doc)
    return 0


def _no_configuration(_target: Target) -> bool:
    raise Unreachable("no usable printer configuration")


def _save_if_changed(
    path: str, before: Mapping[str, Any], doc: Mapping[str, Any]
) -> None:
    if dict(doc) != dict(before):
        save_state(path, doc)


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
        elif a == "--grace":
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
    name = names[0]
    if not name.replace("-", "").replace("_", "").replace(
        ".", ""
    ).isalnum() or name.startswith("."):
        raise UsageError(f"{name!r} is not a printer name", command="printer ondemand")
    return name, opts


def _flag_grace(opts: Mapping[str, Any]) -> float | None:
    from errors import UsageError

    if "--grace" not in opts:
        return None
    try:
        return _seconds(float(opts["--grace"]), "--grace", 0.0)
    except ValueError as exc:
        raise UsageError(
            f"--grace must be a number of seconds, got {opts['--grace']!r}",
            command="printer ondemand",
        ) from exc


def _would(printing: bool | None, running: int, total: int) -> str:
    if printing is None:
        return "retry with backoff"
    if printing:
        return "nothing" if running == total else "start the workers"
    return "stop them after the grace period" if running else "nothing"


def _once(target: Target, grace_s: float) -> int:
    printing: bool | None
    try:
        printing, error = PROBES[target.adapter](target), None
    except Unreachable as exc:
        printing, error = None, str(exc)
    states = Workers(target.units(), target.name).states()
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
                "grace_s": grace_s,
            }
        )
    )
    return 0 if printing is not None else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    name, opts = _parse(argv)
    grace = _flag_grace(opts)
    if opts.get("--resolve"):
        try:
            print(json.dumps(resolve(name).to_json()))
        except NotSupervised as exc:
            print(json.dumps({"message": str(exc), "files": list(exc.files)}))
            return EX_CONFIG
        return 0
    if opts.get("--once"):
        from errors import UsageError

        try:
            target = resolve(name)
        except NotSupervised as exc:
            raise UsageError(str(exc), command="printer ondemand") from exc
        return _once(target, grace_seconds(grace))
    return run_check(name, path=state_path(name), grace_s=grace)


def _entry() -> int:
    """`python -m printer_ops.ondemand`: structured errors rendered, not a traceback."""
    try:
        return main(sys.argv[1:])
    except Exception as exc:
        render = getattr(exc, "render", None)
        if render is None:
            raise
        print(render(), file=sys.stderr)
        return int(getattr(exc, "exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(_entry())
