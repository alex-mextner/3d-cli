"""watch_incidents.py — draft incidents from `3d printer watch` events (plan §6 "автоинциденты").

ACCESSED VIA: 3d-api POST /v1/incidents/from-watch, fed by the watch's exec sink:

    3d printer watch u1 --exec '3d printops incidents from-watch'

(the exec sink pipes each event's JSON to the command's stdin; `from-watch` posts it). The
watch itself stays untouched and read-only; this module only interprets its event JSON
(printer_ops.watch.Event.to_json: ts, printer, kind, severity, text, key, data).

WHICH EVENTS: warning/critical events that describe a failing print — filament runout/motion,
printer exceptions (U1 noodle detection, …), G-code errors, Klipper shutdown, heater trouble,
lost homing, the bed switched off mid-job, a stalled job — while a job is printing or paused
(Klipper shutdown/error always). Snapshots, milestones, connection flaps, plain state changes
and warnings-only G-code lines are not incidents.

WHAT A DRAFT IS: an incident with status `draft`, reported_by `watch`, the best-guess kind,
severity and a confidence (how sure the mapping is), the event as evidence and the usual
auto-context. A human confirms it (correcting kind/severity) or dismisses it; drafts do not
count in rates.

ONE FAILURE, ONE DRAFT: events of the same printer and job within DRAFT_WINDOW_S of an open
draft are appended to it as evidence (a noodle pause is exception + gcode lines + state at once);
a more specific later event upgrades an `other` draft's kind. The same event key twice is a no-op.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from printops import incidents
from printops.incident_context import Readers, resolve
from printops.incidents import Incident, IncidentError, Report, iso, obj
from printops.spoolman import Spoolman

DRAFT_WINDOW_S = 900
ACTIVE = ("printing", "paused")


@dataclass(frozen=True)
class Draft:
    kind: str
    severity: str
    confidence: float
    tags: tuple[str, ...] = field(default_factory=tuple)


# (pattern over the event text + data, kind, confidence, tag, severity or None = from the event)
_TEXT_RULES: tuple[tuple[re.Pattern[str], str, float, str, str | None], ...] = tuple(
    (re.compile(p, re.I), k, c, t, s)
    for p, k, c, t, s in (
        (r"noodle|spaghetti", "other", 0.6, "spaghetti", "print_lost"),
        (r"thermal runaway|not heating at expected rate|verify_heater|adc out of range"
         r"|heater \S+ not heating", "other", 0.8, "heater_fault", "hardware_risk"),
        (r"run ?out|no filament|filament (?:not detected|absent)", "runout", 0.8, "runout", None),
        (r"tangle", "tangle", 0.7, "tangle", None),
        (r"clog|jam|blocked|motion stopped", "clog", 0.5, "clog", None),
        (r"skipped steps|lost steps|layer shift|tmc .*(?:error|fault)|stepper.*(?:fault|stall)",
         "layer_shift", 0.5, "motion_fault", "hardware_risk"),
        (r"below minimum temp|cold extru", "other", 0.6, "cold_extrude", None),
        (r"exceeds maximum extrusion|extrude only move too long", "other", 0.6, "extrusion_limit", None),
    )
)


def _event_severity(event: Mapping[str, Any]) -> str:
    return "print_lost" if event.get("severity") == "critical" else "part_lost"


def _by_text(event: Mapping[str, Any], text: str, fallback_tag: str) -> Draft:
    for pattern, kind, conf, tag, sev in _TEXT_RULES:
        if pattern.search(text):
            return Draft(kind, sev or _event_severity(event), conf, (tag,))
    return Draft("other", _event_severity(event), 0.3, (fallback_tag,))


def classify(event: Mapping[str, Any]) -> Draft | str:
    """The draft a watch event suggests, or the reason it is not an incident."""
    sev = event.get("severity")
    if sev not in ("warning", "critical"):
        return f"{sev or 'unknown'}-severity events are not incidents"
    kind = event.get("kind")
    data = obj(event.get("data"))
    text = f"{event.get('text') or ''} {data.get('message') or ''} {data.get('line') or ''}"
    exc = data.get("exception")
    if isinstance(exc, dict):
        text += f" {exc.get('message') or ''}"
    if kind == "klippy":
        if data.get("klippy") in ("shutdown", "error"):
            d = _by_text(event, text, "klippy_shutdown")
            return d if d.tags != ("klippy_shutdown",) else Draft("other", "hardware_risk", 0.4, d.tags)
        return f"Klipper {data.get('klippy') or 'state'} is not an incident"
    if data.get("state") not in ACTIVE:
        return f"no job printing ({data.get('state') or 'state unknown'})"
    if kind == "filament":
        sensor = str(data.get("sensor") or "")
        if sensor.startswith("filament_motion_sensor"):
            return Draft("clog", _event_severity(event), 0.4, ("filament_motion",))
        if sensor.startswith("filament_switch_sensor"):
            return Draft("runout", _event_severity(event), 0.8, ("runout_sensor",))
        return Draft("runout", _event_severity(event), 0.4, ("filament_sensor",))
    if kind in ("exception", "gcode_error"):
        return _by_text(event, text, kind)
    if kind == "temperature":
        heater = str(data.get("heater") or "")
        return Draft("other", "hardware_risk", 0.5, ("heater_fault", f"heater:{heater}" if heater else "heater"))
    if kind == "homing_lost":
        return Draft("other", "hardware_risk", 0.6, ("homing_lost",))
    if kind == "bed_off":
        return Draft("poor_bed_adhesion", "part_lost", 0.3, ("bed_off",))
    if kind == "stall":
        if "moving again" in text:
            return "the stall cleared"
        return Draft("other", "part_lost", 0.3, ("stall",))
    if kind == "state" and data.get("to") == "error":
        return _by_text(event, text, "printer_error")
    return f"watch {kind} events are not incidents"


def _event_at(event: Mapping[str, Any]) -> str | None:
    ts = event.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return iso(incidents.parse_ts(ts))
    except ValueError:
        return None


def _open_draft(conn: sqlite3.Connection, printer: str, job_file: str | None, at: str) -> Incident | None:
    lo = iso(incidents.parse_ts(at) - datetime.timedelta(seconds=DRAFT_WINDOW_S))
    row = conn.execute(
        "SELECT * FROM incident WHERE status = 'draft' AND reported_by = 'watch' AND printer = ? "
        "AND job_file IS ? AND at >= ? AND at <= ? ORDER BY at DESC LIMIT 1",
        (printer, job_file, lo, at),
    ).fetchone()
    return Incident.from_row(row) if row else None


@dataclass
class Outcome:
    incident: Incident | None
    created: bool
    merged: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"incident": self.incident.to_dict() if self.incident else None, "created": self.created,
                "merged": self.merged, "reason": self.reason}


def draft_from_watch(
    conn: sqlite3.Connection,
    spoolman: Spoolman | None,
    event: Mapping[str, Any],
    *,
    readers: Readers,
    actor: str,
) -> Outcome:
    printer = event.get("printer")
    key = event.get("key")
    if not isinstance(printer, str) or not printer or not isinstance(key, str) or not key:
        raise IncidentError(422, "not a watch event: needs printer and key (3d printer watch JSON)")
    source_key = f"watch:{key}"
    if (seen := incidents.by_source_key(conn, source_key)) is not None:
        return Outcome(seen, False, False, "this watch event was already recorded")
    found = classify(event)
    if isinstance(found, str):
        return Outcome(None, False, False, found)
    data = obj(event.get("data"))
    at = _event_at(event) or iso(datetime.datetime.now(datetime.timezone.utc))
    evidence = {"key": key, "kind": event.get("kind"), "severity": event.get("severity"),
                "text": event.get("text"), "ts": event.get("ts")}
    job_file = data.get("file") if isinstance(data.get("file"), str) else None
    if (draft := _open_draft(conn, printer, job_file, at)) is not None:
        merged = incidents.attach_evidence(conn, draft, evidence, actor=actor)
        if merged.kind == "other" and found.kind != "other":
            merged = incidents.update(conn, merged.id, {"kind": found.kind, "confidence": found.confidence,
                                                        "add_tags": list(found.tags)}, actor=actor)
        return Outcome(merged, False, True, f"added to draft #{draft.id} (same failure)")
    ctx = resolve(conn, spoolman, printer=printer, readers=readers)
    if "layer" not in ctx.fields and isinstance(data.get("layer"), int):
        ctx.put("layer", data["layer"], "watch event")
    if "job_file" not in ctx.fields and job_file:
        ctx.put("job_file", job_file, "watch event")
    report = Report(kind=found.kind, severity=found.severity, reported_by="watch", status="draft", at=at,
                    confidence=found.confidence, note=str(event.get("text") or "") or None,
                    tags=["watch", *found.tags], source_key=source_key)
    try:
        inc = incidents.create(conn, report, actor=actor, auto=ctx.fields,
                               context={**ctx.to_dict(), "evidence": [evidence]})
    except IncidentError as e:
        if e.status == 409 and (seen := incidents.by_source_key(conn, source_key)) is not None:
            return Outcome(seen, False, False, "this watch event was already recorded")
        raise
    return Outcome(inc, True, False, f"draft {found.kind} ({found.severity}, confidence {found.confidence:g})")
