"""watch_incidents.py — draft incidents from `3d printer watch` / vision events (plan §6 "автоинциденты").

ACCESSED VIA: 3d-api POST /v1/incidents/from-watch, fed by the watch's exec sink:

    3d printer watch u1 --exec '3d printops incidents from-watch'

(the exec sink pipes each event's JSON to the command's stdin; `from-watch` posts it). The
watch itself stays untouched and read-only; this module only interprets its event JSON
(printer_ops.watch.Event.to_json: ts, printer, kind, severity, text, key, data).

WHICH EVENTS: warning/critical events that describe a failing print — filament runout/motion,
printer exceptions (U1 noodle detection, …), G-code errors, Klipper shutdown, heater trouble,
lost homing, the bed switched off mid-job, a stalled job — while a job is printing or paused
(Klipper shutdown/error always). Camera detections of printer_ops.vision (`vision_spaghetti`,
`printer_noodle_verdict` — same Event JSON, emitted only while printing) become `vision`
drafts with the detector's score as confidence and the evidence frame as photo. Snapshots,
milestones, connection flaps, camera health, plain state changes and warnings-only G-code lines
are not incidents.

WHAT A DRAFT IS: an incident with status `draft`, reported_by `watch` (or `vision`), the best-guess kind,
severity and a confidence (how sure the mapping is), the event as evidence and the usual
auto-context. A human confirms it (correcting kind/severity) or dismisses it; drafts do not
count in rates.

ONE FAILURE, ONE DRAFT: events of the same printer and job within DRAFT_WINDOW_S of an open
draft are appended to it as evidence (a noodle pause is exception + gcode lines + state at once);
a more specific later event upgrades an `other` draft's kind. An event without `data.file` (vision,
Klipper) takes the printer's current file from one status read, so a detection on a new job never
lands in the previous job's draft. The same event key twice is a no-op, merged or not.

TIME BUDGET: the exec sink kills its command after 60 s, so the G-code scan of a watch draft ends
WATCH_DEADLINE_S after the request arrived, printer reads included; past that the draft is
recorded without G-code settings (a warning) rather than the event being lost. A watch draft never
downloads the G-code from the printer (PRINTOPS_GCODE_FETCH): only a local copy is scanned.

CONCURRENCY: `printer watch` and `printer vision` often report one failure at once. Finding the
open draft and merging into it or creating it happen under one lock (3d-api is one process), and
the slow auto-context is resolved outside it, so two events of one failure make one draft. A draft
confirmed or dismissed in between takes no more evidence: the event opens a new draft instead.
"""

from __future__ import annotations

import datetime
import math
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from printops import incidents
from printops.incident_context import Readers, current_job_file, resolve
from printops.incidents import Incident, IncidentError, Report, iso, obj
from printops.spoolman import Spoolman

DRAFT_WINDOW_S = 900
WATCH_DEADLINE_S = 45.0
ACTIVE = ("printing", "paused")
_DRAFT_LOCK = threading.Lock()


@dataclass(frozen=True)
class Draft:
    kind: str
    severity: str
    confidence: float
    tags: tuple[str, ...] = field(default_factory=tuple)
    reporter: str = "watch"  # `vision` for camera detections (printer_ops.vision)


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


def _confidence(p: Any) -> float:
    """A detector score clamped to 0..1; 0.5 when the event carries none (or NaN/inf)."""
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p):
        return 0.5
    return min(max(float(p), 0.0), 1.0)


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
    if kind in ("vision_spaghetti", "printer_noodle_verdict"):
        # printer_ops.vision (camera score / the U1's own noodle detector) emits these only while
        # printing and without a job state; one that does carry a state must still be printing.
        if data.get("state") is not None and data.get("state") not in ACTIVE:
            return f"no job printing ({data.get('state')})"
        conf = _confidence(data["p"] if data.get("p") is not None else data.get("prob"))
        return Draft("other", _event_severity(event), round(conf, 2), ("spaghetti",), "vision")
    if isinstance(kind, str) and kind.startswith("vision_"):
        return f"camera health ({kind}) is not an incident"
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


def _window(at: str) -> tuple[str, str]:
    """DRAFT_WINDOW_S either side of the event: a delayed event may be older than the draft."""
    t, span = incidents.parse_ts(at), datetime.timedelta(seconds=DRAFT_WINDOW_S)
    return iso(t - span), iso(t + span)


# FROM/WHERE fragment shared by both draft lookups; placeholders: printer, window start, window end
_RECENT_DRAFT_SQL = ("FROM incident WHERE status = 'draft' AND reported_by IN ('watch', 'vision') AND printer = ? "
                     "AND at >= ? AND at <= ?")


def _any_recent_draft(conn: sqlite3.Connection, printer: str, at: str) -> bool:
    return conn.execute(f"SELECT 1 {_RECENT_DRAFT_SQL} LIMIT 1", (printer, *_window(at))).fetchone() is not None


def _open_draft(conn: sqlite3.Connection, printer: str, job_file: str | None, at: str) -> Incident | None:
    row = conn.execute(
        f"SELECT * {_RECENT_DRAFT_SQL} AND job_file IS ? ORDER BY at DESC LIMIT 1",
        (printer, *_window(at), job_file),
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


def _join_open_draft(conn: sqlite3.Connection, printer: str, job_file: str | None, at: str,
                     evidence: Mapping[str, Any], photos: list[str], found: Draft, *, actor: str) -> Outcome | None:
    """Add the event to the printer's open draft of this job (the caller holds _DRAFT_LOCK); None
    when there is none, or it was confirmed or dismissed meanwhile."""
    draft = _open_draft(conn, printer, job_file, at)
    if draft is None:
        return None
    # tags always join (a camera's `vision`/`spaghetti` on a stall draft); kind and confidence
    # only refine a draft that is still `other`
    upgrade: dict[str, Any] = {"add_tags": [found.reporter, *found.tags]}
    if found.kind != "other":
        upgrade.update(kind=found.kind, confidence=found.confidence)
    try:
        merged = incidents.attach_evidence(conn, draft, evidence, actor=actor, photos=photos, upgrade=upgrade)
    except IncidentError as e:
        if e.status == 409:
            return None
        raise
    if merged is None:
        return Outcome(draft, False, False, "this watch event was already recorded")
    return Outcome(merged, False, True, f"added to draft #{draft.id} (same failure)")


def draft_from_watch(
    conn: sqlite3.Connection,
    spoolman: Spoolman | None,
    event: Mapping[str, Any],
    *,
    readers: Readers,
    actor: str,
) -> Outcome:
    started = time.monotonic()
    printer = event.get("printer")
    key = event.get("key")
    if not isinstance(printer, str) or not printer or not isinstance(key, str) or not key:
        raise IncidentError(422, "not a watch event: needs printer and key (3d printer watch JSON)")
    source_key = f"watch:{key}"
    seen = incidents.by_source_key(conn, source_key) or incidents.by_evidence_key(conn, printer, key)
    if seen is not None:
        return Outcome(seen, False, False, "this watch event was already recorded")
    found = classify(event)
    if isinstance(found, str):
        return Outcome(None, False, False, found)
    data = obj(event.get("data"))
    at = _event_at(event) or iso(datetime.datetime.now(datetime.timezone.utc))
    evidence = {"key": key, "kind": event.get("kind"), "severity": event.get("severity"),
                "text": event.get("text"), "ts": event.get("ts")}
    job_file = data.get("file") if isinstance(data.get("file"), str) else None
    if job_file is None and _any_recent_draft(conn, printer, at):
        job_file = current_job_file(printer, readers)  # read only when there is a draft to match
    # a vision event's evidence frame (a path on the watch host) is the draft's first photo
    photos = [data["frame"]] if isinstance(data.get("frame"), str) and data["frame"] else []
    with _DRAFT_LOCK:
        if (out := _join_open_draft(conn, printer, job_file, at, evidence, photos, found, actor=actor)) is not None:
            return out
    ctx = resolve(conn, spoolman, printer=printer,
                  readers=replace(readers, deadline_at=started + WATCH_DEADLINE_S, download=None))
    if "layer" not in ctx.fields and isinstance(data.get("layer"), int):
        ctx.put("layer", data["layer"], "watch event")
    if "job_file" not in ctx.fields and job_file:
        ctx.put("job_file", job_file, "watch event")
    report = Report(kind=found.kind, severity=found.severity, reported_by=found.reporter, status="draft", at=at,
                    confidence=found.confidence, note=str(event.get("text") or "") or None,
                    tags=[found.reporter, *found.tags], photos=photos, source_key=source_key)
    with _DRAFT_LOCK:
        # another event of this failure may have opened the draft while the context resolved
        # (the event's own file wins: a delayed event may arrive after the next job started)
        job_now = job_file if job_file is not None else ctx.fields.get("job_file")
        if (out := _join_open_draft(conn, printer, job_now, at, evidence, photos, found, actor=actor)) is not None:
            return out
        try:
            inc = incidents.create(conn, report, actor=actor, auto=ctx.fields,
                                   context={**ctx.to_dict(), "evidence": [evidence]})
        except IncidentError as e:
            if e.status == 409 and (seen := incidents.by_source_key(conn, source_key)) is not None:
                return Outcome(seen, False, False, "this watch event was already recorded")
            raise
    return Outcome(inc, True, False, f"draft {found.kind} ({found.severity}, confidence {found.confidence:g})")
