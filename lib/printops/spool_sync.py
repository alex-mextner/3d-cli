"""spool_sync.py — the invariant between Spoolman and PrintOps: detect every divergence, heal it.

ACCESSED VIA: 3d-api `routes_spool_sync.py` (the scheduled pass, `GET /v1/spool-sync`,
`POST /v1/spool-sync/heal`), `3d printops spool drift`, `3d printops health`, and
`printops.gates` (a drifted spool in a print gate). Design and its justification:
docs/printops/spool-sync.md.

THE RULE (one sentence): Spoolman holds the CURRENT value of every spool field; PrintOps holds
what it did about it (the append-only ledger, events, anomalies) and a bookmark of what it last
processed (`spool_sync`, see printops.spool_mirror). A divergence is therefore always "something
changed in Spoolman that PrintOps has not processed yet" — never "which of two copies is right" —
and healing it means PrintOps catches up by APPENDING (a ledger row, an event, a note) and never
by overwriting Spoolman with an old copy.

THE INVARIANT (what `detect` checks, `heal` restores; tolerance `TOLERANCE_G`):
  - weight     ledger mid (floored at 0) = Spoolman remaining_weight, within TOLERANCE_G;
  - identity   every Spoolman spool is known to PrintOps (bookmark row) and every spool PrintOps
               knows exists in Spoolman;
  - placement  Spoolman `location` is the ONE place a spool is: it names a PrintOps location
               (`location` table, by path), a slot is the location `slot:<printer>/<head>`, one
               spool per slot, no archived spool in a slot; every move was recorded (events);
  - archived   the bookmark agrees with Spoolman (the change was processed: event, note);
  - derived    `remaining_confidence_g` = the ledger σ; a spool Spoolman has no tare for gets
               the tare PrintOps resolves (native `spool_weight` only).

WHAT A CHANGE IN SPOOLMAN BECOMES (weight): a drop of a spool loaded in a Moonraker-`[spoolman]`
printer after `last_used` moved is that printer's usage → a `moonraker_live` delta row; any other
change (a typed remaining weight, a /measure, a first sight, a rise) is a weighing → a `weigh`
anchor whose ± is the scale's and the tare's. The classification only labels the ledger row; the
resulting mid is Spoolman's either way, so a wrong label can never leave the two apart.

INVARIANTS:
  - `detect` is read-only (Spoolman reads, DB reads). `heal` writes PrintOps rows and, only
    through the `SyncedSpoolman` it is given, the derived fields above; it NEVER writes the weight to
    Spoolman, so it cannot echo anything and a second run finds nothing to do.
  - A spool is healed in ONE transaction that also holds the Spoolman reads/writes (the same
    write lock every PrintOps writer takes): a failure rolls its rows back and the next pass
    retries. The bookmark stores what was READ, so a Spoolman change that lands during the
    heal is a pending difference of the next pass, never lost.
  - Deleting is recorded only after a 404 for that very spool, and never when more than
    `MISSING_FRACTION` of the known spools look gone at once (a wiped or wrong Spoolman): that is
    `spoolman_empty`, which only a person resolves. A Spoolman restored from a backup (its
    `last_used` went backwards) is `spoolman_rolled_back`: nothing is healed on that spool.
  - Drifts that cannot self-heal (`healable` False) or whose heal failed become anomalies of scope
    `spools` (`run_pass`); they clear on their own when the condition does.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from errors import ThreeDError
from printops import db, ledger, locations, spools, tares
from printops import events as ev
from printops import reconcile as rc
from printops.spool_mirror import (
    DELETED,
    PRESENT,
    Seen,
    SyncedSpoolman,
    extra_of,
    load,
    load_all,
    seen_of,
    slot_of_location,
    store,
)
from printops.spoolman import Spoolman, SpoolmanError, encode_extra

log = logging.getLogger("printops.spool_sync")

ACTOR = "spool-sync"
SCOPE = "spools"  # anomaly scope of what cannot self-heal
TOLERANCE_G = (
    0.5  # ledger mid and Spoolman remaining "agree" within this (float/rounding noise)
)
CONFIDENCE_TOLERANCE_G = 0.05  # remaining_confidence_g is cached rounded to 0.1
MISSING_MIN = 3  # fewer vanished spools than this are never "Spoolman looks empty"
MISSING_FRACTION = 0.5
CONFIDENCE_EXTRA = "remaining_confidence_g"

# one pass at a time per process: the scheduled pass and a CLI-triggered heal never interleave
PASS_LOCK = threading.Lock()

# Kinds (stable names: anomaly `kind`, JSON `kind`, docs). `healable` says what --heal can do.
KIND_UNKNOWN = "unknown_spool"
KIND_DELETED = "spool_deleted"
KIND_RETURNED = "spool_returned"
KIND_EMPTY = "spoolman_empty"
KIND_REMAINING = "remaining"
KIND_ROLLED_BACK = "spoolman_rolled_back"
KIND_ARCHIVED = "archived"
KIND_LOCATION = "location"
KIND_LOCATION_UNKNOWN = "location_unknown"
KIND_SLOT_CONFLICT = "slot_conflict"
KIND_TARE = "tare"
KIND_CONFIDENCE = "confidence"
KINDS = (
    KIND_UNKNOWN,
    KIND_DELETED,
    KIND_RETURNED,
    KIND_EMPTY,
    KIND_REMAINING,
    KIND_ROLLED_BACK,
    KIND_ARCHIVED,
    KIND_LOCATION,
    KIND_LOCATION_UNKNOWN,
    KIND_SLOT_CONFLICT,
    KIND_TARE,
    KIND_CONFIDENCE,
)

# Kinds that make a print decision on the spool untrustworthy: the gates grade them hard (FAIL
# under policy `block`). The others are bookkeeping that Spoolman already has right (a move that
# has no event yet, a cached ±): the gates warn.
BLOCKING_KINDS = frozenset(
    {
        KIND_REMAINING,
        KIND_ROLLED_BACK,
        KIND_SLOT_CONFLICT,
        KIND_LOCATION_UNKNOWN,
        KIND_UNKNOWN,
        KIND_DELETED,
    }
)


@dataclass(frozen=True)
class Drift:
    """One divergence. `spool_id` None = about Spoolman as a whole (`spoolman_empty`)."""

    spool_id: int | None
    kind: str
    summary: str
    healable: bool
    action: str  # what --heal does ("" when only a person can)
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return "spoolman" if self.spool_id is None else f"spool:{self.spool_id}"

    @property
    def blocks(self) -> bool:
        """A gate must not trust the spool while this stands (see BLOCKING_KINDS)."""
        if self.kind == KIND_LOCATION:
            return "archived_in_slot" in self.detail.get("change", ())
        return self.kind in BLOCKING_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "spool_id": self.spool_id,
            "kind": self.kind,
            "summary": self.summary,
            "healable": self.healable,
            "blocks": self.blocks,
            "action": self.action,
            "detail": dict(self.detail),
        }


def gate_state(drifts: Sequence[Drift]) -> dict[str, Any]:
    """The `sync` key of a bound spool in the gates state (printops.gates)."""
    return {
        "status": "drift" if drifts else "ok",
        "findings": [
            {
                "kind": d.kind,
                "summary": d.summary,
                "healable": d.healable,
                "blocks": d.blocks,
            }
            for d in drifts
        ],
    }


# -- context: everything a plan reads besides the spool itself ---------------------------------


class Context:
    """The PrintOps side of a pass, loaded once: bookmarks, the location index, the slot
    occupancy of the Spoolman listing, and (lazily) the fleet."""

    def __init__(
        self, conn: sqlite3.Connection, pool: Sequence[Mapping[str, Any]]
    ) -> None:
        self.conn = conn
        self.seen = load_all(conn)
        self.by_id = {
            loc.id: loc for loc in locations.all_locations(conn, include_archived=True)
        }
        self.current = {loc.path.lower(): loc.id for loc in self.by_id.values()}
        self.former = {
            str(r["path"]).lower(): int(r["location_id"])
            for r in conn.execute("SELECT path, location_id FROM location_alias")
        }
        self.occupancy: dict[str, list[int]] = {}
        for sp in pool:
            key = slot_of_location(str(sp.get("location") or ""))
            if key and not sp.get("archived"):
                self.occupancy.setdefault(key.casefold(), []).append(int(sp["id"]))
        self._fleet: Any = False  # False = not loaded yet; None = no usable fleet

    def owner(self, text: str) -> tuple[locations.Location, str] | None:
        """(location, "current"|"former") a `location` text names, else None."""
        key = text.strip().lower()
        if key in self.current:
            return self.by_id[self.current[key]], "current"
        lid = self.former.get(key)
        if lid is not None and lid in self.by_id:
            return self.by_id[lid], "former"
        return None

    def moonraker_books(self, slot: str) -> bool:
        """The printer of `slot` is a single-head Moonraker printer: its `[spoolman]` section (when
        on) books that printer's usage into Spoolman itself (printops.head_sync)."""
        if not slot:
            return False
        if self._fleet is False:
            from printops.fleet import load_fleet

            try:
                self._fleet = load_fleet()
            except ThreeDError:
                self._fleet = None
        fleet = self._fleet
        printer = (
            fleet.printers.get(slot.partition("/")[0]) if fleet is not None else None
        )
        return (
            printer is not None
            and printer.device.get("adapter") == "moonraker"
            and len(printer.heads) == 1
        )


# -- planning: one function decides both "is there drift" and "what heals it" ------------------


@dataclass
class Heal:
    """What a step may touch while a spool is healed."""

    conn: sqlite3.Connection
    spool: Mapping[str, Any]
    patch: dict[str, Any] = field(
        default_factory=dict
    )  # ONE Spoolman PATCH for the whole spool

    @property
    def spool_id(self) -> int:
        return int(self.spool["id"])

    def set_extra(self, key: str, value: Any) -> None:
        self.patch.setdefault("extra", {})[key] = (
            None if value is None else encode_extra(value)
        )


Step = Callable[[Heal], None]


@dataclass
class Plan:
    spool_id: int
    drifts: list[Drift] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def add(self, drift: Drift, step: Step | None = None) -> None:
        self.drifts.append(drift)
        if step is not None and drift.healable:
            self.steps.append(step)


def _remaining(spool: Mapping[str, Any]) -> float | None:
    v = spool.get("remaining_weight")
    return float(v) if isinstance(v, (int, float)) else None


def _text(spool: Mapping[str, Any]) -> str:
    return str(spool.get("location") or "").strip()


def _name(spool: Mapping[str, Any]) -> str:
    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    label = " ".join(
        str(p) for p in (vendor, fil.get("name") or fil.get("material")) if p
    )
    return f"#{spool.get('id')} {label}".strip()


def _edit_sigma(conn: sqlite3.Connection, spool: Mapping[str, Any]) -> float:
    """± of a weight somebody typed or measured in Spoolman: the scale's and the tare's, as
    `spools.measure` has it; with no tare known, a hand estimate."""
    tare = tares.resolve(conn, spool).tare
    return (
        math.hypot(spools.SCALE_SIGMA, tare.sigma)
        if tare is not None
        else spools.MANUAL_REMAINING_MIN_G
    )


# -- weight ------------------------------------------------------------------------------------

_WEIGHT_VERB = {
    "usage": "usage booked in Spoolman (Moonraker `[spoolman]`)",
    "edit": "weight changed in Spoolman",
    "first_sync": "first sync: the ledger and Spoolman disagreed",
    "ledger_moved": "the ledger moved without Spoolman",
    "adopt": "spool first seen with a weight",
}


def _rolled_back(m: Seen, s: Mapping[str, Any]) -> bool:
    """Spoolman's last use is OLDER than the one PrintOps saw: it was restored from a backup."""
    last_used = str(s["last_used"]) if s.get("last_used") else None
    return bool(m.last_used and last_used and last_used < m.last_used)


def _is_usage(
    ctx: Context, s: Mapping[str, Any], m: Seen, now: float, mid: float
) -> bool:
    """A drop after Spoolman's `last_used` moved, on a spool in a Moonraker-`[spoolman]` printer."""
    last_used = str(s["last_used"]) if s.get("last_used") else None
    if last_used == m.last_used or now >= mid:
        return False
    return ctx.moonraker_books(
        slot_of_location(_text(s)) or slot_of_location(m.location)
    )


def _weight_cause(
    ctx: Context, s: Mapping[str, Any], m: Seen | None, est: ledger.Interval, now: float
) -> str | None:
    """Why the ledger and Spoolman differ, None when they agree (or there is nothing to compare)."""
    moved = m is None or m.remaining_g is None or abs(now - m.remaining_g) > TOLERANCE_G
    if not est.anchored:
        if not moved:
            return None  # still never weighed, nothing new in Spoolman: "never weighed" is true
        return "adopt" if m is None else "edit"
    mid = max(est.mid, 0.0)
    if abs(now - mid) <= TOLERANCE_G:
        return None
    if m is None:
        return "first_sync"
    if _rolled_back(m, s):
        return "rolled_back"
    if not moved:
        return "ledger_moved"
    return "usage" if _is_usage(ctx, s, m, now, mid) else "edit"


def _weight_step(
    s: Mapping[str, Any], cause: str, before: float | None, now: float, note: str
) -> Step:
    sid = int(s["id"])

    def step(h: Heal) -> None:
        if cause == "usage":
            assert before is not None
            row = ledger.observe_usage(
                h.conn, sid, now - before, "moonraker_live", actor=ACTOR, note=note
            )
            payload: dict[str, Any] = {
                "field": "remaining_weight",
                "cause": cause,
                "from": before,
                "to": now,
                "ledger_id": row,
            }
        else:
            sealed = (
                cause == "adopt"
                and not s.get("used_weight")
                and not s.get("first_used")
            )
            source, sigma = (
                ("receive", ledger.receive_sigma(now))
                if sealed
                else ("weigh", _edit_sigma(h.conn, s))
            )
            row = ledger.anchor(h.conn, sid, now, source, sigma, actor=ACTOR, note=note)
            payload = {
                "field": "remaining_weight",
                "cause": cause,
                "from": before,
                "to": now,
                "sigma_g": round(sigma, 1),
                "ledger_id": row,
            }
        ev.emit(h.conn, "spool.synced", f"spool:{sid}", payload, ACTOR)

    return step


def _plan_weight(
    ctx: Context, s: Mapping[str, Any], m: Seen | None, plan: Plan
) -> None:
    """Ledger mid ↔ Spoolman remaining. `m` is None for a spool seen for the first time."""
    sid = int(s["id"])
    now = _remaining(s)
    if now is None:
        return
    est = ledger.estimate(ctx.conn, sid, fallback_g=now)
    cause = _weight_cause(ctx, s, m, est, now)
    if cause is None:
        return
    before = max(est.mid, 0.0) if est.anchored else None
    if cause == "rolled_back":
        assert m is not None and before is not None
        plan.add(
            Drift(
                sid,
                KIND_ROLLED_BACK,
                f"{_name(s)}: Spoolman's last use ({s.get('last_used')}) is older than the one PrintOps saw ({m.last_used}): "
                "Spoolman was restored from a backup. Weigh the spool (3d printops spool measure) to re-anchor",
                False,
                "",
                {
                    "ledger_g": round(before, 1),
                    "spoolman_g": now,
                    "last_used": s.get("last_used"),
                    "seen_last_used": m.last_used,
                },
            )
        )
        return
    verb = _WEIGHT_VERB[cause]
    have = "no anchor" if before is None else f"ledger {before:.1f} g"
    action = (
        "book the usage in the ledger (no Spoolman write)"
        if cause == "usage"
        else "anchor the ledger at Spoolman's weight (a weighing, ± scale and tare)"
    )
    plan.add(
        Drift(
            sid,
            KIND_REMAINING,
            f"{_name(s)}: {have}, Spoolman {now:.1f} g — {verb}",
            True,
            action,
            {
                "cause": cause,
                "ledger_g": None if before is None else round(before, 1),
                "spoolman_g": now,
                "delta_g": None if before is None else round(now - before, 1),
            },
        ),
        _weight_step(
            s, cause, before, now, f"{verb}: {have} → Spoolman {now:.1f} g (spool-sync)"
        ),
    )


# -- archived ----------------------------------------------------------------------------------


def _plan_archived(s: Mapping[str, Any], base: Seen, plan: Plan) -> None:
    sid = int(s["id"])
    archived = bool(s.get("archived"))
    if archived == base.archived:
        return
    word = "archived" if archived else "un-archived"

    def step(h: Heal) -> None:
        est = ledger.estimate(h.conn, sid, fallback_g=_remaining(s))
        left = f"{max(est.mid, 0.0):.0f} g" if est.anchored else "an unknown weight"
        ledger.note(
            h.conn,
            sid,
            f"{word} in Spoolman with {left} left (spool-sync)",
            actor=ACTOR,
        )
        ev.emit(
            h.conn,
            "spool.archived" if archived else "spool.unarchived",
            f"spool:{sid}",
            {
                "leftover_estimate": est.to_dict() if est.anchored else None,
                "location": s.get("location"),
                "via": "spoolman",
            },
            ACTOR,
        )

    plan.add(
        Drift(
            sid,
            KIND_ARCHIVED,
            f"{_name(s)} was {word} in Spoolman",
            True,
            "record it (event + ledger note)",
            {"archived": archived, "remaining_g": _remaining(s)},
        ),
        step,
    )


# -- placement: the location text is the one place a spool is -----------------------------------


def _slot_event(
    h: Heal, kind: str, slot: str, why: str, *, to: str | None = None
) -> None:
    printer, _, head = slot.partition("/")
    if kind == "slot.loaded":
        payload: dict[str, Any] = {
            "printer": printer,
            "head": head,
            "spool_id": h.spool_id,
            "replaced": [],
            "via": "spoolman",
        }
    else:
        payload = {
            "printer": printer,
            "head": head,
            "spool_id": h.spool_id,
            "to": to,
            "why": why,
        }
    ev.emit(h.conn, kind, f"slot:{slot}", payload, ACTOR)


def _slot_clash(ctx: Context, s: Mapping[str, Any], key: str) -> Drift | None:
    """Two (non-archived) spools in one slot: only a person decides which stays."""
    if not key:
        return None
    others = sorted(
        i for i in ctx.occupancy.get(key.casefold(), []) if i != int(s["id"])
    )
    if not others:
        return None
    return Drift(
        int(s["id"]),
        KIND_SLOT_CONFLICT,
        f"{_name(s)}: slot {key} also holds " + ", ".join(f"#{i}" for i in others),
        False,
        "",
        {"slot": key, "others": others, "identity": f"{key}|{others}"},
    )


def _archived_in_slot(s: Mapping[str, Any], key: str) -> tuple[Drift, Step]:
    def step(h: Heal) -> None:
        h.patch["location"] = ""
        _slot_event(h, "slot.unloaded", key, "archived in Spoolman")

    drift = Drift(
        int(s["id"]),
        KIND_LOCATION,
        f"{_name(s)} is archived but still in slot {key}",
        True,
        "take it out of the slot (clear the location)",
        {"change": ["archived_in_slot"], "slot": key},
    )
    return drift, step


def _placement_step(
    base_text: str, loc: locations.Location | None, changes: list[str], clash: bool
) -> Step:
    def step(h: Heal) -> None:
        if "former_path" in changes and loc is not None:
            h.patch["location"] = loc.path
        new_path = "" if loc is None else loc.path
        old_key, new_key = slot_of_location(base_text), slot_of_location(new_path)
        if "moved" in changes:
            ev.emit(
                h.conn,
                "spool.moved",
                f"spool:{h.spool_id}",
                {
                    "from": base_text or None,
                    "to": new_path or None,
                    "location_id": None if loc is None else loc.id,
                    "via": "spoolman",
                },
                ACTOR,
            )
            if old_key:
                _slot_event(
                    h,
                    "slot.unloaded",
                    old_key,
                    "moved in Spoolman",
                    to=new_path or None,
                )
        if (
            new_key
            and not clash
            and ("moved" in changes or "adopted_in_slot" in changes)
        ):
            _slot_event(h, "slot.loaded", new_key, "loaded in Spoolman")

    return step


def _unknown_location(s: Mapping[str, Any], text: str) -> Drift:
    return Drift(
        int(s["id"]),
        KIND_LOCATION_UNKNOWN,
        f"{_name(s)}: location {text!r} is not an active PrintOps location "
        "(add it with `3d printops loc add`, or move the spool to a known one)",
        False,
        "",
        {"location": text, "identity": text},
    )


def _placement_changes(how: str, adopt: bool, text_moved: bool, key: str) -> list[str]:
    """What happened to the placement since PrintOps last looked."""
    changes = ["former_path"] if how == "former" else []
    if not adopt and text_moved:
        changes.append("moved")
    elif adopt and key:
        changes.append("adopted_in_slot")
    return changes


def _placement_drift(
    s: Mapping[str, Any],
    base: Seen,
    loc: locations.Location | None,
    how: str,
    changes: list[str],
) -> Drift:
    text = _text(s)
    summary = f"{_name(s)}: location {text or '(none)'!r}"
    if "moved" in changes:
        summary += f" (moved in Spoolman from {base.location or '(none)'})"
    former = how == "former" and loc is not None
    if former and loc is not None:
        summary += f" is a former path of {loc.path}"
    action = "record the move" + (
        f" and set the text to {loc.path}" if former and loc else ""
    )
    return Drift(
        int(s["id"]),
        KIND_LOCATION,
        summary,
        True,
        action,
        {"change": changes, "from": base.location or None, "to": text or None},
    )


def _plan_placement(
    ctx: Context, s: Mapping[str, Any], base: Seen, adopt: bool, plan: Plan
) -> None:
    """The location text names a PrintOps location; a move (and a slot change that comes with it) is recorded."""
    text = _text(s)
    key = slot_of_location(text)
    if key and s.get("archived"):
        drift, step = _archived_in_slot(s, key)
        plan.add(drift, step)
        return
    owner = ctx.owner(text) if text else None
    if text and (owner is None or owner[0].archived):
        plan.add(_unknown_location(s, text))
        return
    loc, how = owner if owner is not None else (None, "")
    clash = _slot_clash(ctx, s, slot_of_location("" if loc is None else loc.path))
    if clash is not None:
        plan.add(clash)
    changes = _placement_changes(how, adopt, text != base.location, key)
    if changes:
        plan.add(
            _placement_drift(s, base, loc, how, changes),
            _placement_step(base.location, loc, changes, clash is not None),
        )


# -- tare, confidence --------------------------------------------------------------------------


def _plan_tare(ctx: Context, s: Mapping[str, Any], plan: Plan) -> None:
    """Spoolman's own /measure and remaining maths use only native tares (spool, filament, vendor
    `empty_spool_weight`). A spool with none, whose tare PrintOps resolves from its tare DB, gets it
    written to `spool_weight`; PrintOps keeps the provenance (`spool_tare_choice`)."""
    if s.get("archived"):
        return
    res = tares.resolve(ctx.conn, s)
    if res.tare is None or res.tare.origin != "db":
        return
    tare = res.tare
    sid = int(s["id"])

    def step(h: Heal) -> None:
        h.patch["spool_weight"] = tare.grams
        tares.record_choice(h.conn, sid, tare, actor=ACTOR)

    plan.add(
        Drift(
            sid,
            KIND_TARE,
            f"{_name(s)}: Spoolman knows no empty-spool weight, the tare DB says {tare.grams:g} g",
            True,
            f"write spool_weight = {tare.grams:g} g",
            {"tare_g": tare.grams, "source": tare.source},
        ),
        step,
    )


def _confidence_stale(conn: sqlite3.Connection, s: Mapping[str, Any]) -> float | None:
    """The σ to write into `remaining_confidence_g` when it differs from the cached one (the cache
    is what the UI shows next to an ACTIVE spool: an archived one is left as it was)."""
    if s.get("archived"):
        return None
    est = ledger.estimate(conn, int(s["id"]), fallback_g=_remaining(s))
    if not est.anchored:
        return None
    want = round(est.sigma, 1)
    have = extra_of(s, CONFIDENCE_EXTRA)
    if (
        isinstance(have, (int, float))
        and not isinstance(have, bool)
        and abs(float(have) - want) <= CONFIDENCE_TOLERANCE_G
    ):
        return None
    return want


def _plan_confidence(ctx: Context, s: Mapping[str, Any], plan: Plan) -> None:
    want = _confidence_stale(ctx.conn, s)
    if want is not None:
        plan.add(
            Drift(
                int(s["id"]),
                KIND_CONFIDENCE,
                f"{_name(s)}: remaining_confidence_g is {extra_of(s, CONFIDENCE_EXTRA)!r}, the ledger says ±{want:g} g",
                True,
                f"write remaining_confidence_g = {want:g}",
                {"wanted": want, "found": extra_of(s, CONFIDENCE_EXTRA)},
            )
        )


# -- one spool ---------------------------------------------------------------------------------


def _plan_identity(s: Mapping[str, Any], m: Seen | None, plan: Plan) -> None:
    """A spool PrintOps has never seen, or one that is back after it was recorded as deleted."""
    sid = int(s["id"])
    if m is not None and m.state != DELETED:
        return
    kind, kind_event = (
        (KIND_UNKNOWN, "spool.adopted")
        if m is None
        else (KIND_RETURNED, "spool.returned")
    )
    said = (
        "is in Spoolman but PrintOps has never seen it"
        if m is None
        else "is in Spoolman again after it was deleted"
    )

    def step(h: Heal) -> None:
        ev.emit(
            h.conn,
            kind_event,
            f"spool:{sid}",
            {
                "name": _name(s),
                "location": s.get("location"),
                "remaining_g": _remaining(s),
                "archived": bool(s.get("archived")),
            },
            ACTOR,
        )

    plan.add(
        Drift(
            sid,
            kind,
            f"{_name(s)} {said}",
            True,
            "adopt it (event, anchor from its weight)",
            {"location": s.get("location"), "remaining_g": _remaining(s)},
        ),
        step,
    )


def plan_spool(ctx: Context, s: Mapping[str, Any]) -> Plan:
    """Everything that differs for one Spoolman spool, and how to fix it."""
    plan = Plan(int(s["id"]))
    m = ctx.seen.get(int(s["id"]))
    first = m is None or m.state == DELETED
    base = seen_of(s) if first or m is None else m
    _plan_identity(s, m, plan)
    _plan_weight(ctx, s, None if first else m, plan)
    _plan_archived(s, base, plan)
    _plan_placement(ctx, s, base, first, plan)
    _plan_tare(ctx, s, plan)
    _plan_confidence(ctx, s, plan)
    return plan


# -- detection ----------------------------------------------------------------------------------


@dataclass
class Report:
    checked: int
    drifts: list[Drift]
    pool: list[Mapping[str, Any]] = field(
        default_factory=list, repr=False
    )  # the Spoolman listing it was made from

    @property
    def healable(self) -> list[Drift]:
        return [d for d in self.drifts if d.healable]

    @property
    def unhealable(self) -> list[Drift]:
        return [d for d in self.drifts if not d.healable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.drifts,
            "tolerance_g": TOLERANCE_G,
            "checked": self.checked,
            "drift": len(self.drifts),
            "healable": len(self.healable),
            "unhealable": len(self.unhealable),
            "drifts": [d.to_dict() for d in self.drifts],
        }


def _gone(
    conn: sqlite3.Connection, listed: set[int], seen: Mapping[int, Seen]
) -> tuple[list[int], int]:
    """(ids PrintOps knows from its bookmark or ledger that Spoolman no longer lists, how many it knows)."""
    known = {i for i, m in seen.items() if m.state == PRESENT}
    known |= {
        int(r["spool_id"]) for r in conn.execute("SELECT DISTINCT spool_id FROM ledger")
    }
    known -= {i for i, m in seen.items() if m.state == DELETED}
    return sorted(known - listed), len(known)


def _confirmed_deleted(sm: Spoolman, sid: int, m: Seen | None) -> Drift | None:
    """The 404 that turns "not listed" into "deleted"."""
    try:
        sm.get("spool", sid)
    except SpoolmanError as e:
        if e.status != 404:
            raise
        return Drift(
            sid,
            KIND_DELETED,
            f"#{sid} was deleted in Spoolman (PrintOps still has its ledger)",
            True,
            "record the deletion (event, ledger note)",
            {
                "last_remaining_g": None if m is None else m.remaining_g,
                "location": None if m is None else m.location or None,
            },
        )
    return None


def _vanished(
    conn: sqlite3.Connection,
    sm: Spoolman,
    listed: set[int],
    seen: Mapping[int, Seen],
    only: int | None,
) -> list[Drift]:
    """Spools PrintOps knows that Spoolman no longer lists — each confirmed by a 404, and none when
    most of them vanish at once (a wiped Spoolman or a wrong URL is a person's call)."""
    gone, known = _gone(conn, listed, seen)
    if len(gone) >= MISSING_MIN and len(gone) > MISSING_FRACTION * known:
        if only is not None:
            return []
        return [
            Drift(
                None,
                KIND_EMPTY,
                f"{len(gone)} of the {known} spools PrintOps knows are missing from Spoolman at once: a wiped Spoolman or a wrong "
                "SPOOLMAN_URL. Nothing is recorded as deleted until you check",
                False,
                "",
                {"missing": gone, "known": known},
            )
        ]
    out = (
        _confirmed_deleted(sm, sid, seen.get(sid))
        for sid in gone
        if only is None or sid == only
    )
    return [d for d in out if d is not None]


def detect(
    conn: sqlite3.Connection, sm: Spoolman, *, only: int | None = None
) -> Report:
    """Read-only: every divergence between Spoolman and PrintOps right now (`only`: one spool)."""
    pool = sm.find("spool", {"allow_archived": "true"})
    ctx = Context(conn, pool)
    wanted = [s for s in pool if only is None or int(s["id"]) == only]
    drifts = [d for s in wanted for d in plan_spool(ctx, s).drifts]
    drifts.extend(_vanished(conn, sm, {int(s["id"]) for s in pool}, ctx.seen, only))
    return Report(len(wanted), drifts, list(pool))


def detect_spools(
    conn: sqlite3.Connection,
    spools_json: Sequence[Mapping[str, Any]],
    pool: Sequence[Mapping[str, Any]],
) -> dict[int, list[Drift]]:
    """Per-spool drift for spools already in hand (print gates): no Spoolman request, no write."""
    ctx = Context(conn, pool)
    return {int(s["id"]): plan_spool(ctx, s).drifts for s in spools_json}


# -- healing -----------------------------------------------------------------------------------


def heal_spool(
    conn: sqlite3.Connection,
    sm: SyncedSpoolman,
    spool_id: int,
    pool: Sequence[Mapping[str, Any]],
) -> list[Drift]:
    """Heal one spool in one transaction; the drifts that were healed. Raises on a refused write
    (the transaction, and so every row of this spool, is rolled back)."""
    with db.transaction(conn):
        s = sm.get("spool", spool_id)
        plan = plan_spool(Context(conn, pool), s)
        healable = [d for d in plan.drifts if d.healable]
        if not healable or any(d.kind == KIND_ROLLED_BACK for d in plan.drifts):
            return []
        h = Heal(conn, s)
        for step in plan.steps:
            step(h)
        sigma = _confidence_stale(conn, s)  # after the steps: an anchor changed σ
        if sigma is not None:
            h.set_extra(CONFIDENCE_EXTRA, sigma)
        # the bookmark moves to what was READ; the write below then moves only the fields it sets
        store(conn, seen_of(s))
        if h.patch:
            sm.update("spool", spool_id, h.patch)
        return healable


def _heal_deleted(conn: sqlite3.Connection, spool_id: int) -> None:
    with db.transaction(conn):
        m = load(conn, spool_id)
        est = ledger.estimate(
            conn, spool_id, fallback_g=None if m is None else m.remaining_g
        )
        left = f"{max(est.mid, 0.0):.0f} g" if est.anchored else "an unknown weight"
        ledger.note(
            conn,
            spool_id,
            f"deleted in Spoolman with {left} left (spool-sync)",
            actor=ACTOR,
        )
        ev.emit(
            conn,
            "spool.deleted",
            f"spool:{spool_id}",
            {
                "leftover_estimate": est.to_dict() if est.anchored else None,
                "location": None if m is None else m.location or None,
            },
            ACTOR,
        )
        key = "" if m is None else slot_of_location(m.location)
        if key:
            printer, _, head = key.partition("/")
            ev.emit(
                conn,
                "slot.unloaded",
                f"slot:{key}",
                {
                    "printer": printer,
                    "head": head,
                    "spool_id": spool_id,
                    "to": None,
                    "why": "spool deleted in Spoolman",
                },
                ACTOR,
            )
        store(conn, Seen(spool_id, DELETED, None, None, False, ""))


@dataclass
class HealReport:
    before: Report
    healed: list[Drift]
    failed: list[dict[str, Any]]
    after: Report

    @property
    def ok(self) -> bool:
        return not self.after.drifts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "before": self.before.to_dict(),
            "healed": [d.to_dict() for d in self.healed],
            "failed": self.failed,
            "after": self.after.to_dict(),
        }


def _heal_one(
    conn: sqlite3.Connection, sm: SyncedSpoolman, sid: int, before: Report
) -> list[Drift]:
    gone = [d for d in before.healable if d.spool_id == sid and d.kind == KIND_DELETED]
    if gone:
        _heal_deleted(conn, sid)
        return gone
    return heal_spool(conn, sm, sid, before.pool)


def _try_heal_one(
    conn: sqlite3.Connection, sm: SyncedSpoolman, sid: int, before: Report
) -> tuple[list[Drift], str | None]:
    """(healed drifts, None) or ([], why a refused write or a DB error stopped this spool)."""
    try:
        return _heal_one(conn, sm, sid, before), None
    except (SpoolmanError, spools.SpoolError) as e:
        return [], e.message
    except (ledger.LedgerError, sqlite3.Error) as e:
        return [], str(e)


def heal(
    conn: sqlite3.Connection, sm: SyncedSpoolman, *, only: int | None = None
) -> HealReport:
    """Heal every healable drift, then detect again: `after` is what is left. One spool's failure
    (Spoolman refused a write) is reported and does not stop the others. Callers hold PASS_LOCK."""
    before = detect(conn, sm, only=only)
    healed: list[Drift] = []
    failed: list[dict[str, Any]] = []
    for sid in sorted({d.spool_id for d in before.healable if d.spool_id is not None}):
        done, error = _try_heal_one(conn, sm, sid, before)
        healed.extend(done)
        if error is not None:
            log.warning("spool-sync: healing #%s failed: %s", sid, error)
            failed.append({"spool_id": sid, "error": error})
    return HealReport(before, healed, failed, detect(conn, sm, only=only))


# -- the scheduled pass: heal, then anomalies for what is left --------------------------------------


@dataclass
class PassStatus:
    """The outcome of the latest pass, for `GET /v1/spool-sync` and `/v1/health`."""

    at: str | None = None
    checked: int = 0
    drift: int = 0
    healed: int = 0
    failed: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def anomaly_findings(report: HealReport) -> list[rc.Finding]:
    """What a pass could not fix: unhealable drifts, and healable ones whose heal failed."""
    errors = {f["spool_id"]: f["error"] for f in report.failed}
    out = []
    for d in report.after.drifts:
        if d.healable and d.spool_id not in errors:
            continue  # a change that landed after the heal: the next pass takes it
        evidence = d.to_dict()
        if d.spool_id in errors:
            evidence["heal_error"] = errors[d.spool_id]
        out.append(
            rc.Finding(
                d.kind, SCOPE, d.subject, evidence, (str(d.detail.get("identity", "")),)
            )
        )
    return out


def run_pass(
    conn: sqlite3.Connection, sm: SyncedSpoolman, *, only: int | None = None
) -> tuple[HealReport, PassStatus]:
    """One pass: heal (`only`: one spool), then — for a whole pass — make the open `spools`
    anomalies match what is left. Passes never interleave (PASS_LOCK)."""
    with PASS_LOCK:
        report = heal(conn, sm, only=only)
        now = db.utc_now()
        if only is None:
            with db.transaction(conn):
                rc.sync(conn, SCOPE, anomaly_findings(report), live_ok=True, now=now)
        return report, PassStatus(
            now,
            report.before.checked,
            len(report.after.drifts),
            len(report.healed),
            len(report.failed),
            None,
        )
