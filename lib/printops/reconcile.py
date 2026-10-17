"""reconcile.py — compare what printers report in their slots with the spools bound there (plan §5.3).

WHAT: `bindings(spools)` reads the Spoolman spools whose extra `printops_slot` names a slot
  ("u1/T3"); `compare_printer` / `compare_unknown` turn a printer `Reading` (printops.slots) plus
  those bindings into `Finding`s; `sync` keeps the `anomaly` table (migration 0030) in step with
  the findings of one scope and emits the events; `Reconciler` ties it to the fleet, Spoolman, the
  latest readings and the DB (used by the 3d-api poller and routes_reconcile.py).

KINDS (evidence in anomaly.evidence):
  live — need a successful printer reading; never auto-resolved while the printer is unreadable:
    type_mismatch      printer material ≠ bound spool's filament.material (case/space-insensitive)
    color_mismatch     CIEDE2000(printer colour, spool colour) > max_delta_e (spool colour =
                       filament extra `measured_hex`, else color_hex / multi_color_hexes, nearest)
    tag_mismatch       the slot read an RFID UID that is not in the bound spool's `tag_uids`
    empty_with_spool   the printer says the slot is empty, a spool is bound to it
    unbound_filament   the printer says filament is loaded, no spool is bound to it
  binding — from Spoolman alone:
    multiple_spools    two or more spools bound to one slot
    unusable_head      a spool bound to a head the fleet marks usable: false
    unknown_slot       printops_slot names a printer or head that is not in the fleet
    location_conflict  the spool's Spoolman location is set and is not this slot ("slot:u1/T3"),
                       i.e. the spool is in two places at once
  write — from the event log (printops.head_sync, #400):
    printer_write_failed  the latest head write of a slot (`slot.printer_written` /
                       `slot.printer_write_failed`) failed for the spool still bound there. Clears
                       on a later successful write, when the binding changes, or — for a failed
                       filament write — when a live reading shows the head holding the spool's
                       material and colour (set on the touchscreen). A failed pressure-advance
                       restore clears only by writing again (re-run `spool load`) or by unbinding.
  slot.mismatch is emitted with every newly opened live kind and multiple_spools.

LIFECYCLE: one open/ack anomaly per dedupe_key (kind + subject + the evidence that identifies
  it, e.g. the printer's material, so a second swap is a new anomaly). A condition that is no
  longer found is resolved by "reconcile" (anomaly.resolved). ack = "known, leave it" until it
  clears; resolving by hand a condition that is still there opens a new anomaly on the next pass.

INVARIANTS: never writes Spoolman or a printer; all DB changes of one pass are one transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printops import events as ev
from printops.color import hex_delta_e, parse_hex
from printops.db import open_db, transaction, utc_now
from printops.fleet import Fleet, FleetPrinter
from printops.slots import LiveSlot, Reading
from printops.spoolman import Spoolman, decode_extra

log = logging.getLogger("printops.reconcile")

DEFAULT_MAX_DELTA_E = 15.0
LIVE_KINDS = (
    "type_mismatch",
    "color_mismatch",
    "tag_mismatch",
    "empty_with_spool",
    "unbound_filament",
)
BINDING_KINDS = (
    "multiple_spools",
    "unusable_head",
    "unknown_slot",
    "location_conflict",
)
WRITE_KINDS = ("printer_write_failed",)
KINDS = LIVE_KINDS + BINDING_KINDS + WRITE_KINDS
WRITE_EVENTS = ("slot.printer_written", "slot.printer_write_failed")  # printops.head_sync
SLOT_MISMATCH_KINDS = frozenset((*LIVE_KINDS, "multiple_spools"))
STATUSES = ("open", "ack", "resolved")
FLEET_SCOPE = "fleet"
ACTOR = "reconcile"


@dataclass(frozen=True)
class Binding:
    spool_id: int
    slot: str  # "u1/T3"
    printer: str
    head: str
    name: str
    material: str | None
    colors: tuple[str, ...]  # "#RRGGBB", measured first
    tag_uids: tuple[str, ...]
    location: str | None
    remaining_g: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "spool_id": self.spool_id,
            "slot": self.slot,
            "name": self.name,
            "material": self.material,
            "colors": list(self.colors),
            "tag_uids": list(self.tag_uids),
            "location": self.location,
            "remaining_g": self.remaining_g,
        }


def _extra(obj: Mapping[str, Any], key: str) -> Any:
    raw = (obj.get("extra") or {}).get(key)
    try:
        return decode_extra(raw)
    except ValueError:
        return None


def _tag_uids(value: Any) -> tuple[str, ...]:
    """Spool extra `tag_uids`: a text field holding a JSON list ('["04A1..."]'); anything else → ()."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    return tuple(str(t) for t in value) if isinstance(value, list) else ()


def _spool_name(spool: Mapping[str, Any]) -> str:
    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    return (
        " ".join(str(p) for p in (vendor, fil.get("name")) if p)
        or f"spool {spool.get('id')}"
    )


def binding_of(spool: Mapping[str, Any]) -> Binding | None:
    """The slot binding of one Spoolman spool, or None when `printops_slot` is unset/empty."""
    slot = _extra(spool, "printops_slot")
    if not isinstance(slot, str) or not slot.strip():
        return None
    slot = slot.strip()
    printer, _, head = slot.partition("/")
    fil = spool.get("filament") or {}
    # A measured colour (what the spool really looks like) replaces the catalogue colour(s).
    measured = _extra(fil, "measured_hex")
    colors = (
        [measured]
        if parse_hex(measured)
        else [fil.get("color_hex"), *str(fil.get("multi_color_hexes") or "").split(",")]
    )
    tags = _tag_uids(_extra(spool, "tag_uids"))
    remaining = spool.get("remaining_weight")
    return Binding(
        spool_id=int(spool["id"]),
        slot=slot,
        printer=printer,
        head=head,
        name=_spool_name(spool),
        material=fil.get("material") or None,
        colors=tuple(dict.fromkeys(c for c in (parse_hex(x) for x in colors) if c)),
        tag_uids=tags,
        location=spool.get("location") or None,
        remaining_g=float(remaining) if isinstance(remaining, (int, float)) else None,
    )


def bindings(spools: Iterable[Mapping[str, Any]]) -> list[Binding]:
    """Every non-archived spool bound to a slot."""
    return [
        b for s in spools if not s.get("archived") and (b := binding_of(s)) is not None
    ]


@dataclass(frozen=True)
class WriteFailure:
    """The latest head write of a slot, when it failed (event `slot.printer_write_failed`)."""

    slot: str  # "u1/T3"
    spool_id: int
    error: str
    stage: str  # "filament" (head not written) | "pressure_advance" (written, K not restored)
    at: str
    event_id: int


def write_failures(conn: sqlite3.Connection) -> dict[str, WriteFailure]:
    """Slot → its latest head write, for the slots whose latest write failed."""
    rows = conn.execute(
        "SELECT e.* FROM events e JOIN (SELECT subject, MAX(id) AS id FROM events"
        " WHERE kind IN (?, ?) GROUP BY subject) last ON e.id = last.id WHERE e.kind = ?",
        (*WRITE_EVENTS, WRITE_EVENTS[1]),
    )
    out = {}
    for r in rows:
        p = json.loads(r["payload"])
        slot = str(r["subject"]).removeprefix("slot:")
        out[slot] = WriteFailure(slot, int(p.get("spool_id") or 0), str(p.get("error") or ""),
                                 str(p.get("stage") or "filament"), str(r["at"]), int(r["id"]))
    return out


def _norm_material(m: str) -> str:
    return "".join(m.upper().replace("-", " ").replace("_", " ").split())


@dataclass(frozen=True)
class Finding:
    kind: str
    scope: str
    subject: str
    evidence: dict[str, Any]
    identity: tuple[str, ...] = ()  # what makes two findings "the same anomaly"

    @property
    def dedupe_key(self) -> str:
        return "|".join((self.kind, self.subject, *self.identity))


def _head_matches(live: LiveSlot | None, b: Binding, max_delta_e: float) -> bool:
    """The printer shows the bound spool's material and colour on this head."""
    return bool(
        live is not None and live.material and b.material and live.color and b.colors
        and _norm_material(live.material) == _norm_material(b.material)
        and min(hex_delta_e(live.color, c) for c in b.colors) <= max_delta_e
    )


def compare_printer(
    printer: FleetPrinter,
    reading: Reading | None,
    binds: Sequence[Binding],
    max_delta_e: float = DEFAULT_MAX_DELTA_E,
    failures: Mapping[str, WriteFailure] | None = None,
) -> list[Finding]:
    """Findings for one fleet printer. `reading` None/failed → binding and write checks only.
    `failures` = write_failures() (slot → latest failed head write)."""
    heads = {h.label: h for h in printer.heads}
    mine = [b for b in binds if b.printer == printer.name]
    by_head: dict[str, list[Binding]] = {}
    for b in mine:
        by_head.setdefault(b.head, []).append(b)
    out: list[Finding] = []

    def add(
        kind: str, subject: str, evidence: dict[str, Any], *identity: object
    ) -> None:
        out.append(
            Finding(
                kind, printer.name, subject, evidence, tuple(str(i) for i in identity)
            )
        )

    for b in mine:
        head = heads.get(b.head)
        if head is None:
            add(
                "unknown_slot",
                f"spool:{b.spool_id}",
                {
                    "spool": b.to_dict(),
                    "printer": printer.name,
                    "known_heads": sorted(heads),
                    "why": f"{printer.name} has no head {b.head!r}",
                },
                b.slot,
            )
        elif not head.usable:
            add(
                "unusable_head",
                f"slot:{b.slot}",
                {"spool": b.to_dict(), "reason": head.reason},
                b.spool_id,
            )
        if b.location and b.location != f"slot:{b.slot}":
            add(
                "location_conflict",
                f"spool:{b.spool_id}",
                {"spool": b.to_dict(), "printops_slot": b.slot, "location": b.location},
                b.slot,
                b.location,
            )
    for label, bs in by_head.items():
        if label in heads and len(bs) > 1:
            add(
                "multiple_spools",
                f"slot:{printer.name}/{label}",
                {"spools": [b.to_dict() for b in bs]},
                *sorted(b.spool_id for b in bs),
            )
    live_ok = reading is not None and reading.ok
    live_by_head = {s.head: s for s in reading.slots} if reading is not None and live_ok else {}
    for b in mine:
        f = (failures or {}).get(b.slot)
        if f is None or f.spool_id != b.spool_id:
            continue
        if f.stage == "filament" and _head_matches(live_by_head.get(b.head), b, max_delta_e):
            continue
        add(
            "printer_write_failed",
            f"slot:{b.slot}",
            {"spool": b.to_dict(), "error": f.error, "stage": f.stage, "at": f.at, "event_id": f.event_id},
            b.spool_id,
        )

    if reading is None or not reading.ok:
        return out
    for live in reading.slots:
        if live.head not in heads:
            continue
        slot = f"{printer.name}/{live.head}"
        subject = f"slot:{slot}"
        bs = by_head.get(live.head, [])
        seen = {
            "present": live.present,
            "material": live.material,
            "color": live.color,
            "vendor": live.vendor,
            "sub_type": live.sub_type,
            "tag_uid": live.tag_uid,
        }
        if live.present is False:
            for b in bs:
                add(
                    "empty_with_spool",
                    subject,
                    {"printer": seen, "spool": b.to_dict()},
                    b.spool_id,
                )
            continue
        if live.present and not bs:
            add(
                "unbound_filament",
                subject,
                {"printer": seen},
                live.material or "?",
                live.color or "?",
                live.tag_uid or "",
            )
            continue
        if len(bs) != 1:
            continue
        b = bs[0]
        if (
            live.material
            and b.material
            and _norm_material(live.material) != _norm_material(b.material)
        ):
            add(
                "type_mismatch",
                subject,
                {"printer": seen, "spool": b.to_dict()},
                b.spool_id,
                _norm_material(live.material),
            )
        if live.color and b.colors:
            de = min(hex_delta_e(live.color, c) for c in b.colors)
            if de > max_delta_e:
                add(
                    "color_mismatch",
                    subject,
                    {
                        "printer": seen,
                        "spool": b.to_dict(),
                        "delta_e2000": round(de, 2),
                        "max_delta_e": max_delta_e,
                    },
                    b.spool_id,
                    live.color,
                )
        if live.tag_uid and b.tag_uids and live.tag_uid not in b.tag_uids:
            add(
                "tag_mismatch",
                subject,
                {"printer": seen, "spool": b.to_dict()},
                b.spool_id,
                live.tag_uid,
            )
    return out


def compare_unknown(fleet: Fleet | None, binds: Sequence[Binding]) -> list[Finding]:
    """Bindings to printers that are not in the fleet (scope "fleet")."""
    known = set(fleet.printers) if fleet is not None else set()
    return [
        Finding(
            "unknown_slot",
            FLEET_SCOPE,
            f"spool:{b.spool_id}",
            {
                "spool": b.to_dict(),
                "why": f"printer {b.printer!r} is not in fleet/printers.yaml",
            },
            (b.slot,),
        )
        for b in binds
        if b.printer not in known
    ]


# ---- anomaly store ----------------------------------------------------------------------------


@dataclass
class SyncResult:
    opened: list[int] = field(default_factory=list)
    resolved: list[int] = field(default_factory=list)
    seen: list[int] = field(default_factory=list)


def _anomaly_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["evidence"] = json.loads(d["evidence"])
    return d


def _event_payload(row_id: int, f: Finding) -> dict[str, Any]:
    return {
        "anomaly_id": row_id,
        "kind": f.kind,
        "scope": f.scope,
        "evidence": f.evidence,
    }


def sync(
    conn: sqlite3.Connection,
    scope: str,
    findings: Sequence[Finding],
    *,
    live_ok: bool,
    now: str,
) -> SyncResult:
    """Make the open/ack anomalies of `scope` match `findings`, inside the caller's
    transaction (a reconcile pass commits every scope together)."""
    res = SyncResult()
    wanted = {f.dedupe_key: f for f in findings}
    live = {
        r["dedupe_key"]: r
        for r in conn.execute(
            "SELECT * FROM anomaly WHERE scope = ? AND status IN ('open', 'ack')",
            (scope,),
        )
    }
    for key, f in wanted.items():
        row = live.get(key)
        evidence = json.dumps(f.evidence, sort_keys=True, separators=(",", ":"))
        if row is not None:
            conn.execute(
                "UPDATE anomaly SET last_seen_at = ?, evidence = ? WHERE id = ?",
                (now, evidence, row["id"]),
            )
            res.seen.append(int(row["id"]))
            continue
        cur = conn.execute(
            "INSERT INTO anomaly (at, kind, subject, scope, evidence, status, dedupe_key, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            (now, f.kind, f.subject, scope, evidence, key, now),
        )
        row_id = int(cur.lastrowid or 0)
        res.opened.append(row_id)
        ev.emit(conn, "anomaly.opened", f.subject, _event_payload(row_id, f), ACTOR)
        if f.kind in SLOT_MISMATCH_KINDS:
            ev.emit(conn, "slot.mismatch", f.subject, _event_payload(row_id, f), ACTOR)
    for key, row in live.items():
        if key in wanted or (row["kind"] in LIVE_KINDS and not live_ok):
            continue
        conn.execute(
            "UPDATE anomaly SET status = 'resolved', resolved_at = ?, resolved_by = ? WHERE id = ?",
            (now, ACTOR, row["id"]),
        )
        res.resolved.append(int(row["id"]))
        ev.emit(
            conn,
            "anomaly.resolved",
            row["subject"],
            {
                "anomaly_id": int(row["id"]),
                "kind": row["kind"],
                "scope": scope,
                "by": ACTOR,
                "why": "condition no longer seen",
            },
            ACTOR,
        )
    return res


def list_anomalies(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str] = ("open", "ack"),
    scope: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM anomaly WHERE status IN ({marks})"
    args: list[Any] = list(statuses)
    if scope is not None:
        sql += " AND scope = ?"
        args.append(scope)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_anomaly_row(r) for r in conn.execute(sql, args)]


def get_anomaly(conn: sqlite3.Connection, anomaly_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM anomaly WHERE id = ?", (anomaly_id,)).fetchone()
    return _anomaly_row(row) if row is not None else None


class TransitionError(ValueError):
    """ack/resolve on an anomaly whose status does not allow it."""


def set_status(
    conn: sqlite3.Connection,
    anomaly_id: int,
    status: str,
    actor: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    """ack (open → ack) or resolve (open/ack → resolved) by a person; emits anomaly.acked/resolved.
    None when the anomaly does not exist."""
    if status not in ("ack", "resolved"):
        raise ValueError(f"status must be ack or resolved, got {status!r}")
    now = utc_now()
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM anomaly WHERE id = ?", (anomaly_id,)
        ).fetchone()
        if row is None:
            return None
        allowed = ("open",) if status == "ack" else ("open", "ack")
        if row["status"] not in allowed:
            raise TransitionError(
                f"anomaly #{anomaly_id} is {row['status']}; cannot {status}"
            )
        if status == "ack":
            conn.execute(
                "UPDATE anomaly SET status = 'ack', acked_at = ?, acked_by = ?, note = COALESCE(?, note)"
                " WHERE id = ?",
                (now, actor, note, anomaly_id),
            )
            kind = "anomaly.acked"
        else:
            conn.execute(
                "UPDATE anomaly SET status = 'resolved', resolved_at = ?, resolved_by = ?,"
                " note = COALESCE(?, note) WHERE id = ?",
                (now, actor, note, anomaly_id),
            )
            kind = "anomaly.resolved"
        ev.emit(
            conn,
            kind,
            row["subject"],
            {
                "anomaly_id": anomaly_id,
                "kind": row["kind"],
                "scope": row["scope"],
                "by": actor,
                **({"note": note} if note else {}),
            },
            actor,
        )
    return get_anomaly(conn, anomaly_id)


# ---- service ----------------------------------------------------------------------------------

ReadFn = Callable[[FleetPrinter], Reading]


class Reconciler:
    """Fleet + Spoolman + latest readings → anomalies. Thread-safe; blocking (run in a worker thread)."""

    def __init__(
        self,
        db_path: Path,
        spoolman: Spoolman | None,
        fleet: Fleet | None,
        read: ReadFn,
        *,
        max_delta_e: float = DEFAULT_MAX_DELTA_E,
    ) -> None:
        self.db_path = db_path
        self.spoolman = spoolman
        self.fleet = fleet
        self.read = read
        self.max_delta_e = max_delta_e
        self.readings: dict[str, Reading] = {}
        self.last_error: str | None = None
        self._lock = threading.Lock()  # guards readings
        self._pass = (
            threading.Lock()
        )  # one pass at a time: a slow pass must not overwrite a newer one

    @property
    def printers(self) -> dict[str, FleetPrinter]:
        return dict(self.fleet.printers) if self.fleet is not None else {}

    def poll(self, name: str) -> Reading:
        """Read printer `name` once (one request) and reconcile the whole fleet with it."""
        reading = self.read(self.printers[name])
        with self._lock:
            self.readings[name] = reading
        self.reconcile()
        return reading

    def reconcile(self) -> dict[str, SyncResult]:
        """One pass over every fleet printer (latest readings, no printer request) + unknown bindings."""
        with self._pass:
            return self._reconcile()

    def _reconcile(self) -> dict[str, SyncResult]:
        if self.spoolman is None:
            self.last_error = "SPOOLMAN_URL is not set"
            return {}
        if (
            self.fleet is None
        ):  # fleet layer disabled: every binding would look "unknown"
            return {}
        binds = bindings(self.spoolman.find("spool"))
        with self._lock:
            readings = dict(self.readings)
        conn = open_db(self.db_path)
        try:
            failures = write_failures(conn)
            # findings first, outside the write lock; then the whole pass in ONE transaction, so a
            # failure on any scope leaves no half-applied pass (and listeners wake once, at COMMIT)
            scopes: list[tuple[str, list[Finding], bool]] = []
            for name, printer in self.printers.items():
                reading = readings.get(name)
                findings = compare_printer(printer, reading, binds, self.max_delta_e, failures)
                scopes.append((name, findings, reading is not None and reading.ok))
            scopes.append((FLEET_SCOPE, compare_unknown(self.fleet, binds), True))
            now = utc_now()
            with transaction(conn):
                out = {
                    scope: sync(conn, scope, findings, live_ok=live_ok, now=now)
                    for scope, findings, live_ok in scopes
                }
        finally:
            conn.close()
        self.last_error = None
        return out

    def reading(self, name: str) -> Reading | None:
        with self._lock:
            return self.readings.get(name)
