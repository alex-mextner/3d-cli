"""bed.py — the stdlib half of the printer bed state (issue #303): config, state file, events,
the start gate, the optional clear acknowledgement and the `3d printer watch` child process.

WHAT: the camera can tell whether the bed is clear (the parts were taken off) and whether the
  removable build plate is on it; the pixel work is lib/bed_state.py (numpy + OpenCV, run
  through cli.pyrun). This module holds everything that must stay importable without them:
  * `BedConfig`   the `watch.bed:` block of a printer (fleet/printers.yaml or
                  printer-devices.yaml), validated;
  * paths         references in <data dir>/bed/<printer>/, the last verdict in
                  <state dir>/bed/<printer>.json, evidence frames in <cache dir>/vision/bed/;
  * `BedChild`    `3d printer watch` runs `bed_state.py monitor NAME` per printer with
                  `watch.bed.enabled: true` and turns its JSON lines into watch Events
                  (bed_clear, bed_occupied, plate_present, plate_missing: info; bed_unknown,
                  the camera cannot tell right after a job: warning, so it reaches a person);
  * the gate     preflight.check_bed, the `bed` line of `3d printer start` / `3d print-preflight --printer`:
                  FAIL on objects or a missing plate, PASS on a clear bed with the plate,
                  WARN when the camera cannot tell;
  * `clear_ack`   optional `watch.bed.clear_ack`: one short motor sound after bed_clear so the
                  person at the printer hears that the camera saw the clear bed.

WRITES: `clear_ack` is the only write to a printer anywhere in the bed code, and the only one
  `3d printer watch` can cause: one G-code line that the printer's `trusted_macros` lists, only
  when the printer reads idle/complete/cancelled just before, at most once per bed_clear event
  and per debounce window, never below `min_z_mm` (a Z buzz must not reach the nozzle).
  Everything else only reads (camera snapshot, print_stats, toolhead).

INVARIANTS: stdlib only at import time; numpy/OpenCV live in bed_state.py.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printer_ops.watch import Event

# What bed_state.py imports (numpy/OpenCV for pixels, scipy+pyyaml for the camera calibration
# and printer-devices.yaml) when it runs outside the repo .venv.
BED_DEPS = "numpy,opencv-python-headless,pyyaml,scipy"
BED_TOOL = "bed_state.py"

CLEAR, OCCUPIED, PRINTING, UNKNOWN = "clear", "occupied", "printing", "unknown"
PRESENT, MISSING = "present", "missing"
KINDS = ("bed_clear", "bed_occupied", "plate_present", "plate_missing", "bed_unknown")
# Printer states in which the bed may be judged and acknowledged (base.py normalized states).
QUIET_STATES = ("idle", "complete", "cancelled")

BED_KEYS = ("enabled", "interval_s", "roi", "volume_height_mm", "z_tol_mm", "stale_s", "clear_ack", "thresholds")
# The per-printer overrides of bed_state.Thresholds (tuned on the U1 and K1 SE frames of tests/data/bed;
# another camera may need other values): every field of that dataclass, a test keeps the two in step.
THRESHOLD_KEYS = ("level", "detrend_px", "edge_px", "clear_blob_px", "clear_max", "occupied_blob_px", "occupied_min",
                  "plate_missing_min", "plate_tie", "surface_min", "change_min", "settle_frames", "match_px", "view_min")
ACK_KEYS = ("enabled", "command", "debounce_minutes", "min_z_mm")


def _log(msg: str) -> None:
    print(f"bed: {msg}", file=sys.stderr, flush=True)


# ---- config ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class AckConfig:
    command: tuple[str, ...]  # G-code lines sent as one script; each first word is in trusted_macros
    debounce_s: float = 600.0
    min_z_mm: float = 10.0  # no ack while the bed is closer than this to the nozzle


@dataclass(frozen=True)
class BedConfig:
    enabled: bool = False
    interval_s: float = 1.0               # camera + state poll while the printer is quiet
    roi: tuple[tuple[float, float], ...] | None = None  # bed polygon (0..1) at the reference Z
    volume_height_mm: float = 80.0        # calibrated cameras: judge the bed up to this height
    z_tol_mm: float = 0.5                 # no camera calibration (roi): a reference is used only within this Z distance
    stale_s: float = 120.0                # the gate re-checks when the last verdict is older
    clear_ack: AckConfig | None = None
    thresholds: tuple[tuple[str, float], ...] = ()  # overrides of bed_state.Thresholds fields (THRESHOLD_KEYS)


def _num(value: Any, what: str, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < minimum:
        raise InvalidArgument(what, str(value), [f"a number >= {minimum:g}"], command="printer")
    return float(value)


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArgument(what, str(value), ["true", "false"], command="printer")
    return value


def _thresholds(value: Any, what: str) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, dict):
        raise InvalidArgument(what, str(value), [f"a mapping of {', '.join(THRESHOLD_KEYS)} to numbers"], command="printer")
    unknown = sorted(set(value) - set(THRESHOLD_KEYS))
    if unknown:
        raise InvalidArgument(what, ", ".join(unknown), THRESHOLD_KEYS, command="printer")
    return tuple((k, _num(v, f"{what}.{k}", 0)) for k, v in value.items())


def _roi(value: Any, what: str) -> tuple[tuple[float, float], ...]:
    ok = (isinstance(value, list) and len(value) >= 3
          and all(isinstance(p, list) and len(p) == 2
                  and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0 for v in p)
                  for p in value))
    if not ok:
        raise InvalidArgument(what, str(value), ["a polygon of >= 3 [x, y] points, x and y in 0..1 of the frame"],
                              command="printer")
    return tuple((float(x), float(y)) for x, y in value)


def _ack(value: Any, device: Device) -> AckConfig | None:
    where = f"{device.name}.watch.bed.clear_ack"
    if not isinstance(value, dict):
        raise InvalidArgument(where, str(value), ["a mapping: {enabled, command, debounce_minutes, min_z_mm}"],
                              command="printer", extra=f"Fix {device.source}.")
    unknown = sorted(set(value) - set(ACK_KEYS))
    if unknown:
        raise InvalidArgument(where, ", ".join(unknown), ACK_KEYS, command="printer", extra=f"Fix {device.source}.")
    if not _flag(value.get("enabled", False), f"{where}.enabled"):
        return None
    raw = value.get("command")
    lines = [raw] if isinstance(raw, str) else raw
    if (not isinstance(lines, list) or not lines
            or not all(isinstance(c, str) and c.strip() and not any(ch in c for ch in "\n\r;") for c in lines)):
        raise InvalidArgument(f"{where}.command", str(raw),
                              ["one G-code line (STEPPER_BUZZ STEPPER=stepper_z) or a list of lines, no comments"],
                              command="printer", extra=f"Fix {device.source}.")
    for line in lines:
        word = line.split()[0].upper()
        if word in ("STEPPER_BUZZ", "FORCE_MOVE") and not re.search(r"\bSTEPPER=stepper_z\d*\b", line, re.IGNORECASE):
            raise InvalidArgument(f"{where}.command", line, [f"{word} STEPPER=stepper_z (a Z stepper)"],
                                  command="printer", extra="min_z_mm guards the bed only: buzz a Z stepper.")
        if word not in device.trusted_macros:
            raise InvalidArgument(f"{where}.command", line, [f"a command listed in {device.name}.trusted_macros"],
                                  command="printer",
                                  extra=f"Add {word} to {device.name}.trusted_macros in {device.source} first "
                                        "(it is the list of commands 3d may let the printer run).")
    return AckConfig(tuple(c.strip() for c in lines),
                     _num(value.get("debounce_minutes", 10), f"{where}.debounce_minutes", 0) * 60,
                     _num(value.get("min_z_mm", 10), f"{where}.min_z_mm", 0))


def bed_config(device: Device) -> BedConfig:
    """`watch.bed` of a device, validated (absent = monitor off)."""
    raw = (device.watch or {}).get("bed")
    if raw is None:
        return BedConfig()
    where = f"{device.name}.watch.bed"
    if not isinstance(raw, dict):
        raise InvalidArgument(where, str(raw), ["a mapping, e.g. {enabled: true}"], command="printer",
                              extra=f"Fix {device.source}.")
    unknown = sorted(set(raw) - set(BED_KEYS))
    if unknown:
        raise InvalidArgument(where, ", ".join(unknown), BED_KEYS, command="printer", extra=f"Fix {device.source}.")
    cfg = BedConfig(
        enabled=_flag(raw.get("enabled", False), f"{where}.enabled"),
        interval_s=_num(raw.get("interval_s", 1.0), f"{where}.interval_s", 0.2),
        roi=_roi(raw["roi"], f"{where}.roi") if "roi" in raw else None,
        volume_height_mm=_num(raw.get("volume_height_mm", 80.0), f"{where}.volume_height_mm", 0),
        z_tol_mm=_num(raw.get("z_tol_mm", 0.5), f"{where}.z_tol_mm", 0),
        stale_s=_num(raw.get("stale_s", 120.0), f"{where}.stale_s", 1),
        clear_ack=_ack(raw["clear_ack"], device) if "clear_ack" in raw else None,
        thresholds=_thresholds(raw["thresholds"], f"{where}.thresholds") if "thresholds" in raw else (),
    )
    if cfg.enabled:
        from printer_ops.camera import camera_spec

        spec = camera_spec(device)
        if not spec.present:  # the monitor would fail on its first frame: say so when the config is read
            raise InvalidArgument(f"{where}.enabled", "true", ["a printer with a camera (watch.vision.camera)"],
                                  command="printer", extra=f"{device.name} has no camera ({spec.source}); fix {device.source}.")
    return cfg


# ---- files ----------------------------------------------------------------------------------

def refs_dir(printer: str) -> Path:
    """Reference frames of the empty bed (user-captured, not regenerable: data dir)."""
    from cli.paths import data_dir

    return data_dir() / "bed" / printer


def state_path(printer: str) -> Path:
    from cli.paths import state_dir

    return state_dir() / "bed" / f"{printer}.json"


def evidence_dir() -> Path:
    from cli.paths import cache_dir

    return cache_dir() / "vision" / "bed"


def read_state(printer: str) -> dict[str, Any] | None:
    try:
        data = json.loads(state_path(printer).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(printer: str, data: dict[str, Any]) -> None:
    path = state_path(printer)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


# ---- events ---------------------------------------------------------------------------------

def transitions(prev_bed: str | None, prev_plate: str | None, bed: str, plate: str) -> list[str]:
    """Event kinds for a new verdict. Unknown never forgets the last known value and fires only
    as bed_unknown on the first verdict after a job (the bed was "printing"): never silence
    after a job. plate_present fires only after plate_missing (not after every job)."""
    kinds = []
    if bed in (CLEAR, OCCUPIED) and bed != prev_bed:
        kinds.append(f"bed_{bed}")
    elif bed == UNKNOWN and prev_bed == PRINTING:
        kinds.append("bed_unknown")
    if plate == MISSING and prev_plate != MISSING:
        kinds.append("plate_missing")
    elif plate == PRESENT and prev_plate == MISSING:
        kinds.append("plate_present")
    return kinds


def event_from(printer: str, obj: dict[str, Any]) -> Event:
    """A monitor JSON `event` object -> watch Event (unknown kinds are refused; bed_unknown is a
    warning, the rest info)."""
    from printer_ops.watch import INFO, WARNING, Event

    kind = str(obj.get("kind", ""))
    if kind not in KINDS:
        raise ValueError(f"unknown bed event kind {kind!r}")
    raw = obj.get("data")
    data: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    ts = obj.get("ts")
    return Event(printer, kind, WARNING if kind == "bed_unknown" else INFO, str(obj.get("text") or kind), data,
                 key=str(obj.get("key") or f"{printer}:{kind}"),
                 ts=float(ts) if isinstance(ts, (int, float)) else time.time())


# ---- watch child ----------------------------------------------------------------------------

def monitor_argv(printer: str) -> list[str]:
    from cli.pyrun import tool_argv

    return tool_argv(BED_DEPS, BED_TOOL, ["monitor", printer])


class BedChild:
    """`bed_state.py monitor NAME` as a child of `3d printer watch`: its stdout JSON lines
    become Events (`{"event": {...}}`) and heartbeat fields (`{"state": {...}}`); stderr goes
    to the watcher's stderr (the journal). A crash restarts it with backoff 10 s .. 5 min."""

    def __init__(self, printer: str, submit: Callable[[Event], None], stop: threading.Event,
                 on_state: Callable[[str, dict[str, Any]], None] | None = None,
                 argv: Callable[[str], list[str]] = monitor_argv) -> None:
        self.printer = printer
        self.submit = submit
        self.stop = stop
        self.on_state = on_state
        self.argv = argv
        self.proc: subprocess.Popen[str] | None = None
        self.restarts = 0
        self.thread = threading.Thread(target=self.run, name=f"bed-{printer}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def line(self, text: str) -> None:
        try:
            obj = json.loads(text)
        except ValueError:
            _log(f"{self.printer}: monitor wrote a non-JSON line: {text[:200]}")
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("event"), dict):
            try:
                self.submit(event_from(self.printer, obj["event"]))
            except ValueError as exc:
                _log(f"{self.printer}: {exc}")
        if isinstance(obj.get("state"), dict) and self.on_state is not None:
            self.on_state(self.printer, obj["state"])

    def run(self) -> None:
        backoff = 10.0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.proc = subprocess.Popen(self.argv(self.printer), stdout=subprocess.PIPE, text=True,
                                             stdin=subprocess.DEVNULL)
            except Exception as exc:  # no runtime (MissingDependency) / exec failure: retry later
                _log(f"{self.printer}: cannot start the bed monitor: {exc}")
            else:
                assert self.proc.stdout is not None
                for text in self.proc.stdout:
                    if text.strip():
                        self.line(text)
                code = self.proc.wait()
                if self.stop.is_set():
                    break
                _log(f"{self.printer}: bed monitor exited with {code}; restart in {backoff:.0f} s")
            self.restarts += 1
            if time.monotonic() - started > 600:
                backoff = 10.0
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 300.0)

    def close(self, timeout: float = 5.0) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.thread.join(timeout)


# ---- the clear acknowledgement --------------------------------------------------------------

def clear_ack(device: Device, ack: AckConfig, *, z: float | None, last_ack: float | None, now: float,
              adapter: Any = None) -> tuple[bool, str]:
    """Send `ack.command` once (see WRITES above). -> (sent, what happened)."""
    untrusted = [c.split()[0].upper() for c in ack.command if c.split()[0].upper() not in device.trusted_macros]
    if device.access != "full":
        return False, f"{device.name} is {device.access}: no writes"
    if untrusted:
        return False, f"{', '.join(untrusted)} not in {device.name}.trusted_macros"
    if last_ack is not None and now - last_ack < ack.debounce_s:
        return False, f"debounced (last ack {now - last_ack:.0f} s ago, window {ack.debounce_s:.0f} s)"
    if z is None or z < ack.min_z_mm:
        return False, f"bed Z {z} is unknown or below min_z_mm {ack.min_z_mm:g}"
    if adapter is None:
        from printer_ops import make_adapter

        adapter = make_adapter(device, timeout=60.0)  # Moonraker answers after the buzz ran (~10 s)
    st = adapter.status()  # re-read right before the write: the bed may have been jogged since the frame
    if st.state not in QUIET_STATES:
        return False, f"printer is {st.state} [{st.raw_state}]"
    pos = (adapter.query("toolhead").get("toolhead") or {}).get("position")
    z_now = pos[2] if isinstance(pos, list) and len(pos) > 2 and isinstance(pos[2], (int, float)) else None
    if z_now is None or not math.isfinite(z_now) or z_now < ack.min_z_mm:
        return False, f"bed Z now {z_now} is unknown or below min_z_mm {ack.min_z_mm:g}"
    script = "\n".join(ack.command)
    adapter.run_gcode(script)
    return True, f"sent {' | '.join(ack.command)}"


__all__ = ["BED_DEPS", "BED_TOOL", "KINDS", "AckConfig", "BedChild", "BedConfig", "bed_config", "clear_ack",
           "event_from", "read_state", "refs_dir", "state_path", "transitions", "write_state"]
