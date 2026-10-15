"""events.py — the PrintOps append-only event log and its in-process fan-out.

ACCESSED VIA: every slice (`emit(conn, "spool.measured", "spool:12", {...}, actor)`), the
3d-api `GET /v1/events` (SSE/JSON) and `WS /v1/ws` endpoints, and `3d printops events`.

INVARIANTS:
  - Rows live in the `events` table (migration 0001); triggers make it append-only.
  - The table is the source of truth; `EventBus` only WAKES listeners, who then read
    `id > cursor` from the DB. A wake-up for a rolled-back write finds nothing, and a missed
    wake-up is caught by the listener's periodic re-read — so the bus can never deliver an
    event that is not durably stored, nor lose one that is.
  - `emit` outside a transaction publishes at once; inside `printops.db.transaction()` the
    publish happens at COMMIT (via `db.on_commit`).
  - Kinds are dotted lower-case names (plan §11.2: `slot.mismatch`, `spool.measured`,
    `drying.due`, …). The format is validated; the set is open so slices add kinds freely.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from printops import db

KIND_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
MAX_PAGE = 1000
MAX_CURSOR = 2**63 - 1  # SQLite INTEGER range: a larger `since` would overflow the query


@dataclass(frozen=True)
class Event:
    id: int
    at: str
    kind: str
    subject: str
    payload: dict[str, Any]
    actor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "kind": self.kind,
            "subject": self.subject,
            "payload": self.payload,
            "actor": self.actor,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(
            id=int(row["id"]),
            at=str(row["at"]),
            kind=str(row["kind"]),
            subject=str(row["subject"]),
            payload=json.loads(row["payload"]),
            actor=str(row["actor"]),
        )


class EventBus:
    """Wake-up fan-out from any thread to asyncio listeners (SSE/WS handlers)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def publish(self) -> None:
        """Wake every listener; safe to call from worker threads."""
        with self._lock:
            waiters = list(self._waiters)
        for loop, flag in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(flag.set)

    def listener(self) -> Listener:
        """A listener bound to the running event loop; close() it when done."""
        flag = asyncio.Event()
        entry = (asyncio.get_running_loop(), flag)
        with self._lock:
            self._waiters.add(entry)
        return Listener(self, entry)

    def _remove(self, entry: tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
        with self._lock:
            self._waiters.discard(entry)

    def listener_count(self) -> int:
        with self._lock:
            return len(self._waiters)


class Listener:
    def __init__(self, bus: EventBus, entry: tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
        self._bus = bus
        self._entry = entry

    async def wait(self, timeout: float) -> bool:
        """Wait for a publish (True) or the timeout (False); re-arms itself."""
        flag = self._entry[1]
        try:
            await asyncio.wait_for(flag.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        flag.clear()
        return True

    def close(self) -> None:
        self._bus._remove(self._entry)


# One process, one bus: every app/connection in this process shares it (a spurious wake-up
# only costs a cheap `id > cursor` query).
BUS = EventBus()
db.on_commit(BUS.publish)


def emit(
    conn: sqlite3.Connection,
    kind: str,
    subject: str,
    payload: Mapping[str, Any] | None = None,
    actor: str = "system",
) -> Event:
    """Append one event and wake listeners (now, or at COMMIT if inside a transaction)."""
    if not KIND_RE.fullmatch(kind):  # fullmatch: `$` would accept a trailing newline (breaks SSE framing)
        raise ValueError(f"event kind {kind!r} must be dotted lower-case, e.g. 'spool.measured'")
    if not subject:
        raise ValueError("event subject must not be empty")
    if not actor:
        raise ValueError("event actor must not be empty")
    body = dict(payload or {})
    at = db.utc_now()
    cur = conn.execute(
        "INSERT INTO events (at, kind, subject, payload, actor) VALUES (?, ?, ?, ?, ?)",
        (at, kind, subject, json.dumps(body, sort_keys=True, separators=(",", ":")), actor),
    )
    event = Event(id=int(cur.lastrowid or 0), at=at, kind=kind, subject=subject, payload=body, actor=actor)
    if not conn.in_transaction:
        BUS.publish()
    return event


def since(conn: sqlite3.Connection, cursor: int = 0, limit: int = 100) -> list[Event]:
    """Events with id > cursor, oldest first, at most `limit` (capped at MAX_PAGE)."""
    limit = max(1, min(limit, MAX_PAGE))
    rows = conn.execute(
        "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (cursor, limit)
    ).fetchall()
    return [Event.from_row(r) for r in rows]


def last(conn: sqlite3.Connection, count: int = 20) -> list[Event]:
    """The most recent `count` events, oldest first."""
    count = max(1, min(count, MAX_PAGE))
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (count,)).fetchall()
    return [Event.from_row(r) for r in reversed(rows)]


def latest_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])
