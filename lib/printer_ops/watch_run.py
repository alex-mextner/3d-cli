"""watch_run.py — `3d printer watch`: live streams, reconnects, heartbeat, user systemd service.

WHAT: one thread per printer keeps a websocket open and feeds its detector (watch.py); the
  events go through one Dispatcher (watch_sinks.py) to stdout/exec/steer and the agent
  sessions registered with `3d printer subscribe` (subscriptions.py).
  moonraker       ws://host:port/websocket, JSON-RPC: server.info, printer.objects.list,
                  printer.objects.subscribe (re-sent after notify_klippy_ready),
                  server.files.metadata (once per job file: the G-code body bounds that job
                  progress is measured over, #255), then
                  notify_status_update / notify_gcode_response / notify_klippy_* pushes.
  creality-stock  ws://host:9999 pushes JSON state deltas; "heart_beat" is answered "ok".
  A stream with no data for `silence_s` is treated as dead (Moonraker pushes proc stats every
  second, the K1 heartbeats), then reconnects with exponential backoff (2 s .. 30 s: a printer that is
  switched off is polled at most 30 s apart, so the first answer after power-on is seen at once, #640).

READ-ONLY: the only frames ever sent on the stream are the JSON-RPC queries above and the K1
  heartbeat reply — never a G-code, start, pause or cancel. The single, opt-out exception is
  outside the stream: for a printer with `watch.pause_without_agent` (default on for own
  printers with access full and a camera) a high-confidence critical finding that NO agent could
  judge pauses the print through the adapter (auto_pause.py), once per job, never resume/cancel.

HEARTBEAT: a JSON file (default ~/.local/state/3d-cli/watch/<names>.json) rewritten every
  `heartbeat_s` with per-printer connection/state/last-message age and per-sink counters,
  so a supervisor (or a human) can tell a quiet printer from a dead watcher. Per printer it also
  carries `last_poll_ok` (epoch of the last data from the printer, null before the first),
  `unreachable_since` (epoch while no session is up, else null), `job` ({file, started, adopted}
  while a job is printing/paused, else null; kept as last known while unreachable) and `quiet`
  (the printer is unreachable and its intentional-off marker is set, intentional_off.py).

SERVICE: the user units are shipped and installed by `3d printer supervise install` (supervise_units.py): one
  `3d-printer-watch@NAME` per owned printer running `3d printer watch NAME`, Restart=always, no --dry-run.
"""
from __future__ import annotations

import json
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument, UsageError
from printer_ops import intentional_off
from printer_ops.watch import (
    INFO,
    SEVERITIES,
    CrealityDetector,
    Event,
    MoonrakerDetector,
    WatchLimits,
    moonraker_subscription,
)
from printer_ops.ws import WebSocket, WebSocketError

if TYPE_CHECKING:
    from printer_ops.bed import BedChild
    from printer_ops.devices import Device

# printer-devices.yaml `<name>.watch:` keys and their CLI flags.
# `vision` belongs to `3d printer vision` (issue #150), which shares the same sinks; watch ignores it.
# `bed` starts the bed monitor child (printer_ops/bed.py, issue #303).
WATCH_KEYS = ("agent_fallback", "agent_ack_seconds", "tg_fallback", "message_cooldown_minutes", "pause_without_agent",
              "steer", "exec", "stdout", "min_severity", "stall_minutes", "heat_timeout_minutes", "milestones",
              "dedup_minutes", "vision", "bed")
DEFAULT_AGENT_ACK_S = 90.0
DEFAULT_MESSAGE_COOLDOWN_MIN = 10.0
MAINTAIN_EVERY_S = 300.0   # how often the watcher prunes dead subscriptions (Dispatcher.maintain)
RETRY_FIRST_S = 2.0        # first wait before reconnecting; doubles up to RETRY_MAX_S
RETRY_MAX_S = 30.0         # a powered-off printer is polled at most this far apart
GOOD_SESSION_S = 120.0     # a session that ran this long resets the wait to RETRY_FIRST_S

USAGE = """3d printer watch NAME... | --all [options]
  Subscribe to the printers' live state (read-only) and report anomalies as events:
  state changes (paused/error/cancelled/complete/started), printer exceptions (U1 defect
  detection, e.g. code 2 "detected noodle"), G-code errors (`!!`) and warnings, filament
  runout/motion stop, heaters not reaching or drifting off target, bed target dropped to 0
  while printing/paused, no progress while printing, homing lost while paused/printing,
  Klipper shutdown/disconnect, connection loss. The first reading is reported as a
  `snapshot` plus anything already wrong. Runs until stopped (or --duration).
  A printer that is switched off is not an alarm: the watcher keeps polling it (every 2-30 s), says
  so once (info; warning only if a job was running), and supervises it the moment it answers,
  however a print was started (`print_started` event). The marker
  ~/.local/state/3d-cli/NAME.intentional-off silences even that one event; the first answer clears it.
  Printers with `watch.bed.enabled: true` also get the camera bed monitor (`3d printer bed
  --help`): bed_clear / bed_occupied / plate_present / plate_missing (info) once the parts are
  taken off, put on, or the plate goes missing, and bed_unknown (warning) when the camera cannot
  tell right after a job; nothing is grabbed while a job runs.

Sinks (several at once; per printer also from printer-devices.yaml `<name>.watch:`):
  stdout JSONL (default)  {"ts","printer","kind","severity","text","key","data"} per line
  --exec CMD              run `sh -c CMD` per event: event JSON on stdin, env
                          PRINTER_EVENT_PRINTER/KIND/SEVERITY/TEXT (repeatable)
  --steer TARGET          message into a live omp session (tg-ctl omp-live, like omp-steer):
                          TARGET = sessionId (prefix), pid, or a unique title substring;
                          `all` = every session. Resolved at each delivery (repeatable)
  --no-stdout             no JSONL on stdout
  subscribers             every agent session registered with `3d printer subscribe`
                          (no flag; see `3d printer subscribe --help`)
  printer agent           warning+ events no live agent took go to a temporary omp agent
                          for that printer (`3d printer agent --help`); no flag
  tg (emergency)          warning+ events NO agent could take (no live subscriber, and the printer
                          agent did not start or did not come up within `watch.agent_ack_seconds`,
                          default 90, after one silent restart of the agent) go to the phone as ONE
                          short message per incident (<= 5 lines: headline, `+N related: kinds`, the
                          pause outcome) with the frame: the only case where the watcher messages the
                          user itself. Same-or-lower severity alerts within
                          `watch.message_cooldown_minutes` (default 10) are folded into one `+N more:
                          kinds` line when it ends; a higher severity goes through once. On by default;
                          yaml `watch.tg_fallback: false` turns it off (then nobody is told).
                          At start the watcher sends ONE `[TEST]` message through the same `tg` command
                          line (at most once an hour across restarts) and asserts tg's exit code: a tg
                          that refuses is an `ERROR` line in the log and `failed` / `last_error` of the
                          `tg` sink in the heartbeat file; the watcher keeps running. Not in --dry-run.
  pause (emergency)       before that message, a high-confidence critical finding (verified
                          vision_spaghetti, vision_part_detached; docs/commands/printer-vision.md
                          #pause-without-an-agent) pauses the print if `watch.pause_without_agent`
                          is `critical-only` (the default for own, access: full printers with a
                          camera; `off` disables it). Never resumes or cancels.

Options:
  --all                    every printer in printer-devices.yaml
  --min-severity LEVEL     info | warning | critical (default info)
  --stall-min N            printing without progress for N minutes -> stall (default 15)
  --heat-timeout-min N     heater target not reached after N minutes (default 10)
  --milestones P,P         also report progress milestones, percent (e.g. 50,90)
  --dedup-min N            drop a repeated event key within N minutes (default 10)
  --heartbeat FILE         heartbeat JSON (default ~/.local/state/3d-cli/watch/<names>.json)
  --duration S             stop after S seconds (demo / smoke test)
  --dry-run                steer/exec/subscribers/agent/tg print what they would send (stderr);
                           refused in a service (stderr is the journal): a service never holds alerts
  --install-service        gone: `3d printer supervise install` ships and installs the units

Delivery: events arriving within 2 s are batched into one message; steer sends at most 6
messages per 10 min (critical events always go through), tg at most one per incident. Severity:
critical = needs a decision now (error, exception, homing lost, filament runout while
printing, Klipper shutdown); warning = paused/cancelled/bed off/temperature/stall;
info = started/resumed/complete/snapshot/milestones.

Config (printer-devices.yaml):
  u1:
    adapter: moonraker
    host: 192.168.0.26
    watch: {steer: 01a12cd6, min_severity: info, stall_minutes: 20}   # tg_fallback is on unless false
    # also: agent_ack_seconds: 90, message_cooldown_minutes: 10, pause_without_agent: critical-only | off

Examples:
  3d printer watch u1 --duration 20
  3d printer watch u1 k1se --steer 01a12cd6
  3d printer watch --all --min-severity warning --exec 'logger -t printer'
  3d printer supervise install --all --yes   # the always-on units (watch + vision) for every owned printer
  systemctl --user enable --now 3d-printer-watch@u1"""

_VALUE = {"--exec", "--steer", "--min-severity", "--stall-min", "--heat-timeout-min", "--milestones",
          "--dedup-min", "--heartbeat", "--duration"}
_FLAGS = {"--all", "--no-stdout", "--dry-run", "--install-service"}
_REPEAT = {"--exec", "--steer"}
# Removed when tg became emergency-only. Units installed before still pass them in WATCH_ARGS
# (and old yaml has `tg:`): rejecting them would crash-loop the service and leave the printer
# unwatched, so they are read as `tg_fallback: true` with a warning.
_DEPRECATED_TG = {"--tg", "--tg-always"}


def _log(msg: str) -> None:
    print(f"watch: {msg}", file=sys.stderr, flush=True)


def parse_args(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """-> (printer names, options). opts["tokens"] keeps the option tokens in order (what
    --install-service forwards to the unit, minus --install-service/--all)."""
    names: list[str] = []
    opts: dict[str, Any] = {"--exec": [], "--steer": [], "tokens": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="printer")
            if a in _REPEAT:
                opts[a].append(argv[i + 1])
            else:
                opts[a] = argv[i + 1]
            opts["tokens"] += [a, argv[i + 1]]
            i += 2
            continue
        if a in _DEPRECATED_TG:
            _log(f"{a} is deprecated: tg is now emergency-only (`watch.tg_fallback: true` in "
                 "printer-devices.yaml); treating it as that. Remove it from the unit's WATCH_ARGS.")
            opts["--tg"] = True
            opts["tokens"].append("--tg")   # --install-service keeps the fallback in WATCH_ARGS
        elif a in _FLAGS:
            opts[a] = True
            if a not in ("--install-service", "--all"):
                opts["tokens"].append(a)
        elif a.startswith("--"):
            raise UsageError(f"unknown option {a} for `printer watch`", command="printer",
                             remediation=["See `3d printer watch --help`."])
        else:
            names.append(a)
        i += 1
    if bool(names) == bool(opts.get("--all")):
        raise UsageError("`printer watch` needs printer NAME(s) or --all (not both)", command="printer",
                         remediation=["See `3d printer watch --help`."])
    return names, opts


def _number(value: Any, what: str, *, minimum: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = float("nan")
    if not (math.isfinite(n) and n >= minimum):
        raise InvalidArgument(what, str(value), [f"a finite number >= {minimum:g}"], command="printer")
    return n


def _severity(value: Any, what: str) -> str:
    if value not in SEVERITIES:
        raise InvalidArgument(what, str(value), SEVERITIES, command="printer")
    return str(value)


def _milestones(value: Any, what: str) -> tuple[float, ...]:
    items = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
    out = []
    for v in items:
        p = _number(v, what)
        if not 0 < p <= 100:
            raise InvalidArgument(what, str(value), ["percentages 1-100, e.g. 50,90"], command="printer")
        out.append(p / 100.0)
    return tuple(sorted(out))


def settings_for(device: Device, opts: dict[str, Any]) -> dict[str, Any]:
    """`<name>.watch` from printer-devices.yaml overlaid with CLI flags -> validated settings."""
    cfg = dict(device.watch or {})
    legacy_tg = cfg.pop("tg", None)
    if legacy_tg not in (None, False):
        _log(f"{device.source}: {device.name}.watch.tg is deprecated (tg is emergency-only now); "
             "treating it as `tg_fallback: true` — rename the key.")
    unknown = sorted(set(cfg) - set(WATCH_KEYS))
    if unknown:
        raise InvalidArgument(f"{device.name}.watch", ", ".join(unknown), WATCH_KEYS, command="printer",
                              extra=f"Fix {device.source}.")
    where = f"{device.name}.watch"
    flag_map = {"--min-severity": "min_severity", "--stall-min": "stall_minutes",
                "--heat-timeout-min": "heat_timeout_minutes", "--milestones": "milestones",
                "--dedup-min": "dedup_minutes"}
    for flag, key in flag_map.items():
        if flag in opts:
            cfg[key] = opts[flag]
    sinks: dict[str, Any] = {
        "exec": [*_strs(cfg.get("exec"), f"{where}.exec"), *opts.get("--exec", [])],
        "steer": [*_strs(cfg.get("steer"), f"{where}.steer"), *opts.get("--steer", [])],
        # On unless explicitly false: a printer without the key (a user override replaces the whole
        # fleet entry, `watch:` included) once left the user uninformed when no agent could start.
        "tg_fallback": _bool(cfg.get("tg_fallback", legacy_tg is not False), f"{where}.tg_fallback")
        or legacy_tg not in (None, False) or bool(opts.get("--tg")),
        "stdout": bool(cfg.get("stdout", True)) and not opts.get("--no-stdout"),
    }
    from printer_ops.auto_pause import resolve_policy

    pause_policy = resolve_policy(device, cfg.get("pause_without_agent"))
    if pause_policy != "off" and not sinks["tg_fallback"]:
        if cfg.get("pause_without_agent") is not None:
            raise InvalidArgument(f"{where}.pause_without_agent", str(cfg["pause_without_agent"]),
                                  ["off", f"or tg_fallback: true ({device.name}: a pause nobody is told about)"],
                                  command="printer", extra=f"Fix {device.source}.")
        pause_policy = "off"   # the default never pauses silently
    return {
        "sinks": sinks,
        "pause_policy": pause_policy,
        "agent_ack_s": _number(cfg.get("agent_ack_seconds", DEFAULT_AGENT_ACK_S), f"{where}.agent_ack_seconds",
                               minimum=5.0),
        "agent_fallback": _bool(cfg.get("agent_fallback", True), f"{where}.agent_fallback"),
        "message_cooldown_s": _number(cfg.get("message_cooldown_minutes", DEFAULT_MESSAGE_COOLDOWN_MIN),
                                      f"{where}.message_cooldown_minutes") * 60,
        "min_severity": _severity(cfg.get("min_severity", INFO), f"{where}.min_severity / --min-severity"),
        "limits": WatchLimits(
            stall_s=_number(cfg.get("stall_minutes", 15), "--stall-min", minimum=1) * 60,
            heat_timeout_s=_number(cfg.get("heat_timeout_minutes", 10), "--heat-timeout-min", minimum=1) * 60,
            milestones=_milestones(cfg.get("milestones", []), "--milestones"),
        ),
        "dedup_s": _number(cfg.get("dedup_minutes", 10), "--dedup-min") * 60,
    }


def _strs(value: Any, what: str) -> list[str]:
    if value in (None, False, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise InvalidArgument(what, str(value), ["a string or a list of strings"], command="printer")


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArgument(what, str(value), ["true", "false"], command="printer")
    return value


# ---- heartbeat ------------------------------------------------------------------------------

class Heartbeat:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.started = time.time()
        self.printers: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def update(self, printer: str, **kw: Any) -> None:
        with self.lock:
            self.printers.setdefault(printer, {}).update(kw)

    def snapshot(self, sinks: dict[str, Any], last_events: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            printers = {}
            for name, p in self.printers.items():
                d = dict(p)
                rx = d.pop("last_rx", None)
                d["last_message_age_s"] = round(now - rx, 1) if rx else None
                d["last_event"] = last_events.get(name)
                printers[name] = d
        return {"pid": os.getpid(), "started": self.started, "updated": now, "printers": printers, "sinks": sinks}

    def write(self, sinks: dict[str, Any], last_events: dict[str, Any]) -> None:
        if self.path is None:
            return
        data = self.snapshot(sinks, last_events)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")  # never shared with another watcher
            tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            _log(f"heartbeat {self.path}: {exc}")


# ---- streams --------------------------------------------------------------------------------

@dataclass(frozen=True)
class IntentionalOffMarker:
    """The `NAME.intentional-off` marker as the stream uses it (printer_ops/intentional_off.py).
    A seam: tests hand in recording callables instead of the state dir."""

    is_set: Callable[[str], bool] = intentional_off.is_set
    clear: Callable[[str], bool] = intentional_off.clear_marker


class _Stream:
    """Reconnect loop around `session()` on its own daemon thread; subclasses implement one
    connected session. (Composition, not a Thread subclass: Thread's private attributes,
    e.g. 3.13's `_handle`, must never collide with ours.)"""

    silence_s = 60.0
    tick_s = 5.0

    def __init__(self, device: Device, submit: Callable[[Event], None], stop: threading.Event,
                 heartbeat: Heartbeat, limits: WatchLimits, *, marker: IntentionalOffMarker | None = None,
                 wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.device = device
        self.submit = submit
        self.stop = stop   # the wait between reconnect attempts is `stop.wait(...)`: tests inject a fake clock
        self.heartbeat = heartbeat
        self.marker = marker or IntentionalOffMarker()
        self._wall = wall
        self._mono = mono
        self.detector: MoonrakerDetector | CrealityDetector = self._detector(limits)
        self._last_tick = 0.0
        self._last_rx = 0.0
        self._unreachable_since: float | None = None
        heartbeat.update(device.name, last_poll_ok=None, unreachable_since=None, job=None, quiet=False)
        self.thread = threading.Thread(target=self.run, name=f"watch-{device.name}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def _detector(self, limits: WatchLimits) -> MoonrakerDetector | CrealityDetector:
        raise NotImplementedError

    def session(self) -> None:
        raise NotImplementedError

    def emit(self, events: list[Event]) -> None:
        for e in events:
            self.submit(e)
        frame = self.detector.frame
        if frame is not None:
            self.heartbeat.update(self.device.name, state=frame.state, raw_state=frame.raw_state,
                                  file=frame.filename, progress=frame.progress)
        self.heartbeat.update(self.device.name, job=self.detector.job_info())

    def received(self) -> None:
        self._last_rx = self._mono()
        wall = self._wall()
        self.heartbeat.update(self.device.name, last_rx=wall, last_poll_ok=wall)

    def idle_checks(self) -> None:
        """Called after every recv: periodic detector tick + silence watchdog."""
        now = self._mono()
        if now - self._last_tick >= self.tick_s:
            self._last_tick = now
            self.emit(self.detector.tick(self._wall()))
        if now - self._last_rx > self.silence_s:
            raise WebSocketError(f"no data for {self.silence_s:.0f} s")

    def run(self) -> None:
        """Keep a session up. While the printer is unreachable (switched off between prints) it is
        retried after 2, 4, 8, 16, then every 30 s; the outage is reported once (watch.py) unless the
        printer's intentional-off marker is set, and the first answer clears that marker."""
        backoff = RETRY_FIRST_S
        while not self.stop.is_set():
            started = self._mono()
            try:
                self.session()
                reason = "closed"
            except (OSError, ValueError) as exc:  # WebSocketError is an OSError
                reason = f"{exc.__class__.__name__}: {exc}"
            except Exception as exc:  # a bug in a detector must not end the watcher silently
                reason = f"internal error {exc.__class__.__name__}: {exc}"
                _log(f"{self.device.name}: {reason}")
            if self.stop.is_set():
                break
            now = self._wall()
            if self._unreachable_since is None:
                self._unreachable_since = now
            quiet = self.marker.is_set(self.device.name)
            self.heartbeat.update(self.device.name, connected=False, last_error=reason,
                                  unreachable_since=self._unreachable_since, quiet=quiet)
            self.emit(self.detector.on_connection(False, reason, now, quiet=quiet))
            if self._mono() - started > GOOD_SESSION_S:
                backoff = RETRY_FIRST_S  # it was a working session: retry quickly
            self.stop.wait(backoff)
            backoff = min(backoff * 2, RETRY_MAX_S)

    def _connected(self) -> None:
        now = self._wall()
        self._last_rx = self._last_tick = self._mono()
        self._unreachable_since = None
        self.heartbeat.update(self.device.name, connected=True, last_error=None, last_rx=now,
                              unreachable_since=None, quiet=False)
        self.emit(self.detector.on_connection(True, "", now))
        if self.marker.clear(self.device.name):
            _log(f"{self.device.name}: answered again, cleared the intentional-off marker")


class MoonrakerStream(_Stream):
    _ids = 0               # JSON-RPC request counter (per session)
    _need_sub = False      # (re)subscribe at the next loop turn: connect / notify_klippy_ready
    rpc_timeout_s = 15.0   # a JSON-RPC request without a reply for this long times out

    def _detector(self, limits: WatchLimits) -> MoonrakerDetector:
        self.mr = MoonrakerDetector(self.device.name, limits)
        self._meta_retry_at: dict[tuple[str, int], float] = {}  # (file, job) -> next metadata try
        return self.mr

    def session(self) -> None:
        ws = WebSocket.connect(self.device.host, self.device.port, "/websocket", timeout=10.0)
        try:
            self._ids = 0
            self._connected()
            info = self._call(ws, "server.info")
            klippy = str(info.get("klippy_state", "ready")) if isinstance(info, dict) else "ready"
            if klippy != "ready":  # subscribe once notify_klippy_ready arrives
                self.emit(self.mr.on_klippy(klippy, self._wall()))
            self._need_sub = klippy == "ready"
            while not self.stop.is_set():
                if self._need_sub:
                    self._need_sub = False
                    self._subscribe(ws)
                msg = ws.recv(1.0)
                if msg is not None:
                    self.received()
                    self._on_text(msg)
                    self._fetch_file_body(ws, self.mr.wants_file_body())
                self.idle_checks()
        finally:
            ws.close()

    def _call(self, ws: WebSocket, method: str, params: dict[str, Any] | None = None) -> Any:
        """JSON-RPC request; pushes arriving meanwhile are handled, not lost."""
        self._ids += 1
        rid = self._ids
        req: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            req["params"] = params
        ws.send_text(json.dumps(req))
        deadline = self._mono() + self.rpc_timeout_s
        while (left := deadline - self._mono()) > 0 and not self.stop.is_set():
            msg = ws.recv(min(left, 1.0))
            if msg is None:
                continue
            self.received()
            obj = json.loads(msg)
            if isinstance(obj, dict) and obj.get("id") == rid:
                if "error" in obj:
                    raise _RpcError(f"{method}: {obj['error']}")
                return obj.get("result")
            self._on_rpc(obj)
        raise _RpcTimeout(f"{method}: no reply within {self.rpc_timeout_s:g} s")

    def _subscribe(self, ws: WebSocket) -> None:
        try:
            listed = self._call(ws, "printer.objects.list")
            objects = listed.get("objects", []) if isinstance(listed, dict) else []
            result = self._call(ws, "printer.objects.subscribe", {"objects": moonraker_subscription(objects)})
        except _RpcError as exc:  # klippy not ready: wait for notify_klippy_ready
            _log(f"{self.device.name}: subscribe deferred ({exc})")
            return
        status = result.get("status") if isinstance(result, dict) else None
        if isinstance(status, dict):
            # The subscription is live now: pushes that arrive while a later call waits for its
            # reply are evaluated at once, so the full status goes in first.
            self.emit(self.mr.on_status(status, self._wall(), full=True))
            self._fetch_file_body(ws, self.mr.wants_file_body())

    def _fetch_file_body(self, ws: WebSocket, filename: str | None) -> None:
        """server.files.metadata (read-only) -> the G-code body bounds that make job progress
        exclude a slicer header/footer (#255). Asked once per job; after an error it is asked
        again at most once a minute (whole-file progress meanwhile). Known bounds re-evaluate
        the current status, so held events see the body progress."""
        now = self._mono()
        key = (filename or "", self.mr.jobs)  # a new job with the same file name asks at once
        if not filename or now < self._meta_retry_at.get(key, 0.0):
            return
        try:
            meta = self._call(ws, "server.files.metadata", {"filename": filename})
        except (_RpcError, _RpcTimeout) as exc:  # optional data: never a reason to reconnect
            _log(f"{self.device.name}: no metadata for {filename} ({exc}); whole-file progress, retry in 60 s")
            self._meta_retry_at = {key: now + 60.0}  # only the current job's backoff matters
            return
        if self.mr.jobs != key[1]:
            return  # a new job started while waiting (maybe a re-slice under the same name): ask again
        self.mr.set_file_body(filename, meta)
        if self.mr.file_body is not None and self.mr.file_body[1] is not None:
            self.emit(self.mr.on_status({}, self._wall()))

    def _on_text(self, msg: str) -> None:
        try:
            obj = json.loads(msg)
        except ValueError:
            return
        self._on_rpc(obj)

    def _on_rpc(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        method, params = obj.get("method"), obj.get("params") or []
        now = self._wall()
        d = self.mr
        if method == "notify_status_update" and params and isinstance(params[0], dict):
            self.emit(d.on_status(params[0], now))
        elif method == "notify_gcode_response" and params:
            for line in str(params[0]).splitlines():
                self.emit(d.on_gcode(line, now))
        elif method == "notify_klippy_ready":
            self.emit(d.on_klippy("ready", now))
            self._need_sub = True  # objects can change after a firmware restart
        elif method == "notify_klippy_shutdown":
            self.emit(d.on_klippy("shutdown", now))
        elif method == "notify_klippy_disconnected":
            self.emit(d.on_klippy("disconnected", now))


class _RpcError(Exception):
    pass


class _RpcTimeout(WebSocketError):
    """No reply to a JSON-RPC request within `rpc_timeout_s` (the socket itself still works).
    A WebSocketError, so a required call still ends the session and reconnects."""


class CrealityStream(_Stream):
    silence_s = 120.0

    def _detector(self, limits: WatchLimits) -> CrealityDetector:
        self.k1 = CrealityDetector(self.device.name, limits)
        return self.k1

    def session(self) -> None:
        ws = WebSocket.connect(self.device.host, self.device.ws_port or 9999, "/", timeout=10.0)
        try:
            self._connected()
            d = self.k1
            while not self.stop.is_set():
                msg = ws.recv(1.0)
                if msg is not None:
                    self.received()
                    if "heart_beat" in msg:
                        ws.send_text("ok")
                    else:
                        try:
                            obj = json.loads(msg)
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict):
                            self.emit(d.on_push(obj, self._wall()))
                self.idle_checks()
        finally:
            ws.close()


def stream_for(device: Device, submit: Callable[[Event], None], stop: threading.Event, heartbeat: Heartbeat,
               limits: WatchLimits) -> _Stream:
    cls = MoonrakerStream if device.adapter == "moonraker" else CrealityStream
    return cls(device, submit, stop, heartbeat, limits)


# ---- entry ----------------------------------------------------------------------------------

def default_heartbeat(names: list[str]) -> Path:
    from cli.paths import state_dir

    return state_dir() / "watch" / ("+".join(names) + ".json")  # '+' never occurs in a printer name


def _bed_children(devices: list[Device], submit: Callable[[Event], None], stop: threading.Event,
                  heartbeat: Heartbeat) -> list[BedChild]:
    """One started `bed_state.py monitor` child per Moonraker printer with watch.bed.enabled."""
    from printer_ops.bed import BedChild, bed_config

    out = []
    for d in devices:
        if not bed_config(d).enabled:
            continue
        if d.adapter != "moonraker":
            _log(f"{d.name}: watch.bed needs a Moonraker printer (camera + toolhead Z); adapter {d.adapter} skipped")
            continue
        child = BedChild(d.name, submit, stop, on_state=lambda name, s: heartbeat.update(name, bed=s))
        child.start()
        out.append(child)
    return out


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    names, opts = parse_args(argv)
    from printer_ops.devices import get_device, load_devices

    if opts.get("--all"):
        names = sorted(load_devices())
        if not names:
            get_device("-")  # raises the "no printers configured" guidance
    if opts.get("--install-service"):
        raise UsageError(
            "`--install-service` is gone: the watch and vision units are shipped from the repo and installed for "
            "every owned printer by one command (a second writer of 3d-printer-watch@.service would undo it)",
            command="printer",
            remediation=["3d printer supervise install --all --plan   # what it would do",
                         "3d printer supervise install --all --yes    # templates, enable --now, linger",
                         "docs/commands/printer.md#always-on-supervision"])
    devices = [get_device(n) for n in names]
    settings = {d.name: settings_for(d, opts) for d in devices}
    from printer_ops.bed import bed_config

    for d in devices:
        bed_config(d)  # a broken watch.bed fails here, before any stream starts
    duration = _number(opts["--duration"], "--duration") if "--duration" in opts else None

    from printer_ops.watch_sinks import Dispatcher

    dispatcher = Dispatcher()
    for d in devices:
        dispatcher.add_settings(d.name, settings[d.name], dry_run=bool(opts.get("--dry-run")))
    heartbeat = Heartbeat(Path(opts["--heartbeat"]) if "--heartbeat" in opts else default_heartbeat(names))
    stop = threading.Event()
    dispatcher.maintain()   # prune subscriptions dead for too long before the first alert
    dispatcher.start_startup_test(names, "watch")   # a tg that refuses every message must be known now, not at the first alert
    next_maintain = time.monotonic() + MAINTAIN_EVERY_S

    def _term(*_: Any) -> None:
        stop.set()

    if threading.current_thread() is threading.main_thread():  # signal() raises anywhere else
        signal.signal(signal.SIGTERM, _term)
    dispatcher.start()
    streams = [stream_for(d, dispatcher.submit, stop, heartbeat, settings[d.name]["limits"]) for d in devices]
    for st in streams:
        st.start()
    beds = _bed_children(devices, dispatcher.submit, stop, heartbeat)
    _log(f"watching {', '.join(names)} (sinks: {', '.join(dispatcher.routes)}; heartbeat {heartbeat.path}"
         + (f"; bed monitor: {', '.join(b.printer for b in beds)}" if beds else "") + ")")
    deadline = time.monotonic() + duration if duration is not None else None
    try:
        while not stop.is_set():
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                break
            stop.wait(min(15.0, left) if left is not None else 15.0)
            heartbeat.write(dispatcher.stats(), dispatcher.last_event)
            if time.monotonic() >= next_maintain:
                next_maintain = time.monotonic() + MAINTAIN_EVERY_S
                dispatcher.maintain()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for st in streams:
            st.join(5.0)
        for b in beds:
            b.close()
        dispatcher.close()
        heartbeat.write(dispatcher.stats(), dispatcher.last_event)
    return 0


__all__ = ["USAGE", "WATCH_KEYS", "Heartbeat", "parse_args", "run", "settings_for"]
