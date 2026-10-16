"""calibrations.py — calibration records, the requirement engine and the `calibration_current` gate.

ACCESSED VIA: 3d-api routes_calibrations.py (`/v1/calibrations…`) → `3d calibrate` and, through
the same REST call, print-preflight's PrintOps checks (#164). Issue #167, plan §7.2 and §8.

RECORDS (table `calibration`, migration 0080): a row is created `planned` when a run starts and
becomes `recorded` with its result values; a manual entry is created recorded. The scope
(printer, head, nozzle, plate, filament + lot) is SNAPSHOT at creation — the dimensions the
catalog (fleet/calibrations.yaml) lists for the kind, nothing else. A newer record for the
identical scope supersedes the older one (`superseded_by`). Past records imported from the
settings-history buffer (#264, printops.settings_history) keep their measurement time as
`recorded_at`, and superseding between them follows that time (`insert_past`).

REQUIREMENT ENGINE (plan §8.1): for a printer head and the spool bound to it (Spoolman
`printops_slot`, #160) every catalog kind of the printer's model gets a status:
  current            a valid record matches every scope dimension
  assumed            baseline `assumed` kind, never recorded and never invalidated (factory state)
  new_lot            a valid record exists for this filament, but for another (or unknown) lot
  invalidated        the matching record (or, for an `assumed` kind, the factory state) is older
                     than a `calibration.invalidated` event for its printer/head
  expired            the matching record is past `valid_until`
  new_nozzle         records for this filament exist only for another nozzle
  new_filament       records exist for this vendor + material, none for this filament product
  new_vendor         records exist for this material type, none from this vendor
  new_material_type  no record for this material type at all
  no_spool           the kind needs a filament and no spool is bound to the head (UNVERIFIED)
The policy (fleet/policy.yaml `calibrations:`) turns a reason into block (FAIL) / warn / off.

INVALIDATION (#166 contract): event kind `calibration.invalidated`, payload {printer, head|null,
trigger, kinds|null, reason, maintenance_id|null, nozzle_install_id|null}. It voids every record
of the printer (head-scoped kinds: of that head, or of every head when head is null) recorded
before the event, for the listed kinds — or, with kinds null, every kind whose catalog
`invalidated_by` lists the trigger. Filament-only kinds (temp tower) are never invalidated.

GATE `calibration_current` (plan §8.2 step 5): the requirements of every head the G-code uses,
plus a footer comparison: Orca-family slicers write `; pressure_advance = …`,
`; filament_flow_ratio = …`, `; filament_max_volumetric_speed = …` per filament. A value that
differs from the current record is `gcode_mismatch`. Observed on Snapmaker Orca, Orca and
Bambu Studio exports (babushkin-dom print/, 2026-10): a multi-filament file may carry ONE value
for these keys — the first filament's — so for the other tools the value is UNVERIFIED, never
assumed equal. `enable_pressure_advance = 0` means the G-code leaves K to the firmware: PASS
when the record was applied to Klipper (`applied_to`), WARN otherwise. A FAIL can be accepted
per kind with a reason (`--accept calibration:<kind> --reason …`); every acceptance is logged as
a `calibration.accepted` event.

NOZZLE: `printops.maintenance.current_nozzle_install(conn, printer, head)` (#166) when that
module exists, else the head's nozzle in fleet/printers.yaml. Records compare by install id when
both sides have one, else by diameter/material/type (unknown on either side = no conflict).
"""

from __future__ import annotations

import datetime
import importlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from printops import db
from printops import events as ev
from printops.calibration_catalog import Catalog, CatalogError, Kind
from printops.calibration_policy import CalibrationPolicy
from printops.fleet import Fleet, FleetPrinter, Head
from printops.spoolman import JSON, Spoolman, SpoolmanError

PASS, WARN, FAIL, UNVERIFIED, OFF = "PASS", "WARN", "FAIL", "UNVERIFIED", "OFF"
_RANK = {PASS: 0, OFF: 0, UNVERIFIED: 1, WARN: 2, FAIL: 3}
_POLICY_LEVEL = {"block": FAIL, "warn": WARN, "off": OFF}
STATUSES = ("planned", "recorded", "abandoned")
INVALIDATED = "calibration.invalidated"
ACCEPT_PREFIX = "calibration:"
_TRUE = {"1", "true", "yes", "on"}


class CalibrationError(Exception):
    """A request that cannot be done; `status` is the HTTP status 3d-api answers."""

    def __init__(self, status: int, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = dict(detail or {})


@dataclass
class Ctx:
    conn: sqlite3.Connection
    spoolman: Spoolman | None
    actor: str
    catalog: Catalog
    policy: CalibrationPolicy
    fleet: Fleet


# -- scope ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Nozzle:
    install_id: int | None
    diameter_mm: float | None
    material: str | None
    type: str | None

    def same(self, other: Nozzle) -> bool:
        if self.install_id is not None and other.install_id is not None:
            return self.install_id == other.install_id
        pairs = ((self.diameter_mm, other.diameter_mm), (self.material, other.material), (self.type, other.type))
        return all(a is None or b is None or a == b for a, b in pairs)

    def label(self) -> str:
        parts = [f"{self.diameter_mm:g} mm" if self.diameter_mm else "", self.material or "", self.type or ""]
        text = " ".join(p for p in parts if p) or "unknown nozzle"
        return f"{text} (install #{self.install_id})" if self.install_id is not None else text

    def to_dict(self) -> dict[str, Any]:
        return {"install_id": self.install_id, "diameter_mm": self.diameter_mm,
                "material": self.material, "type": self.type}


@dataclass(frozen=True)
class Filament:
    filament_id: int | None
    name: str | None
    vendor: str | None
    material: str | None
    lot_nr: str | None
    spool_id: int | None = None

    @classmethod
    def from_filament(cls, fil: Mapping[str, Any], *, lot_nr: str | None, spool_id: int | None) -> Filament:
        vendor = fil.get("vendor") or {}
        material = fil.get("material")
        return cls(
            filament_id=int(fil["id"]) if fil.get("id") is not None else None,
            name=fil.get("name") or None,
            vendor=(vendor.get("name") if isinstance(vendor, dict) else None) or None,
            material=str(material).strip().upper() if material else None,
            lot_nr=_lot(lot_nr),
            spool_id=spool_id,
        )

    @classmethod
    def from_spool(cls, spool: Mapping[str, Any]) -> Filament:
        return cls.from_filament(spool.get("filament") or {}, lot_nr=spool.get("lot_nr"),
                                 spool_id=int(spool["id"]))

    def label(self) -> str:
        what = " ".join(x for x in (self.vendor, self.name or self.material) if x) or "unknown filament"
        return what + (f" lot {self.lot_nr}" if self.lot_nr else "")

    def to_dict(self) -> dict[str, Any]:
        return {"filament_id": self.filament_id, "name": self.name, "vendor": self.vendor,
                "material": self.material, "lot_nr": self.lot_nr, "spool_id": self.spool_id}


def _lot(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _ci(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


# -- records ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Calibration:
    id: int
    kind: str
    status: str
    printer: str | None
    head: str | None
    nozzle: Nozzle | None
    plate: str | None
    filament: Filament | None
    method: str
    params: dict[str, Any]
    gcode_path: str | None
    gcode_sha: str | None
    values: dict[str, Any] | None
    evidence: list[Any]
    applied_to: list[dict[str, Any]]
    note: str | None
    created_at: str
    created_by: str
    recorded_at: str | None
    recorded_by: str | None
    valid_until: str | None
    superseded_by: int | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Calibration:
        has_nozzle = any(r[k] is not None for k in ("nozzle_install_id", "nozzle_diameter_mm",
                                                     "nozzle_material", "nozzle_type"))
        has_fil = r["filament_id"] is not None or r["material"] is not None
        return cls(
            id=int(r["id"]),
            kind=str(r["kind"]),
            status=str(r["status"]),
            printer=r["printer"],
            head=r["head"],
            nozzle=Nozzle(r["nozzle_install_id"], r["nozzle_diameter_mm"], r["nozzle_material"],
                          r["nozzle_type"]) if has_nozzle else None,
            plate=r["plate"],
            filament=Filament(r["filament_id"], r["filament_name"], r["vendor"], r["material"],
                              r["lot_nr"], r["spool_id"]) if has_fil else None,
            method=str(r["method"]),
            params=json.loads(r["params"]),
            gcode_path=r["gcode_path"],
            gcode_sha=r["gcode_sha"],
            values=json.loads(r["values_json"]) if r["values_json"] is not None else None,
            evidence=json.loads(r["evidence"]),
            applied_to=json.loads(r["applied_to"]),
            note=r["note"],
            created_at=str(r["created_at"]),
            created_by=str(r["created_by"]),
            recorded_at=r["recorded_at"],
            recorded_by=r["recorded_by"],
            valid_until=r["valid_until"],
            superseded_by=r["superseded_by"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "printer": self.printer, "head": self.head,
            "nozzle": self.nozzle.to_dict() if self.nozzle else None,
            "plate": self.plate,
            "filament": self.filament.to_dict() if self.filament else None,
            "method": self.method, "params": self.params,
            "gcode_path": self.gcode_path, "gcode_sha": self.gcode_sha,
            "values": self.values, "evidence": self.evidence, "applied_to": self.applied_to,
            "note": self.note, "created_at": self.created_at, "created_by": self.created_by,
            "recorded_at": self.recorded_at, "recorded_by": self.recorded_by,
            "valid_until": self.valid_until, "superseded_by": self.superseded_by,
        }

    def applied_to_firmware(self) -> dict[str, Any] | None:
        return next((a for a in reversed(self.applied_to)
                     if str(a.get("target", "")).startswith("klipper:")), None)


def get(conn: sqlite3.Connection, cal_id: int) -> Calibration:
    row = conn.execute("SELECT * FROM calibration WHERE id = ?", (cal_id,)).fetchone()
    if row is None:
        raise CalibrationError(404, f"no calibration #{cal_id}")
    return Calibration.from_row(row)


def list_calibrations(
    conn: sqlite3.Connection,
    *,
    printer: str | None = None,
    head: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    current: bool = False,
    limit: int = 100,
) -> list[Calibration]:
    """Newest first. `current` = recorded and not superseded (validity is the engine's call)."""
    if status is not None and status not in STATUSES:
        raise CalibrationError(422, f"status must be one of {', '.join(STATUSES)}, got {status!r}")
    where: list[str] = ["1 = 1"]
    args: list[str | int] = []
    for col, val in (("printer", printer), ("kind", kind), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if head is not None:
        where.append("head = ? COLLATE NOCASE")
        args.append(head)
    if current:
        where.append("status = 'recorded' AND superseded_by IS NULL")
    args.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        f"SELECT * FROM calibration WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", args
    ).fetchall()
    return [Calibration.from_row(r) for r in rows]


# -- fleet / Spoolman lookups -----------------------------------------------------------------


def fleet_printer(fleet: Fleet, name: str) -> FleetPrinter:
    p = fleet.printers.get(name)
    if p is None:
        raise CalibrationError(404, f"unknown printer {name!r}; fleet has: {', '.join(sorted(fleet.printers))}")
    return p


def fleet_head(p: FleetPrinter, label: str) -> Head:
    h = next((x for x in p.heads if x.label.casefold() == label.casefold()), None)
    if h is None:
        raise CalibrationError(404, f"{p.name} has no head {label!r}; heads: "
                                    f"{', '.join(x.label for x in p.heads) or '-'}")
    return h


def current_nozzle(conn: sqlite3.Connection, printer: str, head: Head) -> Nozzle | None:
    """The nozzle on a head now: #166's install table when present, else the fleet file."""
    try:
        maintenance = importlib.import_module("printops.maintenance")
    except ModuleNotFoundError as e:
        if e.name != "printops.maintenance":
            raise
        maintenance = None
    fn = getattr(maintenance, "current_nozzle_install", None)
    if fn is not None:
        row = fn(conn, printer, head.label)
        if row:
            return Nozzle(row.get("id"), row.get("diameter_mm"), row.get("material"), row.get("type"))
    n = head.nozzle
    return Nozzle(None, n.diameter_mm, n.material, n.type) if n else None


def _plate(p: FleetPrinter, plate: str | None, *, required: bool) -> str | None:
    if plate is not None:
        if p.plates is not None and plate not in p.plates:
            raise CalibrationError(422, f"{p.name} has no plate {plate!r}; plates: {', '.join(p.plates) or '-'}")
        return plate
    if p.plates is not None and len(p.plates) == 1:
        return p.plates[0]
    if required and p.plates:
        raise CalibrationError(422, f"{p.name} has several plates ({', '.join(p.plates)}): pass plate")
    return None


def _spool(sm: Spoolman | None, spool_id: int) -> JSON:
    if sm is None:
        raise CalibrationError(503, "Spoolman is not configured (set SPOOLMAN_URL)")
    try:
        return sm.get("spool", spool_id)
    except SpoolmanError as e:
        if e.status == 404:
            raise CalibrationError(404, f"no spool #{spool_id} in Spoolman") from e
        raise CalibrationError(502, e.message) from e


def _filament(sm: Spoolman | None, filament_id: int) -> JSON:
    if sm is None:
        raise CalibrationError(503, "Spoolman is not configured (set SPOOLMAN_URL)")
    try:
        return sm.get("filament", filament_id)
    except SpoolmanError as e:
        if e.status == 404:
            raise CalibrationError(404, f"no filament #{filament_id} in Spoolman") from e
        raise CalibrationError(502, e.message) from e


@dataclass(frozen=True)
class Bound:
    """What sits on a head: the spool (None = nothing bound) or why that is not known."""

    filament: Filament | None
    note: str | None = None


def bound_filament(sm: Spoolman | None, printer: str, head: Head) -> Bound:
    if sm is None:
        return Bound(None, "Spoolman is not configured: the loaded spool is unknown")
    from printops.spools import SlotRef, SpoolError, spool_in_slot

    try:
        found = spool_in_slot(sm, SlotRef(printer, head.label))
    except SpoolError as e:
        return Bound(None, f"Spoolman unreachable: {e.message}")
    if not found:
        return Bound(None, f"no spool bound to {printer}/{head.label} (`3d printops spool load`)")
    if len(found) > 1:
        ids = ", ".join(f"#{s['id']}" for s in found)
        return Bound(None, f"{len(found)} spools bound to {printer}/{head.label} ({ids}): fix the binding")
    return Bound(Filament.from_spool(found[0]))


# -- runs and results -------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRequest:
    kind: str
    method: str
    printer: str | None = None
    head: str | None = None
    spool_id: int | None = None
    filament_id: int | None = None
    lot_nr: str | None = None
    plate: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    gcode_path: str | None = None
    gcode_sha: str | None = None
    note: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunRequest:
        def text(key: str, *, required: bool = False) -> str | None:
            v = data.get(key)
            if v is None or v == "":
                if required:
                    raise CalibrationError(422, f"{key} is required")
                return None
            if not isinstance(v, str):
                raise CalibrationError(422, f"{key} must be a string, got {v!r}")
            return v.strip()

        def integer(key: str) -> int | None:
            v = data.get(key)
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, int):
                raise CalibrationError(422, f"{key} must be an integer, got {v!r}")
            return v

        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise CalibrationError(422, "params must be an object")
        sha = text("gcode_sha")
        if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise CalibrationError(422, "gcode_sha must be a lower-case hex sha256")
        known = {"kind", "method", "printer", "head", "spool_id", "filament_id", "lot_nr", "plate",
                 "params", "gcode_path", "gcode_sha", "note", "values", "evidence", "valid_until"}
        unknown = sorted(str(k) for k in data if k not in known)
        if unknown:
            raise CalibrationError(422, f"unknown field(s) {', '.join(unknown)}")
        kind = text("kind", required=True)
        assert kind is not None
        return cls(
            kind=kind,
            method=text("method") or "manual",
            printer=text("printer"),
            head=text("head"),
            spool_id=integer("spool_id"),
            filament_id=integer("filament_id"),
            lot_nr=text("lot_nr"),
            plate=text("plate"),
            params=dict(params),
            gcode_path=text("gcode_path"),
            gcode_sha=sha,
            note=text("note"),
        )


@dataclass(frozen=True)
class _Snapshot:
    kind: Kind
    printer: str | None
    head: str | None
    nozzle: Nozzle | None
    plate: str | None
    filament: Filament | None
    spool_id: int | None


def _kind(catalog: Catalog, kind_id: str) -> Kind:
    try:
        return catalog.kind(kind_id)
    except CatalogError as e:
        raise CalibrationError(422, str(e)) from e


def _snapshot(ctx: Ctx, req: RunRequest) -> _Snapshot:
    """Resolve the request's scope against the fleet and Spoolman (reads only)."""
    kind = _kind(ctx.catalog, req.kind)
    if req.method not in kind.methods:
        raise CalibrationError(422, f"{kind.id}: unknown method {req.method!r}; accepted: {', '.join(kind.methods)}")
    printer = head = plate = None
    nozzle: Nozzle | None = None
    head_obj: Head | None = None
    p: FleetPrinter | None = None
    if req.printer is not None:
        p = fleet_printer(ctx.fleet, req.printer)
        if not kind.applies_to(p.model):
            raise CalibrationError(422, f"{kind.id} does not exist for {p.model} ({', '.join(kind.models or ())} only)")
        if req.head is not None:
            head_obj = fleet_head(p, req.head)
    elif kind.printer_scoped or req.head is not None:
        raise CalibrationError(422, f"{kind.id} is measured on a printer: pass printer")
    if kind.head_scoped and head_obj is None:
        raise CalibrationError(422, f"{kind.id} is per head: pass head (T1..Tn)")
    if head_obj is not None and not head_obj.usable:
        raise CalibrationError(409, f"{req.printer} {head_obj.label} is not usable: {head_obj.reason or 'see fleet'}")
    if kind.printer_scoped:
        printer = req.printer
    if kind.head_scoped and head_obj is not None:
        head = head_obj.label
    if "nozzle" in kind.scope and head_obj is not None and req.printer is not None:
        nozzle = current_nozzle(ctx.conn, req.printer, head_obj)
    if "plate" in kind.scope and p is not None:
        plate = _plate(p, req.plate, required=True)

    filament: Filament | None = None
    spool_id = req.spool_id
    if req.spool_id is not None:
        filament = Filament.from_spool(_spool(ctx.spoolman, req.spool_id))
    elif req.filament_id is not None:
        filament = Filament.from_filament(_filament(ctx.spoolman, req.filament_id), lot_nr=None, spool_id=None)
    elif "filament" in kind.scope and head_obj is not None and req.printer is not None:
        bound = bound_filament(ctx.spoolman, req.printer, head_obj)
        if bound.filament is None:
            raise CalibrationError(422, f"{kind.id} is per filament: pass spool_id or filament_id ({bound.note})")
        filament = bound.filament
        spool_id = filament.spool_id
    if "filament" in kind.scope and filament is None:
        raise CalibrationError(422, f"{kind.id} is per filament: pass spool_id or filament_id")
    if filament is not None and req.lot_nr is not None:
        filament = Filament(filament.filament_id, filament.name, filament.vendor, filament.material,
                            _lot(req.lot_nr), filament.spool_id)
    if "filament" not in kind.scope:
        filament = None
    return _Snapshot(kind, printer, head, nozzle, plate, filament, spool_id)


def _insert(ctx: Ctx, snap: _Snapshot, req: RunRequest) -> int:
    n, f = snap.nozzle, snap.filament
    cur = ctx.conn.execute(
        """INSERT INTO calibration (kind, status, printer, head, nozzle_install_id, nozzle_diameter_mm,
               nozzle_material, nozzle_type, plate, filament_id, filament_name, vendor, material, lot_nr,
               spool_id, method, params, gcode_path, gcode_sha, note, created_at, created_by)
           VALUES (?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            snap.kind.id, snap.printer, snap.head,
            n.install_id if n else None, n.diameter_mm if n else None,
            n.material if n else None, n.type if n else None,
            snap.plate,
            f.filament_id if f else None, f.name if f else None, f.vendor if f else None,
            f.material if f else None, f.lot_nr if f else None,
            snap.spool_id, req.method,
            json.dumps(dict(req.params), sort_keys=True), req.gcode_path, req.gcode_sha, req.note,
            db.utc_now(), ctx.actor,
        ),
    )
    return int(cur.lastrowid or 0)


def _event_scope(cal: Calibration) -> dict[str, Any]:
    return {
        "id": cal.id, "kind": cal.kind, "printer": cal.printer, "head": cal.head,
        "filament_id": cal.filament.filament_id if cal.filament else None,
        "lot_nr": cal.filament.lot_nr if cal.filament else None,
        "method": cal.method,
    }


def start_run(ctx: Ctx, req: RunRequest) -> Calibration:
    """Create a `planned` calibration (a run whose result comes later)."""
    snap = _snapshot(ctx, req)
    with db.transaction(ctx.conn):
        cal_id = _insert(ctx, snap, req)
        cal = get(ctx.conn, cal_id)
        ev.emit(ctx.conn, "calibration.planned", f"calibration:{cal_id}",
                {**_event_scope(cal), "gcode_sha": cal.gcode_sha}, ctx.actor)
    return cal


def _valid_until(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise CalibrationError(422, f"valid_until must be an ISO date, got {value!r}")
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise CalibrationError(422, f"valid_until must be an ISO date, got {value!r}") from e
    dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _same_scope(a: Calibration, b: Calibration, lot_sensitive: bool) -> bool:
    """Same scope = the newer record supersedes the older. The lot only separates records of a
    lot-sensitive kind; for the others two lots of one filament are the same scope."""
    fa, fb = a.filament, b.filament
    same_fil = (fa is None and fb is None) or (
        fa is not None and fb is not None and fa.filament_id == fb.filament_id
        and (fa.filament_id is not None or _ci(fa.material, fb.material))
        and (not lot_sensitive or fa.lot_nr == fb.lot_nr)
    )
    same_nozzle = (a.nozzle is None and b.nozzle is None) or (
        a.nozzle is not None and b.nozzle is not None and a.nozzle.same(b.nozzle)
    )
    return (a.kind == b.kind and a.printer == b.printer and a.head == b.head and a.plate == b.plate
            and same_fil and same_nozzle)


def _apply_result(ctx: Ctx, cal: Calibration, result: Mapping[str, Any]) -> Calibration:
    if cal.status != "planned":
        raise CalibrationError(409, f"calibration #{cal.id} is {cal.status}, not planned")
    kind = _kind(ctx.catalog, cal.kind)
    try:
        values = kind.check_values(result.get("values"))
    except CatalogError as e:
        raise CalibrationError(422, str(e)) from e
    evidence = result.get("evidence") or []
    if not isinstance(evidence, list):
        raise CalibrationError(422, "evidence must be a list (photo URLs, notes)")
    note = result.get("note")
    if note is not None and not isinstance(note, str):
        raise CalibrationError(422, "note must be a string")
    until = _valid_until(result.get("valid_until"))
    now = db.utc_now()
    ctx.conn.execute(
        """UPDATE calibration SET status = 'recorded', values_json = ?, evidence = ?, recorded_at = ?,
               recorded_by = ?, valid_until = ?, note = COALESCE(?, note) WHERE id = ?""",
        (json.dumps(values, sort_keys=True), json.dumps(evidence), now, ctx.actor, until, note, cal.id),
    )
    new = get(ctx.conn, cal.id)
    older = [c for c in list_calibrations(ctx.conn, kind=cal.kind, current=True, limit=1000)
             if c.id != cal.id and _same_scope(c, new, _kind(ctx.catalog, cal.kind).lot_sensitive)]
    for c in older:
        ctx.conn.execute("UPDATE calibration SET superseded_by = ? WHERE id = ?", (cal.id, c.id))
    ev.emit(ctx.conn, "calibration.recorded", f"calibration:{cal.id}",
            {**_event_scope(new), "values": values, "superseded": [c.id for c in older]}, ctx.actor)
    return new


def record_result(ctx: Ctx, cal_id: int, result: Mapping[str, Any]) -> Calibration:
    """Close a planned run with its values: {values, evidence?, note?, valid_until?}."""
    with db.transaction(ctx.conn):
        return _apply_result(ctx, get(ctx.conn, cal_id), result)


def record(ctx: Ctx, req: RunRequest, result: Mapping[str, Any]) -> Calibration:
    """A manual entry: create and record in one step (same checks as run + result)."""
    snap = _snapshot(ctx, req)
    with db.transaction(ctx.conn):
        cal_id = _insert(ctx, snap, req)
        return _apply_result(ctx, get(ctx.conn, cal_id), result)


# -- past records (settings-history import, #264) ---------------------------------------------


@dataclass(frozen=True)
class PastRecord:
    """A calibration measured before 3d-api knew of it (a line of the settings-history buffer):
    the scope request, the values, when and by whom, and where it was applied."""

    req: RunRequest
    values: Mapping[str, Any]
    at: str  # db.utc_now() format: when it was measured
    by: str
    evidence: list[Any]
    applied_to: list[dict[str, Any]]
    nozzle_diameter_mm: float | None


@dataclass(frozen=True)
class PreparedRecord:
    snap: _Snapshot
    past: PastRecord
    values: dict[str, Any]


def prepare_past(ctx: Ctx, past: PastRecord) -> PreparedRecord:
    """Resolve the scope (fleet and Spoolman reads, no DB writes) and check the values.

    The nozzle is the entry's diameter (none named: unknown), with the head's material and type
    from the fleet when the diameters agree, and never an install id: the install on the head now may postdate the
    measurement. A later nozzle swap voids the record through its invalidation event instead."""
    snap = _snapshot(ctx, past.req)
    try:
        values = snap.kind.check_values(dict(past.values))
    except CatalogError as e:
        raise CalibrationError(422, str(e)) from e
    if snap.nozzle is not None and past.nozzle_diameter_mm is None:
        snap = replace(snap, nozzle=None)  # the line names no nozzle: unknown, never today's
    elif snap.nozzle is not None:
        n, d = snap.nozzle, past.nozzle_diameter_mm
        same = n.diameter_mm is None or d == n.diameter_mm
        snap = replace(snap, nozzle=Nozzle(None, d, n.material if same else None, n.type if same else None))
    return PreparedRecord(snap, past, values)


def insert_past(ctx: Ctx, prep: PreparedRecord) -> Calibration:
    """Insert a prepared past record as `recorded`, inside the caller's transaction.

    Unlike record(): created_at/recorded_at are the measurement time (invalidation events are
    compared with it), and superseding follows that time, not insertion order — the record
    supersedes older current records of its scope and is superseded by a newer one already
    present, so importing an old buffer never displaces a later calibration."""
    past = prep.past
    cal_id = _insert(ctx, prep.snap, past.req)
    ctx.conn.execute(
        """UPDATE calibration SET status = 'recorded', values_json = ?, evidence = ?, applied_to = ?,
               created_at = ?, created_by = ?, recorded_at = ?, recorded_by = ? WHERE id = ?""",
        (json.dumps(prep.values, sort_keys=True), json.dumps(past.evidence), json.dumps(past.applied_to),
         past.at, past.by, past.at, past.by, cal_id),
    )
    new = get(ctx.conn, cal_id)
    kind = prep.snap.kind
    same = [c for c in list_calibrations(ctx.conn, kind=kind.id, current=True, limit=1000)
            if c.id != cal_id and _same_scope(c, new, kind.lot_sensitive)]
    older = [c for c in same if (c.recorded_at or "") <= past.at]
    newer = sorted((c for c in same if (c.recorded_at or "") > past.at), key=lambda c: c.recorded_at or "")
    for c in older:
        ctx.conn.execute("UPDATE calibration SET superseded_by = ? WHERE id = ?", (cal_id, c.id))
    if newer:
        ctx.conn.execute("UPDATE calibration SET superseded_by = ? WHERE id = ?", (newer[0].id, cal_id))
        new = get(ctx.conn, cal_id)
    ev.emit(ctx.conn, "calibration.recorded", f"calibration:{cal_id}",
            {**_event_scope(new), "values": prep.values, "superseded": [c.id for c in older],
             "imported": True}, ctx.actor)
    return new


# -- invalidation -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Invalidation:
    event_id: int
    at: str
    printer: str
    head: str | None
    trigger: str | None
    kinds: tuple[str, ...] | None
    reason: str | None

    def hits(self, kind: Kind, head: str | None) -> bool:
        if self.kinds is not None:
            if kind.id not in self.kinds:
                return False
        elif self.trigger not in kind.invalidated_by:
            return False
        if kind.head_scoped and self.head is not None and head is not None:
            return self.head.casefold() == head.casefold()
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "at": self.at, "printer": self.printer, "head": self.head,
                "trigger": self.trigger, "kinds": list(self.kinds) if self.kinds is not None else None,
                "reason": self.reason}


def invalidations(conn: sqlite3.Connection, printer: str) -> list[Invalidation]:
    rows = conn.execute(
        "SELECT id, at, payload FROM events WHERE kind = ? AND json_extract(payload, '$.printer') = ? "
        "ORDER BY id",
        (INVALIDATED, printer),
    ).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        kinds = p.get("kinds")
        out.append(Invalidation(
            event_id=int(r["id"]), at=str(r["at"]), printer=printer,
            head=p.get("head") or None, trigger=p.get("trigger"),
            kinds=tuple(str(k) for k in kinds) if isinstance(kinds, list) else None,
            reason=p.get("reason"),
        ))
    return out


# -- requirement engine -----------------------------------------------------------------------


@dataclass
class Requirement:
    kind: Kind | None  # None: a gate row about a G-code tool the printer has no usable head for
    printer: str | None
    head: str | None
    status: str
    level: str
    detail: str
    record: Calibration | None = None
    invalidation: Invalidation | None = None
    filament: Filament | None = None
    nozzle: Nozzle | None = None
    accepted: str | None = None  # bypass reason
    tool: int | None = None  # G-code tool index (gate only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.id if self.kind else None, "title": self.kind.title if self.kind else None,
            "printer": self.printer, "head": self.head, "tool": self.tool,
            "status": self.status, "level": self.level, "detail": self.detail,
            "record": self.record.to_dict() if self.record else None,
            "invalidation": self.invalidation.to_dict() if self.invalidation else None,
            "filament": self.filament.to_dict() if self.filament else None,
            "nozzle": self.nozzle.to_dict() if self.nozzle else None,
            "accepted": self.accepted,
            "estimate": {"minutes": self.kind.estimate_minutes if self.kind else 0.0,
                         "grams": self.kind.estimate_grams if self.kind else 0.0},
        }


@dataclass
class _Eval:
    """One kind on one head, with the inputs the engine looks at."""

    kind: Kind
    printer: str
    head: str | None
    nozzle: Nozzle | None
    plate: str | None
    filament: Filament | None
    filament_note: str | None
    plates_known: int = 0  # how many plates the printer has (fleet); 0 = not known


def _candidates(conn: sqlite3.Connection, e: _Eval) -> list[Calibration]:
    k = e.kind
    if k.printer_scoped:
        rows = list_calibrations(conn, kind=k.id, printer=e.printer, current=True, limit=1000)
    else:
        rows = list_calibrations(conn, kind=k.id, current=True, limit=1000)
    out = []
    for c in rows:
        if k.head_scoped and not _ci(c.head, e.head):
            continue
        if "plate" in k.scope and e.plate is not None and c.plate is not None and c.plate != e.plate:
            continue
        out.append(c)
    return out


def _tier(c: Calibration, f: Filament) -> str | None:
    cf = c.filament
    if cf is None:
        return None
    if cf.filament_id is not None and cf.filament_id == f.filament_id:
        return "filament"
    if _ci(cf.material, f.material):
        return "vendor" if _ci(cf.vendor, f.vendor) else "type"
    return None


def _when(at: str | None) -> str:
    return (at or "?")[:10]


def evaluate(conn: sqlite3.Connection, policy: CalibrationPolicy, e: _Eval,
             invs: Sequence[Invalidation], now: str) -> Requirement:
    k = e.kind

    def req(status: str, detail: str, record: Calibration | None = None,
            inv: Invalidation | None = None, level: str | None = None) -> Requirement:
        if level is None:
            level = _POLICY_LEVEL[policy.level(e.printer, k.id, status)]
        return Requirement(kind=k, printer=e.printer, head=e.head, status=status, level=level,
                           detail=detail, record=record, invalidation=inv, filament=e.filament,
                           nozzle=e.nozzle)

    def stale(c: Calibration) -> Invalidation | None:
        # `>=`: timestamps have millisecond resolution, and an invalidation in the same instant as
        # the recording must not be lost; a needless recalibration is the safe side.
        hits = [i for i in invs if i.at >= (c.recorded_at or "") and i.hits(k, c.head)] if k.printer_scoped else []
        return hits[-1] if hits else None

    if "filament" in k.scope and e.filament is None:
        return req("no_spool", e.filament_note or "the loaded filament is unknown", level=UNVERIFIED)
    if "plate" in k.scope and e.plate is None and e.plates_known > 1:
        # a record for one plate says nothing about another: never take one as current
        return req("no_plate", f"{e.printer} has {e.plates_known} plates: pass plate", level=UNVERIFIED)

    cands = _candidates(conn, e)
    nozzle_ok = [c for c in cands if "nozzle" not in k.scope or e.nozzle is None or c.nozzle is None
                 or c.nozzle.same(e.nozzle)]
    f = e.filament
    exact = [c for c in nozzle_ok if f is None or _tier(c, f) == "filament"]
    valid = [c for c in exact if stale(c) is None and not (c.valid_until and c.valid_until < now)]
    if valid:
        same_lot = [c for c in valid if f is None or c.filament is None or c.filament.lot_nr == f.lot_nr]
        best = (same_lot or valid)[0]  # newest first
        if k.lot_sensitive and f is not None and not same_lot:
            was = best.filament.lot_nr if best.filament else None
            return req("new_lot", f"lot {f.lot_nr or 'unknown'} is new; #{best.id} was measured on lot "
                                  f"{was or 'unknown'} ({_when(best.recorded_at)})", best)
        return req("current", f"#{best.id} {_values_text(best)} ({best.method}, {_when(best.recorded_at)})",
                   best, level=PASS)
    if exact:
        newest = exact[0]
        inv = stale(newest)
        if inv is not None:
            return req("invalidated", f"#{newest.id} predates {inv.trigger or 'maintenance'} on "
                                      f"{_when(inv.at)}" + (f": {inv.reason}" if inv.reason else ""), newest, inv)
        return req("expired", f"#{newest.id} expired {_when(newest.valid_until)}", newest)
    if k.baseline == "assumed":
        hits = [i for i in invs if i.hits(k, e.head)]
        if hits:
            inv = hits[-1]
            return req("invalidated", f"never recorded since {inv.trigger or 'maintenance'} on {_when(inv.at)}"
                                      + (f": {inv.reason}" if inv.reason else ""), None, inv)
        return req("assumed", "no record; factory state assumed (no maintenance since)", level=PASS)
    assert f is not None  # baseline required ⇒ filament in scope (catalog schema)
    other_nozzle = [c for c in cands if _tier(c, f) == "filament" and c not in nozzle_ok]
    if other_nozzle:
        c = other_nozzle[0]
        was = c.nozzle.label() if c.nozzle else "another nozzle"
        now_n = e.nozzle.label() if e.nozzle else "unknown"
        return req("new_nozzle", f"#{c.id} was measured with {was}; now {now_n}", c)
    tiers = {_tier(c, f) for c in nozzle_ok}
    if "vendor" in tiers:
        return req("new_filament", f"{f.label()} has no record; other {f.vendor} {f.material} filaments do")
    if "type" in tiers:
        return req("new_vendor", f"no {f.material} from {f.vendor or 'this vendor'} recorded yet")
    return req("new_material_type", f"no {f.material or 'material'} recorded yet ({f.label()})")


def _values_text(c: Calibration) -> str:
    vals = c.values or {}
    return ", ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in vals.items()) or "done"


@dataclass
class RequirementSet:
    printer: str
    items: list[Requirement]
    skipped: list[dict[str, Any]]

    @property
    def needed(self) -> list[Requirement]:
        return [r for r in self.items if r.level in (FAIL, WARN, UNVERIFIED)]

    def to_dict(self, *, all_items: bool = False) -> dict[str, Any]:
        shown = [r for r in self.items if all_items or r.level in (FAIL, WARN, UNVERIFIED)]
        need = [r for r in self.items if r.level in (FAIL, WARN)]
        return {
            "printer": self.printer,
            "overall": worst(r.level for r in self.items),
            "items": [r.to_dict() for r in shown],
            "skipped": self.skipped,
            "estimate": {"minutes": sum(r.kind.estimate_minutes for r in need if r.kind),
                         "grams": sum(r.kind.estimate_grams for r in need if r.kind)},
        }


def worst(levels: Iterable[str]) -> str:
    best = PASS
    for lv in levels:
        if _RANK[lv] > _RANK[best]:
            best = lv
    return best


def _head_evals(ctx: Ctx, p: FleetPrinter, head: Head, bound: Bound, plate: str | None) -> list[_Eval]:
    nozzle = current_nozzle(ctx.conn, p.name, head)
    return [
        _Eval(kind=k, printer=p.name, head=head.label, nozzle=nozzle, plate=plate,
              filament=bound.filament, filament_note=bound.note, plates_known=len(p.plates or ()))
        for k in ctx.catalog.kinds.values()
        if k.applies_to(p.model) and ("head" in k.scope or "filament" in k.scope)
        and not ctx.policy.disabled(p.name, k.id)
    ]


def _printer_evals(ctx: Ctx, p: FleetPrinter, plate: str | None) -> list[_Eval]:
    return [
        _Eval(kind=k, printer=p.name, head=None, nozzle=None, plate=plate, filament=None,
              filament_note=None, plates_known=len(p.plates or ()))
        for k in ctx.catalog.kinds.values()
        if k.applies_to(p.model) and "head" not in k.scope and "filament" not in k.scope
        and not ctx.policy.disabled(p.name, k.id)
    ]


def _run(ctx: Ctx, p: FleetPrinter, evals: list[_Eval], now: str | None) -> list[Requirement]:
    invs = invalidations(ctx.conn, p.name)
    stamp = now or db.utc_now()
    out = [evaluate(ctx.conn, ctx.policy, e, invs, stamp) for e in evals]
    return [r for r in out if r.level != OFF]


def requirements(
    ctx: Ctx,
    printer: str,
    *,
    head: str | None = None,
    spool_id: int | None = None,
    plate: str | None = None,
    now: str | None = None,
) -> RequirementSet:
    """What `printer` (one head, or every usable head) needs before printing with what is loaded."""
    p = fleet_printer(ctx.fleet, printer)
    if spool_id is not None and head is None:
        raise CalibrationError(422, "spool needs head: which head would it be loaded on?")
    heads = [fleet_head(p, head)] if head is not None else list(p.heads)
    plate_now = _plate(p, plate, required=False)
    evals: list[_Eval] = []
    skipped: list[dict[str, Any]] = []
    for h in heads:
        if not h.usable:
            skipped.append({"head": h.label, "reason": h.reason or "not usable"})
            continue
        if spool_id is not None:
            bound = Bound(Filament.from_spool(_spool(ctx.spoolman, spool_id)))
        else:
            bound = bound_filament(ctx.spoolman, p.name, h)
        evals += _head_evals(ctx, p, h, bound, plate_now)
    evals += _printer_evals(ctx, p, plate_now)
    return RequirementSet(p.name, _run(ctx, p, evals, now), skipped)


# -- gate -------------------------------------------------------------------------------------


def _footer_list(raw: str) -> list[str]:
    return [x.strip().strip('"') for x in re.split(r"[;,]", raw) if x.strip()]


def filament_count(footer: Mapping[str, str]) -> int:
    for key in ("filament_settings_id", "filament_type", "filament_colour"):
        raw = footer.get(key)
        if raw:
            return max(1, len([x for x in raw.split(";") if x.strip()]))
    return 1


def footer_value(footer: Mapping[str, str], key: str, tool: int) -> tuple[str | None, str | None]:
    """(raw value for `tool`, why it is unknown)."""
    raw = footer.get(key)
    if raw is None:
        return None, f"the G-code footer has no `{key}`"
    parts = _footer_list(raw)
    if not parts:
        return None, f"`{key}` is empty"
    n = filament_count(footer)
    if len(parts) == 1:
        if tool == 0 or n == 1:
            return parts[0], None
        return None, (f"`{key}` carries one value for {n} filaments (the first filament's); "
                      f"T{tool + 1}'s value is not in the file")
    if tool >= len(parts):
        return None, f"`{key}` has no entry for T{tool + 1}"
    return parts[tool], None


@dataclass(frozen=True)
class _FooterNote:
    level: str
    note: str
    mismatch: bool = False  # the G-code carries a value other than the recorded one


def _compare_footer(r: Requirement, footer: Mapping[str, str], tool: int,
                    policy: CalibrationPolicy) -> list[_FooterNote]:
    """One note per gcode check of the kind against the record r stands on."""
    out: list[_FooterNote] = []
    rec, kind = r.record, r.kind
    if rec is None or kind is None or not rec.values:
        return out
    for chk in kind.gcode:
        want = rec.values.get(chk.value)
        if not isinstance(want, (int, float)):
            continue
        if chk.enabled_by is not None:
            flag, _ = footer_value(footer, chk.enabled_by, tool)
            if flag is not None and flag.strip().lower() not in _TRUE:
                applied = rec.applied_to_firmware()
                if applied:
                    out.append(_FooterNote(PASS, f"`{chk.enabled_by}` is off: the firmware holds {chk.value} "
                                      f"(#{rec.id} applied to {applied.get('target')} {_when(applied.get('at'))})"))
                else:
                    out.append(_FooterNote(WARN, f"`{chk.enabled_by}` is off: the G-code leaves {chk.value} to the "
                                      f"firmware, and #{rec.id} ({chk.value}={want:g}) was never applied to "
                                      "the printer"))
                continue
        raw, why = footer_value(footer, chk.key, tool)
        if raw is None:
            out.append(_FooterNote(UNVERIFIED, why or f"no `{chk.key}`"))
            continue
        try:
            got = float(raw)
        except ValueError:
            out.append(_FooterNote(UNVERIFIED, f"`{chk.key}` = {raw!r} is not a number"))
            continue
        ok = abs(got - want) <= chk.tolerance if chk.compare == "eq" else got <= want + chk.tolerance
        if ok:
            continue
        sign = "≠" if chk.compare == "eq" else ">"
        level = _POLICY_LEVEL[policy.level(r.printer, kind.id, "gcode_mismatch")]
        out.append(_FooterNote(level, f"G-code {chk.key} {got:g} {sign} recorded {want:g} (#{rec.id}, "
                           f"{_when(rec.recorded_at)})", mismatch=True))
    return out


@dataclass
class GateResult:
    printer: str
    file: str | None
    gcode_sha: str | None
    items: list[Requirement]

    @property
    def overall(self) -> str:
        return worst(r.level for r in self.items) if self.items else UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": "calibration_current",
            "printer": self.printer, "file": self.file, "gcode_sha": self.gcode_sha,
            "overall": self.overall,
            "items": [r.to_dict() for r in self.items],
        }


def parse_accept(values: Iterable[str], reason: str | None, catalog: Catalog) -> dict[str, str]:
    """`calibration:<kind>` (or a bare kind) → {kind: reason}; reason is mandatory."""
    out: dict[str, str] = {}
    for raw in values:
        kind = raw.strip()
        kind = kind[len(ACCEPT_PREFIX):] if kind.startswith(ACCEPT_PREFIX) else kind
        _kind(catalog, kind)
        if not reason or not reason.strip():
            raise CalibrationError(422, f"accepting calibration:{kind} needs a reason")
        out[kind] = reason.strip()
    return out


def check_gcode(
    ctx: Ctx,
    printer: str,
    footer: Mapping[str, str],
    used_tools: Sequence[int],
    *,
    spools: Mapping[int, int] | None = None,
    accept: Mapping[str, str] | None = None,
    plate: str | None = None,
    gcode_sha: str | None = None,
    file: str | None = None,
    now: str | None = None,
) -> GateResult:
    """The `calibration_current` gate for a G-code about to print on `printer`."""
    p = fleet_printer(ctx.fleet, printer)
    accept = dict(accept or {})
    for kind in accept:
        _kind(ctx.catalog, kind)
    spools = dict(spools or {})
    plate_now = _plate(p, plate, required=False)
    by_index = {h.index: h for h in p.heads}
    items: list[Requirement] = []
    head_evals: list[tuple[int, _Eval]] = []
    for tool in sorted(set(used_tools)):
        h = by_index.get(tool)
        if h is None or not h.usable:
            why = f"{p.name} has no head T{tool + 1}" if h is None else \
                f"{p.name} {h.label} is not usable: {h.reason or 'see fleet'}"
            items.append(Requirement(None, p.name, None, "no_head", UNVERIFIED, why, tool=tool))
            continue
        if tool in spools:
            bound = Bound(Filament.from_spool(_spool(ctx.spoolman, spools[tool])))
        else:
            bound = bound_filament(ctx.spoolman, p.name, h)
        head_evals += [(tool, e) for e in _head_evals(ctx, p, h, bound, plate_now)]
    invs = invalidations(ctx.conn, p.name)
    stamp = now or db.utc_now()
    for tool, e in head_evals:
        r = evaluate(ctx.conn, ctx.policy, e, invs, stamp)
        r.tool = tool
        if r.level == OFF:
            continue
        notes = _compare_footer(r, footer, tool, ctx.policy) if r.status in ("current", "new_lot") else []
        if notes:
            r.level = worst([r.level, *(n.level for n in notes if n.level != OFF)])
            r.detail += "; " + "; ".join(n.note for n in notes)
            if r.status == "current" and any(n.mismatch and n.level != OFF for n in notes):
                r.status = "gcode_mismatch"
        items.append(r)
    items += [r for r in (evaluate(ctx.conn, ctx.policy, e, invs, stamp) for e in _printer_evals(ctx, p, plate_now))
              if r.level != OFF]
    result = GateResult(p.name, file, gcode_sha, items)
    _accept(ctx, result, accept)
    return result


def _accept(ctx: Ctx, result: GateResult, accept: Mapping[str, str]) -> None:
    hits = [r for r in result.items if r.level == FAIL and r.kind is not None and r.kind.id in accept]
    if not hits:
        return
    with db.transaction(ctx.conn):
        for r in hits:
            assert r.kind is not None
            reason = accept[r.kind.id]
            ev.emit(ctx.conn, "calibration.accepted",
                    f"{result.printer}/{r.head}" if r.head else result.printer,
                    {"printer": result.printer, "head": r.head, "tool": r.tool, "kind": r.kind.id,
                     "status": r.status, "detail": r.detail, "reason": reason,
                     "gcode_sha": result.gcode_sha, "file": result.file,
                     "record_id": r.record.id if r.record else None},
                    ctx.actor)
            r.level = WARN
            r.accepted = reason
            r.detail += f"; accepted by {ctx.actor}: {reason}"
