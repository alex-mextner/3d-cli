"""spool_mirror.py — the bookmark of what PrintOps last processed of a spool, and the ONE path
through which 3d-api writes to Spoolman spools.

ACCESSED VIA: `ApiState.synced(conn)` / `printops.api.deps.get_spoolman` (a `SyncedSpoolman` per
request or pass), and `printops.spool_sync` (the engine that reads the bookmark). Domain code
(ledger, spools, locations, drying) keeps calling the plain `Spoolman` Protocol; what makes it
safe is WHICH object 3d-api hands it.

WHY (the invariant, docs/printops/spool-sync.md): two independent writers change a spool in
Spoolman besides PrintOps — the Spoolman web UI and Moonraker's `[spoolman]` — so a PrintOps copy of
a Spoolman value can never be trusted to stay equal. PrintOps therefore keeps NO copy of a Spoolman
value; it keeps a bookmark (`spool_sync`, migration 0105): the value it last processed. A spool
whose Spoolman state differs from its bookmark was changed by somebody else and has work waiting
(a ledger row, an event); `printops.spool_sync` does it exactly once.

INVARIANTS:
  - The bookmark moves in only two ways: the sync engine processed a change, or PrintOps itself
    wrote that field through `SyncedSpoolman` (so its own write is never mistaken for somebody
    else's, and nothing is "echoed" back). A write moves ONLY the fields it set: a concurrent UI edit
    of another field stays visible to the next pass.
  - A usage PrintOps books moves the bookmark by the grams it booked, not to the value Spoolman
    answered with: a Moonraker `/use` that landed just before ours stays a pending difference
    instead of being swallowed.
  - The bookmark row is written inside the caller's transaction when there is one, so a rolled-back
    PrintOps change rolls its bookmark back too and the next pass sees the Spoolman write that
    survived as the change it is.
  - A spool without a bookmark row is unknown to PrintOps (created in the Spoolman UI, or by an
    older 3d-api): a write to it records nothing; the engine adopts it on its next pass.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from printops import db
from printops.spoolman import JSON, Entity, Spoolman, decode_extra

PRESENT = "present"
DELETED = "deleted"
SLOT_PREFIX = "slot:"  # a spool whose `location` is `slot:<printer>/<head>` is loaded in that slot


@dataclass(frozen=True)
class Seen:
    """The bookmark of one spool: the Spoolman state PrintOps last processed."""

    spool_id: int
    state: str  # PRESENT | DELETED
    remaining_g: float | None
    last_used: str | None
    archived: bool
    location: str


def extra_of(item: Mapping[str, Any], key: str) -> Any:
    """A Spoolman extra field, decoded; None when unset or not valid JSON (typed by hand)."""
    try:
        return decode_extra((item.get("extra") or {}).get(key))
    except ValueError:
        return None


def slot_of_location(text: str) -> str:
    """`slot:u1/T3` → `u1/T3`; '' for any other location."""
    text = text.strip()
    return text[len(SLOT_PREFIX) :] if text.casefold().startswith(SLOT_PREFIX) else ""


def seen_of(spool: Mapping[str, Any]) -> Seen:
    """Normalise a Spoolman spool JSON into its bookmark form."""
    remaining = spool.get("remaining_weight")
    last_used = spool.get("last_used")
    return Seen(
        spool_id=int(spool["id"]),
        state=PRESENT,
        remaining_g=float(remaining) if isinstance(remaining, (int, float)) else None,
        last_used=str(last_used) if last_used else None,
        archived=bool(spool.get("archived")),
        location=str(spool.get("location") or "").strip(),
    )


def _row(r: sqlite3.Row) -> Seen:
    return Seen(
        spool_id=int(r["spool_id"]),
        state=str(r["state"]),
        remaining_g=None if r["remaining_g"] is None else float(r["remaining_g"]),
        last_used=r["last_used"],
        archived=bool(r["archived"]),
        location=str(r["location"]),
    )


def load(conn: sqlite3.Connection, spool_id: int) -> Seen | None:
    row = conn.execute(
        "SELECT * FROM spool_sync WHERE spool_id = ?", (spool_id,)
    ).fetchone()
    return None if row is None else _row(row)


def load_all(conn: sqlite3.Connection) -> dict[int, Seen]:
    return {
        int(r["spool_id"]): _row(r) for r in conn.execute("SELECT * FROM spool_sync")
    }


def store(conn: sqlite3.Connection, seen: Seen) -> None:
    conn.execute(
        "INSERT INTO spool_sync (spool_id, state, remaining_g, last_used, archived, location, seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (spool_id) DO UPDATE SET state = excluded.state,"
        " remaining_g = excluded.remaining_g, last_used = excluded.last_used, archived = excluded.archived,"
        " location = excluded.location, seen_at = excluded.seen_at",
        (
            seen.spool_id,
            seen.state,
            seen.remaining_g,
            seen.last_used,
            int(seen.archived),
            seen.location,
            db.utc_now(),
        ),
    )


class SyncedSpoolman:
    """A `Spoolman` that keeps the bookmark in step with every write PrintOps makes to a spool.

    Reads, extra-field registration and vendor/filament writes pass straight through. One
    instance per DB connection (the bookmark rows go through `conn`, in its open transaction)."""

    def __init__(self, raw: Spoolman, conn: sqlite3.Connection) -> None:
        self.raw = raw
        self.conn = conn

    # -- reads and registry: untouched ---------------------------------------------------------
    def info(self) -> JSON:
        return self.raw.info()

    def list_fields(self, entity: Entity) -> list[JSON]:
        return self.raw.list_fields(entity)

    def set_field(
        self, entity: Entity, key: str, params: Mapping[str, Any]
    ) -> list[JSON]:
        return self.raw.set_field(entity, key, params)

    def delete_field(self, entity: Entity, key: str) -> list[JSON]:
        return self.raw.delete_field(entity, key)

    def link_tag(self, spool_id: int, uid: str) -> JSON:
        return self.raw.link_tag(spool_id, uid)

    def find(
        self, entity: Entity, params: Mapping[str, str] | None = None
    ) -> list[JSON]:
        return self.raw.find(entity, params)

    def get(self, entity: Entity, item_id: int) -> JSON:
        return self.raw.get(entity, item_id)

    # -- spool writes: the bookmark follows -----------------------------------------------------
    def create(self, entity: Entity, data: Mapping[str, Any]) -> JSON:
        out = self.raw.create(entity, data)
        if entity == "spool":
            store(self.conn, seen_of(out))
        return out

    def update(self, entity: Entity, item_id: int, data: Mapping[str, Any]) -> JSON:
        out = self.raw.update(entity, item_id, data)
        if entity == "spool":
            self._after_patch(item_id, data, out)
        return out

    def delete(self, entity: Entity, item_id: int) -> None:
        self.raw.delete(entity, item_id)
        if entity == "spool":
            cur = load(self.conn, item_id)
            if cur is not None:
                store(self.conn, replace(cur, state=DELETED))

    def use(
        self,
        spool_id: int,
        *,
        use_weight: float | None = None,
        use_length: float | None = None,
    ) -> JSON:
        out = self.raw.use(spool_id, use_weight=use_weight, use_length=use_length)
        cur = load(self.conn, spool_id)
        if cur is not None:
            after = seen_of(out)
            expected = _expected_after_use(
                cur.remaining_g, use_weight, after.remaining_g
            )
            store(
                self.conn, replace(cur, remaining_g=expected, last_used=after.last_used)
            )
        return out

    def measure(self, spool_id: int, weight: float) -> JSON:
        out = self.raw.measure(spool_id, weight)
        cur = load(self.conn, spool_id)
        if cur is not None:
            after = seen_of(out)
            store(
                self.conn,
                replace(cur, remaining_g=after.remaining_g, last_used=after.last_used),
            )
        return out

    def _after_patch(
        self, spool_id: int, body: Mapping[str, Any], reply: Mapping[str, Any]
    ) -> None:
        cur = load(self.conn, spool_id)
        if cur is None:
            return
        after = seen_of(reply)
        new = cur
        if any(
            k in body for k in ("remaining_weight", "used_weight", "initial_weight")
        ):
            new = replace(new, remaining_g=after.remaining_g)
        if "location" in body:
            new = replace(new, location=after.location)
        if "archived" in body:
            new = replace(new, archived=after.archived)
        if new != cur:
            store(self.conn, new)


def _expected_after_use(
    before: float | None, use_weight: float | None, answered: float | None
) -> float | None:
    """The remaining the bookmark should hold after PrintOps booked `use_weight` g: the old one
    minus OUR grams (Spoolman floors at 0 and cannot hand back more than was used), so a usage some
    other writer landed meanwhile stays a pending difference. A length (grams unknown) takes the answer."""
    if use_weight is None or before is None:
        return answered
    expected = max(before - use_weight, 0.0)
    if use_weight < 0 and answered is not None:
        expected = min(expected, answered)
    return expected
