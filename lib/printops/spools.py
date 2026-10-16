"""spools.py — the spool lifecycle: receive → store/move → load into a printer slot → unload → empty.

ACCESSED VIA: 3d-api `routes_spools.py` (the only caller that writes; 3d-api is the single
Spoolman writer, plan §5.1) and therefore `3d printops spool …`, the bot and HA. Issues #160/#163.

WHERE THINGS LIVE (decision D2): vendor / filament / spool identity, remaining weight, lot,
price, Spoolman `location` (+ extra `printops_location_id`, written only through
`printops.locations.place_spool`), extra `printops_slot` ("u1/T3", "" when not loaded),
`opened_at`, `external_key` (import idempotency) — in Spoolman. The ledger (interval of
remaining), the tare provenance and the events — in the PrintOps DB.

INVARIANTS:
  - A spool sits in at most one printer slot and a slot holds at most one spool: `load` refuses
    an occupied slot unless told where the current spool goes (`replace_to`).
  - `load` refuses a head the fleet registry marks unusable (fleet/printers.yaml, #157), with
    the fleet's reason, and a head with open maintenance faults (#166, #233), naming them;
    printer-wide faults (no head) and AMS slots are not gated.
  - Every mutation runs inside one PrintOps DB transaction that also holds the Spoolman write;
    a Spoolman failure rolls the DB rows and events back. (A DB failure after Spoolman accepted
    is the one non-atomic window; the event log then simply lacks the change.)
  - Events: spool.received, spool.moved, slot.loaded, slot.unloaded (subject `slot:<p>/<head>`,
    payload printer/head/spool_id), spool.used (ledger), spool.measured, spool.empty.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from printops import db, ledger, locations, maintenance, tares
from printops import events as ev
from printops.spoolman import JSON, Spoolman, SpoolmanError, decode_extra, encode_extra

# Densities (g/cm³) Spoolman needs to create a filament when the caller gives none: SpoolmanDB
# material defaults (Spoolman 0.27.0 GET /api/v1/external/material, read 2026-10-14).
DENSITY = {
    "PLA": 1.24,
    "PLA+": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PA": 1.14,
    "PC": 1.2,
    "PVA": 1.19,
    "HIPS": 1.03,
    "PET": 1.27,
    "PCTG": 1.23,
}
DEFAULT_DIAMETER = 1.75
SCALE_SIGMA = 1.0  # kitchen scale ±1 g [assumption; pass scale_sigma_g to override]
GIVEN_TARE_SIGMA = 5.0  # a tare typed at weighing time without provenance [assumption]
MANUAL_REMAINING_FRACTION = (
    0.10  # "about 600 g left" by eye: ±10 %, at least 20 g [assumption]
)
MANUAL_REMAINING_MIN_G = 20.0


# A refused load names the oldest few open faults of the head, each symptom shortened: enough to
# act on, bounded however many faults were opened (`maint fault ls` has them all).
FAULTS_NAMED = 3
FAULT_SYMPTOM_CHARS = 80


class SpoolError(Exception):
    """A lifecycle request that cannot be done; `status` is the HTTP status 3d-api answers."""

    def __init__(
        self, status: int, message: str, *, detail: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = dict(detail or {})


@dataclass
class Ctx:
    conn: sqlite3.Connection
    spoolman: Spoolman
    actor: str


def _hex(value: str | None) -> str | None:
    if value is None:
        return None
    h = value.strip().lstrip("#").upper()
    if len(h) not in (6, 8) or any(c not in "0123456789ABCDEF" for c in h):
        raise SpoolError(
            422, f"colour must be a hex RGB(A) like #1B5E20, got {value!r}"
        )
    return h


def _ci(a: Any, b: Any) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def extra_value(item: Mapping[str, Any], key: str) -> Any:
    """A Spoolman extra field, decoded; None when unset or not valid JSON (edited by hand)."""
    try:
        return decode_extra((item.get("extra") or {}).get(key))
    except ValueError:
        return None


def slot_of(spool: Mapping[str, Any]) -> str:
    return str(extra_value(spool, "printops_slot") or "")


def get_spool(sm: Spoolman, spool_id: int) -> JSON:
    try:
        return sm.get("spool", spool_id)
    except SpoolmanError as e:
        if e.status == 404:
            raise SpoolError(404, f"no spool #{spool_id} in Spoolman") from e
        raise SpoolError(502, e.message) from e


def all_spools(sm: Spoolman, *, archived: bool = False) -> list[JSON]:
    try:
        return sm.find("spool", {"allow_archived": "true"} if archived else None)
    except SpoolmanError as e:
        raise SpoolError(502, e.message) from e


# -- views -----------------------------------------------------------------------------------


def view(conn: sqlite3.Connection, spool: Mapping[str, Any]) -> dict[str, Any]:
    """The PrintOps view of a Spoolman spool: identity + location/slot + remaining interval + tare."""
    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    est = ledger.estimate(
        conn, int(spool["id"]), fallback_g=_num(spool.get("remaining_weight"))
    )
    tare = tares.resolve(conn, spool)
    return {
        "id": spool["id"],
        "vendor": vendor,
        "material": fil.get("material"),
        "name": fil.get("name"),
        "color_hex": fil.get("color_hex"),
        "filament_id": fil.get("id"),
        "location": spool.get("location") or None,
        "location_id": extra_value(spool, "printops_location_id"),
        "slot": slot_of(spool) or None,
        "lot_nr": spool.get("lot_nr"),
        "price": spool.get("price"),
        "opened_at": extra_value(spool, "opened_at"),
        "external_key": extra_value(spool, "external_key"),
        "archived": bool(spool.get("archived")),
        "initial_weight_g": spool.get("initial_weight"),
        "spoolman_remaining_g": spool.get("remaining_weight"),
        "remaining": est.to_dict(),
        "remaining_text": est.text(),
        "tare": tare.tare.to_dict() if tare.tare else None,
        "tare_problem": tare.reason or None,
        "comment": spool.get("comment"),
    }


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def list_spools(
    ctx: Ctx,
    *,
    material: str | None = None,
    color: str | None = None,
    location: str | None = None,
    vendor: str | None = None,
    loaded: bool | None = None,
    archived: bool = False,
) -> list[dict[str, Any]]:
    out = []
    for sp in all_spools(ctx.spoolman, archived=archived):
        v = view(ctx.conn, sp)
        if material and not _ci(v["material"], material):
            continue
        if vendor and not _ci(v["vendor"], vendor):
            continue
        if color and not _color_matches(v, color):
            continue
        if location and not _under(str(v["location"] or ""), location):
            continue
        if loaded is not None and bool(v["slot"]) != loaded:
            continue
        out.append(v)
    return out


def _under(path: str, prefix: str) -> bool:
    """`path` is `prefix` or inside it: WH:A matches WH:A and WH:A/S1, not WH:AB."""
    p, q = path.casefold(), prefix.rstrip("/").casefold()
    return p == q or p.startswith(q + "/") or (q.endswith(":") and p.startswith(q))


def _color_matches(v: Mapping[str, Any], color: str) -> bool:
    """`color` is a hex (#1B5E20) compared with the filament colour, or a word in its name."""
    try:
        if _ci(v["color_hex"], _hex(color)):
            return True
    except SpoolError:
        pass
    return color.casefold() in str(v["name"] or "").casefold()


def show(ctx: Ctx, spool_id: int, *, ledger_rows: int = 50) -> dict[str, Any]:
    sp = get_spool(ctx.spoolman, spool_id)
    v = view(ctx.conn, sp)
    v["ledger"] = [
        r.to_dict() for r in ledger.history(ctx.conn, spool_id, limit=ledger_rows)
    ]
    v["tare_resolution"] = tares.resolve(ctx.conn, sp).to_dict()
    return v


# -- receive ----------------------------------------------------------------------------------


@dataclass
class Receive:
    """A spool arriving (bought, gifted, found on a shelf). Either `filament_id`, or the
    filament described by vendor/material/name/color_hex (matched in Spoolman, else created)."""

    filament_id: int | None = None
    vendor: str | None = None
    material: str | None = None
    name: str | None = None
    color_hex: str | None = None
    density: float | None = None
    diameter: float | None = None
    net_g: float | None = None  # nominal net on the label (filament.weight)
    remaining_g: float | None = None  # already used: estimated remaining now
    remaining_confidence_g: float | None = None
    tare_g: float | None = None  # empty-spool weight, if known for this spool
    tare_source: str | None = None
    spool_kind: str | None = None  # cardboard | plastic | refill (tare lookup)
    lot_nr: str | None = None
    price: float | None = None
    location: str | None = None
    opened: bool = False
    comment: str | None = None
    external_key: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Receive:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise SpoolError(422, f"unknown receive field(s): {', '.join(unknown)}")
        for k, v in known.items():
            want = _RECEIVE_TYPES.get(k, str)
            if v is None or _matches_type(v, want):
                continue
            names = {
                int: "an integer",
                float: "a number",
                bool: "true/false",
                str: "a string",
            }
            what = names[want] if isinstance(want, type) else "a number"
            raise SpoolError(422, f"receive field {k} must be {what}, got {v!r}")
        return cls(**known)

    def check_values(self) -> None:
        """The value checks a receive needs before its first write (422); the JSON types are
        checked by from_dict. Every number a later step would refuse is refused here, so an
        import never fails half-way (after Spoolman already holds some of its spools)."""
        for name in (
            "net_g",
            "remaining_g",
            "remaining_confidence_g",
            "price",
            "tare_g",
        ):
            v = getattr(self, name)
            if v is not None and (not math.isfinite(v) or v < 0):
                raise SpoolError(422, f"{name} must be a finite number ≥ 0, got {v}")
        for name in ("density", "diameter"):
            v = getattr(self, name)
            if v is not None and (not math.isfinite(v) or v <= 0):
                raise SpoolError(422, f"{name} must be a finite number > 0, got {v}")
        if self.spool_kind is not None and self.spool_kind not in tares.SPOOL_KINDS:
            raise SpoolError(
                422, f"spool_kind must be one of {', '.join(tares.SPOOL_KINDS)}"
            )


def _matches_type(v: Any, want: type | tuple[type, ...]) -> bool:
    """`v` has JSON type `want`. bool is an int subclass in Python, but `true` is no filament
    id or price: a bool matches only where bool itself is wanted."""
    return isinstance(v, want) and (want is bool or not isinstance(v, bool))


# JSON types of the non-string Receive fields (int counts as a number)
_NUMBER = (int, float)
_RECEIVE_TYPES: dict[str, type | tuple[type, ...]] = {
    "filament_id": int,
    "opened": bool,
    **dict.fromkeys(
        (
            "density",
            "diameter",
            "net_g",
            "remaining_g",
            "remaining_confidence_g",
            "tare_g",
            "price",
        ),
        _NUMBER,
    ),
}


def _match_vendor(sm: Spoolman, name: str, *, create: bool) -> JSON | None:
    key = tares.vendor_key(name)
    for v in sm.find("vendor"):
        if tares.vendor_key(str(v.get("name", ""))) == key:
            return v
    return sm.create("vendor", {"name": name.strip()}) if create else None


def _filament_spec(r: Receive) -> dict[str, Any]:
    if not r.material or not r.name:
        raise SpoolError(
            422,
            "receive needs filament_id, or at least material and name (plus vendor, color_hex)",
        )
    material = r.material.strip()
    density = r.density or DENSITY.get(material.upper())
    if density is None:
        raise SpoolError(
            422,
            f"no default density for material {material!r}; pass density (g/cm³)",
            detail={"known": sorted(DENSITY)},
        )
    return {
        "material": material,
        "name": r.name.strip(),
        "color_hex": _hex(r.color_hex),
        "density": density,
        "diameter": r.diameter or DEFAULT_DIAMETER,
        "weight": r.net_g,
    }


def match_filament(
    sm: Spoolman, vendor: Mapping[str, Any] | None, spec: Mapping[str, Any]
) -> JSON | None:
    """An existing filament with the same vendor, material, name, colour and (if both set) net."""
    params = {"vendor.id": str(vendor["id"])} if vendor else None
    for f in sm.find("filament", params):
        if vendor is None and f.get("vendor"):
            continue
        if not (
            _ci(f.get("material"), spec["material"])
            and _ci(f.get("name"), spec["name"])
        ):
            continue
        if not _ci(f.get("color_hex"), spec["color_hex"]):
            continue
        if (
            spec.get("weight")
            and f.get("weight")
            and float(f["weight"]) != float(spec["weight"])
        ):
            continue
        return f
    return None


def _resolve_filament(ctx: Ctx, r: Receive) -> tuple[JSON, bool]:
    """(filament, created?)"""
    sm = ctx.spoolman
    if r.filament_id is not None:
        try:
            return sm.get("filament", r.filament_id), False
        except SpoolmanError as e:
            raise SpoolError(404 if e.status == 404 else 502, e.message) from e
    spec = _filament_spec(r)
    vendor = _match_vendor(sm, r.vendor, create=True) if r.vendor else None
    found = match_filament(sm, vendor, spec)
    if found is not None:
        return found, False
    body = {k: v for k, v in spec.items() if v is not None}
    if vendor is not None:
        body["vendor_id"] = vendor["id"]
    return sm.create("filament", body), True


def receive(ctx: Ctx, r: Receive) -> dict[str, Any]:
    """Create the spool in Spoolman (filament/vendor matched or created), place it, anchor the
    ledger when the net is known, record the tare. Returns the spool view (+ `created` flags)."""
    r.check_values()
    loc = (
        _storage(ctx.conn, r.location, "receive into storage, then `load`")
        if r.location
        else None
    )
    sm = ctx.spoolman
    with db.transaction(ctx.conn):
        if r.external_key:
            _claim_key(ctx, r.external_key)
        try:
            fil, fil_created = _resolve_filament(ctx, r)
        except SpoolmanError as e:
            raise SpoolError(502, e.message) from e
        net = r.net_g if r.net_g is not None else _num(fil.get("weight"))
        if r.tare_g is not None:
            tare: tares.Tare | None = tares.Tare(
                r.tare_g,
                GIVEN_TARE_SIGMA if not r.tare_source else tares.MEASURED_SIGMA,
                r.tare_source or "given at receive",
                "given",
            )
        else:
            tare = tares.resolve_new(ctx.conn, fil, spool_kind=r.spool_kind).tare
        extra: dict[str, Any] = {}
        if r.opened:
            extra["opened_at"] = db.utc_now()
        if r.external_key:
            extra["external_key"] = r.external_key
        if loc is not None:
            extra["printops_location_id"] = loc.id
        body: dict[str, Any] = {
            "filament_id": fil["id"],
            "extra": {k: encode_extra(v) for k, v in extra.items()},
        }
        if loc is not None:
            body["location"] = locations.spoolman_path(loc)
        if net is not None:
            body["initial_weight"] = net
        if r.remaining_g is not None:
            body["initial_weight"] = max(net or 0.0, r.remaining_g)
            body["remaining_weight"] = r.remaining_g
        if tare is not None:
            body["spool_weight"] = tare.grams
        for k in ("lot_nr", "price", "comment"):
            if getattr(r, k) is not None:
                body[k] = getattr(r, k)
        try:
            spool = sm.create("spool", body)
        except SpoolmanError as e:
            raise SpoolError(502 if e.status in (0, 500) else 422, e.message) from e
        sid = int(spool["id"])
        if r.external_key:
            ctx.conn.execute(
                "INSERT INTO spool_import (external_key, spool_id, at, actor) VALUES (?, ?, ?, ?)",
                (r.external_key, sid, db.utc_now(), ctx.actor),
            )
        if tare is not None:
            tares.record_choice(ctx.conn, sid, tare, actor=ctx.actor)
        if r.remaining_g is not None:
            conf = r.remaining_confidence_g
            if conf is None:
                conf = max(
                    MANUAL_REMAINING_MIN_G, r.remaining_g * MANUAL_REMAINING_FRACTION
                )
            ledger.anchor(
                ctx.conn,
                sid,
                r.remaining_g,
                "manual",
                conf,
                actor=ctx.actor,
                note="received part-used",
            )
        elif net is not None:
            ledger.anchor(
                ctx.conn,
                sid,
                net,
                "receive",
                r.remaining_confidence_g or ledger.receive_sigma(net),
                actor=ctx.actor,
                note="received sealed",
            )
        ev.emit(
            ctx.conn,
            "spool.received",
            f"spool:{sid}",
            {
                "filament_id": fil["id"],
                "filament_created": fil_created,
                "location": body.get("location"),
                "net_g": net,
                "remaining_g": r.remaining_g,
                "tare_g": tare.grams if tare else None,
                "external_key": r.external_key,
            },
            ctx.actor,
        )
    spool = ledger.sync_confidence(ctx.conn, sm, sid, spool)
    return {**view(ctx.conn, spool), "filament_created": fil_created}


def _claim_key(ctx: Ctx, key: str) -> None:
    """Refuse an external key that already names a spool (409). Runs inside the receive
    transaction (BEGIN IMMEDIATE), so two concurrent imports of one key cannot both pass; a key
    whose spool was deleted in Spoolman by hand is released."""
    row = ctx.conn.execute(
        "SELECT spool_id FROM spool_import WHERE external_key = ?", (key,)
    ).fetchone()
    if row is None:
        return
    sid = int(row["spool_id"])
    try:
        ctx.spoolman.get("spool", sid)
    except SpoolmanError as e:
        if e.status != 404:
            raise SpoolError(502, e.message) from e
        ctx.conn.execute("DELETE FROM spool_import WHERE external_key = ?", (key,))
        return
    raise SpoolError(
        409, f"spool #{sid} already has external key {key!r}", detail={"spool_id": sid}
    )


# -- placement ---------------------------------------------------------------------------------


def _location(conn: sqlite3.Connection, ref: str) -> locations.Location:
    try:
        return locations.resolve(conn, ref)
    except LookupError as e:
        raise SpoolError(404, f"unknown location {ref!r}") from e


FLEET_KINDS = ("printer", "ams", "printer_slot")


def _storage(conn: sqlite3.Connection, ref: str, hint: str) -> locations.Location:
    """A storage location (not a printer / printer slot, which only `load` may use)."""
    loc = _location(conn, ref)
    if loc.kind in FLEET_KINDS:
        raise SpoolError(422, f"{loc.path} is a printer slot, not storage: {hint}")
    return loc


def _slot_location(ctx: Ctx, slot: SlotRef) -> locations.Location:
    """The `slot:<p>/<head>` location, after printops.locations has synced the fleet slots
    (a cheap stat when fleet/printers.yaml did not change)."""
    locations.ensure_fleet_slots(ctx.conn, ctx.spoolman, actor=ctx.actor)
    return _location(ctx.conn, slot.path)


def _place(
    ctx: Ctx, spool_id: int, loc: locations.Location | None, extra: Mapping[str, Any]
) -> JSON:
    try:
        return locations.place_spool(ctx.conn, ctx.spoolman, spool_id, loc, extra=extra)
    except SpoolmanError as e:
        raise SpoolError(502, e.message) from e
    except ValueError as e:  # LocationError: archived location
        raise SpoolError(409, str(e)) from e


def _emit_unloaded(
    ctx: Ctx, spool_id: int, slot: str, to: str | None, why: str
) -> None:
    printer, _, head = slot.partition("/")
    ev.emit(
        ctx.conn,
        "slot.unloaded",
        f"slot:{slot}",
        {"printer": printer, "head": head, "spool_id": spool_id, "to": to, "why": why},
        ctx.actor,
    )


def _live_spool(ctx: Ctx, spool_id: int) -> JSON:
    """The spool, refused (409) when archived. Call it INSIDE the write transaction: every
    3d-api writer takes the SQLite write lock first, so a concurrent `empty` cannot slip in
    between this check and the caller's writes."""
    sp = get_spool(ctx.spoolman, spool_id)
    if sp.get("archived"):
        raise SpoolError(409, f"spool #{spool_id} is archived (empty)")
    return sp


def move(ctx: Ctx, spool_id: int, location: str) -> dict[str, Any]:
    """Store/move a spool to a storage location; a loaded spool is unloaded on the way."""
    loc = _storage(ctx.conn, location, "use `load` (it checks the head)")
    with db.transaction(ctx.conn):
        sp = _live_spool(ctx, spool_id)
        was = slot_of(sp)
        if was:
            _emit_unloaded(ctx, spool_id, was, loc.path, "moved")
        ev.emit(
            ctx.conn,
            "spool.moved",
            f"spool:{spool_id}",
            {"from": sp.get("location"), "to": loc.path, "location_id": loc.id},
            ctx.actor,
        )
        sp = _place(ctx, spool_id, loc, {"printops_slot": ""} if was else {})
    return view(ctx.conn, sp)


@dataclass(frozen=True)
class SlotRef:
    printer: str
    head: str  # head label (T3) or "<ams-id>/<n>"

    @property
    def key(self) -> str:
        return f"{self.printer}/{self.head}"

    @property
    def path(self) -> str:
        return f"slot:{self.key}"

    @property
    def is_head(self) -> bool:
        """A print head (`T3`), not an AMS-like slot (`ams1/2`)."""
        return "/" not in self.head


def check_slot(printer: str, head: str, *, usable: bool = True) -> SlotRef:
    """Validate a slot against the fleet registry: known printer, known head (or AMS slot),
    head usable (unless `usable=False`: taking a spool OUT of a broken head is fine). Returns the
    canonical slot (`t3` → `T3`, `ams1/02` → `ams1/2`). Raises SpoolError 404/409/503."""
    from printops.fleet import FleetError, load_fleet

    try:
        fleet = load_fleet()
    except FleetError as e:
        raise SpoolError(503, f"fleet registry unreadable: {e}") from e
    if fleet is None:
        raise SpoolError(
            503, "no fleet registry (fleet/printers.yaml): cannot check the head"
        )
    p = fleet.printers.get(printer)
    if p is None:
        raise SpoolError(
            404,
            f"unknown printer {printer!r}; fleet has: {', '.join(sorted(fleet.printers))}",
        )
    h = next((x for x in p.heads if x.label.casefold() == head.casefold()), None)
    if h is not None:
        if usable and not h.usable:
            raise SpoolError(
                409,
                f"{printer} {h.label} is not usable: {h.reason or 'marked unusable in the fleet'}",
                detail={"printer": printer, "head": h.label, "reason": h.reason},
            )
        return SlotRef(printer, h.label)
    unit, _, n = head.partition("/")
    for ams in p.ams or ():
        if ams.id == unit and n.isdigit() and 1 <= int(n) <= ams.slots:
            return SlotRef(printer, f"{ams.id}/{int(n)}")
    heads = ", ".join(x.label for x in p.heads)
    raise SpoolError(404, f"{printer} has no slot {head!r}; heads: {heads or '-'}")


def spool_in_slot(sm: Spoolman, slot: SlotRef) -> list[JSON]:
    """Spools whose `printops_slot` is `slot` (case-insensitive: a slot of a printer that
    left the fleet cannot be canonicalised by check_slot)."""
    key = slot.key.casefold()
    return [sp for sp in all_spools(sm) if slot_of(sp).casefold() == key]


def load(
    ctx: Ctx, printer: str, head: str, spool_id: int, *, replace_to: str | None = None
) -> dict[str, Any]:
    """Bind a spool to a printer slot (`printops_slot` + location `slot:<p>/<head>`)."""
    slot = check_slot(printer, head)
    loc = _slot_location(ctx, slot)
    # Occupancy is read from Spoolman INSIDE the write transaction: every 3d-api writer takes
    # the same SQLite write lock first, so two concurrent loads into one slot are serialised and
    # the second sees the first's spool.
    with db.transaction(ctx.conn):
        sp = _live_spool(ctx, spool_id)
        # db.transaction is BEGIN IMMEDIATE: this read holds the write lock, so a fault committed
        # by a concurrent request is seen. Only head faults gate a load (a printer-wide fault —
        # head NULL — and AMS slots do not).
        faults = maintenance.list_faults(ctx.conn, slot.printer, head=slot.head) if slot.is_head else []
        if faults:
            named = [
                {"id": fault["id"], "symptom": maintenance.one_line(fault["symptom"], FAULT_SYMPTOM_CHARS)}
                for fault in faults[:FAULTS_NAMED]
            ]
            shown = "; ".join(f"#{n['id']} {n['symptom']}" for n in named)
            more = f" (+{len(faults) - len(named)} more)" if len(faults) > len(named) else ""
            raise SpoolError(
                409,
                f"{slot.key} has {len(faults)} open fault(s): {shown}{more} — close all of them before loading",
                detail={"printer": slot.printer, "head": slot.head, "open_faults": len(faults), "faults": named},
            )
        occupants = [
            o for o in spool_in_slot(ctx.spoolman, slot) if int(o["id"]) != spool_id
        ]
        if occupants and replace_to is None:
            o = occupants[0]
            raise SpoolError(
                409,
                f"{slot.key} already holds spool #{o['id']} ({view(ctx.conn, o)['name']}); "
                "unload it first, or pass replace_to (where it goes)",
                detail={"occupant": o["id"]},
            )
        back = (
            _storage(ctx.conn, replace_to, "replace_to is where the current spool goes")
            if occupants and replace_to
            else None
        )
        was = slot_of(sp)
        for o in occupants:
            _emit_unloaded(
                ctx, int(o["id"]), slot.key, back.path if back else None, "replaced"
            )
            _place(ctx, int(o["id"]), back, {"printops_slot": ""})
        if was and was != slot.key:
            _emit_unloaded(ctx, spool_id, was, slot.path, "moved to another slot")
        extra: dict[str, Any] = {"printops_slot": slot.key}
        if not extra_value(sp, "opened_at"):
            extra["opened_at"] = db.utc_now()
        ev.emit(
            ctx.conn,
            "slot.loaded",
            slot.path,
            {
                "printer": slot.printer,
                "head": slot.head,
                "spool_id": spool_id,
                "replaced": [int(o["id"]) for o in occupants],
            },
            ctx.actor,
        )
        sp = _place(ctx, spool_id, loc, extra)
    return view(ctx.conn, sp)


def unload(
    ctx: Ctx,
    printer: str,
    head: str,
    *,
    to: str | None = None,
    gross_g: float | None = None,
    spool_id: int | None = None,
) -> dict[str, Any]:
    """Take the spool out of a slot (to `to`, else nowhere); optionally weigh it first.

    With `gross_g` the weighing is committed BEFORE the unload (its own transaction): a
    weighing that fails (no tare, Spoolman down) leaves the slot as it was, and an unload that
    fails after it leaves a correctly weighed spool still loaded — retry the unload alone. One
    transaction cannot cover both, because each is its own Spoolman write. Residual race: a
    request that moves the spool between the two steps wins; the unload then answers 404 and
    the weighing (still true) stays booked."""
    try:
        slot = check_slot(printer, head, usable=False)
    except SpoolError as e:
        if e.status not in (404, 503):
            raise
        # a printer/head that left the fleet (or no readable fleet) can still be emptied; the
        # raw name matches the stored slot case-insensitively (spool_in_slot)
        slot = SlotRef(printer, head)
    loc = (
        _storage(
            ctx.conn, to, "unload goes to storage; load into the other slot instead"
        )
        if to
        else None
    )
    drift: float | None = None
    if gross_g is not None:
        bound = _bound(ctx, slot, spool_id)
        if len(bound) > 1:
            raise SpoolError(
                409,
                f"{slot.key} holds {len(bound)} spools in Spoolman; pass spool_id to say which "
                "one is on the scale",
                detail={"spool_ids": [int(b["id"]) for b in bound]},
            )
        weighed = measure(ctx, int(bound[0]["id"]), gross_g, note="weighed at unload")
        drift = weighed["drift_g"]
        spool_id = int(bound[0]["id"])
    with db.transaction(ctx.conn):
        placed: list[JSON] = []
        # read the slot again under the write lock: the weighing above committed on its own,
        # so the slot may have changed since. >1 only if Spoolman was edited by hand; unload all
        for sp in _bound(ctx, slot, spool_id):
            sid = int(sp["id"])
            _emit_unloaded(ctx, sid, slot_of(sp), loc.path if loc else None, "unloaded")
            placed.append(_place(ctx, sid, loc, {"printops_slot": ""}))
    out = view(ctx.conn, placed[-1])
    if gross_g is not None:
        out["drift_g"] = drift
    return out


def _bound(ctx: Ctx, slot: SlotRef, spool_id: int | None) -> list[JSON]:
    """The spools Spoolman has in `slot` (just `spool_id`, when given); 404 when none."""
    bound = spool_in_slot(ctx.spoolman, slot)
    if spool_id is not None:
        bound = [b for b in bound if int(b["id"]) == spool_id]
    if not bound:
        raise SpoolError(
            404,
            f"no spool is loaded in {slot.key}"
            + (f" as #{spool_id}" if spool_id else ""),
        )
    return bound


# -- usage, weighing, empty ---------------------------------------------------------------------


def use(
    ctx: Ctx,
    spool_id: int,
    grams: float,
    source: str,
    *,
    confidence_g: float | None = None,
    job_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    sp = get_spool(ctx.spoolman, spool_id)
    if sp.get("archived"):
        raise SpoolError(409, f"spool #{spool_id} is archived (empty)")
    try:
        row, sp = ledger.book(
            ctx.conn,
            ctx.spoolman,
            spool_id,
            grams,
            source,
            confidence_g,
            job_id,
            note,
            actor=ctx.actor,
        )
    except ledger.LedgerError as e:
        raise SpoolError(422, str(e)) from e
    except SpoolmanError as e:
        raise SpoolError(502, e.message) from e
    return {**view(ctx.conn, sp), "ledger_row": row.to_dict()}


def measure(
    ctx: Ctx,
    spool_id: int,
    gross_g: float,
    *,
    tare_g: float | None = None,
    tare_source: str | None = None,
    scale_sigma_g: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Weigh the spool (gross, with the empty spool): one Spoolman write + an absolute ledger
    anchor whose ± is the scale's and the tare's, which resets the accumulated error.

    The Spoolman write is `/measure` when Spoolman already holds this tare (the usual case:
    receive and earlier weighings write it); otherwise one PATCH sets spool_weight together
    with the remaining weight /measure would compute (and raises initial_weight the way
    /measure does for a spool heavier than its initial weight). Never two writes, so a
    Spoolman failure leaves Spoolman untouched and rolls the ledger row back."""
    scale = SCALE_SIGMA if scale_sigma_g is None else scale_sigma_g
    if scale < 0 or not math.isfinite(scale):
        raise SpoolError(422, "scale_sigma_g must be a finite number ≥ 0")
    sm = ctx.spoolman
    with db.transaction(ctx.conn):
        sp = get_spool(sm, spool_id)
        tare = _weighing_tare(ctx.conn, sp, gross_g, tare_g, tare_source)
        net = gross_g - tare.grams
        sigma = math.hypot(scale, tare.sigma)
        if net < -2 * sigma:
            raise SpoolError(
                422,
                f"{gross_g:g} g is less than the tare {tare.grams:g} g ± {tare.sigma:g}: wrong spool "
                "or wrong tare",
            )
        net = max(net, 0.0)
        before = ledger.estimate(
            ctx.conn, spool_id, fallback_g=_num(sp.get("remaining_weight"))
        )
        if tare.origin not in (
            "choice",
            "spool",
        ):  # remember where the tare we write came from
            tares.record_choice(ctx.conn, spool_id, tare, actor=ctx.actor)
        row_id = ledger.anchor(
            ctx.conn,
            spool_id,
            net,
            "weigh",
            sigma,
            actor=ctx.actor,
            gross_g=gross_g,
            tare_g=tare.grams,
            note=note,
        )
        ev.emit(
            ctx.conn,
            "spool.measured",
            f"spool:{spool_id}",
            {
                "gross_g": gross_g,
                "tare_g": tare.grams,
                "tare_source": tare.source,
                "net_g": round(net, 1),
                "sigma_g": round(sigma, 1),
                "previous": before.to_dict(),
                "ledger_id": row_id,
            },
            ctx.actor,
        )
        same_tare = (
            sp.get("spool_weight") is not None
            and abs(float(sp["spool_weight"]) - tare.grams) < 0.05
        )
        own_initial = _num(sp.get("initial_weight")) or 0.0
        initial = own_initial or _num((sp.get("filament") or {}).get("weight")) or 0.0
        try:
            if same_tare and own_initial > 0:
                sp = sm.measure(spool_id, gross_g)
            else:
                sp = sm.update(
                    "spool",
                    spool_id,
                    {
                        "spool_weight": tare.grams,
                        "initial_weight": max(initial, net),
                        "remaining_weight": net,
                    },
                )
        except SpoolmanError as e:
            raise SpoolError(502, e.message) from e
    sp = ledger.sync_confidence(ctx.conn, sm, spool_id, sp)
    out = view(ctx.conn, sp)
    out["drift_g"] = round(net - before.mid, 1) if before.anchored else None
    return out


def _weighing_tare(
    conn: sqlite3.Connection,
    sp: Mapping[str, Any],
    gross_g: float,
    tare_g: float | None,
    tare_source: str | None,
) -> tares.Tare:
    """The tare a weighing of `sp` subtracts (given, else the tare chain); SpoolError 422/409."""
    if not math.isfinite(gross_g) or gross_g <= 0:
        raise SpoolError(422, f"gross_g must be > 0, got {gross_g}")
    if tare_g is not None:
        if not math.isfinite(tare_g) or tare_g <= 0:
            raise SpoolError(422, "tare_g must be > 0")
        return tares.Tare(
            tare_g,
            tares.MEASURED_SIGMA if tare_source else GIVEN_TARE_SIGMA,
            tare_source or "given at weighing",
            "given",
        )
    res = tares.resolve(conn, sp)
    if res.tare is None:
        raise SpoolError(
            409,
            f"spool #{sp['id']}: no tare — {res.reason}. Weigh with tare_g, or add the "
            "vendor's empty-spool weight (`3d printops tare add`)",
            detail=res.to_dict(),
        )
    return res.tare


def empty(ctx: Ctx, spool_id: int, *, note: str | None = None) -> dict[str, Any]:
    """The spool is used up: anchor remaining 0, unload/unplace it, archive it in Spoolman."""
    sm = ctx.spoolman
    with db.transaction(ctx.conn):
        sp = _live_spool(ctx, spool_id)
        before = ledger.estimate(
            ctx.conn, spool_id, fallback_g=_num(sp.get("remaining_weight"))
        )
        was = slot_of(sp)
        ledger.anchor(
            ctx.conn,
            spool_id,
            0.0,
            "manual",
            0.0,
            actor=ctx.actor,
            note=note or "empty",
        )
        if was:
            _emit_unloaded(ctx, spool_id, was, None, "empty")
        ev.emit(
            ctx.conn,
            "spool.empty",
            f"spool:{spool_id}",
            {
                "leftover_estimate": before.to_dict(),
                "location": sp.get("location"),
                "slot": was or None,
            },
            ctx.actor,
        )
        _place(ctx, spool_id, None, {"printops_slot": ""})
        try:
            body: dict[str, Any] = {"archived": True}
            if sp.get("initial_weight"):
                body["remaining_weight"] = 0
            sp = sm.update("spool", spool_id, body)
        except SpoolmanError as e:
            raise SpoolError(502, e.message) from e
    return view(ctx.conn, sp)


# -- import -------------------------------------------------------------------------------------


def import_spools(
    ctx: Ctx, items: Iterable[Mapping[str, Any]], *, dry_run: bool
) -> dict[str, Any]:
    """Idempotent import keyed by `external_key`: a key already in Spoolman (archived too) is
    left alone and only reported (with fields that drifted from the source); a new key is
    received. Every item is validated (field types and values, filament spec, location, unique
    keys) before the first write, so a bad item fails the whole request instead of
    half-importing it. dry_run computes the same plan without writing."""
    parsed = [Receive.from_dict(raw) for raw in items]
    seen: set[str] = set()
    for r in parsed:
        if not r.external_key:
            raise SpoolError(422, "every imported spool needs an external_key")
        if r.external_key in seen:
            raise SpoolError(
                422, f"external_key {r.external_key!r} appears twice in the import"
            )
        seen.add(r.external_key)
        r.check_values()
        if r.filament_id is None:
            _filament_spec(r)
        if r.location:
            _storage(ctx.conn, r.location, "import into storage")
    existing = {
        extra_value(sp, "external_key"): sp
        for sp in all_spools(ctx.spoolman, archived=True)
        if extra_value(sp, "external_key")
    }
    actions: list[dict[str, Any]] = []
    for r in parsed:
        assert r.external_key is not None
        have = existing.get(r.external_key)
        if have is not None:
            fil = have.get("filament") or {}
            want = _filament_spec(r) if r.filament_id is None else {}
            differs = [
                f"{k}: {fil.get(k)!r} in Spoolman, {want[k]!r} in the source"
                for k in ("material", "name", "color_hex")
                if k in want and not _ci(fil.get(k), want[k])
            ]
            if r.vendor and not _ci((fil.get("vendor") or {}).get("name"), r.vendor):
                differs.append(
                    f"vendor: {(fil.get('vendor') or {}).get('name')!r} in Spoolman, {r.vendor!r} in the source"
                )
            actions.append(
                {
                    "key": r.external_key,
                    "action": "exists",
                    "spool_id": have["id"],
                    "differs": differs,
                }
            )
            continue
        if dry_run:
            spec = (
                _filament_spec(r)
                if r.filament_id is None
                else {"filament_id": r.filament_id}
            )
            vendor = (
                _match_vendor(ctx.spoolman, r.vendor, create=False)
                if r.vendor
                else None
            )
            fil = (
                match_filament(ctx.spoolman, vendor, spec)
                if r.filament_id is None
                else None
            )
            actions.append(
                {
                    "key": r.external_key,
                    "action": "create",
                    "vendor": r.vendor,
                    "vendor_exists": vendor is not None or not r.vendor,
                    "filament": spec,
                    "filament_id": fil["id"] if fil else r.filament_id,
                    "location": r.location,
                    "price": r.price,
                    "net_g": r.net_g,
                }
            )
            continue
        created = receive(ctx, r)
        actions.append(
            {
                "key": r.external_key,
                "action": "created",
                "spool_id": created["id"],
                "filament_id": created["filament_id"],
                "filament_created": created["filament_created"],
                "location": created["location"],
            }
        )
    counts: dict[str, int] = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    changes = counts.get("create", 0) + counts.get("created", 0)
    return {
        "dry_run": dry_run,
        "changes": changes,
        "counts": counts,
        "actions": actions,
    }
