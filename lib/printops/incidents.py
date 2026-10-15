"""incidents.py — structured print incidents (plan §6): report, confirm, resolve, list, rates.

ACCESSED VIA: 3d-api routes_incidents.py (→ `3d printops incidents …`, HA, the bot, voice),
printops.watch_incidents (drafts from `3d printer watch` events) and lint_incidents.

MODEL (migration 0060): kind / severity / resolution enums of plan §6; a snapshot of what was
printing (printer, head, spool → filament → vendor → lot, job, layer, z, slicer feature,
temperatures, speed, volumetric flow, fan, days since dried, humidity) filled by
printops.incident_context and overridable field by field by the reporter.

LIFECYCLE: draft (automatic report awaiting a human) → open → resolved; draft/open → dismissed.
Rates count only `open` and `resolved` incidents: a draft is unconfirmed, a dismissed one was
not real.

RATES (`stats`): incidents per kg printed and per print-hour, grouped by vendor, filament, lot,
printer or head, over a window. The denominators are printops.exposure rows of the same group
in the same window — never absolute counts (a lot that printed 5 kg is expected to show more
incidents than one that printed 200 g).

INVARIANTS:
  - Every change is one transaction with its event: incident.created / confirmed / updated /
    resolved / dismissed (subject `incident:<id>`).
  - `source_key` makes automatic reports idempotent: the same watch event never makes two
    incidents.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from printops import db
from printops import events as ev

KINDS = (
    "clog", "stringing", "oozing_blobs", "warping", "delamination", "under_extrusion",
    "over_extrusion", "moisture_popping", "brittleness", "poor_bed_adhesion", "layer_shift",
    "colour_off", "runout", "tangle", "heat_creep", "nozzle_drag", "other",
)
# Plan §6 spells some kinds with a slash; the stored name is the first, these are accepted too.
KIND_ALIASES = {
    "layer_split": "delamination", "snap": "brittleness", "brittle": "brittleness",
    "oozing": "oozing_blobs", "blobs": "oozing_blobs", "color_off": "colour_off",
    "jam": "clog", "bed_adhesion": "poor_bed_adhesion", "adhesion": "poor_bed_adhesion",
    "underextrusion": "under_extrusion", "overextrusion": "over_extrusion",
}
SEVERITIES = ("cosmetic", "part_lost", "print_lost", "hardware_risk")
RESOLUTIONS = (
    "dried", "temp_change", "speed_change", "retraction_change", "cleaned_nozzle",
    "replaced_nozzle", "glue_plate", "recalibrated", "discarded_spool", "none",
)
RESOLUTION_ALIASES = {"glue": "glue_plate", "plate": "glue_plate", "glue/plate": "glue_plate"}
REPORTERS = ("voice", "telegram", "cli", "station", "watch", "vision", "api")
STATUSES = ("draft", "open", "resolved", "dismissed")
COUNTED = ("open", "resolved")

# The snapshot of what was printing; filled by incident_context, editable by the reporter.
SNAPSHOT_INT = ("spool_id", "filament_id", "vendor_id", "layer")
SNAPSHOT_FLOAT = (
    "z_mm", "nozzle_temp", "bed_temp", "chamber_temp", "speed_mm_s", "volumetric_mm3_s",
    "fan_pct", "days_since_dried", "dry_box_rh", "room_rh",
)
SNAPSHOT_TEXT = (
    "printer", "head", "vendor", "material", "filament_name", "lot_nr", "job_id", "job_file",
    "feature",
)
SNAPSHOT = (*SNAPSHOT_TEXT, *SNAPSHOT_INT, *SNAPSHOT_FLOAT)
EDITABLE = (*SNAPSHOT, "kind", "severity", "note", "tags", "photos", "confidence", "at",
            "linked_calibration_id")

MAX_LIST = 1000
GROUPS = ("vendor", "filament", "lot", "printer", "head")
# Below both, a rate is flagged low_exposure: one incident in 50 g is not a trend.
LOW_EXPOSURE_KG = 0.25
LOW_EXPOSURE_HOURS = 2.0


class IncidentError(Exception):
    """A request that cannot be done; `status` is the HTTP status 3d-api answers."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Incident:
    id: int
    at: str
    created_at: str
    updated_at: str
    status: str
    reported_by: str
    confidence: float
    kind: str
    severity: str
    printer: str | None
    head: str | None
    spool_id: int | None
    filament_id: int | None
    vendor_id: int | None
    vendor: str | None
    material: str | None
    filament_name: str | None
    lot_nr: str | None
    job_id: str | None
    job_file: str | None
    layer: int | None
    z_mm: float | None
    feature: str | None
    nozzle_temp: float | None
    bed_temp: float | None
    chamber_temp: float | None
    speed_mm_s: float | None
    volumetric_mm3_s: float | None
    fan_pct: float | None
    days_since_dried: float | None
    dry_box_rh: float | None
    room_rh: float | None
    resolution_kind: str | None
    resolution_text: str | None
    resolved_at: str | None
    linked_calibration_id: int | None
    photos: list[str]
    tags: list[str]
    note: str | None
    context: dict[str, Any]
    source_key: str | None
    actor: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Incident:
        data = {k: row[k] for k in row.keys()}  # noqa: SIM118 — sqlite3.Row iterates values
        for key in ("photos", "tags", "context"):
            data[key] = json.loads(data[key])
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def label(self) -> str:
        where = "/".join(x for x in (self.printer, self.head) if x) or "?"
        return f"#{self.id} {self.kind} ({self.severity}) on {where}"


# -- parsing ---------------------------------------------------------------------------------


def norm_kind(raw: Any) -> str:
    key = _key(raw, "kind")
    key = KIND_ALIASES.get(key, key)
    if key not in KINDS:
        raise IncidentError(422, f"unknown incident kind {raw!r}; one of: {', '.join(KINDS)}")
    return key


def norm_severity(raw: Any) -> str:
    key = _key(raw, "severity")
    if key not in SEVERITIES:
        raise IncidentError(422, f"unknown severity {raw!r}; one of: {', '.join(SEVERITIES)}")
    return key


def norm_resolution(raw: Any) -> str:
    key = _key(raw, "resolution_kind")
    key = RESOLUTION_ALIASES.get(key, key)
    if key not in RESOLUTIONS:
        raise IncidentError(422, f"unknown resolution {raw!r}; one of: {', '.join(RESOLUTIONS)}")
    return key


def _key(raw: Any, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise IncidentError(422, f"{what} must be a non-empty string, got {raw!r}")
    return raw.strip().casefold().replace("-", "_").replace(" ", "_")


_SINCE = re.compile(r"^(\d+(?:\.\d+)?)\s*([hdwmy])$")
_SINCE_UNIT = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}


def parse_since(raw: str, now: datetime.datetime | None = None) -> datetime.datetime:
    """`90d`, `12h`, `2w`, `6m` (30 d), `1y`, or an ISO date/time (naive = UTC) → aware UTC."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    text = raw.strip().lower()
    if m := _SINCE.fullmatch(text):
        return now - datetime.timedelta(days=float(m[1]) * _SINCE_UNIT[m[2]])
    try:
        return parse_ts(raw.strip())
    except ValueError:
        raise IncidentError(422, f"since must look like 90d / 12h / 2w / 2026-09-01, got {raw!r}") from None


def parse_ts(raw: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def iso(dt: datetime.datetime) -> str:
    """The DB's timestamp format (db.utc_now): millisecond ISO-8601 UTC with `Z`."""
    u = dt.astimezone(datetime.timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def _num(body: Mapping[str, Any], key: str, *, integer: bool = False) -> float | int | None:
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise IncidentError(422, f"{key} must be a number, got {v!r}")
    if integer:
        if int(v) != v:
            raise IncidentError(422, f"{key} must be an integer, got {v!r}")
        return int(v)
    return float(v)


def _text(body: Mapping[str, Any], key: str) -> str | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise IncidentError(422, f"{key} must be a string, got {v!r}")
    return v.strip() or None


def _strings(body: Mapping[str, Any], key: str) -> list[str] | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise IncidentError(422, f"{key} must be a list of non-empty strings, got {v!r}")
    return [x.strip() for x in v]


def snapshot_from(body: Mapping[str, Any]) -> dict[str, Any]:
    """The snapshot fields present in a request body, type-checked (absent keys are left out)."""
    out: dict[str, Any] = {}
    for key in SNAPSHOT_TEXT:
        if key in body:
            out[key] = _text(body, key)
    for key in SNAPSHOT_INT:
        if key in body:
            out[key] = _num(body, key, integer=True)
    for key in SNAPSHOT_FLOAT:
        if key in body:
            out[key] = _num(body, key)
    for key in ("dry_box_rh", "room_rh", "fan_pct"):
        v = out.get(key)
        if v is not None and not 0 <= v <= 100:
            raise IncidentError(422, f"{key} must be 0..100 %, got {v}")
    return out


@dataclass
class Report:
    """A new incident as reported (by a person, voice, the watch, vision)."""

    kind: str
    severity: str
    reported_by: str = "api"
    status: str = "open"  # open | draft
    at: str | None = None
    confidence: float = 1.0
    note: str | None = None
    tags: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    linked_calibration_id: int | None = None
    source_key: str | None = None
    given: dict[str, Any] = field(default_factory=dict)  # snapshot fields the reporter set

    @classmethod
    def from_dict(cls, body: Mapping[str, Any]) -> Report:
        known = {"kind", "severity", "reported_by", "draft", "at", "confidence", "note", "tags",
                 "photos", "linked_calibration_id", "source_key", "auto_context", *SNAPSHOT}
        unknown = sorted(set(body) - known)
        if unknown:
            raise IncidentError(422, f"unknown field(s): {', '.join(unknown)}")
        reported_by = _text(body, "reported_by") or "api"
        if reported_by not in REPORTERS:
            raise IncidentError(422, f"reported_by must be one of {', '.join(REPORTERS)}, got {reported_by!r}")
        draft = body.get("draft", False)
        if not isinstance(draft, bool):
            raise IncidentError(422, f"draft must be true/false, got {draft!r}")
        confidence = _num(body, "confidence")
        if confidence is not None and not 0 <= confidence <= 1:
            raise IncidentError(422, f"confidence must be 0..1, got {confidence}")
        at = _text(body, "at")
        if at is not None:
            try:
                at = iso(parse_ts(at))
            except ValueError:
                raise IncidentError(422, f"at must be an ISO date/time, got {at!r}") from None
        cal = _num(body, "linked_calibration_id", integer=True)
        return cls(
            kind=norm_kind(body.get("kind")),
            severity=norm_severity(body.get("severity")),
            reported_by=reported_by,
            status="draft" if draft else "open",
            at=at,
            confidence=1.0 if confidence is None else float(confidence),
            note=_text(body, "note"),
            tags=_strings(body, "tags") or [],
            photos=_strings(body, "photos") or [],
            linked_calibration_id=int(cal) if cal is not None else None,
            source_key=_text(body, "source_key"),
            given=snapshot_from(body),
        )


def obj(v: Any) -> dict[str, Any]:
    """`v` when it is a JSON object, else {} (for loosely shaped printer / watch payloads)."""
    return v if isinstance(v, dict) else {}


def spool_snapshot(spool: Mapping[str, Any]) -> dict[str, Any]:
    """The identity columns of a Spoolman spool (GET /spool/{id}, filament + vendor nested)."""
    fil = spool.get("filament") or {}
    vendor = fil.get("vendor") or {}
    return {
        "spool_id": int(spool["id"]),
        "filament_id": fil.get("id"),
        "vendor_id": vendor.get("id"),
        "vendor": vendor.get("name"),
        "material": fil.get("material"),
        "filament_name": fil.get("name"),
        "lot_nr": spool.get("lot_nr") or None,
    }


# -- storage ---------------------------------------------------------------------------------


def get(conn: sqlite3.Connection, incident_id: int) -> Incident:
    row = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise IncidentError(404, f"no incident #{incident_id}")
    return Incident.from_row(row)


def by_source_key(conn: sqlite3.Connection, key: str) -> Incident | None:
    row = conn.execute("SELECT * FROM incident WHERE source_key = ?", (key,)).fetchone()
    return Incident.from_row(row) if row else None


def _summary(inc: Incident) -> dict[str, Any]:
    return {
        "id": inc.id, "status": inc.status, "kind": inc.kind, "severity": inc.severity,
        "printer": inc.printer, "head": inc.head, "spool_id": inc.spool_id, "lot_nr": inc.lot_nr,
        "reported_by": inc.reported_by,
    }


def create(
    conn: sqlite3.Connection,
    report: Report,
    *,
    actor: str,
    auto: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> Incident:
    """Store a reported incident. `auto` = auto-filled snapshot (incident_context); the
    reporter's own fields (`report.given`) win over it. `context` = provenance + warnings."""
    snap = {k: v for k, v in (auto or {}).items() if k in SNAPSHOT and v is not None}
    snap.update(report.given)
    now = db.utc_now()
    cols = {
        "at": report.at or now, "created_at": now, "updated_at": now, "status": report.status,
        "reported_by": report.reported_by, "confidence": report.confidence, "kind": report.kind,
        "severity": report.severity, **{k: snap.get(k) for k in SNAPSHOT},
        "linked_calibration_id": report.linked_calibration_id,
        "photos": json.dumps(report.photos), "tags": json.dumps(sorted(set(report.tags))),
        "note": report.note, "context": json.dumps(dict(context or {}), sort_keys=True, default=str),
        "source_key": report.source_key, "actor": actor,
    }
    with db.transaction(conn):
        try:
            cur = conn.execute(
                f"INSERT INTO incident ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                tuple(cols.values()),
            )
        except sqlite3.IntegrityError as e:
            if report.source_key and "source_key" in str(e):
                raise IncidentError(409, f"an incident for {report.source_key!r} already exists") from e
            raise
        inc = get(conn, int(cur.lastrowid or 0))
        ev.emit(conn, "incident.created", f"incident:{inc.id}", _summary(inc), actor)
    return inc


def _check_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(EDITABLE) - {"add_tags", "remove_tags", "add_photos"})
    if unknown:
        raise IncidentError(422, f"not editable: {', '.join(unknown)}; editable: {', '.join(EDITABLE)}")
    out = snapshot_from(changes)
    if "kind" in changes:
        out["kind"] = norm_kind(changes["kind"])
    if "severity" in changes:
        out["severity"] = norm_severity(changes["severity"])
    if "note" in changes:
        out["note"] = _text(changes, "note")
    if "confidence" in changes:
        c = _num(changes, "confidence")
        if c is None or not 0 <= c <= 1:
            raise IncidentError(422, f"confidence must be 0..1, got {changes['confidence']!r}")
        out["confidence"] = c
    if "at" in changes:
        raw = _text(changes, "at")
        try:
            out["at"] = iso(parse_ts(raw or ""))
        except ValueError:
            raise IncidentError(422, f"at must be an ISO date/time, got {raw!r}") from None
    if "linked_calibration_id" in changes:
        out["linked_calibration_id"] = _num(changes, "linked_calibration_id", integer=True)
    for key in ("tags", "photos", "add_tags", "remove_tags", "add_photos"):
        if key in changes:
            out[key] = _strings(changes, key) or []
    return out


def _apply_lists(inc: Incident, ch: dict[str, Any]) -> dict[str, Any]:
    tags = ch.pop("tags", inc.tags)
    tags = sorted((set(tags) | set(ch.pop("add_tags", []))) - set(ch.pop("remove_tags", [])))
    photos = list(ch.pop("photos", inc.photos))
    photos += [p for p in ch.pop("add_photos", []) if p not in photos]
    if tags != inc.tags:
        ch["tags"] = tags
    if photos != inc.photos:
        ch["photos"] = photos
    return ch


def _write(conn: sqlite3.Connection, inc: Incident, cols: Mapping[str, Any]) -> None:
    values = {k: json.dumps(v) if k in ("tags", "photos") else v for k, v in cols.items()}
    values["updated_at"] = db.utc_now()
    conn.execute(
        f"UPDATE incident SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
        (*values.values(), inc.id),
    )


def update(conn: sqlite3.Connection, incident_id: int, changes: Mapping[str, Any], *, actor: str) -> Incident:
    """Edit fields (fix a wrong spool, add photos/tags, refine kind or severity)."""
    ch = _check_changes(changes)
    with db.transaction(conn):
        inc = get(conn, incident_id)
        ch = _apply_lists(inc, ch)
        diff = {k: v for k, v in ch.items() if getattr(inc, k) != v}
        if not diff:
            return inc
        _write(conn, inc, diff)
        ev.emit(conn, "incident.updated", f"incident:{inc.id}",
                {"id": inc.id, "changes": {k: {"from": getattr(inc, k), "to": v} for k, v in diff.items()}},
                actor)
        return get(conn, incident_id)


def confirm(conn: sqlite3.Connection, incident_id: int, changes: Mapping[str, Any], *, actor: str) -> Incident:
    """A human confirms an automatic draft (optionally correcting it): draft → open."""
    ch = _check_changes(changes)
    with db.transaction(conn):
        inc = get(conn, incident_id)
        if inc.status != "draft":
            raise IncidentError(409, f"incident #{inc.id} is {inc.status}, not a draft")
        ch = _apply_lists(inc, ch)
        ch = {k: v for k, v in ch.items() if getattr(inc, k) != v}
        _write(conn, inc, {**ch, "status": "open"})
        ev.emit(conn, "incident.confirmed", f"incident:{inc.id}", {"id": inc.id, "changes": sorted(ch)}, actor)
        return get(conn, incident_id)


def resolve(
    conn: sqlite3.Connection,
    incident_id: int,
    resolution_kind: str,
    *,
    actor: str,
    text: str | None = None,
    calibration_id: int | None = None,
    at: str | None = None,
) -> Incident:
    """What fixed it: open → resolved (a draft must be confirmed first)."""
    kind = norm_resolution(resolution_kind)
    when = iso(parse_ts(at)) if at else db.utc_now()
    with db.transaction(conn):
        inc = get(conn, incident_id)
        if inc.status == "draft":
            raise IncidentError(409, f"incident #{inc.id} is a draft: confirm or dismiss it first")
        if inc.status != "open":
            raise IncidentError(409, f"incident #{inc.id} is already {inc.status}")
        cols: dict[str, Any] = {"status": "resolved", "resolution_kind": kind, "resolution_text": text,
                                "resolved_at": when}
        if calibration_id is not None:
            cols["linked_calibration_id"] = calibration_id
        _write(conn, inc, cols)
        ev.emit(conn, "incident.resolved", f"incident:{inc.id}",
                {"id": inc.id, "resolution_kind": kind, "linked_calibration_id": calibration_id}, actor)
        return get(conn, incident_id)


def dismiss(conn: sqlite3.Connection, incident_id: int, *, actor: str, reason: str | None = None) -> Incident:
    """Not a real incident (a false watch alarm, a duplicate): draft/open → dismissed."""
    with db.transaction(conn):
        inc = get(conn, incident_id)
        if inc.status not in ("draft", "open"):
            raise IncidentError(409, f"incident #{inc.id} is {inc.status}; only drafts and open ones can be dismissed")
        cols: dict[str, Any] = {"status": "dismissed"}
        if reason:
            cols["note"] = f"{inc.note}\n[dismissed] {reason}" if inc.note else f"[dismissed] {reason}"
        _write(conn, inc, cols)
        ev.emit(conn, "incident.dismissed", f"incident:{inc.id}", {"id": inc.id, "reason": reason}, actor)
        return get(conn, incident_id)


def attach_evidence(conn: sqlite3.Connection, inc: Incident, evidence: Mapping[str, Any], *, actor: str) -> Incident:
    """Append one more piece of evidence (another watch event of the same failure) to a draft."""
    ctx = dict(inc.context)
    ctx["evidence"] = [*ctx.get("evidence", []), dict(evidence)]
    with db.transaction(conn):
        conn.execute("UPDATE incident SET context = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(ctx, sort_keys=True, default=str), db.utc_now(), inc.id))
        ev.emit(conn, "incident.updated", f"incident:{inc.id}",
                {"id": inc.id, "evidence": evidence.get("kind")}, actor)
        return get(conn, inc.id)


def list_incidents(
    conn: sqlite3.Connection,
    *,
    status: Sequence[str] | None = None,
    printer: str | None = None,
    head: str | None = None,
    kind: str | None = None,
    spool_id: int | None = None,
    lot: str | None = None,
    since: datetime.datetime | None = None,
    limit: int = 50,
) -> list[Incident]:
    where, args = ["1 = 1"], list[Any]()
    if status:
        bad = sorted(set(status) - set(STATUSES))
        if bad:
            raise IncidentError(422, f"unknown status {', '.join(bad)}; one of: {', '.join(STATUSES)}")
        where.append(f"status IN ({', '.join('?' for _ in status)})")
        args += list(status)
    for col, value in (("printer", printer), ("lot_nr", lot), ("spool_id", spool_id)):
        if value is not None:
            where.append(f"{col} = ?")
            args.append(value)
    if head is not None:
        where.append("head = ? COLLATE NOCASE")
        args.append(head)
    if kind is not None:
        where.append("kind = ?")
        args.append(norm_kind(kind))
    if since is not None:
        where.append("at >= ?")
        args.append(iso(since))
    if not 1 <= limit <= MAX_LIST:
        raise IncidentError(422, f"limit must be 1..{MAX_LIST}, got {limit}")
    rows = conn.execute(
        f"SELECT * FROM incident WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [Incident.from_row(r) for r in rows]


# -- rates -----------------------------------------------------------------------------------


def group_of(by: str, row: Mapping[str, Any]) -> tuple[Any, str]:
    """(key, label) of an incident or exposure row (they share the snapshot columns)."""
    vendor = row.get("vendor")
    fil = " ".join(str(x) for x in (vendor, row.get("material"), row.get("filament_name")) if x)
    if by == "vendor":
        if row.get("vendor_id") is not None:
            return ("vendor", row["vendor_id"]), vendor or f"vendor #{row['vendor_id']}"
        if vendor:
            return ("vendor", vendor.casefold()), vendor
        return None, "unknown vendor"
    if by == "filament":
        if row.get("filament_id") is not None:
            return ("filament", row["filament_id"]), fil or f"filament #{row['filament_id']}"
        return None, "unknown filament"
    if by == "lot":
        if row.get("filament_id") is None:
            return None, "unknown spool"
        lot = row.get("lot_nr")
        name = fil or f"filament #{row['filament_id']}"
        return ("lot", row["filament_id"], lot), f"lot {lot} · {name}" if lot else f"no lot · {name}"
    if by == "printer":
        p = row.get("printer")
        return (("printer", p), p) if p else (None, "unknown printer")
    if by == "head":
        p, h = row.get("printer"), row.get("head")
        if p and h:
            return ("head", p, h.upper()), f"{p}/{h.upper()}"
        return None, f"{p or 'unknown printer'}/unknown head"
    raise IncidentError(422, f"by must be one of {', '.join(GROUPS)}, got {by!r}")


PARENT = {"lot": "vendor", "filament": "vendor", "head": "printer"}


@dataclass
class _Acc:
    label: str
    incidents: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    grams: float = 0.0
    grams_known: bool = False
    hours: float = 0.0
    exposure_rows: int = 0

    def rates(self) -> dict[str, Any]:
        kg = self.grams / 1000 if self.grams_known else None
        per_kg = self.incidents / kg if kg else None
        per_hour = self.incidents / self.hours if self.hours > 0 else None
        return {
            "incidents": self.incidents,
            "kinds": dict(sorted(self.kinds.items(), key=lambda kv: (-kv[1], kv[0]))),
            "kg": round(kg, 3) if kg is not None else None,
            "hours": round(self.hours, 2),
            "per_kg": round(per_kg, 3) if per_kg is not None else None,
            "per_hour": round(per_hour, 4) if per_hour is not None else None,
            "low_exposure": (kg or 0) < LOW_EXPOSURE_KG and self.hours < LOW_EXPOSURE_HOURS,
        }


def _accumulate(by: str, incidents: Iterable[Mapping[str, Any]], exposure: Iterable[Mapping[str, Any]]) -> dict[Any, _Acc]:
    acc: dict[Any, _Acc] = {}
    for row in incidents:
        key, label = group_of(by, row)
        a = acc.setdefault(key, _Acc(label))
        a.incidents += 1
        a.kinds[row["kind"]] = a.kinds.get(row["kind"], 0) + 1
    for row in exposure:
        key, label = group_of(by, row)
        a = acc.setdefault(key, _Acc(label))
        a.hours += float(row["hours"])
        a.exposure_rows += 1
        if row.get("grams") is not None:
            a.grams += float(row["grams"])
            a.grams_known = True
    return acc


def stats(
    conn: sqlite3.Connection,
    by: str,
    *,
    since: datetime.datetime | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Incident rates per kg printed and per print-hour, grouped `by`, over [since, now]."""
    if by not in GROUPS:
        raise IncidentError(422, f"by must be one of {', '.join(GROUPS)}, got {by!r}")
    where, args = [f"status IN ({', '.join('?' for _ in COUNTED)})"], list(COUNTED)
    exp_where, exp_args = ["1 = 1"], []
    if since is not None:
        where.append("at >= ?")
        args.append(iso(since))
        exp_where.append("started_at >= ?")
        exp_args.append(iso(since))
    if kind is not None:
        where.append("kind = ?")
        args.append(norm_kind(kind))
    inc_rows = [dict(r) for r in conn.execute(f"SELECT * FROM incident WHERE {' AND '.join(where)}", args)]
    exp_rows = [dict(r) for r in conn.execute(f"SELECT * FROM exposure WHERE {' AND '.join(exp_where)}", exp_args)]

    groups = _accumulate(by, inc_rows, exp_rows)
    parent_by = PARENT.get(by)
    parents = _accumulate(parent_by, inc_rows, exp_rows) if parent_by else {}
    overall = _Acc("all")
    for a in groups.values():
        overall.incidents += a.incidents
        overall.hours += a.hours
        overall.grams += a.grams
        overall.grams_known = overall.grams_known or a.grams_known
        for k, n in a.kinds.items():
            overall.kinds[k] = overall.kinds.get(k, 0) + n

    out_groups = []
    for key, a in groups.items():
        g: dict[str, Any] = {"key": list(key) if key else None, "label": a.label, **a.rates()}
        if parent_by:
            sample = next((r for r in (*inc_rows, *exp_rows) if group_of(by, r)[0] == key), None)
            pkey, plabel = group_of(parent_by, sample) if sample else (None, "")
            p = parents.get(pkey)
            if p is not None and key is not None:
                pr = p.rates()
                g["parent"] = {"by": parent_by, "label": plabel, "per_kg": pr["per_kg"], "per_hour": pr["per_hour"]}
        out_groups.append(g)
    out_groups.sort(key=lambda g: (g["per_kg"] is None, -(g["per_kg"] or 0), -(g["per_hour"] or 0), -g["incidents"], g["label"]))

    notes = []
    if not exp_rows:
        notes.append("no exposure recorded in this window: rates need `3d printops incidents exposure sync` "
                     "(Moonraker job history) or manual `exposure add` rows")
    elif not overall.grams_known:
        notes.append("exposure rows carry no grams: per-kg rates are unknown")
    unattributed = groups.get(None)
    if unattributed and unattributed.incidents:
        notes.append(f"{unattributed.incidents} incident(s) have no {by} (no spool/printer in the snapshot)")
    return {
        "by": by,
        "since": iso(since) if since else None,
        "kind": norm_kind(kind) if kind else None,
        "overall": overall.rates(),
        "groups": out_groups,
        "notes": notes,
    }
