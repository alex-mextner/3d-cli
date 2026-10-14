"""drying.py — drying sessions and the "is this spool due for drying?" assessment (#162, plan §5.2).

ACCESSED VIA: 3d-api (`routes_drying.py`: REST + the periodic check) and, for the
`drying_due` print gate (#164), `assess` / `assess_all`.

MODEL:
  - A session puts spools into a dryer location (kind dryer|dry_box) at temp_c for target_h.
    Missing temp/hours come from the spools' recipes (fleet/policy.yaml `drying:` + filament
    extra overrides): the LOWEST recommended temperature (nothing softens) and the LONGEST time.
  - Finishing writes Spoolman spool extras `last_dried_at` = end time and `dry_state` = fresh,
    and records optional before/after weights as ledger NOTES — water loss is not filament
    usage, so it never reaches Spoolman `/use` and never anchors the remaining weight.
  - The exposure clock of a spool runs from `last_dried_at`, else `opened_at` (Spoolman extras);
    the class limit (days) and the storage RH (location humidity sensor, read from Home Assistant)
    give its state: fresh → ok → due → wet. Never opened and never dried → `unknown` (sealed).

INVARIANTS:
  - Spoolman first, DB second on finish: if a Spoolman write fails nothing is recorded and the
    finish can simply be retried (the writes are idempotent).
  - `due`/`wet` are sticky: the periodic check never lowers them to ok/fresh — only a finished
    drying session does (or a human editing `dry_state` in Spoolman).
  - `drying.due` is emitted when a spool ENTERS due or wet (the stored Spoolman state is the
    dedupe key), so a spool that stays due does not re-alert every check.
  - `drying.finished` is emitted with trigger "timer" once when target_h elapses on an open
    session, and with trigger "finish" when the session is finished.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from printops import events, ledger, locations
from printops.db import transaction, utc_now
from printops.drying_policy import DryingPolicy, Recipe
from printops.homeassistant import HaError, HaStates, Reading
from printops.spoolman import Spoolman, SpoolmanError, decode_extra, encode_extra

DRYER_KINDS = ("dryer", "dry_box")
SOURCES = ("cli", "api", "ha", "voice", "station", "bot")
STATES = ("fresh", "ok", "due", "wet")
ALERT_STATES = ("due", "wet")
# Recipe temperatures further apart than this from the session temperature earn a warning.
TEMP_TOLERANCE_C = 5


class DryingError(ValueError):
    """The request cannot be honoured as given (bad dryer, unknown spool, missing temperature)."""


class SessionNotFound(LookupError):
    pass


class SessionConflict(RuntimeError):
    """A spool is already in an open session, or the session is already finished."""


# -- time helpers ---------------------------------------------------------------------------
def parse_ts(raw: str) -> datetime.datetime:
    """ISO-8601 → aware UTC datetime; naive values (Spoolman UI) are taken as UTC."""
    value = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def format_ts(value: datetime.datetime) -> str:
    """Same text form as db.utc_now(): sortable, millisecond precision, `Z` suffix."""
    value = value.astimezone(datetime.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return now or parse_ts(utc_now())


def decoded_extra(obj: Mapping[str, Any]) -> dict[str, Any]:
    """A Spoolman object's `extra` with values JSON-decoded (an undecodable value is dropped)."""
    out: dict[str, Any] = {}
    for key, raw in (obj.get("extra") or {}).items():
        try:
            out[key] = decode_extra(raw)
        except ValueError:  # noqa: PERF203 — a hand-edited bad value drops just that key
            continue
    return out


def _filament(spool: Mapping[str, Any]) -> dict[str, Any]:
    fil = dict(spool.get("filament") or {})
    fil["extra"] = decoded_extra(fil)
    return fil


# -- sessions -------------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionSpool:
    spool_id: int
    weight_before: float | None
    weight_after: float | None


@dataclass(frozen=True)
class Session:
    id: int
    dryer_location_id: int
    dryer: str
    started_at: str
    ended_at: str | None
    temp_c: float
    target_h: float
    rh_before: float | None
    rh_after: float | None
    source: str
    note: str | None
    actor: str
    finished_by: str | None
    timer_event_id: int | None
    spools: tuple[SessionSpool, ...]

    @property
    def ends_at(self) -> str:
        return format_ts(
            parse_ts(self.started_at) + datetime.timedelta(hours=self.target_h)
        )

    def to_dict(self) -> dict[str, Any]:
        body = asdict(self)
        body["spools"] = [asdict(s) for s in self.spools]
        body["open"] = self.ended_at is None
        body["ends_at"] = self.ends_at
        return body


_SESSION_SQL = "SELECT s.*, l.path AS dryer FROM drying_session s JOIN location l ON l.id = s.dryer_location_id"


def _session_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Session:
    spools = tuple(
        SessionSpool(int(r["spool_id"]), r["weight_before"], r["weight_after"])
        for r in conn.execute(
            "SELECT spool_id, weight_before, weight_after FROM drying_spool WHERE session_id = ? ORDER BY spool_id",
            (row["id"],),
        )
    )
    return Session(
        id=int(row["id"]),
        dryer_location_id=int(row["dryer_location_id"]),
        dryer=str(row["dryer"]),
        started_at=str(row["started_at"]),
        ended_at=row["ended_at"],
        temp_c=float(row["temp_c"]),
        target_h=float(row["target_h"]),
        rh_before=row["rh_before"],
        rh_after=row["rh_after"],
        source=str(row["source"]),
        note=row["note"],
        actor=str(row["actor"]),
        finished_by=row["finished_by"],
        timer_event_id=row["timer_event_id"],
        spools=spools,
    )


def get_session(conn: sqlite3.Connection, session_id: int) -> Session:
    row = conn.execute(f"{_SESSION_SQL} WHERE s.id = ?", (session_id,)).fetchone()
    if row is None:
        raise SessionNotFound(f"no drying session #{session_id}")
    return _session_from_row(conn, row)


def list_sessions(
    conn: sqlite3.Connection,
    *,
    open_only: bool = False,
    spool_id: int | None = None,
    limit: int = 50,
) -> list[Session]:
    """Newest first."""
    where, args = [], []
    if open_only:
        where.append("s.ended_at IS NULL")
    if spool_id is not None:
        where.append("s.id IN (SELECT session_id FROM drying_spool WHERE spool_id = ?)")
        args.append(spool_id)
    sql = (
        _SESSION_SQL
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.id DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*args, max(1, min(limit, 1000)))).fetchall()
    return [_session_from_row(conn, r) for r in rows]


def spools_drying(conn: sqlite3.Connection) -> set[int]:
    """Spool ids in an open session."""
    return {
        int(r["spool_id"])
        for r in conn.execute(
            "SELECT d.spool_id FROM drying_spool d JOIN drying_session s ON s.id = d.session_id WHERE s.ended_at IS NULL"
        )
    }


def _check_weights(
    name: str, weights: Mapping[int, float] | None, spool_ids: Sequence[int]
) -> dict[int, float]:
    out: dict[int, float] = {}
    for sid, grams in (weights or {}).items():
        if int(sid) not in spool_ids:
            raise DryingError(
                f"{name}: spool {sid} is not in this session ({', '.join(map(str, spool_ids))})"
            )
        if (
            not isinstance(grams, (int, float))
            or isinstance(grams, bool)
            or not 0 < grams < 20000
        ):
            raise DryingError(
                f"{name}: spool {sid} weight must be grams in (0, 20000), got {grams!r}"
            )
        out[int(sid)] = float(grams)
    return out


def _dryer_rh(
    conn: sqlite3.Connection,
    loc: locations.Location,
    humidity: HaStates | None,
    warnings: list[str],
) -> float | None:
    entity = locations.humidity_sensor(conn, loc)
    if entity is None or humidity is None:
        return None
    try:
        return humidity.humidity(entity).value
    except HaError as e:
        warnings.append(f"RH not recorded: {e}")
        return None


def _refuse_busy(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    busy = sorted(spools_drying(conn) & set(ids))
    if busy:
        raise SessionConflict(
            f"spool(s) {', '.join(map(str, busy))} already in an open drying session; finish it first"
        )


def _session_recipes(
    policy: DryingPolicy, spools: Mapping[int, Mapping[str, Any]]
) -> dict[int, Recipe]:
    return {sid: policy.recipe(_filament(s)) for sid, s in spools.items()}


def _label(spool: Mapping[str, Any]) -> str:
    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    bits = [b for b in (vendor, fil.get("material"), fil.get("name")) if b]
    return (
        f"spool {spool['id']} ({' '.join(str(b) for b in bits)})"
        if bits
        else f"spool {spool['id']}"
    )


def start(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    policy: DryingPolicy,
    *,
    dryer: str | int,
    spool_ids: Sequence[int],
    actor: str,
    source: str = "api",
    temp_c: float | None = None,
    hours: float | None = None,
    rh_before: float | None = None,
    weights_before: Mapping[int, float] | None = None,
    note: str | None = None,
    humidity: HaStates | None = None,
) -> tuple[Session, list[str]]:
    """Open a drying session; returns it and human-readable warnings."""
    ids = [int(s) for s in spool_ids]
    if not ids:
        raise DryingError("a drying session needs at least one spool")
    if len(set(ids)) != len(ids):
        raise DryingError(f"spool listed twice: {ids}")
    if source not in SOURCES:
        raise DryingError(f"source must be one of {', '.join(SOURCES)}, got {source!r}")
    loc = locations.get(conn, dryer)
    if loc.archived:
        raise DryingError(f"location {loc.path} is archived")
    if loc.kind not in DRYER_KINDS:
        raise DryingError(
            f"location {loc.path} is a {loc.kind}, not a dryer ({' or '.join(DRYER_KINDS)})"
        )
    _refuse_busy(conn, ids)
    spools: dict[int, dict[str, Any]] = {}
    for sid in ids:
        try:
            spool = spoolman.get("spool", sid)
        except SpoolmanError as e:
            if e.status == 404:
                raise DryingError(f"spool {sid} does not exist in Spoolman") from e
            raise
        if spool.get("archived"):
            raise DryingError(f"spool {sid} is archived in Spoolman")
        spools[sid] = spool
    weights = _check_weights("weights_before", weights_before, ids)
    recipes = _session_recipes(policy, spools)
    warnings: list[str] = []

    known_temps = {sid: r.temp_c for sid, r in recipes.items() if r.temp_c is not None}
    if temp_c is None:
        if len(known_temps) < len(ids):
            unknown = [_label(spools[s]) for s in ids if s not in known_temps]
            raise DryingError(
                f"no drying temperature known for {', '.join(unknown)}: pass temp_c "
                "(or set filament extra drying_temp_c / add the material to fleet/policy.yaml)"
            )
        temp_c = float(min(known_temps.values()))
    if not 20 <= temp_c <= 150:
        raise DryingError(f"temp_c must be 20..150 °C, got {temp_c}")
    if hours is None:
        known_hours = [r.hours for r in recipes.values() if r.hours is not None]
        if len(known_hours) < len(ids):
            raise DryingError("no drying time known for every spool: pass hours")
        hours = max(known_hours)
    if not 0 < hours <= 96:
        raise DryingError(f"hours must be in (0, 96], got {hours}")
    for sid, want in known_temps.items():
        if want > temp_c + TEMP_TOLERANCE_C:
            warnings.append(
                f"{_label(spools[sid])} is rated {want} °C: at {temp_c:g} °C it may not dry fully"
            )
        elif want < temp_c - TEMP_TOLERANCE_C:
            warnings.append(
                f"{_label(spools[sid])} is rated {want} °C: {temp_c:g} °C may soften or deform it"
            )
    if rh_before is not None and not 0 <= rh_before <= 100:
        raise DryingError(f"rh_before must be 0..100 %, got {rh_before}")
    if rh_before is None:
        rh_before = _dryer_rh(conn, loc, humidity, warnings)

    with transaction(conn):
        _refuse_busy(
            conn, ids
        )  # again under the write lock: a concurrent start may have won
        cur = conn.execute(
            "INSERT INTO drying_session (dryer_location_id, started_at, temp_c, target_h, rh_before, source, note, actor)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (loc.id, utc_now(), temp_c, hours, rh_before, source, note, actor),
        )
        session_id = int(cur.lastrowid or 0)
        conn.executemany(
            "INSERT INTO drying_spool (session_id, spool_id, weight_before) VALUES (?, ?, ?)",
            [(session_id, sid, weights.get(sid)) for sid in ids],
        )
        session = get_session(conn, session_id)
        events.emit(
            conn,
            "drying.started",
            f"drying:{session_id}",
            {
                "session_id": session_id,
                "dryer": loc.path,
                "spools": ids,
                "temp_c": temp_c,
                "target_h": hours,
                "ends_at": session.ends_at,
                "rh_before": rh_before,
                "recipes": {str(sid): r.origin for sid, r in recipes.items()},
            },
            actor,
        )
    return session, warnings


def finish(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    *,
    session_id: int,
    actor: str,
    rh_after: float | None = None,
    weights_after: Mapping[int, float] | None = None,
    humidity: HaStates | None = None,
) -> tuple[Session, list[str]]:
    """Close a session: Spoolman `last_dried_at`/`dry_state=fresh`, weights as ledger notes."""
    session = get_session(conn, session_id)
    if session.ended_at is not None:
        raise SessionConflict(
            f"drying session #{session_id} already finished at {session.ended_at}"
        )
    ids = [s.spool_id for s in session.spools]
    weights = _check_weights("weights_after", weights_after, ids)
    if rh_after is not None and not 0 <= rh_after <= 100:
        raise DryingError(f"rh_after must be 0..100 %, got {rh_after}")
    warnings: list[str] = []
    if rh_after is None:
        rh_after = _dryer_rh(
            conn, locations.get(conn, session.dryer_location_id), humidity, warnings
        )
    ended_at = utc_now()
    for sid in ids:
        spoolman.update(
            "spool",
            sid,
            {
                "extra": {
                    "last_dried_at": encode_extra(ended_at),
                    "dry_state": encode_extra("fresh"),
                }
            },
        )

    with transaction(conn):
        changed = conn.execute(
            "UPDATE drying_session SET ended_at = ?, rh_after = ?, finished_by = ? WHERE id = ? AND ended_at IS NULL",
            (ended_at, rh_after, actor, session_id),
        ).rowcount
        if changed != 1:
            raise SessionConflict(
                f"drying session #{session_id} was finished concurrently"
            )
        for sid, grams in weights.items():
            conn.execute(
                "UPDATE drying_spool SET weight_after = ? WHERE session_id = ? AND spool_id = ?",
                (grams, session_id, sid),
            )
        for s in session.spools:
            before, after = s.weight_before, weights.get(s.spool_id)
            if before is None and after is None:
                continue
            ledger.note(
                conn, s.spool_id, _weight_note(session, before, after), actor=actor
            )
        done = get_session(conn, session_id)
        events.emit(
            conn,
            "drying.finished",
            f"drying:{session_id}",
            {
                "session_id": session_id,
                "trigger": "finish",
                "dryer": done.dryer,
                "spools": ids,
                "started_at": done.started_at,
                "ended_at": ended_at,
                "rh_before": done.rh_before,
                "rh_after": rh_after,
                "water_g": {
                    str(s.spool_id): _loss(s)
                    for s in done.spools
                    if _loss(s) is not None
                },
            },
            actor,
        )
    return done, warnings


def _loss(s: SessionSpool) -> float | None:
    if s.weight_before is None or s.weight_after is None:
        return None
    return round(s.weight_before - s.weight_after, 1)


def _weight_note(session: Session, before: float | None, after: float | None) -> str:
    head = f"drying #{session.id} in {session.dryer} ({session.temp_c:g} °C, {session.target_h:g} h): gross "
    if before is not None and after is not None:
        return (
            head
            + f"{before:g} → {after:g} g ({before - after:+.1f} g water lost, not usage)"
        )
    if before is not None:
        return head + f"{before:g} g before (not weighed after)"
    return head + f"{after:g} g after (not weighed before)"


def fire_timers(
    conn: sqlite3.Connection,
    *,
    now: datetime.datetime | None = None,
    actor: str = "drying-timer",
) -> list[events.Event]:
    """`drying.finished` {trigger: timer} for open sessions whose target_h elapsed (once each)."""
    moment = _now(now)
    fired: list[events.Event] = []
    for session in list_sessions(conn, open_only=True, limit=1000):
        if session.timer_event_id is not None or parse_ts(session.ends_at) > moment:
            continue
        with transaction(conn):
            row = conn.execute(
                "SELECT timer_event_id, ended_at FROM drying_session WHERE id = ?",
                (session.id,),
            ).fetchone()
            if row["timer_event_id"] is not None or row["ended_at"] is not None:
                continue
            event = events.emit(
                conn,
                "drying.finished",
                f"drying:{session.id}",
                {
                    "session_id": session.id,
                    "trigger": "timer",
                    "dryer": session.dryer,
                    "spools": [s.spool_id for s in session.spools],
                    "started_at": session.started_at,
                    "ends_at": session.ends_at,
                    "temp_c": session.temp_c,
                    "target_h": session.target_h,
                },
                actor,
            )
            conn.execute(
                "UPDATE drying_session SET timer_event_id = ? WHERE id = ?",
                (event.id, session.id),
            )
        fired.append(event)
    return fired


# -- due assessment -------------------------------------------------------------------------
@dataclass(frozen=True)
class Humidity:
    entity: str
    rh: float | None
    error: str | None = None


@dataclass
class Assessment:
    spool_id: int
    label: str
    state: str  # fresh | ok | due | wet | unknown | drying
    stored_state: str | None  # Spoolman dry_state before this assessment
    hygroscopic_class: str
    recipe: str
    clock: str | None  # "dried" | "opened" | None
    since: str | None
    age_days: float | None
    limit_days: float | None
    location: str | None
    rh: float | None
    rh_entity: str | None
    max_rh: float
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def alert(self) -> bool:
        return self.state in ALERT_STATES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess(
    policy: DryingPolicy,
    spool: Mapping[str, Any],
    *,
    now: datetime.datetime,
    humidity: Humidity | None = None,
    location: str | None = None,
    drying: bool = False,
) -> Assessment:
    """Pure: the drying state of one Spoolman spool (as returned by GET /spool/{id})."""
    extra = decoded_extra(spool)
    recipe = policy.recipe(_filament(spool))
    limits = policy.classes[recipe.hygroscopic_class]
    stored = extra.get("dry_state") if extra.get("dry_state") in STATES else None
    a = Assessment(
        spool_id=int(spool["id"]),
        label=_label(spool),
        state="unknown",
        stored_state=stored,
        hygroscopic_class=recipe.hygroscopic_class,
        recipe=recipe.origin,
        clock=None,
        since=None,
        age_days=None,
        limit_days=None,
        location=location,
        rh=humidity.rh if humidity else None,
        rh_entity=humidity.entity if humidity else None,
        max_rh=limits.max_rh,
    )
    if humidity is not None and humidity.error:
        a.notes.append(f"RH unknown: {humidity.error}")
    if drying:
        a.state = "drying"
        a.reasons.append("in an open drying session")
        return a
    for key, clock, limit in (
        ("last_dried_at", "dried", limits.max_days_since_dried),
        ("opened_at", "opened", limits.max_days_opened),
    ):
        raw = extra.get(key)
        if not raw:
            continue
        try:
            since = parse_ts(str(raw))
        except ValueError:
            a.notes.append(f"{key} {raw!r} is not a date; ignored")
            continue
        a.clock, a.since, a.limit_days = clock, format_ts(since), limit
        a.age_days = round(max((now - since).total_seconds(), 0) / 86400, 2)
        break

    rh_high = a.rh is not None and a.rh > limits.max_rh
    if rh_high:
        a.reasons.append(f"storage RH {a.rh:g}% > {limits.max_rh:g}% ({a.rh_entity})")
    if a.age_days is None or a.limit_days is None:
        computed = "due" if rh_high else "unknown"
        if not rh_high:
            a.reasons.append(
                "never opened or dried as far as Spoolman knows (no opened_at / last_dried_at)"
            )
    elif a.age_days >= a.limit_days * policy.wet_factor:
        computed = "wet"
        a.reasons.append(
            f"{a.clock} {a.age_days:g} d ago ≥ {policy.wet_factor:g} × {a.limit_days:g} d "
            f"({recipe.hygroscopic_class})"
        )
    elif a.age_days >= a.limit_days or rh_high:
        computed = "due"
        if a.age_days >= a.limit_days:
            a.reasons.append(
                f"{a.clock} {a.age_days:g} d ago ≥ {a.limit_days:g} d ({recipe.hygroscopic_class})"
            )
    elif a.clock == "dried" and a.age_days < policy.fresh_days:
        computed = "fresh"
    else:
        computed = "ok"
    if stored in ALERT_STATES and computed in ("fresh", "ok", "unknown"):
        a.notes.append(
            f"Spoolman says {stored}; only a finished drying session clears it"
        )
        computed = stored
    a.state = computed
    return a


def _spool_location(
    conn: sqlite3.Connection, spool: Mapping[str, Any], notes: list[str]
) -> locations.Location | None:
    ref: str | int | None = decoded_extra(spool).get("printops_location_id")
    if not isinstance(ref, int) or isinstance(ref, bool):
        ref = spool.get("location") or None
    if ref is None:
        return None
    try:
        return locations.get(conn, ref)
    except locations.LocationNotFound:
        notes.append(f"location {ref!r} is not a PrintOps location")
        return None


def assess_all(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    policy: DryingPolicy,
    *,
    humidity: HaStates | None = None,
    now: datetime.datetime | None = None,
    spool_ids: Sequence[int] | None = None,
) -> list[Assessment]:
    """Assess every active (non-archived) spool, or just `spool_ids`. Reads HA once per sensor."""
    moment = _now(now)
    spools = (
        [spoolman.get("spool", sid) for sid in spool_ids]
        if spool_ids is not None
        else spoolman.find("spool")
    )
    drying = spools_drying(conn)
    readings: dict[str, Humidity] = {}
    out: list[Assessment] = []
    for spool in spools:
        if spool.get("archived"):
            continue
        notes: list[str] = []
        loc = _spool_location(conn, spool, notes)
        hum: Humidity | None = None
        entity = locations.humidity_sensor(conn, loc) if loc is not None else None
        if entity is not None:
            if entity not in readings:
                readings[entity] = _read(humidity, entity)
            hum = readings[entity]
        a = assess(
            policy,
            spool,
            now=moment,
            humidity=hum,
            location=loc.path if loc else spool.get("location"),
            drying=int(spool["id"]) in drying,
        )
        a.notes[:0] = notes
        out.append(a)
    return out


def _read(humidity: HaStates | None, entity: str) -> Humidity:
    if humidity is None:
        return Humidity(
            entity, None, "Home Assistant is not configured (PRINTOPS_HA_URL)"
        )
    try:
        reading: Reading = humidity.humidity(entity)
    except HaError as e:
        return Humidity(entity, None, str(e))
    return Humidity(entity, reading.value)


@dataclass
class CheckReport:
    checked: int
    changed: list[dict[str, Any]]
    alerts: list[int]  # event ids of drying.due emitted by this check
    errors: list[str]
    assessments: list[Assessment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "changed": self.changed,
            "alerts": self.alerts,
            "errors": self.errors,
            "due": [a.to_dict() for a in self.assessments if a.alert],
        }


def check(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    policy: DryingPolicy,
    *,
    humidity: HaStates | None = None,
    now: datetime.datetime | None = None,
    actor: str = "drying-check",
) -> CheckReport:
    """Assess all spools, write changed `dry_state` to Spoolman, emit `drying.due` on entering due/wet."""
    assessments = assess_all(conn, spoolman, policy, humidity=humidity, now=now)
    report = CheckReport(
        checked=len(assessments),
        changed=[],
        alerts=[],
        errors=[],
        assessments=assessments,
    )
    for a in assessments:
        if a.state not in STATES or a.state == a.stored_state:
            continue
        try:
            spoolman.update(
                "spool", a.spool_id, {"extra": {"dry_state": encode_extra(a.state)}}
            )
        except SpoolmanError as e:
            report.errors.append(f"spool {a.spool_id}: {e.message}")
            continue
        report.changed.append(
            {"spool_id": a.spool_id, "from": a.stored_state, "to": a.state}
        )
        entered = a.stored_state not in ALERT_STATES or (
            a.stored_state == "due" and a.state == "wet"
        )
        if a.alert and entered:
            event = events.emit(
                conn,
                "drying.due",
                f"spool:{a.spool_id}",
                {
                    "spool_id": a.spool_id,
                    "spool": a.label,
                    "state": a.state,
                    "previous": a.stored_state,
                    "class": a.hygroscopic_class,
                    "reasons": a.reasons,
                    "age_days": a.age_days,
                    "limit_days": a.limit_days,
                    "rh": a.rh,
                    "location": a.location,
                },
                actor,
            )
            report.alerts.append(event.id)
    return report
