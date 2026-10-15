"""exposure.py — how much each printer head printed, from which spool: the denominators of incident rates.

ACCESSED VIA: printops.incidents.stats (rates per kg and per print-hour), 3d-api
routes_incidents (`/v1/exposure…`) → `3d printops incidents exposure ls|add|sync`.

SOURCES (table `exposure`, migration 0060):
  moonraker  `sync_moonraker`: the printer's finished jobs from Moonraker job history (one
             read-only GET /server/history/list per page). One row per job and head that
             extruded: grams = the slicer's per-tool `filament_weight` scaled by the share of
             the job actually printed (filament_used / filament_total); hours = the job's
             print_duration split across its heads in proportion to their grams (so the hours
             of a printer add up to its real print time, and a lot's hours are the time spent
             printing it).
  manual     `add_manual`: offline printers (hackerspace, neighbours — never contacted, D7),
             or a job the history lost.
  job        the job ledger (#170) once it exists.

SPOOL ATTRIBUTION: the spool in a head when the job started comes from the slot event history
(`slot.loaded` / `slot.unloaded`, printops.spools). A slot with no history at all falls back to
its current Spoolman binding, marked in the row's note; before a slot's first event the spool
is unknown (row without spool → "unknown" group in the rates).

INVARIANTS: rows are immutable (a finished job never changes); syncing twice adds nothing
(UNIQUE source + source_key); in-progress jobs are skipped until they finish.
"""

from __future__ import annotations

import datetime
import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from printops import db, printer_read
from printops import events as ev
from printops.incidents import IncidentError, iso, spool_snapshot
from printops.spoolman import Spoolman, SpoolmanError

PAGE = 50
DEFAULT_LIMIT = 50
PACE_S = 5.0  # between history pages: ≤ 1 request per 5 s per printer


@dataclass
class Exposure:
    id: int
    source: str
    source_key: str
    printer: str
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
    job_status: str | None
    started_at: str
    ended_at: str | None
    hours: float
    grams: float | None
    note: str | None
    actor: str
    recorded_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Exposure:
        return cls(**{k: row[k] for k in row.keys()})  # noqa: SIM118 — sqlite3.Row iterates values

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


COLUMNS = (
    "source", "source_key", "printer", "head", "spool_id", "filament_id", "vendor_id", "vendor",
    "material", "filament_name", "lot_nr", "job_id", "job_file", "job_status", "started_at",
    "ended_at", "hours", "grams", "note", "actor", "recorded_at",
)


def _insert(conn: sqlite3.Connection, row: Mapping[str, Any]) -> int | None:
    """INSERT OR IGNORE; the new id, or None when (source, source_key) already exists."""
    cur = conn.execute(
        f"INSERT OR IGNORE INTO exposure ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})",
        tuple(row.get(c) for c in COLUMNS),
    )
    return int(cur.lastrowid or 0) if cur.rowcount else None


def list_exposure(
    conn: sqlite3.Connection,
    *,
    printer: str | None = None,
    since: datetime.datetime | None = None,
    limit: int = 100,
) -> list[Exposure]:
    where, args = ["1 = 1"], []
    if printer:
        where.append("printer = ?")
        args.append(printer)
    if since:
        where.append("started_at >= ?")
        args.append(iso(since))
    if not 1 <= limit <= 5000:
        raise IncidentError(422, f"limit must be 1..5000, got {limit}")
    rows = conn.execute(
        f"SELECT * FROM exposure WHERE {' AND '.join(where)} ORDER BY started_at DESC, id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [Exposure.from_row(r) for r in rows]


def totals(conn: sqlite3.Connection, *, since: datetime.datetime | None = None) -> dict[str, Any]:
    where, args = ("started_at >= ?", [iso(since)]) if since else ("1 = 1", [])
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(hours), 0) AS h, SUM(grams) AS g, MIN(started_at) AS first, "
        f"MAX(started_at) AS last FROM exposure WHERE {where}",
        args,
    ).fetchone()
    return {"rows": row["n"], "hours": round(row["h"], 2), "kg": round(row["g"] / 1000, 3) if row["g"] is not None else None,
            "first": row["first"], "last": row["last"]}


# -- spool at a moment -------------------------------------------------------------------------


def spool_in_slot_at(conn: sqlite3.Connection, printer: str, head: str, at: str) -> tuple[int | None, bool]:
    """(spool id loaded in `printer/head` at `at`, slot has any history). From slot events."""
    subject = f"slot:{printer}/{head}"
    row = conn.execute(
        "SELECT kind, payload FROM events WHERE subject = ? AND kind IN ('slot.loaded', 'slot.unloaded') "
        "AND at <= ? ORDER BY id DESC LIMIT 1",
        (subject, at),
    ).fetchone()
    if row is not None:
        sid = json.loads(row["payload"]).get("spool_id") if row["kind"] == "slot.loaded" else None
        return (int(sid) if isinstance(sid, int) else None), True
    any_row = conn.execute(
        "SELECT 1 FROM events WHERE subject = ? AND kind IN ('slot.loaded', 'slot.unloaded') LIMIT 1", (subject,)
    ).fetchone()
    return None, any_row is not None


@dataclass
class _Spools:
    """Spoolman reads shared by one sync: each spool fetched once; the slot bindings once."""

    spoolman: Spoolman | None
    cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    bindings: dict[str, int] | None = None
    errors: list[str] = field(default_factory=list)

    def snapshot(self, spool_id: int) -> dict[str, Any]:
        if spool_id not in self.cache:
            snap: dict[str, Any] = {"spool_id": spool_id}
            if self.spoolman is not None:
                try:
                    snap = spool_snapshot(self.spoolman.get("spool", spool_id))
                except SpoolmanError as e:
                    self.errors.append(f"spool #{spool_id}: {e.message}")
            self.cache[spool_id] = snap
        return self.cache[spool_id]

    def bound_now(self, slot_key: str) -> int | None:
        if self.spoolman is None:
            return None
        if self.bindings is None:
            from printops.spools import slot_of

            self.bindings = {}
            try:
                for sp in self.spoolman.find("spool"):
                    if s := slot_of(sp):
                        self.bindings[s] = int(sp["id"])
            except SpoolmanError as e:
                self.errors.append(f"Spoolman unreadable, spools unknown: {e.message}")
        return self.bindings.get(slot_key)


def _attribute(conn: sqlite3.Connection, spools: _Spools, printer: str, head: str, at: str) -> tuple[dict[str, Any], str | None]:
    spool_id, has_history = spool_in_slot_at(conn, printer, head, at)
    note = None
    if spool_id is None and not has_history:
        spool_id = spools.bound_now(f"{printer}/{head}")
        if spool_id is not None:
            note = "spool = the slot's current Spoolman binding (no slot history before this job)"
    return (spools.snapshot(spool_id) if spool_id is not None else {}), note


# -- Moonraker history -------------------------------------------------------------------------


def _epoch_iso(v: Any) -> str | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
        return None
    return iso(datetime.datetime.fromtimestamp(float(v), datetime.timezone.utc))


def _floats(v: Any) -> list[float] | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [float(v)]
    if isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        return [float(x) for x in v]
    return None


def job_rows(job: Mapping[str, Any], heads: Mapping[int, str]) -> list[dict[str, Any]]:
    """Pure: one exposure row per head that extruded in a finished Moonraker history job
    (without spool attribution). `heads` = tool index → head label (fleet)."""
    meta = job.get("metadata") or {}
    hours_total = max(float(job.get("print_duration") or 0.0), 0.0) / 3600
    weights = _floats(meta.get("filament_weight") if "filament_weight" in meta else meta.get("filament_weights"))
    used, planned = job.get("filament_used"), meta.get("filament_total")
    ratio: float | None
    if isinstance(used, (int, float)) and isinstance(planned, (int, float)) and planned > 0:
        ratio = min(max(float(used) / float(planned), 0.0), 1.0)
    else:
        ratio = 1.0 if job.get("status") == "completed" else None
    base = {
        "job_id": str(job.get("job_id")), "job_file": job.get("filename"), "job_status": job.get("status"),
        "started_at": _epoch_iso(job.get("start_time")), "ended_at": _epoch_iso(job.get("end_time")),
    }
    if weights is None or not any(w > 0 for w in weights):
        if len(heads) != 1:
            return [{**base, "head": None, "hours": hours_total, "grams": None,
                     "note": "no per-tool filament weights in the job metadata"}] if hours_total > 0 else []
        (only,) = heads.values()
        total = _floats(meta.get("filament_weight_total"))
        grams = total[0] * ratio if total and ratio is not None else None
        return [{**base, "head": only, "hours": hours_total, "grams": grams, "note": None}] if hours_total or grams else []
    planned_total = sum(w for w in weights if w > 0)
    rows = []
    for i, w in enumerate(weights):
        if w <= 0:
            continue
        label = heads.get(i)
        grams = w * ratio if ratio is not None else None
        hours = hours_total * w / planned_total
        if not hours and not grams:
            continue
        rows.append({**base, "head": label, "hours": hours, "grams": grams,
                     "note": None if label else f"tool T{i} is not a head in fleet/printers.yaml"})
    return rows


@dataclass
class SyncReport:
    printer: str
    jobs_seen: int = 0
    in_progress: int = 0
    added: list[dict[str, Any]] = field(default_factory=list)
    already: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"printer": self.printer, "jobs_seen": self.jobs_seen, "in_progress": self.in_progress,
                "added": self.added, "already_synced": self.already, "warnings": self.warnings}


HistoryGet = Callable[[Any, str], Any]


def sync_moonraker(
    conn: sqlite3.Connection,
    spoolman: Spoolman | None,
    printer: str,
    *,
    actor: str,
    limit: int = DEFAULT_LIMIT,
    get: HistoryGet = printer_read.moonraker_get,
    pace_s: float = PACE_S,
) -> SyncReport:
    """Pull the newest `limit` jobs of a Moonraker printer's history into `exposure`."""
    try:
        entry = printer_read.device_for(printer)
    except LookupError as e:
        raise IncidentError(404, str(e)) from e
    except printer_read.PrinterReadError as e:
        raise IncidentError(503, str(e)) from e
    if entry.device is None:
        raise IncidentError(409, f"{printer} is an offline fleet printer (never contacted): "
                                 "record its printing with `exposure add`")
    if entry.device.adapter != "moonraker":
        raise IncidentError(422, f"{printer} ({entry.device.adapter}) has no Moonraker job history; "
                                 "record its printing with `exposure add` until the job ledger (#170) does")
    if not 1 <= limit <= 1000:
        raise IncidentError(422, f"limit must be 1..1000, got {limit}")
    heads = {h.index: h.label for h in entry.printer.heads}
    report = SyncReport(printer)
    jobs: list[Mapping[str, Any]] = []
    start = 0
    while len(jobs) < limit:
        if start:
            time.sleep(pace_s)
        n = min(PAGE, limit - len(jobs))
        try:
            page = get(entry.device, f"/server/history/list?limit={n}&start={start}&order=desc")
        except printer_read.PrinterReadError as e:
            raise IncidentError(502, str(e)) from e
        batch = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(batch, list):
            raise IncidentError(502, f"{printer}: history reply has no jobs list")
        jobs += [j for j in batch if isinstance(j, dict)]
        start += len(batch)
        if len(batch) < n:
            break
    spools = _Spools(spoolman)
    now = db.utc_now()
    with db.transaction(conn):
        for job in jobs:
            report.jobs_seen += 1
            if job.get("status") == "in_progress":
                report.in_progress += 1
                continue
            for row in job_rows(job, heads):
                if row["started_at"] is None:
                    report.warnings.append(f"job {row['job_id']}: no start time, skipped")
                    continue
                key = f"{printer}:{row['job_id']}:{row['head'] or '-'}"
                if conn.execute("SELECT 1 FROM exposure WHERE source = 'moonraker' AND source_key = ?", (key,)).fetchone():
                    report.already += 1
                    continue
                snap, note = _attribute(conn, spools, printer, row["head"], row["started_at"]) if row["head"] else ({}, None)
                full = {**row, **snap, "source": "moonraker", "source_key": key, "printer": printer,
                        "note": "; ".join(x for x in (row["note"], note) if x) or None,
                        "actor": actor, "recorded_at": now}
                if _insert(conn, full) is not None:
                    report.added.append({k: full.get(k) for k in ("job_id", "head", "spool_id", "lot_nr", "hours", "grams")})
        if report.added:
            ev.emit(conn, "exposure.synced", f"printer:{printer}",
                    {"printer": printer, "added": len(report.added), "source": "moonraker"}, actor)
    report.warnings += spools.errors
    return report


def add_manual(
    conn: sqlite3.Connection,
    spoolman: Spoolman | None,
    body: Mapping[str, Any],
    *,
    actor: str,
) -> Exposure:
    """Record printing the history cannot see: {printer, head?, spool_id?, hours, grams?,
    started_at?, note?}. The spool's filament/vendor/lot are snapshotted from Spoolman."""
    from printops.incidents import parse_ts

    unknown = sorted(set(body) - {"printer", "head", "spool_id", "hours", "grams", "started_at", "note", "job_file"})
    if unknown:
        raise IncidentError(422, f"unknown field(s): {', '.join(unknown)}")
    printer = body.get("printer")
    if not isinstance(printer, str) or not printer:
        raise IncidentError(422, "printer is required")
    try:
        entry = printer_read.device_for(printer)
    except LookupError as e:
        raise IncidentError(404, str(e)) from e
    except printer_read.PrinterReadError as e:
        raise IncidentError(503, str(e)) from e
    head = body.get("head")
    if head is not None:
        match = next((h.label for h in entry.printer.heads if h.label.casefold() == str(head).casefold()), None)
        if match is None:
            raise IncidentError(404, f"{printer} has no head {head!r}; heads: {', '.join(h.label for h in entry.printer.heads)}")
        head = match
    elif len(entry.printer.heads) == 1:
        head = entry.printer.heads[0].label
    values: dict[str, float | None] = {}
    for key in ("hours", "grams"):
        v = body.get(key)
        if v is None:
            values[key] = None
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise IncidentError(422, f"{key} must be a number ≥ 0, got {v!r}")
        values[key] = float(v)
    if values["hours"] is None:
        raise IncidentError(422, "hours is required (print time; 0 when only grams are known)")
    started = body.get("started_at")
    try:
        started_at = iso(parse_ts(started)) if isinstance(started, str) else db.utc_now()
    except ValueError:
        raise IncidentError(422, f"started_at must be an ISO date/time, got {started!r}") from None
    spool_id = body.get("spool_id")
    if spool_id is not None and (isinstance(spool_id, bool) or not isinstance(spool_id, int)):
        raise IncidentError(422, f"spool_id must be an integer, got {spool_id!r}")
    spools = _Spools(spoolman)
    snap = spools.snapshot(spool_id) if spool_id is not None else {}
    if spools.errors:
        raise IncidentError(502, spools.errors[0])
    now = db.utc_now()
    row = {**snap, "source": "manual", "source_key": f"{printer}:{head or '-'}:{now}", "printer": printer,
           "head": head, "job_file": body.get("job_file"), "started_at": started_at, "hours": values["hours"],
           "grams": values["grams"], "note": body.get("note"), "actor": actor, "recorded_at": now}
    with db.transaction(conn):
        new_id = _insert(conn, row)
        if new_id is None:
            raise IncidentError(409, "the same manual exposure was just recorded")
        ev.emit(conn, "exposure.recorded", f"printer:{printer}",
                {"id": new_id, "printer": printer, "head": head, "spool_id": spool_id,
                 "hours": values["hours"], "grams": values["grams"]}, actor)
        return Exposure.from_row(conn.execute("SELECT * FROM exposure WHERE id = ?", (new_id,)).fetchone())
