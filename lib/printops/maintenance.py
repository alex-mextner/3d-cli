"""maintenance.py — printer components, the maintenance log and faults (plan §7.1, §7.3; issue #166).

ACCESSED VIA: 3d-api routes_maintenance.py (`/v1/printers/{p}/components|maintenance|faults`,
`/v1/faults`, `/v1/maintenance/…`) → `3d printops maint`; the calibrations engine (#167) calls
`current_nozzle_install` and consumes the `calibration.invalidated` events emitted here.

MODEL (migration 0070): `component` rows are install history — a replacement closes the old
row (removed_at) and inserts the new one; nozzles carry diameter/material/type and are the
scope of flow/PA calibrations (view `nozzle_install`). `maintenance` is the work log (with the
calibration triggers it fired), `fault` what is broken (open → fixed by a maintenance, or
wontfix).

FLEET SEEDING: `ensure_seeded` fills what fleet/printers.yaml knows and the DB has never
recorded: each head's nozzle and each plate (installed at an unknown date), and an open fault
per `usable: false` head (U1 T2, external_key `fleet:u1/T2`). It never overrides recorded
history: a later swap in the DB wins, the fleet file then shows up as `drift`.

EVENTS (subject "<printer>[/<head>]"): component.installed|removed, maintenance.logged,
fault.opened|closed, and `calibration.invalidated` {printer, head|null, trigger, kinds|null,
reason, maintenance_id, nozzle_install_id} — one per trigger and head; kinds null = every
calibration kind whose catalog `invalidates_on` lists the trigger (contract with #167). A nozzle
swap fires `nozzle_swap`, plus `nozzle_diameter` when the diameter changes; other maintenance
fires the trigger fleet/maintenance-rules.yaml `invalidates:` maps it to.

INVARIANTS: stdlib only; all writes of one operation in one transaction; printers and heads
must exist in the fleet file; nothing here talks to a printer.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from printops import db, events
from printops.maintenance_rules import COMPONENTS, TASKS, Rules

COMPONENT_KINDS = tuple(c for c in COMPONENTS if c != "printer")  # what a component row can be
FAULT_CLOSE = ("fixed", "wontfix")
FUTURE_SLACK_S = 300
_JSON_COLS = ("parts_used", "triggers_calibrations")


class MaintError(Exception):
    """A request the domain refuses; `status` is the HTTP status 3d-api answers with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for c in _JSON_COLS:
        if c in d and isinstance(d[c], str):
            d[c] = json.loads(d[c])
    return d


def _subject(printer: str, head: str | None) -> str:
    return f"{printer}/{head}" if head else printer


def norm_at(value: str | None, *, now: datetime.datetime | None = None) -> str:
    """ISO date/datetime (offset or Z; naive = UTC) → db.utc_now() format; None → now. More than
    FUTURE_SLACK_S ahead → error (the slack absorbs clock skew between the CLI host and 3d-api)."""
    if value is None:
        return db.utc_now()
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise MaintError(f"at: {value!r} is not an ISO-8601 date/time, e.g. 2026-10-14T18:30+02:00") from e
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if (dt - now).total_seconds() > FUTURE_SLACK_S:
        raise MaintError(f"at: {value!r} is in the future")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# -- fleet lookups ----------------------------------------------------------------------------
def fleet_printer(fleet: Any, printer: str) -> Any:
    if fleet is None:
        raise MaintError("no fleet file (fleet/printers.yaml) in this 3d-api: printers are unknown", 409)
    p = fleet.printers.get(printer)
    if p is None:
        raise MaintError(f"unknown printer {printer!r}; fleet/printers.yaml has {', '.join(sorted(fleet.printers))}", 404)
    return p


def _head(fleet: Any, printer: str, head: str | None) -> str | None:
    p = fleet_printer(fleet, printer)
    if head is None:
        return None
    label = head.strip().upper()
    if label not in {h.label for h in p.heads}:
        raise MaintError(f"{printer} has no head {head!r} (heads: {', '.join(h.label for h in p.heads)})", 404)
    return label


# -- components --------------------------------------------------------------------------------
def _component_where(printer: str, head: str | None, kind: str, label: str | None) -> tuple[str, tuple[Any, ...]]:
    """Identity of a component slot: a plate by its label, anything else by head + kind."""
    if kind == "plate":
        return "printer = ? AND kind = 'plate' AND label = ?", (printer, label)
    return "printer = ? AND COALESCE(head, '') = ? AND kind = ?", (printer, head or "", kind)


def _active(conn: sqlite3.Connection, printer: str, head: str | None, kind: str,
            label: str | None = None) -> sqlite3.Row | None:
    where, args = _component_where(printer, head, kind, label)
    return conn.execute(f"SELECT * FROM component WHERE {where} AND removed_at IS NULL",  # noqa: S608 — fixed fragment
                        args).fetchone()


def current_nozzle_install(conn: sqlite3.Connection, printer: str, head: str) -> dict[str, Any] | None:
    """The nozzle mounted on a head now: {id, diameter_mm, material, type, installed_at} (installed_at
    None = unknown date, seeded from the fleet file); None when none is recorded. Used by #167."""
    r = _active(conn, printer, head, "nozzle")
    if r is None:
        return None
    return {k: r[k] for k in ("id", "diameter_mm", "material", "type", "installed_at")}


def components(conn: sqlite3.Connection, printer: str, *, include_removed: bool = False) -> list[dict[str, Any]]:
    where = "" if include_removed else " AND removed_at IS NULL"
    rows = conn.execute(f"SELECT * FROM component WHERE printer = ?{where} "  # noqa: S608 — fixed fragment
                        "ORDER BY COALESCE(head, ''), kind, id", (printer,)).fetchall()
    return [dict(r) for r in rows]


def heads(conn: sqlite3.Connection, fleet: Any, printer: str) -> list[dict[str, Any]]:
    """Each fleet head with its nozzle now and its open faults; usable = the fleet says so AND
    no fault is open on it."""
    p = fleet_printer(fleet, printer)
    open_by_head: dict[str, list[int]] = {}
    for r in conn.execute("SELECT id, head FROM fault WHERE printer = ? AND status = 'open' AND head IS NOT NULL "
                          "ORDER BY id", (printer,)):
        open_by_head.setdefault(str(r["head"]), []).append(int(r["id"]))
    out = []
    for h in p.heads:
        faults = open_by_head.get(h.label, [])
        out.append({"label": h.label, "index": h.index, "extruder": h.extruder, "fleet_usable": h.usable,
                    "fleet_reason": h.reason, "usable": h.usable and not faults, "open_faults": faults,
                    "nozzle": current_nozzle_install(conn, printer, h.label)})
    return out


def _install(conn: sqlite3.Connection, *, printer: str, head: str | None, kind: str, label: str | None,
             diameter_mm: float | None = None, material: str | None = None, type_: str | None = None,
             installed_at: str | None, reason: str | None, source: str, maintenance_id: int | None,
             actor: str) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO component (printer, head, kind, label, diameter_mm, material, type, installed_at, install_reason, "
        "source, maintenance_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (printer, head, kind, label, diameter_mm, material, type_, installed_at, reason, source, maintenance_id,
         db.utc_now()))
    row = dict(conn.execute("SELECT * FROM component WHERE id = ?", (cur.lastrowid,)).fetchone())
    events.emit(conn, "component.installed", _subject(printer, head),
                {k: row[k] for k in ("id", "printer", "head", "kind", "label", "diameter_mm", "material", "type",
                                     "installed_at", "source", "maintenance_id")}, actor)
    return row


def _remove(conn: sqlite3.Connection, row: sqlite3.Row, *, at: str, reason: str | None, actor: str) -> dict[str, Any]:
    if row["installed_at"] is not None and at < row["installed_at"]:
        raise MaintError(f"{row['kind']} #{row['id']} on {_subject(row['printer'], row['head'])} was installed at "
                         f"{row['installed_at']}, after {at}", 409)
    conn.execute("UPDATE component SET removed_at = ?, removal_reason = ? WHERE id = ?", (at, reason, row["id"]))
    events.emit(conn, "component.removed", _subject(row["printer"], row["head"]),
                {"id": row["id"], "kind": row["kind"], "label": row["label"], "removed_at": at, "reason": reason}, actor)
    return dict(conn.execute("SELECT * FROM component WHERE id = ?", (row["id"],)).fetchone())


# -- maintenance -------------------------------------------------------------------------------
def _insert_maintenance(conn: sqlite3.Connection, *, printer: str, head: str | None, component: str, kind: str,
                        at: str, actor: str, parts: Sequence[str], notes: str | None, rule_id: str | None,
                        triggers: list[dict[str, Any]]) -> int:
    cur = conn.execute(
        "INSERT INTO maintenance (printer, head, component, kind, at, actor, parts_used, notes, rule_id, "
        "triggers_calibrations, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (printer, head, component, kind, at, actor, json.dumps(list(parts), ensure_ascii=False), notes, rule_id,
         json.dumps(triggers), db.utc_now()))
    return int(cur.lastrowid or 0)


def get_maintenance(conn: sqlite3.Connection, maintenance_id: int) -> dict[str, Any]:
    r = _row(conn.execute("SELECT * FROM maintenance WHERE id = ?", (maintenance_id,)).fetchone())
    if r is None:
        raise MaintError(f"no maintenance #{maintenance_id}", 404)
    return r


def list_maintenance(conn: sqlite3.Connection, printer: str, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM maintenance WHERE printer = ? ORDER BY at DESC, id DESC LIMIT ?",
                        (printer, max(1, min(limit, 1000)))).fetchall()
    return [d for d in map(_row, rows) if d is not None]


def _invalidate(conn: sqlite3.Connection, *, printer: str, head: str | None, triggers: Iterable[dict[str, Any]],
                reason: str, maintenance_id: int, nozzle_install_id: int | None, actor: str) -> list[dict[str, Any]]:
    out = []
    for t in triggers:
        payload = {"printer": printer, "head": head, "trigger": t["trigger"], "kinds": t["kinds"], "reason": reason,
                   "maintenance_id": maintenance_id, "nozzle_install_id": nozzle_install_id}
        ev = events.emit(conn, "calibration.invalidated", _subject(printer, head), payload, actor)
        out.append({"event_id": ev.id, **payload})
    return out


def _parts(parts: Iterable[str] | None) -> list[str]:
    given = list(parts or ())  # once: a one-shot iterable must not be read twice
    out = [p.strip() for p in given if isinstance(p, str) and p.strip()]
    if len(out) != len(given):
        raise MaintError("parts_used: must be a list of non-empty strings")
    return out


def _kinds(kinds: Sequence[str] | None) -> list[str] | None:
    if kinds is None:
        return None
    clean = [k.strip() for k in kinds if isinstance(k, str) and k.strip()]
    if not clean or len(clean) != len(kinds):
        raise MaintError("invalidates: must be a non-empty list of calibration kinds, e.g. [flow, pressure_advance]")
    return clean


def _nozzle_spec(diameter_mm: Any, material: Any, type_: Any) -> None:
    from printops.fleet import NOZZLE_MATERIALS, NOZZLE_TYPES

    if diameter_mm is not None and (isinstance(diameter_mm, bool) or not isinstance(diameter_mm, (int, float))
                                    or not 0 < diameter_mm <= 2):
        raise MaintError(f"diameter_mm: {diameter_mm!r} is not a nozzle diameter in mm (0 < d <= 2)")
    if material is not None and material not in NOZZLE_MATERIALS:
        raise MaintError(f"material: {material!r} is not one of {', '.join(NOZZLE_MATERIALS)}")
    if type_ is not None and type_ not in NOZZLE_TYPES:
        raise MaintError(f"type: {type_!r} is not one of {', '.join(NOZZLE_TYPES)}")


def swap_nozzle(conn: sqlite3.Connection, fleet: Any, printer: str, head: str, *, reason: str, actor: str,
                diameter_mm: float | None = None, material: str | None = None, type_: str | None = None,
                label: str | None = None, at: str | None = None, notes: str | None = None,
                parts: Sequence[str] = ()) -> dict[str, Any]:
    """Replace the nozzle of one head. Unset diameter/material/type/label are kept from the nozzle
    removed (a like-for-like swap). Fires calibration.invalidated nozzle_swap (+ nozzle_diameter)."""
    hd = _head(fleet, printer, head)
    if hd is None:
        raise MaintError("a nozzle swap names the head: PRINTER/HEAD")
    if not isinstance(reason, str) or not reason.strip():
        raise MaintError("reason: required (why the nozzle was swapped: clog, worn, abrasive filament, …)")
    _nozzle_spec(diameter_mm, material, type_)
    when = norm_at(at)
    used = _parts(parts)
    with db.transaction(conn):
        old = _active(conn, printer, hd, "nozzle")
        inherited = []
        spec: dict[str, Any] = {"diameter_mm": diameter_mm, "material": material, "type": type_, "label": label}
        for key in spec:
            if spec[key] is None and old is not None and old[key] is not None:
                spec[key] = old[key]
                inherited.append(key)
        if spec["diameter_mm"] is None:
            raise MaintError(f"no nozzle is recorded on {printer}/{hd}: give its diameter (diameter_mm)")
        changed_dia = old is not None and old["diameter_mm"] is not None and \
            abs(float(old["diameter_mm"]) - float(spec["diameter_mm"])) > 1e-9
        triggers = [{"trigger": "nozzle_swap", "kinds": None}]
        if changed_dia:
            triggers.append({"trigger": "nozzle_diameter", "kinds": None})
        mid = _insert_maintenance(conn, printer=printer, head=hd, component="nozzle", kind="replace", at=when,
                                  actor=actor, parts=used, notes=notes, rule_id=None, triggers=triggers)
        removed = _remove(conn, old, at=when, reason=reason.strip(), actor=actor) if old is not None else None
        new = _install(conn, printer=printer, head=hd, kind="nozzle", label=spec["label"],
                       diameter_mm=float(spec["diameter_mm"]), material=spec["material"], type_=spec["type"],
                       installed_at=when, reason=reason.strip(), source="manual", maintenance_id=mid, actor=actor)
        events.emit(conn, "maintenance.logged", _subject(printer, hd),
                    {"id": mid, "component": "nozzle", "kind": "replace", "at": when, "rule": None,
                     "nozzle_install_id": new["id"]},
                    actor)
        what = f"nozzle swap on {printer}/{hd}: {reason.strip()}"
        invalidated = _invalidate(conn, printer=printer, head=hd, triggers=triggers, reason=what, maintenance_id=mid,
                                  nozzle_install_id=int(new["id"]), actor=actor)
    return {"maintenance": get_maintenance(conn, mid), "removed": removed, "installed": new,
            "inherited": inherited, "invalidated": invalidated}


def log_maintenance(conn: sqlite3.Connection, fleet: Any, rules: Rules, printer: str, *, actor: str,
                    component: str | None = None, kind: str | None = None, head: str | None = None,
                    at: str | None = None, notes: str | None = None, parts: Sequence[str] = (),
                    rule_id: str | None = None, label: str | None = None, invalidates: Sequence[str] | None = None,
                    no_invalidate: bool = False, fixes: Sequence[int] = ()) -> dict[str, Any]:
    """Record maintenance work. With `rule_id` component/kind default to the rule's and the rule's
    counter restarts. `kind=replace` on a component kind also records the new component (label =
    the new part). Calibration triggers come from `rules.invalidations` unless `invalidates`
    names the kinds (trigger = the mapped one, else `repair`) or `no_invalidate`."""
    fleet_printer(fleet, printer)
    hd = _head(fleet, printer, head)
    rule = None
    if rule_id is not None:
        rule = rules.get(rule_id)
        if rule is None or printer not in rule.printers:
            known = ", ".join(r.id for r in rules.rules if printer in r.printers) or "none"
            raise MaintError(f"no maintenance rule {rule_id!r} for {printer} (rules: {known})", 404)
        for key, given, ruled in (("component", component, rule.component), ("kind", kind, rule.task)):
            if given is not None and given != ruled:
                raise MaintError(f"{key} {given!r} contradicts rule {rule.id} ({key} {ruled!r})")
        component, kind = rule.component, rule.task
        if rule.scope == "head" and hd is None:
            raise MaintError(f"rule {rule.id} is per head: name the head")
        if rule.scope == "printer" and hd is not None:
            # its baseline is printer-wide: work on one head must not restart it for the whole printer
            raise MaintError(f"rule {rule.id} is for the whole printer: give the printer only (no head)")
    if component not in COMPONENTS:
        raise MaintError(f"component: {component!r} is not one of {', '.join(COMPONENTS)}")
    if kind not in TASKS:
        raise MaintError(f"kind: {kind!r} is not one of {', '.join(TASKS)}")
    if component == "nozzle" and kind == "replace":
        raise MaintError("a nozzle replacement is a nozzle swap: `3d printops maint nozzle swap` "
                         "(POST /v1/printers/{p}/components/nozzle-swap) records the new nozzle")
    if component == "plate" and hd is not None:
        raise MaintError("a plate belongs to the whole printer: no head")
    if component == "printer" and hd is not None:
        raise MaintError("component `printer` is the whole printer: no head")
    if no_invalidate and invalidates is not None:
        raise MaintError("invalidates and no_invalidate exclude each other")
    kinds = _kinds(invalidates)
    if component == "plate" and kind == "replace" and label is None:
        # a plate is identified by its label: without one the old plate could not be found and removed
        raise MaintError("a plate replacement names the new plate: label (e.g. textured_pei)")
    if label is not None and (kind != "replace" or component == "printer"):
        raise MaintError("label names the new part of a replacement (kind replace)")
    when = norm_at(at)
    used = _parts(parts)
    trigger = rules.trigger_for(component, kind)
    if no_invalidate:
        triggers: list[dict[str, Any]] = []
    elif kinds is not None:
        triggers = [{"trigger": trigger or "repair", "kinds": kinds}]
    else:
        triggers = [{"trigger": trigger, "kinds": None}] if trigger else []
    with db.transaction(conn):
        mid = _insert_maintenance(conn, printer=printer, head=hd, component=component, kind=kind, at=when,
                                  actor=actor, parts=used, notes=notes, rule_id=rule.id if rule else None,
                                  triggers=triggers)
        removed = installed = None
        if kind == "replace" and component in COMPONENT_KINDS:
            old = _active(conn, printer, hd, component, label)
            if old is not None:
                removed = _remove(conn, old, at=when, reason=notes or "replaced", actor=actor)
            installed = _install(conn, printer=printer, head=hd, kind=component,
                                 label=label if label is not None else (old["label"] if old is not None else None),
                                 installed_at=when, reason=notes or "replacement", source="manual",
                                 maintenance_id=mid, actor=actor)
        events.emit(conn, "maintenance.logged", _subject(printer, hd),
                    {"id": mid, "component": component, "kind": kind, "at": when,
                     "rule": rule.id if rule else None, "nozzle_install_id": None}, actor)
        what = f"{component} {kind} on {_subject(printer, hd)}" + (f": {notes}" if notes else "")
        invalidated = _invalidate(conn, printer=printer, head=hd, triggers=triggers, reason=what,
                                  maintenance_id=mid, nozzle_install_id=None, actor=actor)
        fixed = [_close(conn, fid, printer=printer, status="fixed", root_cause=None, fixed_by=mid, actor=actor)
                 for fid in fixes]
    return {"maintenance": get_maintenance(conn, mid), "removed": removed, "installed": installed,
            "invalidated": invalidated, "fixed": fixed}


# -- faults ------------------------------------------------------------------------------------
_BIDI = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _printable(field_name: str, text: str) -> str:
    """Fault text is echoed to terminals (fault ls, spool load refusals): CRLF/CR become LF; any
    other C0/C1 control character (escape sequences, a lone CR that rewrites a line) or bidi
    override is refused. Tab and LF are fine."""
    text = text.replace("\r\n", "\n")
    if any((unicodedata.category(c) == "Cc" and c not in "\t\n") or c in _BIDI for c in text):
        raise MaintError(f"{field_name}: control characters are not allowed")
    return text


def one_line(text: str, limit: int) -> str:
    """Fault text for a one-line message: whitespace and any control/format character (rows from
    before `_printable`, or seeded from the fleet file) become single spaces; cut to `limit`."""
    cleaned = "".join(" " if unicodedata.category(c) in ("Cc", "Cf") or c.isspace() else c for c in text)
    return " ".join(cleaned.split())[:limit]


def get_fault(conn: sqlite3.Connection, fault_id: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM fault WHERE id = ?", (fault_id,)).fetchone()
    if r is None:
        raise MaintError(f"no fault #{fault_id}", 404)
    return dict(r)


def list_faults(conn: sqlite3.Connection, printer: str | None = None, *, head: str | None = None,
                status: str | None = "open") -> list[dict[str, Any]]:
    """Faults, oldest first per printer. `head` (a canonical label, as `_head` stores it) narrows to
    one head: an open fault there makes the head unusable (spool loads refuse it, #233)."""
    where: list[str] = []
    args: list[str] = []
    if printer is not None:
        where.append("printer = ?")
        args.append(printer)
    if head is not None:
        where.append("head = ?")
        args.append(head)
    if status is not None:
        if status not in ("open", *FAULT_CLOSE):
            raise MaintError(f"status: {status!r} is not one of open, {', '.join(FAULT_CLOSE)}")
        where.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM fault" + (" WHERE " + " AND ".join(where) if where else "")
    return [dict(r) for r in conn.execute(sql + " ORDER BY printer, id", args)]


def open_fault(conn: sqlite3.Connection, fleet: Any, printer: str, *, symptom: str, actor: str,
               head: str | None = None, component: str | None = None, at: str | None = None) -> dict[str, Any]:
    hd = _head(fleet, printer, head)
    if not isinstance(symptom, str) or not symptom.strip():
        raise MaintError("symptom: required (what is wrong, as observed)")
    symptom = _printable("symptom", symptom)
    if component is not None and component not in COMPONENTS:
        raise MaintError(f"component: {component!r} is not one of {', '.join(COMPONENTS)}")
    when = norm_at(at)
    with db.transaction(conn):
        fault = _insert_fault(conn, printer=printer, head=hd, component=component, at=when, symptom=symptom.strip(),
                              source="manual", external_key=None, actor=actor)
    return fault


def _insert_fault(conn: sqlite3.Connection, *, printer: str, head: str | None, component: str | None, at: str,
                  symptom: str, source: str, external_key: str | None, actor: str) -> dict[str, Any]:
    now = db.utc_now()
    cur = conn.execute(
        "INSERT INTO fault (printer, head, component, at, symptom, status, source, external_key, opened_by, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
        (printer, head, component, at, symptom, source, external_key, actor, now, now))
    fault = get_fault(conn, int(cur.lastrowid or 0))
    events.emit(conn, "fault.opened", _subject(printer, head),
                {"id": fault["id"], "component": component, "symptom": symptom, "at": at, "source": source}, actor)
    return fault


def _close(conn: sqlite3.Connection, fault_id: int, *, printer: str | None, status: str, root_cause: str | None,
           fixed_by: int | None, actor: str) -> dict[str, Any]:
    fault = get_fault(conn, fault_id)
    if printer is not None and fault["printer"] != printer:
        raise MaintError(f"fault #{fault_id} is on {fault['printer']}, not {printer}", 409)
    if fault["status"] != "open":
        raise MaintError(f"fault #{fault_id} is already {fault['status']}", 409)
    if status not in FAULT_CLOSE:
        raise MaintError(f"status: {status!r} is not one of {', '.join(FAULT_CLOSE)}")
    if fixed_by is not None:
        if status != "fixed":
            raise MaintError("fixed_by needs status fixed")
        m = get_maintenance(conn, fixed_by)
        if m["printer"] != fault["printer"]:
            raise MaintError(f"maintenance #{fixed_by} is on {m['printer']}, fault #{fault_id} on {fault['printer']}", 409)
        if str(m["at"]) < str(fault["at"]):  # both in the fixed …Z form: text order is time order
            raise MaintError(f"maintenance #{fixed_by} ({m['at'][:10]}) predates fault #{fault_id} ({fault['at'][:10]})", 409)
        if m["head"] is not None and fault["head"] is not None and m["head"] != fault["head"]:
            # printer-wide work may fix a head's fault, and head work a printer-wide one; another head's may not
            raise MaintError(f"maintenance #{fixed_by} is on head {m['head']}, fault #{fault_id} on head {fault['head']}",
                             409)
    now = db.utc_now()
    conn.execute("UPDATE fault SET status = ?, root_cause = COALESCE(?, root_cause), fixed_by_maintenance_id = ?, "
                 "closed_at = ?, updated_at = ? WHERE id = ?", (status, root_cause, fixed_by, now, now, fault_id))
    events.emit(conn, "fault.closed", _subject(fault["printer"], fault["head"]),
                {"id": fault_id, "status": status, "root_cause": root_cause, "fixed_by_maintenance_id": fixed_by}, actor)
    return get_fault(conn, fault_id)


def close_fault(conn: sqlite3.Connection, fault_id: int, *, actor: str, status: str = "fixed",
                root_cause: str | None = None, fixed_by: int | None = None, printer: str | None = None) -> dict[str, Any]:
    if root_cause is not None and not root_cause.strip():
        root_cause = None
    if root_cause is not None:
        root_cause = _printable("root_cause", root_cause)
    with db.transaction(conn):
        return _close(conn, fault_id, printer=printer, status=status, root_cause=root_cause and root_cause.strip(),
                      fixed_by=fixed_by, actor=actor)


# -- fleet seeding -----------------------------------------------------------------------------
@dataclass
class SeedReport:
    installed: list[str] = field(default_factory=list)  # "u1/T1 nozzle 0.4 stainless std"
    faults_opened: list[str] = field(default_factory=list)  # "u1/T2 #1"
    drift: list[dict[str, Any]] = field(default_factory=list)  # fleet file vs recorded state

    @property
    def changed(self) -> bool:
        return bool(self.installed or self.faults_opened)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _never_recorded(conn: sqlite3.Connection, printer: str, head: str | None, kind: str, label: str | None = None) -> bool:
    where, args = _component_where(printer, head, kind, label)
    return conn.execute(f"SELECT 1 FROM component WHERE {where}", args).fetchone() is None  # noqa: S608 — fixed fragment


def seed_from_fleet(conn: sqlite3.Connection, fleet: Any, *, actor: str = "fleet") -> SeedReport:
    """Record what the fleet file knows and the DB never recorded (idempotent); report drift."""
    report = SeedReport()
    seeded = "fleet/printers.yaml (installed at an unknown date)"
    for name, p in sorted(fleet.printers.items()):
        with db.transaction(conn):
            for h in p.heads:
                n = h.nozzle
                if n is not None and _never_recorded(conn, name, h.label, "nozzle"):
                    _install(conn, printer=name, head=h.label, kind="nozzle", label=None, diameter_mm=n.diameter_mm,
                             material=n.material, type_=n.type, installed_at=None, reason=seeded, source="fleet",
                             maintenance_id=None, actor=actor)
                    report.installed.append(f"{name}/{h.label} nozzle {n.diameter_mm:g} {n.material or '?'} {n.type or '?'}")
                elif n is not None:
                    cur = _active(conn, name, h.label, "nozzle")
                    recorded = None if cur is None else {k: cur[k] for k in ("diameter_mm", "material", "type")}
                    if recorded != asdict(n):
                        report.drift.append({"subject": f"{name}/{h.label}", "what": "nozzle",
                                             "fleet": asdict(n), "recorded": recorded})
                key = f"fleet:{name}/{h.label}"
                known = conn.execute("SELECT status FROM fault WHERE external_key = ?", (key,)).fetchone()
                if not h.usable and known is None:
                    f = _insert_fault(conn, printer=name, head=h.label, component="head", at=db.utc_now(),
                                      symptom=h.reason or "unusable (fleet/printers.yaml)", source="fleet",
                                      external_key=key, actor=actor)
                    report.faults_opened.append(f"{name}/{h.label} #{f['id']}")
                elif not h.usable and known["status"] != "open":
                    report.drift.append({"subject": f"{name}/{h.label}", "what": "usable",
                                         "fleet": False, "recorded": f"fault {key} is {known['status']}"})
                elif h.usable and known is not None and known["status"] == "open":
                    # the fleet file says repaired; the fault stays open until someone closes it with a cause
                    report.drift.append({"subject": f"{name}/{h.label}", "what": "usable",
                                         "fleet": True, "recorded": f"fault {key} is open"})
            for plate in p.plates or ():
                if _never_recorded(conn, name, None, "plate", plate):
                    _install(conn, printer=name, head=None, kind="plate", label=plate, installed_at=None,
                             reason=seeded, source="fleet", maintenance_id=None, actor=actor)
                    report.installed.append(f"{name} plate {plate}")
    return report


# database file -> signature (path, mtime, size) of the fleet file last seeded into it, per process
_seeded_signatures: dict[str, tuple[str, int, int]] = {}
# 3d-api runs sync routes and the background check in a threadpool: two cold requests would both
# pass the signature check and race the same INSERTs into the unique component/fault indexes.
_seed_lock = threading.Lock()


def ensure_seeded(conn: sqlite3.Connection, *, actor: str = "fleet", force: bool = False) -> SeedReport | None:
    """seed_from_fleet when fleet/printers.yaml changed since this process last did (or `force`).
    None: no fleet file. A broken fleet file raises printops.fleet.FleetError."""
    from printops.fleet import fleet_path, load_fleet

    path = fleet_path()
    if path is None or not path.is_file():
        return None
    st = path.stat()
    sig = (str(path), st.st_mtime_ns, st.st_size)
    key = str(conn.execute("PRAGMA database_list").fetchone()["file"])
    with _seed_lock:
        # an in-memory DB has no file name ("") to tell it apart from another: never trust the cache for it
        if not force and key and _seeded_signatures.get(key) == sig:
            return SeedReport()
        fleet = load_fleet(path)
        if fleet is None:
            return None
        report = seed_from_fleet(conn, fleet, actor=actor)
        if key:
            _seeded_signatures[key] = sig
        return report
