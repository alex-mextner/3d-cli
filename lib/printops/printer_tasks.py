"""printer_tasks.py — one to-do list per printer: printer_task rows, their derivation and the buffer import
(plan §7.5; issue #281). Part of the maintenance domain of #166 (printops.maintenance).

ACCESSED VIA: 3d-api routes_tasks.py (`/v1/printers/{p}/tasks`, `/v1/tasks…`) → `3d printer todo`
(printops.todo_cli), Home Assistant (#285) and, later, the bot (#284) and preflight (#282).

MODEL (migration 0071): a task says what to do on a printer (head/slot/component), why, whether it
blocks printing (none | printer | head | slot), when it suits (idle | before_print | any), how
long it takes, whether a human is needed, its source and status (open | in_progress | snoozed |
done | dismissed). `depends_on` lists task ids; a task with an open dependency is *waiting*.

DERIVED TASKS (`sync`), materialised idempotently by `source_key` (unique among open tasks):
  fault             an open fault (#166) → repair task; blocks its head, or the printer when the
                    fault has no head. Closed when the fault closes (fixed → done, wontfix → dismissed).
  maintenance_rule  a rule announced in `maintenance_due` (#166) whose baseline is still current →
                    maintenance task; done when maintenance for the rule is logged.
  calibration       every requirement of the engine (#167) that is not current/assumed (never
                    done, new filament/vendor/lot/nozzle, invalidated, expired), gathered into ONE
                    task per head (source_key 'calib:<printer>/<head|->', #387): `requirements` lists
                    each missing kind with its spool and reason, the title and `why` summarise them,
                    a recorded kind drops out, and the task is done when nothing of the head is
                    missing any more and a record satisfied what it covered (when none did, e.g.
                    the spool it was about was unloaded, it is dismissed as no longer needed).
                    Kinds the engine cannot decide (spool unknown: Spoolman down, nothing bound)
                    keep their entry as it was. A person's dismissal sticks for the requirement
                    keys the task covered: another nozzle, spool or lot, or an expiry after a
                    record, opens a new task. The per-kind tasks of the first derivation (#281,
                    any source with such a key) are superseded on the next pass; a task that
                    depended on one depends on the head's task instead, a snooze is not carried.
A derived task whose need is gone for another reason (scope changed: another nozzle or spool;
rule deleted; head no longer usable) is dismissed by `derive`; one a person dismissed stays
dismissed for that key. Derived tasks refuse `done`: the record that does the work closes them
(`done_hint` names the command). A manual/imported task with the key of a derived need is
adopted by the derivation.

IMPORT (`import_lines`): buffer lines (printops.printer_task_buffer) replayed in order inside one
transaction, each op once by its `uid` (table printer_task_op); a second import changes nothing.
An open whose source_key matches an open task merges into it (the buffer uid joins `refs`); a
spool known only by name is bound to the single Spoolman spool of that filament name.

EVENTS (subject "<printer>[/<head>]"): printer_task.opened | updated | closed.
INVARIANTS: stdlib only (+ printops); every operation in one transaction; nothing talks to a printer.
"""

from __future__ import annotations

import datetime
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any

from printops import db, events
from printops import printer_task_buffer as buf
from printops.maintenance_rules import COMPONENTS

SOURCES = (
    "manual",
    "calibration",
    "maintenance_rule",
    "fault",
    "calibration_followup",
    "import",
)
DERIVED = ("calibration", "maintenance_rule", "fault")
CREATABLE = (
    "manual",
    "calibration_followup",
)  # what a person or a client may create directly
STATUSES = ("open", "in_progress", "snoozed", "done", "dismissed")
ACTIVE = ("open", "in_progress", "snoozed")
DERIVE = "derive"  # actor of the derivation; its dismissals do not stick
UNCERTAIN = (
    "no_spool",
    "no_plate",
)  # requirement statuses that say nothing about the need
SATISFIED = ("current", "assumed")
_JSON = ("depends_on", "refs", "calibration_kinds", "spools", "requirements")
_FIELDS = (
    "printer",
    "head",
    "slot",
    "component",
    "type",
    "title",
    "why",
    "blocks",
    "when",
    "when_note",
    "duration_min",
    "needs_human",
    "source",
    "source_key",
    "depends_on",
    "refs",
    "calibration_kinds",
    "spools",
    "spool_id",
    "requirements",
    "note",
    "calibration_id",
    "maintenance_id",
    "fault_id",
)
# What an `update` may change; a derived task keeps what its source decides (title, why, type, scope).
_SCHEDULING = ("when", "when_note", "duration_min", "needs_human", "depends_on", "refs")
_DESCRIPTIVE = ("title", "why", "type", "component", "calibration_kinds", "spools")


class TaskError(Exception):
    """A request the domain refuses; `status` is the HTTP status 3d-api answers with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _subject(printer: str, head: str | None) -> str:
    return f"{printer}/{head}" if head else printer


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in _JSON:
        d[k] = json.loads(d[k]) if d[k] else []
    d["needs_human"] = bool(d["needs_human"])
    d["derived"] = d["source"] in DERIVED
    return d


def _parse_at(value: str, what: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise TaskError(
            f"{what}: {value!r} is not an ISO-8601 date/time, e.g. 2026-10-20 or 2026-10-20T09:00+02:00"
        ) from e
    return (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).astimezone(
        datetime.timezone.utc
    )


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# -- reads -------------------------------------------------------------------------------------
def get(conn: sqlite3.Connection, ref: str | int) -> dict[str, Any]:
    """A task by id (digits) or uid."""
    text = str(ref).strip()
    if text.isdigit():
        r = conn.execute(
            "SELECT * FROM printer_task WHERE id = ?", (int(text),)
        ).fetchone()
    else:
        r = conn.execute("SELECT * FROM printer_task WHERE uid = ?", (text,)).fetchone()
    if r is None:
        raise TaskError(f"no printer task {text!r}", 404)
    return _with_waiting(conn, [_row(r)])[0]


def _with_waiting(
    conn: sqlite3.Connection, tasks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    deps = sorted({int(i) for t in tasks for i in t["depends_on"]})
    open_ids: set[int] = set()
    if deps:
        marks = ",".join("?" * len(deps))
        open_ids = {
            int(r[0])
            for r in conn.execute(
                f"SELECT id FROM printer_task WHERE id IN ({marks}) AND closed_at IS NULL",
                deps,
            )
        }  # noqa: S608 — placeholders only
    for t in tasks:
        t["waiting_on"] = (
            [int(i) for i in t["depends_on"] if int(i) in open_ids]
            if t["closed_at"] is None
            else []
        )
        t["done_hint"] = done_hint(t)
    return tasks


def list_tasks(
    conn: sqlite3.Connection, printer: str | None = None, *, status: str | None = None
) -> list[dict[str, Any]]:
    """Tasks of one printer (or all): status None = active (open, in progress, snoozed), `all`,
    or one status. Blocking first, then the oldest."""
    where: list[str] = []
    args: list[Any] = []
    if printer is not None:
        where.append("printer = ?")
        args.append(printer)
    if status is None:
        where.append("closed_at IS NULL")
    elif status != "all":
        if status not in STATUSES:
            raise TaskError(
                f"status: {status!r} is not one of {', '.join(STATUSES)}, all"
            )
        where.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM printer_task" + (
        " WHERE " + " AND ".join(where) if where else ""
    )
    order = " ORDER BY printer, closed_at IS NOT NULL, blocks = 'none', status = 'snoozed', id"
    return _with_waiting(conn, [_row(r) for r in conn.execute(sql + order, args)])


def summary(tasks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts for a list of active tasks (what Home Assistant shows): `open` excludes snoozed."""
    active = [t for t in tasks if t["status"] in ("open", "in_progress")]
    ready = [t for t in active if not t["waiting_on"]]
    idle = [t for t in ready if t["when"] in ("idle", "any")]
    nxt = idle[0] if idle else None
    return {
        "open": len(active),
        "blocking": sum(1 for t in active if t["blocks"] != "none"),
        "waiting": sum(1 for t in active if t["waiting_on"]),
        "snoozed": sum(1 for t in tasks if t["status"] == "snoozed"),
        "needs_human": sum(1 for t in active if t["needs_human"]),
        "next_idle": None
        if nxt is None
        else {k: nxt[k] for k in ("id", "title", "duration_min", "needs_human")},
    }


def done_hint(task: Mapping[str, Any]) -> str | None:
    """The command that records the work behind a derived task (which closes it); None = `done` works."""
    src, printer, head = task.get("source"), task.get("printer"), task.get("head")
    if src == "calibration":
        kinds = task.get("calibration_kinds") or ["KIND"]
        spool = f" --spool {task['spool_id']}" if task.get("spool_id") else ""
        where = f" --printer {printer}" + (f" --head {head}" if head else "") + spool
        if len(kinds) == 1:
            return f"3d calibrate record {kinds[0]}{where}"
        return f"3d calibrate record <kind>{where} (<kind>: {' | '.join(kinds)})"
    if src == "maintenance_rule":
        rule = (
            str(task["source_key"]).split("/")[1] if task.get("source_key") else "RULE"
        )
        return f"3d printops maint log {_subject(str(printer), head)} --rule {rule}"
    if src == "fault":
        fid = task.get("fault_id") or "FAULT_ID"
        return f"3d printops maint fault close {fid} --fixed-by MAINT_ID (after `3d printops maint log …`)"
    return None


# -- writes ------------------------------------------------------------------------------------
def _emit(
    conn: sqlite3.Connection, kind: str, t: Mapping[str, Any], actor: str, **extra: Any
) -> None:
    payload = {
        "id": t["id"],
        "uid": t["uid"],
        "printer": t["printer"],
        "head": t["head"],
        "title": t["title"],
        "source": t["source"],
        "source_key": t["source_key"],
        "status": t["status"],
        **extra,
    }
    events.emit(
        conn, f"printer_task.{kind}", _subject(t["printer"], t["head"]), payload, actor
    )


def _insert(
    conn: sqlite3.Connection,
    fields: Mapping[str, Any],
    *,
    actor: str,
    uid: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    now = db.utc_now()
    row = {k: fields.get(k) for k in _FIELDS}
    for k in _JSON:
        row[k] = json.dumps(list(fields.get(k) or []), ensure_ascii=False)
    row["needs_human"] = 1 if fields.get("needs_human", True) else 0
    row["blocks"] = fields.get("blocks") or "none"
    row["when"] = fields.get("when") or "any"
    cols = ["uid", *row, "status", "created_by", "created_at", "updated_at"]
    names = ", ".join('"when"' if c == "when" else c for c in cols)
    vals = [uid or buf.uuid7(), *row.values(), "open", actor, created_at or now, now]
    try:
        cur = conn.execute(
            f"INSERT INTO printer_task ({names}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608 — fixed columns
            vals,
        )
    except sqlite3.IntegrityError as e:
        raise TaskError(f"cannot record the task: {e}", 409) from e
    task = get(conn, int(cur.lastrowid or 0))
    _emit(
        conn,
        "opened",
        task,
        actor,
        type=task["type"],
        blocks=task["blocks"],
        when=task["when"],
    )
    return task


def _set(conn: sqlite3.Connection, task_id: int, changes: Mapping[str, Any]) -> None:
    cols = []
    vals: list[Any] = []
    for k, v in changes.items():
        cols.append(f'"{k}" = ?' if k == "when" else f"{k} = ?")
        vals.append(
            json.dumps(list(v), ensure_ascii=False)
            if k in _JSON
            else (int(v) if k == "needs_human" else v)
        )
    conn.execute(
        f"UPDATE printer_task SET {', '.join(cols)}, updated_at = ? WHERE id = ?",  # noqa: S608 — fixed columns
        (*vals, db.utc_now(), task_id),
    )


def _close(
    conn: sqlite3.Connection,
    task: Mapping[str, Any],
    status: str,
    *,
    actor: str,
    note: str | None,
    links: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _set(
        conn,
        int(task["id"]),
        {
            "status": status,
            "snoozed_until": None,
            "closed_at": db.utc_now(),
            "closed_by": actor,
            **({"note": note} if note else {}),
            **(links or {}),
        },
    )
    closed = get(conn, int(task["id"]))
    _emit(
        conn,
        "closed",
        closed,
        actor,
        note=closed["note"],
        calibration_id=closed["calibration_id"],
        maintenance_id=closed["maintenance_id"],
        fault_id=closed["fault_id"],
    )
    return closed


def fleet_printer(fleet: Any, printer: str) -> Any:
    p = fleet.printers.get(printer) if fleet is not None else None
    if p is None:
        known = (
            ", ".join(sorted(fleet.printers))
            if fleet is not None
            else "none (no fleet file)"
        )
        raise TaskError(f"unknown printer {printer!r}; fleet has: {known}", 404)
    return p


def _head(p: Any, head: str | None) -> str | None:
    if head is None:
        return None
    h = next(
        (x for x in p.heads if x.label.casefold() == head.strip().casefold()), None
    )
    if h is None:
        raise TaskError(
            f"{p.name} has no head {head!r}; heads: {', '.join(x.label for x in p.heads) or '-'}",
            404,
        )
    return str(h.label)


def _deps(conn: sqlite3.Connection, printer: str, ids: Iterable[Any]) -> list[int]:
    out: list[int] = []
    for raw in ids:
        dep = get(conn, raw)
        if dep["printer"] != printer:
            raise TaskError(
                f"task #{dep['id']} is on {dep['printer']}, not {printer}: dependencies stay on one printer"
            )
        if int(dep["id"]) not in out:
            out.append(int(dep["id"]))
    return out


def _validate(
    conn: sqlite3.Connection, fleet: Any, printer: str, f: dict[str, Any]
) -> dict[str, Any]:
    p = fleet_printer(fleet, printer)
    f["head"] = _head(p, f.get("head"))
    for key in ("title", "why", "slot", "when_note", "note"):
        v = f.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            raise TaskError(f"{key}: must be non-empty text when given")
        f[key] = v.strip() if isinstance(v, str) else None
    if not f.get("title"):
        raise TaskError("title: required (what to do)")
    if f.get("type", "other") not in buf.TYPES:
        raise TaskError(f"type: {f.get('type')!r} is not one of {', '.join(buf.TYPES)}")
    if f.get("blocks", "none") not in buf.BLOCKS:
        raise TaskError(
            f"blocks: {f.get('blocks')!r} is not one of {', '.join(buf.BLOCKS)}"
        )
    if f.get("when", "any") not in buf.WHEN:
        raise TaskError(f"when: {f.get('when')!r} is not one of {', '.join(buf.WHEN)}")
    if f.get("blocks") == "head" and not f["head"]:
        raise TaskError("blocks head: name the head (head T2, or `--blocks head:T2`)")
    if f.get("blocks") == "slot" and not f["slot"]:
        raise TaskError("blocks slot: name the slot (slot T3, or `--blocks slot:T3`)")
    if f.get("component") is not None and f["component"] not in COMPONENTS:
        raise TaskError(
            f"component: {f['component']!r} is not one of {', '.join(COMPONENTS)}"
        )
    dur = f.get("duration_min")
    if dur is not None and (
        isinstance(dur, bool) or not isinstance(dur, int) or dur <= 0
    ):
        raise TaskError(
            f"duration_min: must be a positive whole number of minutes, got {dur!r}"
        )
    if not isinstance(f.get("needs_human", True), bool):
        raise TaskError(f"needs_human: must be true or false, got {f['needs_human']!r}")
    f["depends_on"] = _deps(conn, printer, f.get("depends_on") or ())
    f["printer"] = printer
    return f


def create(
    conn: sqlite3.Connection,
    fleet: Any,
    printer: str,
    *,
    actor: str,
    title: str,
    type_: str = "other",
    source: str = "manual",
    **fields: Any,
) -> dict[str, Any]:
    """A task a person (CLI, HA, bot) or a calibration follow-up asks for."""
    if source not in CREATABLE:
        raise TaskError(
            f"source: {source!r} cannot be created directly (only {', '.join(CREATABLE)}; "
            "derived tasks come from faults, due rules and calibrations)"
        )
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise TaskError(f"unknown task field(s): {', '.join(sorted(unknown))}")
    with db.transaction(conn):
        f = _validate(
            conn,
            fleet,
            printer,
            {
                **fields,
                "title": title,
                "type": type_,
                "source": source,
                "source_key": None,
            },
        )
        return _insert(conn, f, actor=actor)


def _active(conn: sqlite3.Connection, ref: str | int) -> dict[str, Any]:
    t = get(conn, ref)
    if t["closed_at"] is not None:
        raise TaskError(
            f"task #{t['id']} is already {t['status']} ({t['closed_at'][:10]})", 409
        )
    return t


def done(
    conn: sqlite3.Connection, ref: str | int, *, actor: str, note: str | None = None
) -> dict[str, Any]:
    with db.transaction(conn):
        t = _active(conn, ref)
        hint = done_hint(t)
        if hint is not None:
            raise TaskError(
                f"task #{t['id']} is derived from a {t['source'].replace('_', ' ')} and closes when the work "
                f"is recorded: {hint}. To drop it without the work: dismiss it with a reason.",
                409,
            )
        return _close(conn, t, "done", actor=actor, note=(note or "").strip() or None)


def dismiss(
    conn: sqlite3.Connection, ref: str | int, *, actor: str, reason: str
) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise TaskError("reason: required (why the task is dropped without doing it)")
    with db.transaction(conn):
        return _close(
            conn, _active(conn, ref), "dismissed", actor=actor, note=reason.strip()
        )


def snooze(
    conn: sqlite3.Connection,
    ref: str | int,
    *,
    actor: str,
    until: str | None = None,
    reason: str | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Hide a task until `until` (ISO; a date = its midnight UTC) or, without one, until reopened."""
    stamp = None
    if until is not None:
        when = _parse_at(until, "until")
        if when <= (now or datetime.datetime.now(datetime.timezone.utc)):
            raise TaskError(f"until: {until!r} is not in the future")
        stamp = _fmt(when)
    with db.transaction(conn):
        t = _active(conn, ref)
        changes: dict[str, Any] = {"status": "snoozed", "snoozed_until": stamp}
        if reason and reason.strip():
            changes["note"] = reason.strip()
        _set(conn, int(t["id"]), changes)
        out = get(conn, int(t["id"]))
        _emit(conn, "updated", out, actor, changes=sorted(changes), snoozed_until=stamp)
        return out


def reopen(conn: sqlite3.Connection, ref: str | int, *, actor: str) -> dict[str, Any]:
    """A snoozed task back to open now; a done/dismissed one opened again (not a derived done one:
    its record is still there — the derivation opens a new task if the need comes back)."""
    with db.transaction(conn):
        t = get(conn, ref)
        if t["status"] in ("open", "in_progress"):
            raise TaskError(f"task #{t['id']} is already {t['status']}", 409)
        if t["status"] == "done" and t["derived"]:
            raise TaskError(
                f"task #{t['id']} was closed by its record; a new need opens a new task",
                409,
            )
        try:
            _set(
                conn,
                int(t["id"]),
                {
                    "status": "open",
                    "snoozed_until": None,
                    "closed_at": None,
                    "closed_by": None,
                },
            )
        except sqlite3.IntegrityError as e:
            raise TaskError(
                f"task #{t['id']}: another open task already has key {t['source_key']}",
                409,
            ) from e
        out = get(conn, int(t["id"]))
        _emit(
            conn, "updated", out, actor, changes=["status"], reopened_from=t["status"]
        )
        return out


# -- derivation --------------------------------------------------------------------------------
@dataclass
class Want:
    """A derived need: the task fields it implies (source + source_key included)."""

    fields: dict[str, Any]

    @property
    def key(self) -> str:
        return str(self.fields["source_key"])


@dataclass
class Derivation:
    """What the sources say for one printer. `authority` decides which open derived tasks the
    sources may close when their key is neither wanted nor satisfied."""

    want: dict[str, Want] = field(default_factory=dict)
    satisfied: dict[str, tuple[str, str | None, dict[str, Any]]] = field(
        default_factory=dict
    )  # key -> (status, note, links)
    sources_ok: set[str] = field(default_factory=set)
    calibration: dict[str, _HeadNeeds] = field(
        default_factory=dict
    )  # head task key -> the engine's verdicts; `_calibration_tasks` turns them into wants
    kind_titles: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def authoritative(self, task: Mapping[str, Any]) -> bool:
        # A calibration task holding an undecided kind is wanted (its entry is kept), so the
        # source that ran is authoritative for every other one.
        return task["source"] in self.sources_ok


def _fault_wants(conn: sqlite3.Connection, printer: str, d: Derivation) -> None:
    for f in conn.execute(
        "SELECT * FROM fault WHERE printer = ? ORDER BY id", (printer,)
    ):
        key = f"fault:{f['external_key'] or f['id']}"
        if f["status"] != "open":
            status = "done" if f["status"] == "fixed" else "dismissed"
            note = f"fault #{f['id']} {f['status']}" + (
                f": {f['root_cause']}" if f["root_cause"] else ""
            )
            d.satisfied[key] = (
                status,
                note,
                {"fault_id": f["id"], "maintenance_id": f["fixed_by_maintenance_id"]},
            )
            continue
        where = _subject(printer, f["head"])
        d.want[key] = Want(
            {
                "printer": printer,
                "head": f["head"],
                "component": f["component"],
                "type": "repair",
                "title": f"Repair {where}: {f['symptom']}",
                "why": f"open fault #{f['id']} since {str(f['at'])[:10]}; "
                + (
                    "prints using this head are refused until it is fixed"
                    if f["head"]
                    else "the printer is not to print until it is fixed"
                ),
                "blocks": "head" if f["head"] else "printer",
                "when": "any",
                "needs_human": True,
                "source": "fault",
                "source_key": key,
                "fault_id": f["id"],
            }
        )
    d.sources_ok.add("fault")


def _rule_wants(
    conn: sqlite3.Connection, printer: str, rules: Any, d: Derivation
) -> None:
    from printops.maintenance_rules import baseline

    for r in conn.execute(
        "SELECT * FROM maintenance_due WHERE printer = ? ORDER BY rule_id, head, baseline_id",
        (printer,),
    ):
        head = r["head"] or None
        key = f"rule:{printer}/{r['rule_id']}/{head or '-'}/{r['baseline_id']}"
        rule = rules.get(r["rule_id"])
        if rule is None or printer not in rule.printers:
            continue  # the rule left the file: an open task for it is no longer wanted
        base = baseline(conn, rule, printer, head)
        now_id = int(base["id"]) if base is not None else 0
        if now_id != int(r["baseline_id"]):
            d.satisfied[key] = (
                "done",
                f"maintenance #{now_id} logged",
                {"maintenance_id": now_id},
            )
            continue
        d.want[key] = Want(
            {
                "printer": printer,
                "head": head,
                "component": rule.component,
                "type": "maintenance",
                "title": rule.title,
                "why": f"maintenance rule {rule.id} is due: {r['reason']}",
                "blocks": "none",
                "when": "idle",
                "needs_human": True,
                "refs": [rule.source] if rule.source else [],
                "source": "maintenance_rule",
                "source_key": key,
            }
        )
    d.sources_ok.add("maintenance_rule")


def calibration_key(printer: str, req: Any) -> str:
    """'calib:<printer>/<head|->/<kind>[/nozzle:N][/filament:F][/lot:L]' — the full scope of the
    record that would satisfy the requirement, so another nozzle or spool is another task. A plate
    is not in the key: the engine decides a plate-scoped kind only for a printer with one plate
    (several plates → `no_plate`, no task)."""
    k = req.kind
    key = f"calib:{printer}/{req.head or '-'}/{k.id}"
    if "nozzle" in k.scope:
        key += f"/nozzle:{req.nozzle.install_id if req.nozzle and req.nozzle.install_id is not None else '-'}"
    if "filament" in k.scope:
        fil = req.filament
        key += f"/filament:{fil.filament_id if fil and fil.filament_id is not None else '-'}"
        if k.lot_sensitive and fil is not None and fil.lot_nr:
            key += f"/lot:{fil.lot_nr}"
    return key


def calibration_task_key(printer: str, head: str | None) -> str:
    """'calib:<printer>/<head|->' — the one calibration task of a head (#387). The requirement
    keys it covers (`calibration_key`) extend it; a longer key on an open task is a per-kind task
    of the first derivation (#281), superseded by the next pass."""
    return f"calib:{printer}/{head or '-'}"


def _per_kind(t: Mapping[str, Any]) -> bool:
    """A calibration task of the first derivation (#281): one kind, the requirement key itself."""
    key = t["source_key"] or ""
    head = calibration_task_key(t["printer"], t["head"])
    return key.startswith(head + "/") and len(t["calibration_kinds"]) == 1


def _entry(
    *,
    key: str,
    kind: str,
    title: str,
    reason: str | None,
    spool_id: int | None,
    spool: str | None,
    minutes: int | None,
) -> dict[str, Any]:
    """One requirement a calibration task covers (a `requirements` item, migration 0072)."""
    return {
        "key": key,
        "kind": kind,
        "title": title,
        "reason": reason,
        "spool_id": spool_id,
        "spool": spool,
        "minutes": minutes,
    }


def _requirements_of(
    t: Mapping[str, Any], titles: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The requirement entries a calibration task covers; a per-kind task of #281 covers its key."""
    if t["requirements"]:
        return list(t["requirements"])
    if not _per_kind(t):
        return []
    kind = t["calibration_kinds"][0]
    return [
        _entry(
            key=t["source_key"],
            kind=kind,
            title=titles.get(kind, kind),
            reason=t["why"],
            spool_id=t["spool_id"],
            spool=t["spools"][0]["name"] if t["spools"] else None,
            minutes=t["duration_min"],
        )
    ]


@dataclass
class _HeadNeeds:
    """The engine's verdicts for one head (None = the printer-level kinds)."""

    head: str | None
    # missing requirement entries, each with when its own record was measured (expired or
    # invalidated; None = never measured in this scope)
    missing: list[tuple[dict[str, Any], str | None]] = field(default_factory=list)
    uncertain: set[str] = field(
        default_factory=set
    )  # kinds the engine could not decide


def _calibration_want(
    printer: str, head: str | None, key: str, reqs: list[dict[str, Any]]
) -> Want:
    spools: list[dict[str, Any]] = []
    for r in reqs:
        s = {"name": r["spool"], "spool_id": r["spool_id"]}
        if r["spool"] and s not in spools:
            spools.append(s)
    ids = {s["spool_id"] for s in spools if s["spool_id"] is not None}
    for_ = f" for {', '.join(s['name'] for s in spools)}" if spools else ""
    if len(reqs) == 1:
        title = f"Calibrate {reqs[0]['title']}" + (f" on {head}" if head else "") + for_
    else:
        title = f"Calibrate {len(reqs)} kinds on {head or printer}{for_}"
    by_reason: dict[str, list[str]] = {}
    for r in reqs:
        by_reason.setdefault(str(r["reason"]), []).append(str(r["title"]))
    minutes = sum(int(r["minutes"] or 0) for r in reqs)
    return Want(
        {
            "printer": printer,
            "head": head,
            "type": "calibration",
            "title": title,
            "why": "; ".join(f"{', '.join(t)} — {why}" for why, t in by_reason.items()),
            "blocks": "none",
            "when": "idle",
            "needs_human": True,
            "duration_min": minutes or None,
            "calibration_kinds": list(dict.fromkeys(r["kind"] for r in reqs)),
            "spools": spools,
            "spool_id": ids.pop() if len(ids) == 1 else None,
            "requirements": reqs,
            "source": "calibration",
            "source_key": key,
        }
    )


def _dismissed_by_person(
    tasks: Sequence[tuple[Mapping[str, Any], list[dict[str, Any]]]],
    key: str,
    measured: str | None,
) -> bool:
    """A person dismissed the newest task (`tasks` newest first) that covered this requirement
    key, and no record of it was made since: an expiry after a record is a new need."""
    for t, reqs in tasks:
        if any(r["key"] == key for r in reqs):
            return (
                t["status"] == "dismissed"
                and t["closed_by"] != DERIVE
                and (measured is None or measured <= t["closed_at"])
            )
    return False


def _calibration_wants(ctx: Any, printer: str, d: Derivation) -> None:
    """The engine's verdicts per head. No task reads: `_calibration_tasks` weighs them against
    the task history inside the write transaction, so a dismissal racing the pass is seen."""
    from printops.calibrations import CalibrationError, requirements

    try:
        rs = requirements(ctx, printer)
    except CalibrationError as e:
        d.errors.append(f"calibration: {e.message}")
        return
    d.kind_titles = {k.id: k.title for k in ctx.catalog.kinds.values()}
    for r in rs.items:
        k = r.kind
        if k is None:
            continue
        g = d.calibration.setdefault(
            calibration_task_key(printer, r.head), _HeadNeeds(r.head)
        )
        if r.status in UNCERTAIN:
            g.uncertain.add(k.id)
            continue
        key = calibration_key(printer, r)
        if r.status in SATISFIED:
            rec = r.record.id if r.record is not None else None
            d.satisfied[key] = (  # closes a per-kind task of #281 as done
                "done",
                f"calibration #{rec} recorded" if rec else None,
                {"calibration_id": rec},
            )
            continue
        fil = r.filament
        entry = _entry(
            key=key,
            kind=k.id,
            title=k.title,
            reason=f"{r.status.replace('_', ' ')}: {r.detail}",
            spool_id=fil.spool_id if fil else None,
            spool=fil.label() if fil else None,
            minutes=max(1, math.ceil(k.estimate_minutes))
            if k.estimate_minutes
            else None,
        )
        # The record of an expired/invalidated verdict can be of another scope (another lot of
        # the filament; new_lot / new_nozzle always are): only a record of this key counts.
        own = r.record if r.status in ("expired", "invalidated") else None
        if own is not None and calibration_key(printer, _scope_of(r, own)) != key:
            own = None
        g.missing.append((entry, own.recorded_at if own is not None else None))
    d.sources_ok.add("calibration")


def _scope_of(req: Any, record: Any) -> SimpleNamespace:
    """The requirement as `calibration_key` sees it, with the scope of `record` instead."""
    return SimpleNamespace(
        kind=req.kind, head=req.head, nozzle=record.nozzle, filament=record.filament
    )


def _calibration_tasks(conn: sqlite3.Connection, printer: str, d: Derivation) -> None:
    """Per head: the want of its calibration task (missing entries a person has not dismissed,
    plus the entries of undecided kinds an open task already had); and `done` for an open head
    task nothing is wanted for once a record satisfies something it covered."""
    past: dict[str | None, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
    for r in conn.execute(
        "SELECT * FROM printer_task WHERE printer = ? AND source_key LIKE 'calib:%' "
        "ORDER BY id DESC",
        (printer,),
    ):
        t = _row(r)
        past.setdefault(t["head"], []).append((t, _requirements_of(t, d.kind_titles)))
    for key, g in d.calibration.items():
        tasks = past.get(g.head, [])
        reqs = [
            e
            for e, measured in g.missing
            if not _dismissed_by_person(tasks, e["key"], measured)
        ]
        seen = {e["key"] for e, _ in g.missing}
        for t, covered in tasks:
            if t["closed_at"] is None:
                for entry in covered:
                    if entry["kind"] in g.uncertain and entry["key"] not in seen:
                        seen.add(entry["key"])
                        reqs.append(entry)
        if reqs:
            d.want[key] = _calibration_want(printer, g.head, key, reqs)
    for tasks in past.values():
        for t, covered in tasks:
            key = t["source_key"]
            if (
                t["closed_at"] is not None
                or key != calibration_task_key(printer, t["head"])
                or key in d.want
            ):
                continue
            records = [
                d.satisfied[e["key"]][2] for e in covered if e["key"] in d.satisfied
            ]
            if records:
                ids = sorted(
                    {
                        x["calibration_id"]
                        for x in records
                        if x["calibration_id"] is not None
                    }
                )
                d.satisfied[key] = (
                    "done",
                    "every calibration is current"
                    + (f": {', '.join(f'#{i}' for i in ids)} recorded" if ids else ""),
                    {"calibration_id": ids[-1] if ids else None},
                )


@dataclass
class Sources:
    """What derivation reads besides the DB; None = that source is unusable this pass (its
    tasks are left alone, the reason is in `errors`)."""

    fleet: Any
    rules: Any | None = None
    calibration: Any | None = None  # printops.calibrations.Ctx
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    opened: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.opened) + len(self.closed) + len(self.updated)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "changed": self.changed}


def derive(conn: sqlite3.Connection, sources: Sources, printer: str) -> Derivation:
    """Read every source for one printer (no writes)."""
    d = Derivation(errors=list(sources.errors))
    _fault_wants(conn, printer, d)
    if sources.rules is not None:
        _rule_wants(conn, printer, sources.rules, d)
    if sources.calibration is not None:
        _calibration_wants(sources.calibration, printer, d)
    return d


def _refresh(t: Mapping[str, Any], w: Want) -> dict[str, Any]:
    """Fields of an open derived task its source now describes differently (title/why/links/scope)."""
    keys = (
        "title",
        "why",
        "blocks",
        "head",
        "duration_min",
        "fault_id",
        "spool_id",
        "calibration_kinds",
        "spools",
        "requirements",
    )
    return {
        k: w.fields.get(k)
        for k in keys
        if k in w.fields and t.get(k) != w.fields.get(k)
    }


def sync(
    conn: sqlite3.Connection,
    sources: Sources,
    printer: str | None = None,
    *,
    now: datetime.datetime | None = None,
) -> SyncReport:
    """Materialise derived tasks for one printer (or every fleet printer) and close the ones whose
    need is satisfied or gone; reopen snoozed tasks whose time is up. Idempotent."""
    report = SyncReport()
    names = (
        [printer]
        if printer is not None
        else sorted(sources.fleet.printers)
        if sources.fleet
        else []
    )
    stamp = _fmt(now or datetime.datetime.now(datetime.timezone.utc))
    for name in names:
        d = derive(conn, sources, name)  # Spoolman reads stay outside the write lock
        report.errors += d.errors
        with db.transaction(conn):
            _apply(conn, d, name, report)
            for r in conn.execute(
                "SELECT id FROM printer_task WHERE printer = ? AND status = 'snoozed' "
                "AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
                (name, stamp),
            ).fetchall():
                _set(conn, int(r["id"]), {"status": "open", "snoozed_until": None})
                _emit(
                    conn,
                    "updated",
                    get(conn, int(r["id"])),
                    DERIVE,
                    changes=["status"],
                    reopened_from="snoozed",
                )
                report.updated.append(int(r["id"]))
    return report


def _apply(
    conn: sqlite3.Connection, d: Derivation, printer: str, report: SyncReport
) -> None:
    if (
        "calibration" in d.sources_ok
    ):  # inside the write lock: sees every committed dismissal
        _calibration_tasks(conn, printer, d)
    open_by_key: dict[str, dict[str, Any]] = {}
    per_kind: list[dict[str, Any]] = []  # calibration tasks of #281, one per kind
    for r in conn.execute(
        "SELECT * FROM printer_task WHERE printer = ? AND closed_at IS NULL "
        "AND source_key IS NOT NULL",
        (printer,),
    ):
        t = _row(r)
        open_by_key[t["source_key"]] = t
    for key, t in open_by_key.items():
        if (
            key in d.satisfied
        ):  # satisfied keys are derived ones: whoever opened the task, the need is met
            status, note, links = d.satisfied[key]
            _close(
                conn,
                t,
                status,
                actor=DERIVE,
                note=note,
                links={k: v for k, v in links.items() if v is not None},
            )
            report.closed.append(int(t["id"]))
        elif key in d.want:
            w = d.want[key]
            changes = _refresh(t, w)
            if (
                t["source"] not in DERIVED
            ):  # a manual/imported task with a derived key: the source adopts it
                changes["source"] = w.fields["source"]
            if changes:
                _set(conn, int(t["id"]), changes)
                _emit(
                    conn,
                    "updated",
                    get(conn, int(t["id"])),
                    DERIVE,
                    changes=sorted(changes),
                )
                report.updated.append(int(t["id"]))
        elif "calibration" in d.sources_ok and _per_kind(t):
            per_kind.append(t)
        elif t["source"] in DERIVED and d.authoritative(t):
            _close(
                conn,
                t,
                "dismissed",
                actor=DERIVE,
                note="no longer needed: its source no longer asks for it",
            )
            report.closed.append(int(t["id"]))
    for key, w in d.want.items():
        if key in open_by_key:
            continue
        last = conn.execute(
            "SELECT status, closed_by FROM printer_task WHERE source_key = ? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if (
            not w.fields.get(
                "requirements"
            )  # a calibration want left out what a person dismissed
            and last is not None
            and last["status"] == "dismissed"
            and last["closed_by"] != DERIVE
        ):
            continue  # a person dismissed this need: it stays dismissed until its scope changes
        report.opened.append(int(_insert(conn, w.fields, actor=DERIVE)["id"]))
    head_tasks: dict[str, Any] = {}
    moved: dict[
        int, int
    ] = {}  # superseded task id -> the head task that still covers it
    for t in per_kind:  # after the inserts: name the head's task that took over
        head_key = calibration_task_key(printer, t["head"])
        if head_key not in head_tasks:
            head_tasks[head_key] = conn.execute(
                "SELECT id FROM printer_task WHERE source_key = ? AND closed_at IS NULL",
                (head_key,),
            ).fetchone()
        head_task = head_tasks[head_key]
        _close(
            conn,
            t,
            "dismissed",
            actor=DERIVE,
            note="superseded: one calibration task per head"
            + (f" (task #{head_task['id']})" if head_task else ""),
        )
        report.closed.append(int(t["id"]))
        head_want = d.want.get(head_key)
        if (
            head_task
            and head_want
            and any(
                e["key"] == t["source_key"] for e in head_want.fields["requirements"]
            )
        ):
            moved[int(t["id"])] = int(head_task["id"])
    if moved:  # a task that waited on a superseded one waits on the head's task instead
        for r in conn.execute(
            "SELECT * FROM printer_task WHERE printer = ? AND closed_at IS NULL",
            (printer,),
        ).fetchall():
            deps = [int(i) for i in json.loads(r["depends_on"] or "[]")]
            if any(i in moved for i in deps):
                _set(
                    conn,
                    int(r["id"]),
                    {"depends_on": list(dict.fromkeys(moved.get(i, i) for i in deps))},
                )
                _emit(
                    conn,
                    "updated",
                    get(conn, int(r["id"])),
                    DERIVE,
                    changes=["depends_on"],
                )
                report.updated.append(int(r["id"]))


# -- buffer import -----------------------------------------------------------------------------
class _DryRun(Exception):
    pass


# outcome of one imported op (first word) -> the ImportReport counter it adds to
_OUTCOME_COUNTER = {
    "created": "created",
    "merged": "merged",
    "applied": "applied",
    "exists": "applied",
    "refused": "refused",
}


@dataclass
class ImportReport:
    dry_run: bool
    lines: int = 0
    created: int = 0
    merged: int = 0
    applied: int = 0
    refused: int = 0
    skipped: int = 0  # imported before (same uid)
    notices: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return self.created + self.merged + self.applied + self.refused + self.notices

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "added": self.added}


def _resolve_spools(
    spoolman: Any, spools: Sequence[Mapping[str, Any]], n: int, report: ImportReport
) -> list[dict[str, Any]]:
    """Bind a spool named only by its filament name: the one Spoolman spool of exactly that name,
    else the one whose filament name contains it ('Snapdragon' → 'жёлтый Snapdragon'); anything
    else stays unbound with a warning."""
    out = []
    for s in spools:
        sid, name = s.get("spool_id"), s.get("name")
        if sid is None and name and spoolman is not None:
            from printops.spools import SpoolError, all_spools

            want = str(name).casefold()
            try:
                names = [
                    (sp, str((sp.get("filament") or {}).get("name") or "").casefold())
                    for sp in all_spools(spoolman)
                ]
            except SpoolError as e:
                report.warnings.append(
                    f"line {n}: spool {name!r} not bound: Spoolman: {e.message}"
                )
                names = []
            exact = [sp for sp, fil in names if fil == want]
            partial = [sp for sp, fil in names if want in fil]
            if len(exact) == 1 or (not exact and len(partial) == 1):
                hit = (exact or partial)[0]
                sid = int(hit["id"])
                if not exact:
                    report.warnings.append(
                        f"line {n}: spool {name!r} bound to #{sid} by partial filament name "
                        f"{(hit.get('filament') or {}).get('name')!r}"
                    )
            elif names:
                hits = exact or partial
                found = ", ".join(f"#{h['id']}" for h in hits) or "none"
                report.warnings.append(
                    f"line {n}: spool {name!r} not bound: {len(hits)} Spoolman spools of that "
                    f"filament name ({found})"
                )
        out.append({"name": name, "spool_id": sid})
    return out


def _task_for(conn: sqlite3.Connection, task_uid: str) -> dict[str, Any] | None:
    """The task a buffer task uid now is: itself, or the task its open merged into."""
    r = conn.execute(
        "SELECT id FROM printer_task WHERE uid = ?", (task_uid,)
    ).fetchone()
    if r is None:
        r = conn.execute(
            "SELECT task_id AS id FROM printer_task_op WHERE uid = ? AND op = 'open' "
            "AND task_id IS NOT NULL",
            (task_uid,),
        ).fetchone()
    return get(conn, int(r["id"])) if r is not None else None


def _when(d: Mapping[str, Any]) -> str | None:
    raw = d.get("recorded_at") or d.get("at")
    if not raw:
        return None
    try:
        return _fmt(_parse_at(str(raw), "at"))
    except TaskError:
        return None


def _import_open(
    conn: sqlite3.Connection,
    fleet: Any,
    ln: buf.Line,
    spoolman: Any,
    actor: str,
    report: ImportReport,
) -> tuple[int | None, str]:
    d = ln.data
    existing = _task_for(conn, d["task_uid"])
    if existing is not None:
        return int(existing["id"]), "exists"
    key = d.get("source_key")
    if key:
        r = conn.execute(
            "SELECT id FROM printer_task WHERE source_key = ? AND closed_at IS NULL",
            (key,),
        ).fetchone()
        if r is not None:
            t = get(conn, int(r["id"]))
            if t["printer"] != d["printer"]:
                raise buf.BufferError(
                    f"source_key {key} belongs to an open task on {t['printer']}", ln.n
                )
            _set(conn, int(t["id"]), {"refs": [*t["refs"], d["uid"]]})
            _emit(
                conn,
                "updated",
                get(conn, int(t["id"])),
                actor,
                changes=["refs"],
                merged_uid=d["uid"],
            )
            return int(t["id"]), "merged"
    scope, bhead, bslot = buf.blocks_of(d, ln.n) or ("none", None, None)
    spools = _resolve_spools(spoolman, d.get("spools") or [], ln.n, report)
    ids = [s["spool_id"] for s in spools if s["spool_id"] is not None]
    deps = []
    for dep in d.get("depends_on") or []:
        dep_task = _task_for(conn, dep)
        if dep_task is None:
            raise buf.BufferError(
                f"depends_on {dep}: no task with that uid (open it on an earlier line)",
                ln.n,
            )
        deps.append(int(dep_task["id"]))
    f = {
        k: d.get(k)
        for k in (
            "head",
            "slot",
            "component",
            "title",
            "why",
            "when",
            "when_note",
            "duration_min",
            "calibration_kinds",
            "refs",
            "note",
        )
    }
    f.update(
        {
            "type": d["type"],
            "blocks": scope,
            "head": d.get("head") or bhead,
            "slot": d.get("slot") or bslot,
            "needs_human": d.get("needs_human")
            if d.get("needs_human") is not None
            else True,
            "spools": spools,
            "spool_id": ids[0] if len(ids) == 1 else None,
            "depends_on": deps,
            "source": "import",
            "source_key": key,
            "when": d.get("when") or "any",
        }
    )
    try:
        f = _validate(conn, fleet, d["printer"], f)
    except TaskError as e:
        raise buf.BufferError(e.message, ln.n) from e
    task = _insert(
        conn,
        f,
        actor=str(d.get("actor") or actor),
        uid=d["task_uid"],
        created_at=_when(d),
    )
    return int(task["id"]), "created"


def _import_op(
    conn: sqlite3.Connection, ln: buf.Line, actor: str
) -> tuple[int | None, str]:
    d = ln.data
    t = _task_for(conn, d["task_uid"])
    if t is None:
        raise buf.BufferError(
            f"op {d['op']} for task {d['task_uid']} that no earlier line opens", ln.n
        )
    if t["printer"] != d["printer"]:
        raise buf.BufferError(
            f"op {d['op']} names printer {d['printer']}, but the task is on {t['printer']}",
            ln.n,
        )
    who = str(d.get("actor") or actor)
    note = (d.get("note") or d.get("reason") or "").strip() or None
    tid = int(t["id"])
    if d["op"] == "update":
        allowed = _SCHEDULING if t["derived"] else (*_SCHEDULING, *_DESCRIPTIVE)
        changes = {
            k: d[k] for k in allowed if d.get(k) is not None and k != "depends_on"
        }
        if d.get("depends_on") is not None:
            deps = []
            for dep in d["depends_on"]:
                dt = _task_for(conn, dep)
                if dt is None:
                    raise buf.BufferError(
                        f"depends_on {dep}: no task with that uid", ln.n
                    )
                if dt["printer"] != t["printer"]:
                    raise buf.BufferError(
                        f"depends_on {dep}: task #{dt['id']} is on {dt['printer']}; "
                        "dependencies stay on one printer",
                        ln.n,
                    )
                deps.append(int(dt["id"]))
            changes["depends_on"] = deps
        if "refs" in changes:
            changes["refs"] = list(dict.fromkeys([*t["refs"], *changes["refs"]]))
        if changes:
            _set(conn, tid, changes)
            _emit(conn, "updated", get(conn, tid), who, changes=sorted(changes))
        return tid, "applied"
    if t["closed_at"] is not None:
        return tid, f"applied (already {t['status']})"
    if d["op"] == "done":
        hint = done_hint(t)
        if hint is not None:
            return None, f"refused: task #{tid} is derived — record the work: {hint}"
        _close(conn, t, "done", actor=who, note=note)
    elif d["op"] == "dismiss":
        _close(conn, t, "dismissed", actor=who, note=note or "dismissed in the buffer")
    else:
        until = d.get("snoozed_until")
        stamp = _fmt(_parse_at(until, "snoozed_until")) if until else None
        _set(
            conn,
            tid,
            {
                "status": "snoozed",
                "snoozed_until": stamp,
                **({"note": note} if note else {}),
            },
        )
        _emit(
            conn,
            "updated",
            get(conn, tid),
            who,
            changes=["status"],
            snoozed_until=stamp,
        )
    return tid, "applied"


def _import_notice(conn: sqlite3.Connection, d: Mapping[str, Any]) -> bool:
    row = (
        d["printer"],
        d["idle_since"],
        d["state"],
        d["claimed_at"],
        d.get("sent_at"),
        d.get("tg_message_id"),
        json.dumps(d["task_uids"]),
        d.get("task_set_sha"),
        d.get("host"),
    )
    have = conn.execute(
        "SELECT printer, idle_since, state, claimed_at, sent_at, tg_message_id, task_uids, "
        "task_set_sha, host FROM printer_idle_notice WHERE printer = ? AND idle_since = ?",
        row[:2],
    ).fetchone()
    if have is not None and (
        tuple(have) == row or (have["state"] != "pending" and d["state"] == "pending")
    ):
        return False  # unchanged, or an older claim replayed after the period was sent/failed
    conn.execute(
        "INSERT OR REPLACE INTO printer_idle_notice (printer, idle_since, state, claimed_at, sent_at, "
        "tg_message_id, task_uids, task_set_sha, host) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    return True


def import_lines(
    conn: sqlite3.Connection,
    fleet: Any,
    lines: Iterable[str],
    *,
    actor: str,
    spoolman: Any = None,
    dry_run: bool = False,
) -> ImportReport:
    """Replay buffer lines (both schemas) in one transaction; `dry_run` rolls it back and reports."""
    try:
        parsed = buf.parse_lines(lines)
    except buf.BufferError as e:
        raise TaskError(f"buffer {e}; nothing imported") from e
    report = ImportReport(dry_run=dry_run, lines=len(parsed))
    try:
        with db.transaction(conn):
            for ln in parsed:
                d = ln.data
                if d["printer"] not in (fleet.printers if fleet is not None else {}):
                    raise buf.BufferError(
                        f"printer {d['printer']!r} is not in the fleet", ln.n
                    )
                if ln.schema == buf.NOTICE_SCHEMA:
                    if _import_notice(conn, d):
                        report.notices += 1
                        report.items.append(
                            {
                                "line": ln.n,
                                "uid": d["uid"],
                                "op": "idle_notice",
                                "outcome": "stored",
                            }
                        )
                    else:
                        report.skipped += 1
                    continue
                if conn.execute(
                    "SELECT 1 FROM printer_task_op WHERE uid = ?", (d["uid"],)
                ).fetchone():
                    report.skipped += 1
                    continue
                if d["op"] == "open":
                    tid, outcome = _import_open(
                        conn, fleet, ln, spoolman, actor, report
                    )
                else:
                    tid, outcome = _import_op(conn, ln, actor)
                kind = outcome.split(":")[0].split(" ")[0]
                count = _OUTCOME_COUNTER.get(kind, "applied")
                setattr(report, count, getattr(report, count) + 1)
                conn.execute(
                    "INSERT INTO printer_task_op (uid, task_uid, task_id, op, outcome, line, imported_at, "
                    "imported_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        d["uid"],
                        d["task_uid"],
                        tid,
                        d["op"],
                        outcome,
                        ln.raw,
                        db.utc_now(),
                        actor,
                    ),
                )
                report.items.append(
                    {
                        "line": ln.n,
                        "uid": d["uid"],
                        "op": d["op"],
                        "task_id": tid,
                        "outcome": outcome,
                        "title": d.get("title"),
                    }
                )
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    except buf.BufferError as e:
        raise TaskError(f"buffer {e}; nothing imported") from e
    return report
