"""watch.py — turn a printer's live state stream into normalized anomaly events.

WHAT: `Event` (one thing worth telling a human or an agent: printer, kind, severity, one-line
  text, data, dedup key) and two detectors that consume what the printer pushes:
  `MoonrakerDetector` (Moonraker websocket: notify_status_update deltas, gcode responses,
  klippy notifications) and `CrealityDetector` (Creality stock ws:9999 JSON pushes). Both
  are pure: feed payloads + a clock in, get events out, so tests replay recorded/synthetic
  payloads without a socket. Transport, reconnects and delivery live in watch_run.py and
  watch_sinks.py. Issue https://github.com/alex-mextner/3d-cli/issues/145.

WHY: a paused or failed print (U1 noodle detection pause, a macro error that cancels the
  job, a bed heater switched off while paused) was only noticed when someone polled
  `3d printer status`, sometimes hours later.

EVENTS are edge-triggered: a condition fires once when it appears and re-arms when it
  clears; state transitions carry a counter in their key so a second pause in the same job
  is a new event. Kinds: snapshot, state, exception, gcode_error, gcode_warning, filament,
  temperature, bed_off, stall, homing_lost, klippy, connection, milestone.

SNAPSHOT: the first status after start is reported as an info `snapshot` plus events for
  anomalies already present (a paused job, an exception, a lost homing, a cold bed while
  paused), so a freshly started watcher reports what is wrong right now.

INVARIANTS: stdlib only; never sends anything to a printer (read-only by construction).
"""
from __future__ import annotations

import datetime
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from printer_ops.base import (
    BUSY,
    CANCELLED,
    COMPLETE,
    ERROR,
    IDLE,
    PAUSED,
    PRINTING,
    UNKNOWN,
)

INFO, WARNING, CRITICAL = "info", "warning", "critical"
SEVERITIES = (INFO, WARNING, CRITICAL)

JOB_ACTIVE = frozenset({PRINTING, PAUSED})


def severity_rank(severity: str) -> int:
    """info 0 < warning 1 < critical 2; ValueError for anything else."""
    return SEVERITIES.index(severity)


@dataclass
class Event:
    printer: str
    kind: str
    severity: str
    text: str                      # one line: what happened + context + what to decide
    data: dict[str, Any] = field(default_factory=dict)
    key: str = ""                  # dedup key: same key within the dedup window is dropped
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        stamp = datetime.datetime.fromtimestamp(self.ts, datetime.timezone.utc).astimezone()
        return {"ts": stamp.isoformat(timespec="seconds"), "printer": self.printer, "kind": self.kind,
                "severity": self.severity, "text": self.text, "key": self.key or f"{self.printer}:{self.kind}",
                "data": self.data}


@dataclass(frozen=True)
class WatchLimits:
    """Thresholds for the time-based checks (seconds / degrees C)."""

    stall_s: float = 900.0         # printing, but no progress for this long
    heat_timeout_s: float = 600.0  # a heater target set this long ago and still not reached
    reach_tol_c: float = 8.0       # |temp - target| within this counts as reached
    drift_c: float = 15.0          # after reaching: off target by more than this ...
    drift_hold_s: float = 60.0     # ... for this long is a drift
    milestones: tuple[float, ...] = ()  # progress fractions (0..1) to report, e.g. (0.5, 0.9)
    # A job's end macro (END_PRINT / PRINT_END / CANCEL_PRINT) turns the bed off and disables the
    # motors (M84) while the firmware still says printing/paused. Bed-off and homing-lost seen
    # during a job are therefore held this long and dropped if the job ends meanwhile, and
    # ignored outright at or above `end_progress`.
    confirm_s: float = 10.0
    end_progress: float = 0.99
    # The start macro does the same in reverse: K1 stock START_PRINT zeroes the bed and nozzle
    # targets for ~30 s of probing/calibration and homes again. In the start phase (progress at
    # most `start_progress`, or less than `start_phase_s` after "started printing") the hold is
    # `start_confirm_s`, and a target restored or axes re-homed meanwhile drops the event.
    start_confirm_s: float = 90.0
    start_progress: float = 0.01
    start_phase_s: float = 600.0


@dataclass
class Frame:
    """One normalized reading of a printer, built by each detector from its own payload."""

    state: str
    raw_state: str
    filename: str | None = None
    progress: float | None = None
    layer: int | None = None
    total_layer: int | None = None
    heaters: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)  # name -> (temp, target)
    homed: str | None = None       # homed axes, e.g. "xyz"; None when the firmware does not say
    stall_marker: Any = None       # changes whenever the job makes progress
    message: str | None = None     # firmware's own job message (print_stats.message, err text)


@dataclass
class _Heater:
    target: float = 0.0
    since: float = 0.0             # when the current target was set
    reached: bool = False
    off_since: float | None = None  # drift start after reaching
    reach_alerted: bool = False     # "not reaching" reported for this target
    drift_alerted: bool = False     # drift reported; re-armed when back within tolerance


def _short(name: str | None, n: int = 48) -> str:
    if not name:
        return "-"
    return name if len(name) <= n else name[: n - 1] + "…"


def _fmt_t(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}"


def _heater_off(name: str) -> Callable[[Frame], bool]:
    """Still-true check for a held bed_off: the heater still reports a 0 target. A heater
    missing from the frame (objects reset after a firmware restart) is unknown, not off."""
    def check(f: Frame) -> bool:
        reading = f.heaters.get(name)
        return reading is not None and not reading[1]
    return check


def _axes_missing(homed: str | None) -> str:
    """Axes of X/Y/Z missing from `homed_axes` ("" when all homed or when homing is unknown)."""
    return "" if homed is None else "".join(sorted(set("xyz") - set(homed.lower())))


def _unhomed(f: Frame) -> bool:
    """Still-true check for a held homing_lost."""
    return bool(_axes_missing(f.homed))


@dataclass
class _Held:
    """A job-time event awaiting confirmation (see WatchLimits.confirm_s)."""

    since: float
    hold_s: float
    event: Event
    still_true: Callable[[Frame], bool]


class _Detector:
    """Generic, firmware-independent rules over successive Frames."""

    def __init__(self, printer: str, limits: WatchLimits | None = None) -> None:
        self.printer = printer
        self.limits = limits or WatchLimits()
        self.frame: Frame | None = None   # last frame evaluated
        self._transitions = 0
        self._heaters: dict[str, _Heater] = {}
        self._stall_since: float | None = None
        self._stall_alerted = False
        self._milestones_done: set[float] = set()
        self._reason: tuple[str, float] | None = None  # recent error text explaining a pause/cancel
        self._seq = 0
        self.connected: bool | None = None
        self.last_message: float | None = None
        self._job_state: str | None = None  # last printing/paused/idle/... state (never unknown/busy)
        self._held: dict[str, _Held] = {}  # kind -> event awaiting confirmation
        self._job_started: float | None = None  # when this watcher saw the job start printing

    # ---- helpers ------------------------------------------------------------------------
    def _context(self, f: Frame | None = None) -> dict[str, Any]:
        f = f or self.frame
        if f is None:
            return {}
        ctx: dict[str, Any] = {"state": f.state, "raw_state": f.raw_state, "file": f.filename}
        if f.progress is not None:
            ctx["progress"] = round(f.progress, 4)
        if f.layer is not None:
            ctx["layer"] = f.layer
        if f.total_layer:
            ctx["total_layer"] = f.total_layer
        return ctx

    def _where(self, f: Frame | None = None) -> str:
        f = f or self.frame
        if f is None:
            return ""
        bits = []
        if f.layer is not None and f.total_layer:
            bits.append(f"layer {f.layer}/{f.total_layer}")
        if f.progress is not None:
            bits.append(f"{f.progress * 100:.0f}%")
        if f.filename:
            bits.append(_short(f.filename))
        return ", ".join(bits)

    def _event(self, kind: str, severity: str, headline: str, *, hint: str = "", key: str = "",
               now: float | None = None, data: dict[str, Any] | None = None) -> Event:
        where = self._where()
        text = f"{self.printer}: {headline}"
        if where:
            text += f" ({where})"
        if hint:
            text += f" -> {hint}"
        if not key:
            self._seq += 1
            key = f"{self.printer}:{kind}:{self._seq}"
        return Event(self.printer, kind, severity, text, {**self._context(), **(data or {})}, key,
                     ts=now if now is not None else time.time())

    def _note_reason(self, text: str, now: float) -> None:
        self._reason = (text, now)

    def _recent_reason(self, now: float, f: Frame) -> str | None:
        if self._reason and now - self._reason[1] <= 300:
            return self._reason[0]
        return f.message or None

    # ---- the generic rules --------------------------------------------------------------
    def _evaluate(self, f: Frame, now: float) -> list[Event]:
        prev, self.frame = self.frame, f  # events describe the new frame
        out: list[Event] = []
        if prev is None:
            out.append(self._snapshot_event(f, now))
        out += self._state_rules(f, now)
        out += self._homing_rules(prev, f, now)
        out += self._heater_rules(prev, f, now)
        out += self._progress_rules(prev, f, now)
        out += self._release_held(now)
        return out

    def _at_end(self, f: Frame) -> bool:
        """The job is in its end macro: still printing/paused by state, but at `end_progress`."""
        return f.state in JOB_ACTIVE and f.progress is not None and f.progress >= self.limits.end_progress

    def _in_start_phase(self, f: Frame, now: float) -> bool:
        """Start macro window: progress at most `start_progress`, or soon after this watcher saw
        the job start. Unknown progress alone is not a start phase."""
        lim = self.limits
        seen_start = self._job_started is not None and now - self._job_started < lim.start_phase_s
        return seen_start or (f.progress is not None and f.progress <= lim.start_progress)

    def _hold(self, kind: str, event: Event, still_true: Callable[[Frame], bool], f: Frame, now: float,
              *, found_at_start: bool) -> list[Event]:
        """A job-time anomaly: dropped at the very end of a print (end macro), reported at once
        when found at watch start (a steady state), otherwise confirmed after the hold."""
        if self._at_end(f):
            return []
        if found_at_start:
            return [event]
        hold_s = self.limits.start_confirm_s if self._in_start_phase(f, now) else self.limits.confirm_s
        self._held[kind] = _Held(now, hold_s, event, still_true)
        return []

    def _release_held(self, now: float) -> list[Event]:
        out: list[Event] = []
        f = self.frame
        for kind, held in list(self._held.items()):
            if f is None or f.state not in JOB_ACTIVE or self._at_end(f) or not held.still_true(f):
                del self._held[kind]  # the job ended or reached its end macro, or the condition cleared
            elif now - held.since >= held.hold_s:
                del self._held[kind]
                out.append(held.event)
        return out

    def _snapshot_event(self, f: Frame, now: float) -> Event:
        temps = " ".join(f"{k} {_fmt_t(a)}/{_fmt_t(t)}" for k, (a, t) in f.heaters.items()
                         if (t or 0) > 0 or k in ("bed", "heater_bed"))
        head = f"watching: {f.state} [{f.raw_state}]"
        if temps:
            head += f"; {temps}"
        return self._event("snapshot", INFO, head, key=f"{self.printer}:snapshot:{now:.0f}", now=now,
                           data={"heaters": {k: {"temp": a, "target": t} for k, (a, t) in f.heaters.items()}})

    def _state_rules(self, f: Frame, now: float) -> list[Event]:
        if f.state in (UNKNOWN, BUSY):
            return []  # klippy/connection events explain these; keep the last real job state
        before = self._job_state
        if f.state == before:
            return []
        self._job_state = f.state
        reason = self._recent_reason(now, f)
        why = f" (last error: {reason})" if reason else ""
        if before is None:
            # Watch start: only a job that is stuck right now is news; the snapshot covers the rest.
            if f.state == PAUSED:
                return [self._state_event(None, f, WARNING, "is PAUSED" + why,
                                          "check the camera, then resume or cancel", now)]
            if f.state == ERROR:
                return [self._state_event(None, f, CRITICAL, "is in ERROR" + why, "see `3d printer status`", now)]
            return []
        if f.state == PRINTING:
            if before == PAUSED:
                return [self._state_event(before, f, INFO, "resumed printing", "", now)]
            self._milestones_done.clear()
            self._reason = None
            self._job_started = now
            return [self._state_event(before, f, INFO, "started printing", "", now)]
        if f.state == PAUSED:
            return [self._state_event(before, f, WARNING, "PAUSED" + why, "check the camera, then resume or cancel", now)]
        if f.state == ERROR:
            return [self._state_event(before, f, CRITICAL, "ERROR" + why, "the job stopped; inspect before reprinting", now)]
        if f.state == CANCELLED:
            sev = WARNING if before in JOB_ACTIVE else INFO
            return [self._state_event(before, f, sev, "CANCELLED" + why, "", now)]
        if f.state == COMPLETE:
            return [self._state_event(before, f, INFO, "print COMPLETE", "remove the part to free the bed", now)]
        if f.state == IDLE and before in JOB_ACTIVE:
            return [self._state_event(before, f, WARNING, f"job ended without completing (now {f.raw_state})" + why,
                                      "", now)]
        return []

    def _state_event(self, before: str | None, f: Frame, severity: str, headline: str, hint: str,
                     now: float) -> Event:
        self._transitions += 1
        return self._event("state", severity, headline, hint=hint, now=now,
                           key=f"{self.printer}:state:{before}->{f.state}:{self._transitions}",
                           data={"from": before, "to": f.state, "reason": self._recent_reason(now, f)})

    def _homing_rules(self, prev: Frame | None, f: Frame, now: float) -> list[Event]:
        if f.state not in JOB_ACTIVE:
            return []
        lost = _axes_missing(f.homed)
        if not lost:
            return []
        was = prev.homed if prev is not None else None
        if prev is not None and (was is None or _axes_missing(was)):
            return []  # already reported or held (or never known as homed)
        event = self._event("homing_lost", CRITICAL, f"{f.state} but axes {lost.upper()} are NOT homed "
                            f"(homed_axes {f.homed!r})", hint="do not resume blindly: re-home first (Z/X crash risk)",
                            now=now, data={"homed_axes": f.homed, "was": was})
        # Found at watch start while paused: a steady state, report now. While printing it may
        # be a start macro re-homing, so it is held like any other.
        return self._hold("homing_lost", event, _unhomed, f, now, found_at_start=prev is None and f.state == PAUSED)

    def _heater_rules(self, prev: Frame | None, f: Frame, now: float) -> list[Event]:
        out: list[Event] = []
        lim = self.limits
        for name, (temp, target) in f.heaters.items():
            tgt = float(target or 0.0)
            h = self._heaters.get(name)
            if h is None:
                h = self._heaters[name] = _Heater(target=tgt, since=now,
                                                  reached=tgt > 0 and temp is not None and abs(temp - tgt) <= lim.reach_tol_c)
                if prev is None and self._is_bed(name) and tgt == 0 and f.state == PAUSED:
                    out += self._hold("bed_off", self._bed_off_event(name, None, now), _heater_off(name),
                                      f, now, found_at_start=True)
                continue
            if tgt != h.target:
                old = h.target
                if self._is_bed(name) and old > 0 and tgt == 0 and f.state in JOB_ACTIVE:
                    out += self._hold("bed_off", self._bed_off_event(name, old, now), _heater_off(name),
                                      f, now, found_at_start=False)
                h.target, h.since, h.reached, h.off_since = tgt, now, False, None
                h.reach_alerted = h.drift_alerted = False
            if tgt <= 0 or temp is None:
                continue
            delta = temp - tgt
            if abs(delta) <= lim.reach_tol_c:
                h.reached, h.off_since, h.drift_alerted = True, None, False
                continue
            if not h.reached:
                # Only heating can fail to reach: a parked U1 head cooling from 200 to a 40 C
                # standby target takes long and is normal.
                if not h.reach_alerted and delta < 0 and now - h.since >= lim.heat_timeout_s:
                    h.reach_alerted = True
                    out.append(self._event(
                        "temperature", WARNING,
                        f"{name} not reaching target: {temp:.0f}/{tgt:.0f}C after {(now - h.since) / 60:.0f} min",
                        hint="check heater/thermistor/fan", now=now,
                        key=f"{self.printer}:temperature:{name}:not_reaching:{tgt:.0f}",
                        data={"heater": name, "temp": temp, "target": tgt}))
                continue
            if abs(delta) > lim.drift_c:
                if h.off_since is None:
                    h.off_since = now
                elif not h.drift_alerted and now - h.off_since >= lim.drift_hold_s:
                    h.drift_alerted = True
                    sev = CRITICAL if f.state == PRINTING else WARNING
                    out.append(self._event(
                        "temperature", sev, f"{name} drifted off target: {temp:.0f}/{tgt:.0f}C",
                        hint="check heater/thermistor, part cooling, enclosure", now=now,
                        key=f"{self.printer}:temperature:{name}:drift:{tgt:.0f}:{h.off_since:.0f}",
                        data={"heater": name, "temp": temp, "target": tgt}))
            else:
                h.off_since = None
        return out

    @staticmethod
    def _is_bed(name: str) -> bool:
        return name in ("bed", "heater_bed")

    def _bed_off_event(self, name: str, old: float | None, now: float) -> Event:
        was = f" (was {old:.0f}C)" if old else ""
        return self._event("bed_off", WARNING, f"bed heater target 0 while {self.frame.state if self.frame else '?'}{was}",
                           hint="the part may cool and detach; reheat the bed before resuming", now=now,
                           data={"heater": name, "was_target": old})

    def _progress_rules(self, prev: Frame | None, f: Frame, now: float) -> list[Event]:
        out: list[Event] = []
        if f.state != PRINTING:
            self._stall_since, self._stall_alerted = None, False
        else:
            changed = prev is None or prev.state != PRINTING or f.stall_marker != prev.stall_marker
            if changed or self._stall_since is None:
                if changed and self._stall_alerted:
                    out.append(self._event("stall", INFO, "progress moving again", now=now,
                                           key=f"{self.printer}:stall:cleared:{now:.0f}"))
                self._stall_since, self._stall_alerted = now, False
        out += self._stall_check(now)
        if f.progress is not None and f.state == PRINTING and prev is not None:
            for m in self.limits.milestones:
                if m not in self._milestones_done and f.progress >= m:
                    self._milestones_done.add(m)
                    if prev.progress is not None and prev.progress < m:
                        out.append(self._event("milestone", INFO, f"reached {m * 100:.0f}%", now=now,
                                               key=f"{self.printer}:milestone:{m}:{_short(f.filename)}"))
        return out

    def _stall_check(self, now: float) -> list[Event]:
        if self._stall_since is None or self._stall_alerted:
            return []
        if now - self._stall_since < self.limits.stall_s:
            return []
        self._stall_alerted = True
        return [self._event("stall", WARNING, f"printing but no progress for {(now - self._stall_since) / 60:.0f} min",
                            hint="check the camera: stuck heating, jam or a hung macro?", now=now,
                            key=f"{self.printer}:stall:{self._stall_since:.0f}")]

    # ---- time and connection --------------------------------------------------------------
    def tick(self, now: float) -> list[Event]:
        """Time-based checks between pushes (stall, heaters that never report again)."""
        if self.frame is None:
            return []
        return self._heater_rules(self.frame, self.frame, now) + self._stall_check(now) + self._release_held(now)

    def on_connection(self, up: bool, reason: str, now: float) -> list[Event]:
        was = self.connected
        self.connected = up
        if up:
            if was is False:
                head = "reconnected" if self.frame is not None else "connected (was unreachable at start)"
                return [self._event("connection", INFO, head, now=now, key=f"{self.printer}:connection:up:{now:.0f}")]
            return []
        if was is False:
            return []  # still down: reported once
        active = self.frame is not None and self.frame.state in JOB_ACTIVE
        sev = WARNING if active or was is None else INFO
        head = "cannot connect" if was is None else "connection lost"
        hint = "a job was running: check the printer" if active else ""
        return [self._event("connection", sev, f"{head}: {reason}", hint=hint, now=now,
                            key=f"{self.printer}:connection:down:{now:.0f}")]


# ---- Moonraker / Klipper ---------------------------------------------------------------------

_MR_STATE = {"standby": IDLE, "printing": PRINTING, "paused": PAUSED, "complete": COMPLETE,
             "cancelled": CANCELLED, "error": ERROR}
_SENSOR_PREFIXES = ("filament_switch_sensor ", "filament_motion_sensor ")


def moonraker_subscription(objects: list[str]) -> dict[str, list[str] | None]:
    """`printer.objects.list` result -> the `printer.objects.subscribe` objects map.

    Field lists keep the stream small (heaters push temperature ~4x/s)."""
    sub: dict[str, list[str] | None] = {
        "webhooks": ["state", "state_message"],
        "print_stats": ["state", "filename", "message", "info", "exception"],
        "pause_resume": ["is_paused"],
        "virtual_sdcard": ["progress", "file_position", "is_active"],
        "display_status": ["progress", "message"],
        "idle_timeout": ["state"],
    }
    optional = {"toolhead": ["homed_axes"], "exception_manager": ["exceptions"]}
    sub.update({k: v for k, v in optional.items() if k in objects})
    for name in objects:
        if name == "heater_bed" or name == "extruder" or (name.startswith("extruder") and name[8:].isdigit()):
            sub[name] = ["temperature", "target"]
        elif name.startswith(_SENSOR_PREFIXES):
            sub[name] = ["filament_detected", "enabled"]
    return sub


def _merge(dst: dict[str, Any], delta: dict[str, Any]) -> None:
    for obj, fields in delta.items():
        if isinstance(fields, dict) and isinstance(dst.get(obj), dict):
            for k, v in fields.items():
                if isinstance(v, dict) and isinstance(dst[obj].get(k), dict) and k != "exception":
                    dst[obj][k].update(v)
                else:
                    dst[obj][k] = v
        else:
            dst[obj] = fields


def _exc_id(exc: dict[str, Any]) -> str:
    return f"{exc.get('id', '')}:{exc.get('index', '')}:{exc.get('code', '')}:{exc.get('message', '')}"


class MoonrakerDetector(_Detector):
    """Moonraker websocket stream -> events. Feed `on_status` with the subscribe result and every
    notify_status_update delta, `on_gcode` with notify_gcode_response lines, `on_klippy` with
    klippy notifications."""

    def __init__(self, printer: str, limits: WatchLimits | None = None) -> None:
        super().__init__(printer, limits)
        self.status: dict[str, Any] = {}
        self.klippy: str | None = None
        self._exceptions_seen: set[str] = set()
        self._sensors: dict[str, bool] = {}

    def frame_from_status(self) -> Frame:
        s = self.status
        ps = s.get("print_stats") or {}
        raw = str(ps.get("state") or "?")
        klippy = (s.get("webhooks") or {}).get("state", "ready")
        state = _MR_STATE.get(raw, UNKNOWN) if klippy == "ready" else UNKNOWN
        vsd = s.get("virtual_sdcard") or {}
        progress = vsd.get("progress")
        if progress is None:
            progress = (s.get("display_status") or {}).get("progress")
        info_obj = ps.get("info")
        info: dict[str, Any] = info_obj if isinstance(info_obj, dict) else {}
        heaters: dict[str, tuple[float | None, float | None]] = {}
        for name, obj in s.items():
            if (name == "heater_bed" or name.startswith("extruder")) and isinstance(obj, dict) and "target" in obj:
                heaters["bed" if name == "heater_bed" else name] = (obj.get("temperature"), obj.get("target"))
        toolhead = s.get("toolhead")
        homed = toolhead.get("homed_axes") if isinstance(toolhead, dict) and "homed_axes" in toolhead else None
        return Frame(
            state=state, raw_state=raw if klippy == "ready" else f"klippy:{klippy}",
            filename=ps.get("filename") or None,
            progress=float(progress) if progress is not None else None,
            layer=info.get("current_layer"), total_layer=info.get("total_layer"),
            heaters=heaters, homed=homed,
            stall_marker=(vsd.get("file_position"), progress, info.get("current_layer")),
            message=ps.get("message") or None,
        )

    def on_status(self, delta: dict[str, Any], now: float, *, full: bool = False) -> list[Event]:
        """A notify_status_update delta, or (full=True) a subscribe result, which replaces the
        whole picture: objects gone after a firmware restart must not linger."""
        self.last_message = now
        if full:
            self.status = {}
        _merge(self.status, delta)
        out: list[Event] = []
        out += self._klippy_from_webhooks(now)
        out += self._exception_rules(now)
        f = self.frame_from_status()
        out += self._sensor_rules(f, now)
        out += self._evaluate(f, now)
        return out

    def _klippy_from_webhooks(self, now: float) -> list[Event]:
        hooks = self.status.get("webhooks") or {}
        state = hooks.get("state")
        if not state:
            return []
        return self.on_klippy(str(state), now, message=hooks.get("state_message"))

    def on_klippy(self, state: str, now: float, message: str | None = None) -> list[Event]:
        """Klippy state: ready | startup | shutdown | error | disconnected."""
        before = self.klippy
        self.klippy = state
        if state == before:
            return []
        if state == "ready":
            if before is None or before == "startup":
                return []
            return [self._event("klippy", INFO, f"Klipper ready again (was {before})", now=now,
                                key=f"{self.printer}:klippy:ready:{now:.0f}")]
        if state == "startup":
            return []
        active = self.frame is not None and self.frame.state in JOB_ACTIVE
        sev = CRITICAL if state in ("shutdown", "error") or active else WARNING
        msg = (message or "").strip().splitlines()[0] if message else ""
        if msg:
            self._note_reason(f"Klipper {state}: {msg}", now)
        return [self._event("klippy", sev, f"Klipper {state.upper()}" + (f": {msg}" if msg else ""),
                            hint="the job is lost if it was running; FIRMWARE_RESTART only after a look" if active else "",
                            now=now, key=f"{self.printer}:klippy:{state}:{msg}", data={"klippy": state, "message": message})]

    def _exception_rules(self, now: float) -> list[Event]:
        out: list[Event] = []
        current: list[dict[str, Any]] = []
        exc = (self.status.get("print_stats") or {}).get("exception")
        if isinstance(exc, dict) and exc:
            current.append(exc)
        listed = (self.status.get("exception_manager") or {}).get("exceptions") or []
        current.extend(e for e in listed if isinstance(e, dict) and e)
        ids = {_exc_id(e) for e in current}
        for e in current:
            eid = _exc_id(e)
            if eid in self._exceptions_seen:
                continue
            self._exceptions_seen.add(eid)
            code, msg, level = e.get("code"), str(e.get("message") or "").strip(), e.get("level")
            text = f"{msg or 'exception'} (code {code}" + (f", id {e['id']}" if e.get("id") is not None else "") + ")"
            self._note_reason(text, now)
            sev = WARNING if isinstance(level, int) and level <= 1 else CRITICAL
            out.append(self._event("exception", sev, f"printer exception: {text}",
                                   hint="check the camera; resume only after fixing the cause", now=now,
                                   key=f"{self.printer}:exception:{eid}", data={"exception": e}))
        self._exceptions_seen &= ids  # a cleared exception re-arms
        return out

    def _sensor_rules(self, f: Frame, now: float) -> list[Event]:
        out: list[Event] = []
        for name, obj in self.status.items():
            if not name.startswith(_SENSOR_PREFIXES) or not isinstance(obj, dict):
                continue
            detected = obj.get("filament_detected")
            if not isinstance(detected, bool):
                continue
            before = self._sensors.get(name)
            self._sensors[name] = detected
            if before is True and detected is False and obj.get("enabled", True) and f.state in JOB_ACTIVE:
                short = name.split(" ", 1)[1]
                kind = "motion stopped" if name.startswith("filament_motion") else "runout"
                self._note_reason(f"filament {kind} on {short}", now)
                out.append(self._event("filament", CRITICAL if f.state == PRINTING else WARNING,
                                       f"filament {kind} on {short}",
                                       hint="reload/clear the filament path before resuming", now=now,
                                       data={"sensor": name}))
        return out

    def on_gcode(self, line: str, now: float) -> list[Event]:
        """One notify_gcode_response line."""
        self.last_message = now
        text = line.strip()
        low = text.lower()
        active = self.frame is not None and self.frame.state in JOB_ACTIVE
        if text.startswith("!!"):
            body = text.lstrip("!").strip()
            self._note_reason(body.splitlines()[0][:300], now)
            return [self._event("gcode_error", CRITICAL if active else WARNING, f"G-code error: {_short(body, 220)}",
                                now=now, key=f"{self.printer}:gcode_error:{body[:200]}", data={"line": text})]
        if low.startswith("// warning") or low.startswith("warning:") or low.startswith("// !!"):
            body = text.lstrip("/ ").strip()
            return [self._event("gcode_warning", WARNING, f"G-code warning: {_short(body, 220)}", now=now,
                                key=f"{self.printer}:gcode_warning:{body[:200]}", data={"line": text})]
        return []


# ---- Creality stock (K1 family, ws:9999) -----------------------------------------------------

class CrealityDetector(_Detector):
    """Creality ws:9999 JSON pushes (deltas, later keys win) -> events."""

    def __init__(self, printer: str, limits: WatchLimits | None = None) -> None:
        super().__init__(printer, limits)
        self.state: dict[str, Any] = {}
        self._err: int | None = None
        self._material: int | None = None

    def frame_from_state(self) -> Frame:
        from printer_ops.creality import parse_status

        st = parse_status(self.state)
        s = self.state
        homed = None
        auto = s.get("autohome")
        if isinstance(auto, str) and ":" in auto:
            homed = "".join(p.split(":")[0].lower() for p in auto.split() if p.endswith(":1"))
        layer, total = s.get("layer"), s.get("TotalLayer")
        return Frame(
            state=st.state, raw_state=st.raw_state, filename=st.filename, progress=st.progress,
            layer=layer if isinstance(layer, int) else None,
            total_layer=total if isinstance(total, int) and total > 0 else None,
            heaters=dict(st.temps), homed=homed,
            stall_marker=(s.get("printProgress"), s.get("layer"), s.get("usedMaterialLength")),
            message=st.message,
        )

    def on_push(self, obj: dict[str, Any], now: float) -> list[Event]:
        self.last_message = now
        self.state.update(obj)
        out: list[Event] = []
        f = self.frame_from_state()
        out += self._err_rules(now)
        out += self._material_rules(f, now)
        out += self._evaluate(f, now)
        return out

    def _err_rules(self, now: float) -> list[Event]:
        err = self.state.get("err")
        code = err.get("errcode") if isinstance(err, dict) else err
        try:
            code = int(code or 0)
        except (TypeError, ValueError):
            code = 0
        before, self._err = self._err, code
        if not code or code == before:
            return []
        detail = f" {err.get('key', '')} {err.get('value', '')}".rstrip() if isinstance(err, dict) else ""
        self._note_reason(f"err {code}{detail}", now)
        return [self._event("exception", CRITICAL, f"printer error {code}{detail}",
                            hint="see the printer screen; resume/cancel only after a look", now=now,
                            key=f"{self.printer}:exception:err{code}", data={"err": err})]

    def _material_rules(self, f: Frame, now: float) -> list[Event]:
        if self.state.get("materialDetect") != 1:
            return []
        try:
            status = int(self.state.get("materialStatus"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return []
        before, self._material = self._material, status
        if before is None or status == before or f.state not in JOB_ACTIVE:
            return []
        # Meaning of non-zero materialStatus is not documented by Creality; 0 is what a
        # printing K1 SE with filament reports (2026-10), so report the raw change.
        self._note_reason(f"filament sensor materialStatus {before}->{status}", now)
        return [self._event("filament", WARNING, f"filament sensor changed: materialStatus {before}->{status}",
                            hint="check the spool/runout sensor", now=now,
                            data={"materialStatus": status, "was": before})]


__all__ = [
    "CRITICAL", "INFO", "SEVERITIES", "WARNING", "CrealityDetector", "Event", "Frame",
    "MoonrakerDetector", "WatchLimits", "moonraker_subscription", "severity_rank",
]
