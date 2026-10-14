"""ledger.py — the spool ledger: every gram a spool gains or loses, with its uncertainty.

ACCESSED VIA: printops.spools (receive / use / measure / empty, i.e. 3d-api routes_spools), other
slices through the Python API below (drying: `note`; job history and reconcile: `book`,
`remaining`, `history`), and `3d lint --printops` (lint_spools).

MODEL (plan §5.3, migration 0020): remaining = the newest anchor (a weighing, a sealed spool
received with a known net, `empty`) + Σ deltas booked after it. Each row carries confidence_g
(± grams, σ); the half-width of the interval is the root-sum-square of the anchor's σ and every
later delta's σ, so a weighing resets the accumulated error. A spool with no anchor yet has an
unknown remaining: the interval is [0, Spoolman's remaining] and it is flagged `anchored=False`.

INVARIANTS:
  - The ledger is append-only (DB triggers); a mistake is fixed by booking a correction.
  - `book` writes the ledger row and Spoolman `/use` together: the row is inserted inside a
    `BEGIN IMMEDIATE` transaction and committed only after Spoolman accepted the usage, so a
    Spoolman failure leaves no row behind. Spoolman's `remaining_confidence_g` extra field is
    refreshed afterwards (best effort: it is a display cache of `remaining()`).
  - `delta_g` is the change of remaining: usage of 12 g is delta_g = -12.
  - `note` rows never touch Spoolman, never anchor, never widen σ.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from printops import db
from printops import events as ev
from printops.spoolman import JSON, Spoolman, SpoolmanError, encode_extra

Kind = Literal["delta", "anchor", "note"]

# Sources a usage (delta) row may come from, with the default ± when the caller gives none:
# (fraction of |grams|, floor in grams). Assumptions, to be tuned by reconcile's per-source
# drift statistics (plan §5.3 item 5): a slicer estimate misses purges and retries, a live
# Moonraker counter is close, voice is a guess.
DELTA_SOURCES: dict[str, tuple[float, float]] = {
    "gcode_estimate": (0.10, 1.0),
    "moonraker_live": (0.03, 1.0),
    "printer_reported": (0.05, 1.0),
    "voice": (0.20, 5.0),
    "manual": (0.10, 2.0),
    "purge": (0.30, 2.0),
}
ANCHOR_SOURCES = ("weigh", "receive", "manual")
SOURCES = (*DELTA_SOURCES, "weigh", "receive", "note")

# A sealed spool's real net vs the label: vendors fill within a few percent [assumption].
RECEIVE_SIGMA_FRACTION = 0.02
RECEIVE_SIGMA_MIN_G = 10.0


class LedgerError(ValueError):
    """Bad booking input (unknown source, negative confidence, …): HTTP 422."""


@dataclass(frozen=True)
class Interval:
    """Remaining net filament in grams: `mid` ± `sigma`, i.e. [lo, hi] (lo clamped at 0)."""

    mid: float
    sigma: float
    anchored: bool  # False: never weighed nor received with a known net
    anchor_at: str | None
    anchor_source: str | None
    last_weighed_at: str | None
    deltas: int  # delta rows booked since the anchor

    @property
    def lo(self) -> float:
        return max(0.0, self.mid - self.sigma)

    @property
    def hi(self) -> float:
        return max(0.0, self.mid + self.sigma)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mid_g": round(self.mid, 1),
            "sigma_g": round(self.sigma, 1),
            "lo_g": round(self.lo, 1),
            "hi_g": round(self.hi, 1),
            "anchored": self.anchored,
            "anchor_at": self.anchor_at,
            "anchor_source": self.anchor_source,
            "last_weighed_at": self.last_weighed_at,
            "deltas_since_anchor": self.deltas,
        }

    def text(self) -> str:
        if not self.anchored:
            return f"≤{self.hi:.0f} g (never weighed)"
        return f"{self.mid:.0f} ± {self.sigma:.0f} g ({self.lo:.0f}–{self.hi:.0f})"


@dataclass(frozen=True)
class Row:
    id: int
    at: str
    spool_id: int
    kind: Kind
    delta_g: float
    anchor_g: float | None
    source: str
    confidence_g: float
    gross_g: float | None
    tare_g: float | None
    job_id: str | None
    note: str | None
    actor: str

    @classmethod
    def from_db(cls, r: sqlite3.Row) -> Row:
        return cls(**{k: r[k] for k in r.keys()})  # noqa: SIM118 — sqlite3.Row iterates values, not keys

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def default_confidence(source: str, grams: float) -> float:
    frac, floor = DELTA_SOURCES[source]
    return max(floor, abs(grams) * frac)


def receive_sigma(net_g: float) -> float:
    return max(RECEIVE_SIGMA_MIN_G, net_g * RECEIVE_SIGMA_FRACTION)


def history(
    conn: sqlite3.Connection, spool_id: int, *, limit: int | None = None
) -> list[Row]:
    """Ledger rows of a spool, oldest first (the newest `limit` when given)."""
    if limit is None:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE spool_id = ? ORDER BY id", (spool_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM ledger WHERE spool_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (spool_id, max(1, limit)),
        ).fetchall()
    return [Row.from_db(r) for r in rows]


def estimate(
    conn: sqlite3.Connection, spool_id: int, *, fallback_g: float | None = None
) -> Interval:
    """The remaining interval from the ledger alone. `fallback_g` (Spoolman's remaining) bounds a
    spool that has no anchor yet; without it an unanchored spool is [0, 0]."""
    # One pass over the spool's rows (index ledger_spool_id), newest first: the deltas until the
    # newest anchor, and the newest weighing (which may lie further back than that anchor).
    anchor: sqlite3.Row | None = None
    weighed: str | None = None
    total = var = 0.0
    n_deltas = 0
    rows = conn.execute(
        "SELECT id, at, kind, delta_g, anchor_g, source, confidence_g FROM ledger"
        " WHERE spool_id = ? AND kind != 'note' ORDER BY id DESC",
        (spool_id,),
    )
    for row in rows:
        if weighed is None and row["source"] == "weigh":
            weighed = str(row["at"])
        if anchor is None:
            if row["kind"] == "anchor":
                anchor = row
            else:
                total += float(row["delta_g"])
                var += float(row["confidence_g"]) ** 2
                n_deltas += 1
        if anchor is not None and weighed is not None:
            break
    if anchor is None:
        hi = max(0.0, float(fallback_g or 0.0))
        return Interval(
            mid=hi / 2,
            sigma=hi / 2,
            anchored=False,
            anchor_at=None,
            anchor_source=None,
            last_weighed_at=weighed,
            deltas=n_deltas,
        )
    var += float(anchor["confidence_g"]) ** 2
    return Interval(
        mid=float(anchor["anchor_g"]) + total,
        sigma=math.sqrt(var),
        anchored=True,
        anchor_at=str(anchor["at"]),
        anchor_source=str(anchor["source"]),
        last_weighed_at=weighed,
        deltas=n_deltas,
    )


def remaining(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    spool_id: int,
    *,
    spool: Mapping[str, Any] | None = None,
) -> Interval:
    """The remaining interval; reads Spoolman (or uses the given spool JSON) for the fallback."""
    sp = spool if spool is not None else spoolman.get("spool", spool_id)
    return estimate(conn, spool_id, fallback_g=_float(sp.get("remaining_weight")))


def _insert(
    conn: sqlite3.Connection,
    *,
    spool_id: int,
    kind: Kind,
    delta_g: float,
    anchor_g: float | None,
    source: str,
    confidence_g: float,
    actor: str,
    gross_g: float | None = None,
    tare_g: float | None = None,
    job_id: str | None = None,
    note: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO ledger (at, spool_id, kind, delta_g, anchor_g, source, confidence_g, gross_g, tare_g,"
        " job_id, note, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            db.utc_now(),
            spool_id,
            kind,
            delta_g,
            anchor_g,
            source,
            confidence_g,
            gross_g,
            tare_g,
            job_id,
            note,
            actor,
        ),
    )
    return int(cur.lastrowid or 0)


def _check_confidence(confidence_g: float | None) -> None:
    if confidence_g is not None and (
        not math.isfinite(confidence_g) or confidence_g < 0
    ):
        raise LedgerError(
            f"confidence_g must be a finite number ≥ 0, got {confidence_g}"
        )


def book(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    spool_id: int,
    grams: float,
    source: str,
    confidence_g: float | None = None,
    job_id: str | None = None,
    note: str | None = None,
    *,
    actor: str = "system",
) -> tuple[Row, JSON]:
    """Book `grams` of usage (negative = a correction that gives grams back): ledger delta row +
    Spoolman `/use`, atomically (see module invariants). Returns (row, updated Spoolman spool)."""
    if source not in DELTA_SOURCES:
        raise LedgerError(
            f"usage source must be one of {', '.join(DELTA_SOURCES)}; got {source!r}"
        )
    if not math.isfinite(grams) or grams == 0:
        raise LedgerError(f"grams must be a non-zero finite number, got {grams}")
    _check_confidence(confidence_g)
    conf = (
        default_confidence(source, grams)
        if confidence_g is None
        else float(confidence_g)
    )
    with db.transaction(conn):
        row_id = _insert(
            conn,
            spool_id=spool_id,
            kind="delta",
            delta_g=-grams,
            anchor_g=None,
            source=source,
            confidence_g=conf,
            actor=actor,
            job_id=job_id,
            note=note,
        )
        ev.emit(
            conn,
            "spool.used",
            f"spool:{spool_id}",
            {
                "grams": grams,
                "source": source,
                "confidence_g": conf,
                "job_id": job_id,
                "ledger_id": row_id,
            },
            actor,
        )
        spool = spoolman.use(spool_id, use_weight=grams)
    return _row(conn, row_id), sync_confidence(conn, spoolman, spool_id, spool)


def anchor(
    conn: sqlite3.Connection,
    spool_id: int,
    remaining_g: float,
    source: str,
    confidence_g: float,
    *,
    actor: str,
    gross_g: float | None = None,
    tare_g: float | None = None,
    job_id: str | None = None,
    note: str | None = None,
) -> int:
    """Insert an anchor row (absolute remaining). Spoolman is the caller's business (measure,
    receive, empty each write it their own way); call inside the caller's transaction."""
    if source not in ANCHOR_SOURCES:
        raise LedgerError(
            f"anchor source must be one of {', '.join(ANCHOR_SOURCES)}; got {source!r}"
        )
    if not math.isfinite(remaining_g):
        raise LedgerError(f"remaining must be finite, got {remaining_g}")
    _check_confidence(confidence_g)
    before = estimate(conn, spool_id)
    jump = remaining_g - before.mid if before.anchored else 0.0
    return _insert(
        conn,
        spool_id=spool_id,
        kind="anchor",
        delta_g=jump,
        anchor_g=remaining_g,
        source=source,
        confidence_g=confidence_g,
        actor=actor,
        gross_g=gross_g,
        tare_g=tare_g,
        job_id=job_id,
        note=note,
    )


def note(conn: sqlite3.Connection, spool_id: int, text: str, *, actor: str) -> int:
    """A ledger note (e.g. drying water loss): changes nothing, never touches Spoolman."""
    if not text.strip():
        raise LedgerError("a ledger note needs text")
    return _insert(
        conn,
        spool_id=spool_id,
        kind="note",
        delta_g=0.0,
        anchor_g=None,
        source="note",
        confidence_g=0.0,
        actor=actor,
        note=text,
    )


def sync_confidence(
    conn: sqlite3.Connection, spoolman: Spoolman, spool_id: int, spool: JSON
) -> JSON:
    """Write the current ± into Spoolman's `remaining_confidence_g` (a display cache; failures
    are swallowed because the ledger, not Spoolman, is the source of the interval)."""
    est = estimate(conn, spool_id, fallback_g=_float(spool.get("remaining_weight")))
    if not est.anchored:
        return spool
    try:
        return spoolman.update(
            "spool",
            spool_id,
            {"extra": {"remaining_confidence_g": encode_extra(round(est.sigma, 1))}},
        )
    except SpoolmanError:
        return spool


def _row(conn: sqlite3.Connection, row_id: int) -> Row:
    return Row.from_db(
        conn.execute("SELECT * FROM ledger WHERE id = ?", (row_id,)).fetchone()
    )


def _float(v: Any) -> float | None:
    return None if v is None else float(v)
