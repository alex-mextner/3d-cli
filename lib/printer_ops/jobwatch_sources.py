"""jobwatch_sources.py — where `3d printer watch NAME --job` gets its readings: a live Moonraker printer and
camera, or a fixture (a scenario file with saved frames; no network at all).

`LiveSource`     one GET per poll of the objects the always-on watch subscribes to (`moonraker_subscription`),
                 fed to the SAME `MoonrakerDetector` (filament runout, heater loss, bed off, exception, G-code
                 error, homing lost) plus the layer, progress, file position and toolhead Z; the printer's own
                 defect detection from klippy.log (`KlippyLogTap`); the camera snapshot (`open_camera`), taken
                 with the printer's light on when it was off and restored right after (bed_light.py; the only
                 write, and only for the frame). Otherwise read-only: GETs and the `camera.start_monitor` keepalive.
`FixtureSource`  the same surface from a scenario JSON on a virtual clock, with a `FixtureAdapter` that records
                 the PAUSE/CANCEL it is given. This is the dry-run/demo mode: `3d printer watch NAME --job
                 --fixture scenario.json`, nothing is ever sent to a printer or a phone.

Scenario file: {"printer": "k1se", "file": "part.gcode", "steps": [{"t": 0, "state": "printing", "layer": 1,
  "progress": 0.01, "z": 0.2, "frame": "empty.jpg", "events": [{"kind": "filament", "severity": "critical",
  "text": "..."}], "noodles": [["noodle", 0.9]]}, ...]}; frame paths are relative to the scenario file; a step
  holds until the next step's `t` (seconds on the virtual clock).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from errors import InvalidArgument, UsageError
from printer_ops.base import (
    CANCELLED,
    PAUSED,
    PRINTING,
    Adapter,
    JobRecord,
    PrinterCommError,
    PrinterStatus,
)
from printer_ops.bed import bed_config
from printer_ops.bed_light import LightConfig, LightReport, LightSession, write_block
from printer_ops.jobwatch import Observation
from printer_ops.watch import Event, MoonrakerDetector, moonraker_subscription

CMD = "printer watch --job"


def query_string(objects: list[str]) -> str:
    """`printer.objects.list` -> the GET query of everything the always-on watch subscribes to, plus the toolhead."""
    sub = moonraker_subscription(objects)
    sub["toolhead"] = ["position", "homed_axes"]
    return "&".join(
        quote(name, safe="") + ("=" + ",".join(fields) if fields else "")
        for name, fields in sub.items()
    )


def toolhead_z(status: dict[str, Any]) -> float | None:
    pos = (status.get("toolhead") or {}).get("position")
    z = pos[2] if isinstance(pos, list) and len(pos) > 2 else None
    return float(z) if isinstance(z, (int, float)) and not isinstance(z, bool) else None


def file_position(status: dict[str, Any]) -> int | None:
    pos = (status.get("virtual_sdcard") or {}).get("file_position")
    return pos if isinstance(pos, int) and not isinstance(pos, bool) else None


class LiveSource:
    def __init__(self, adapter: Any, camera: Any, tap: Any | None = None) -> None:
        self.adapter = adapter
        self.camera = camera
        self.tap = tap
        self.detector = MoonrakerDetector(adapter.name)
        self._query: str | None = None
        self._names: list[str] | None = None
        # `watch.bed.light` of the printer (the job watch has no bed block of its own: a printer
        # without one uses the defaults; an invalid block fails here, at the start of the watch)
        self._device = getattr(adapter, "device", None)
        self.light_config = (
            bed_config(self._device).light if self._device is not None else LightConfig()
        )
        self.light_report: LightReport | None = (
            None  # what the light did for the last frame
        )
        self._layers: tuple[float, float] | None | bool = (
            False  # (first layer height, layer height); False = unasked
        )

    def _object_names(self) -> list[str]:
        """`printer.objects.list`, read once."""
        if self._names is None:
            listed = self.adapter._get("/printer/objects/list")
            names = listed.get("objects", []) if isinstance(listed, dict) else []
            self._names = [n for n in names if isinstance(n, str)]
        return self._names

    def _objects_query(self) -> str:
        if self._query is None:
            self._query = query_string(self._object_names())
        return self._query

    def poll(self, now: float) -> Observation:
        status = self.adapter.query(self._objects_query())
        events = self.detector.on_status(status, now, full=True)
        f = self.detector.frame
        assert f is not None
        o = Observation(
            now,
            f.state,
            f.filename,
            f.progress,
            f.layer,
            f.total_layer,
            toolhead_z(status),
            file_position(status),
            list(events),
        )
        if o.layer is None:
            o.layer = self._layer_from_z(o)
        return o

    def _layer_from_z(self, o: Observation) -> int | None:
        """No `print_stats.info.current_layer` (a slicer without SET_PRINT_STATS_INFO): the layer from Z and the
        file's layer heights (Moonraker file metadata)."""
        if self._layers is False:
            self._layers = self._read_layer_heights(o.filename)
        if not isinstance(self._layers, tuple) or o.z is None:
            return None
        first, step = self._layers
        return max(1, 1 + round((o.z - first) / step)) if o.z >= first * 0.5 else 1

    def _read_layer_heights(self, filename: str | None) -> tuple[float, float] | None:
        try:
            meta = self.adapter.file_metadata(filename) if filename else {}
        except Exception:  # noqa: BLE001 — no metadata: the layer stays unknown
            return None
        first, step = meta.get("first_layer_height"), meta.get("layer_height")
        if (
            isinstance(first, (int, float))
            and isinstance(step, (int, float))
            and first > 0
            and step > 0
        ):
            return float(first), float(step)
        return None

    def noodles(self) -> list[tuple[str, float]]:
        if self.tap is None:
            return []
        try:
            return [(v.label, v.prob) for v in self.tap.poll()]
        except PrinterCommError:
            return []

    def grab(self) -> bytes:
        """One camera frame, taken with the printer's light on when it was off and put back right after.
        The job is printing, so this is the only moment the light is touched (`printing_ok`), and only
        when it was off; no light, or a failed switch, only means an unlit frame (`self.light_report`)."""
        self.camera.keepalive()
        try:
            names: list[str] | None = self._object_names()
        except PrinterCommError:
            names = None  # the light session reads the list itself and reports it as unreadable
        light = LightSession(
            self.adapter,
            self.light_config,
            printing_ok=True,
            note="the job watch frame",
            blocked=write_block(self._device),
            objects=names,
        )
        with light:
            light.on()
            jpeg = bytes(self.camera.grab().jpeg)
        self.light_report = light.report
        return jpeg


# ---- fixtures ---------------------------------------------------------------------------------


@dataclass
class Step:
    t: float
    data: dict[str, Any]


class Scenario:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InvalidArgument(
                "--fixture",
                str(path),
                ["a scenario JSON file"],
                command=CMD,
                extra=str(exc),
            ) from exc
        steps = doc.get("steps") if isinstance(doc, dict) else None
        if (
            not isinstance(steps, list)
            or not steps
            or not all(isinstance(s, dict) and "t" in s for s in steps)
        ):
            raise InvalidArgument(
                "--fixture",
                str(path),
                ['{"printer", "file", "steps": [{"t": 0, ...}]}'],
                command=CMD,
            )
        self.printer = str(doc.get("printer") or "")
        self.filename = str(doc.get("file") or "fixture.gcode")
        self.steps = sorted((Step(float(s["t"]), s) for s in steps), key=lambda s: s.t)

    def at(self, t: float) -> Step:
        current = self.steps[0]
        for s in self.steps:
            if s.t <= t:
                current = s
        return current

    def frame(self, step: Step) -> bytes:
        name = step.data.get("frame")
        if not name:
            raise PrinterCommError("the fixture step has no frame", command=CMD)
        return (self.path.parent / str(name)).read_bytes()


class FixtureAdapter(Adapter):
    """A printer that exists only in a scenario: reports the step's state and RECORDS pause/cancel."""

    kind = "fixture"

    def __init__(self, name: str, scenario: Scenario, clock: Any) -> None:
        super().__init__(name, "fixture", 0)
        self.scenario, self.clock = scenario, clock
        self.sent: list[str] = []
        self.forced: str | None = None

    def status(self) -> PrinterStatus:
        state = self.forced or str(
            self.scenario.at(self.clock()).data.get("state", PRINTING)
        )
        return PrinterStatus(state, state, self.scenario.filename, None)

    def history(self, limit: int) -> list[JobRecord]:
        return [JobRecord(self.scenario.filename, "in_progress", job_id="FIXTURE")]

    def pause(self) -> None:
        self.sent.append("pause")
        self.forced = PAUSED

    def cancel(self) -> None:
        self.sent.append("cancel")
        self.forced = CANCELLED


class FixtureSource:
    def __init__(self, scenario: Scenario, clock: Any, adapter: FixtureAdapter) -> None:
        self.scenario, self.clock, self.adapter = scenario, clock, adapter

    def poll(self, now: float) -> Observation:
        d = self.scenario.at(now).data
        events = [
            Event("fixture", e["kind"], e["severity"], e["text"], e.get("data", {}))
            for e in d.get("events", [])
        ]
        return Observation(
            now,
            self.adapter.status().state,
            self.scenario.filename,
            d.get("progress"),
            d.get("layer"),
            d.get("total_layer"),
            d.get("z"),
            None,
            events,
        )

    def noodles(self) -> list[tuple[str, float]]:
        return [
            (str(a), float(b))
            for a, b in self.scenario.at(self.clock()).data.get("noodles", [])
        ]

    def grab(self) -> bytes:
        return self.scenario.frame(self.scenario.at(self.clock()))


def require_fixture_printer(scenario: Scenario, name: str) -> None:
    if scenario.printer and scenario.printer != name:
        raise UsageError(
            f"the fixture is for printer {scenario.printer!r}, not {name!r}",
            command=CMD,
            remediation=[
                f"3d printer watch {scenario.printer} --job --fixture {scenario.path}"
            ],
        )
