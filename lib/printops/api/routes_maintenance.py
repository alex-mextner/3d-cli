"""routes_maintenance.py — components, maintenance log, faults and due rules over HTTP (#166;
domain: printops.maintenance + printops.maintenance_rules, counters: printops.counters).

  GET  /v1/printers/{p}/components                read   {printer, heads: [{label, usable, open_faults,
                                                         nozzle, …}], components}; ?removed=true = history
  POST /v1/printers/{p}/components/nozzle-swap    write  {head, reason, diameter_mm?, material?, type?, label?,
                                                         at?, notes?, parts_used?} → 201 {maintenance, removed,
                                                         installed, inherited, invalidated}
  GET  /v1/printers/{p}/maintenance               read   {maintenance: [...]} newest first; ?limit=N (≤ 1000)
  POST /v1/printers/{p}/maintenance               write  {component?, kind?, rule?, head?, at?, notes?, parts_used?,
                                                         label?, invalidates?: [kind], no_invalidate?, fixes?: [id]}
                                                         → 201 {maintenance, removed, installed, invalidated, fixed}
  GET  /v1/printers/{p}/maintenance/due           read   {items}: every rule of the printer with its status
  GET  /v1/maintenance/due                        read   {items} for every printer; ?status=due|soon|ok|no_counter
  POST /v1/maintenance/check                      write  evaluate + emit maintenance.due for new due items; ?printer=
  POST /v1/maintenance/sync-fleet                 write  seed nozzles/plates/faults from fleet/printers.yaml now
  GET  /v1/printers/{p}/faults                    read   {faults}; ?status=open (default) | fixed | wontfix | all
  POST /v1/printers/{p}/faults                    write  {symptom, head?, component?, at?} → 201 fault
  POST /v1/faults/{id}/close                      write  {status?: fixed|wontfix, root_cause?, fixed_by?}
  GET  /v1/faults                                 read   {faults} of every printer; ?status= as above
  GET  /v1/faults/{id}                            read   one fault of any printer (404 when unknown)

Due reads printer counters READ-ONLY (Moonraker GET /server/history/*; printops.counters).
BACKGROUND: the router lifespan runs the check every `check_every_min` of
fleet/maintenance-rules.yaml; a failing round is logged and retried, never fatal.
Errors: MaintError.status (404 unknown printer/head/fault, 409 conflicts, 422 invalid);
a broken rules or fleet file → 503 with its problems.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from printops import maintenance as mt
from printops import maintenance_rules as mr
from printops.api.deps import Db, require
from printops.api.state import ApiState
from printops.counters import CounterProvider, FleetCounters
from printops.db import open_db
from printops.tokens import TokenRecord

if TYPE_CHECKING:
    from printops.fleet import FleetError

log = logging.getLogger("printops.maintenance")

FIRST_CHECK_DELAY_S = 120.0
FALLBACK_CHECK_MIN = 60
# Tests replace this factory; 3d-api reads Moonraker history.
counter_provider: Callable[[], CounterProvider] = FleetCounters

Reader = Annotated[TokenRecord, Depends(require("read"))]
Writer = Annotated[TokenRecord, Depends(require("write"))]


def _unavailable(e: FleetError | mr.RulesError) -> HTTPException:
    """A broken fleet or rules file: 503 with its problems."""
    return HTTPException(503, f"{e.message}: {'; '.join(e.remediation)}" if e.remediation else e.message)


def _fleet() -> Any:
    from printops.fleet import FleetError, load_fleet

    try:
        return load_fleet()
    except FleetError as e:
        raise _unavailable(e) from e


def _rules(fleet: Any) -> mr.Rules:
    """The rules, validated against the fleet's printer names."""
    try:
        return mr.load_rules(printers=None if fleet is None else fleet.printers)
    except mr.RulesError as e:
        raise _unavailable(e) from e


def _seed(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Seed components/faults when the fleet file changed since the last seeding."""
    from printops.fleet import FleetError

    try:
        report = mt.ensure_seeded(conn)
    except FleetError as e:  # a broken fleet file must not take the log down
        log.error("fleet seeding failed: %s", e.message)
        return conn
    if report is not None and (report.changed or report.drift):
        log.info("maintenance fleet seed: %s", report.to_dict())
    return conn


# Seeding writes, so the token is a sub-dependency of the connection: it is checked before any
# seeding whatever the route's parameter order (FastAPI caches it, the route's `who` reuses it).
# A read may seed: that records only server-side fleet facts (actor "fleet"), nothing the caller
# sends, so a read token cannot choose what is written; it keeps reads true to the fleet file.
def _read_conn(conn: Db, who: Reader) -> sqlite3.Connection:
    return _seed(conn)


def _write_conn(conn: Db, who: Writer) -> sqlite3.Connection:
    return _seed(conn)


ReadConn = Annotated[sqlite3.Connection, Depends(_read_conn)]
WriteConn = Annotated[sqlite3.Connection, Depends(_write_conn)]


@contextlib.contextmanager
def _domain() -> Iterator[None]:
    try:
        yield
    except mt.MaintError as e:
        raise HTTPException(e.status, e.message) from e


async def _loop(state: ApiState) -> None:
    await asyncio.sleep(FIRST_CHECK_DELAY_S)
    while True:
        try:
            minutes = await run_in_threadpool(_tick, state)
        except Exception:  # one bad round (printer offline, broken file) must not end the loop
            log.exception("maintenance check failed (retry next round)")
            minutes = FALLBACK_CHECK_MIN
        await asyncio.sleep(minutes * 60)


def _tick(state: ApiState) -> int:
    from printops.fleet import load_fleet

    fleet = load_fleet()
    rules = mr.load_rules(printers=None if fleet is None else fleet.printers)
    conn = open_db(state.settings.db_path)
    try:
        mt.ensure_seeded(conn)
        report = mr.check(conn, rules, fleet, counter_provider())
    finally:
        conn.close()
    if report.announced:
        log.info("maintenance due: %s", [a["rule"] + "@" + a["printer"] for a in report.announced])
    return rules.check_every_min


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_loop(app.state.printops))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


router = APIRouter(lifespan=_lifespan)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NozzleSwap(_Body):
    head: str = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=500)
    diameter_mm: float | None = None
    material: str | None = None
    type: str | None = None  # wire name of the nozzle type; the domain layer calls it type_
    label: str | None = Field(default=None, max_length=200)
    at: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    parts_used: list[str] = Field(default_factory=list, max_length=50)


class MaintenanceBody(_Body):
    component: str | None = None
    kind: str | None = None
    rule: str | None = None
    head: str | None = Field(default=None, max_length=8)
    at: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    parts_used: list[str] = Field(default_factory=list, max_length=50)
    label: str | None = Field(default=None, max_length=200)
    invalidates: list[str] | None = Field(default=None, max_length=20)
    no_invalidate: bool = False
    fixes: list[int] = Field(default_factory=list, max_length=20)


class FaultBody(_Body):
    symptom: str = Field(min_length=1, max_length=2000)
    head: str | None = Field(default=None, max_length=8)
    component: str | None = None
    at: str | None = None


class CloseBody(_Body):
    status: Literal["fixed", "wontfix"] = "fixed"
    root_cause: str | None = Field(default=None, max_length=2000)
    fixed_by: int | None = None


FaultStatus = Literal["open", "fixed", "wontfix", "all"]


@router.get("/v1/printers/{printer}/components")
def get_components(conn: ReadConn, who: Reader, printer: str, removed: bool = False) -> dict[str, Any]:
    fleet = _fleet()
    with _domain():
        return {"printer": printer, "heads": mt.heads(conn, fleet, printer),
                "components": mt.components(conn, printer, include_removed=removed)}


@router.post("/v1/printers/{printer}/components/nozzle-swap", status_code=201)
def nozzle_swap(conn: WriteConn, who: Writer, printer: str, body: NozzleSwap) -> dict[str, Any]:
    fleet = _fleet()
    with _domain():
        return mt.swap_nozzle(conn, fleet, printer, body.head, reason=body.reason, actor=who.name,
                              diameter_mm=body.diameter_mm, material=body.material, type_=body.type, label=body.label,
                              at=body.at, notes=body.notes, parts=body.parts_used)


@router.get("/v1/printers/{printer}/maintenance")
def get_maintenance(conn: ReadConn, who: Reader, printer: str, limit: int = 50) -> dict[str, Any]:
    with _domain():
        mt.fleet_printer(_fleet(), printer)
        return {"maintenance": mt.list_maintenance(conn, printer, limit=limit)}


@router.post("/v1/printers/{printer}/maintenance", status_code=201)
def post_maintenance(conn: WriteConn, who: Writer, printer: str, body: MaintenanceBody) -> dict[str, Any]:
    fleet = _fleet()
    rules = _rules(fleet)
    with _domain():
        return mt.log_maintenance(conn, fleet, rules, printer, actor=who.name, component=body.component,
                                  kind=body.kind, head=body.head, at=body.at, notes=body.notes, parts=body.parts_used,
                                  rule_id=body.rule, label=body.label, invalidates=body.invalidates,
                                  no_invalidate=body.no_invalidate, fixes=body.fixes)


def _due(conn: sqlite3.Connection, printer: str | None) -> list[mr.DueItem]:
    fleet = _fleet()
    with _domain():
        if printer is not None:
            mt.fleet_printer(fleet, printer)
    return mr.evaluate(conn, _rules(fleet), fleet, counter_provider(), printer=printer)


@router.get("/v1/printers/{printer}/maintenance/due")
def printer_due(conn: ReadConn, who: Reader, printer: str) -> dict[str, Any]:
    return {"items": [i.to_dict() for i in _due(conn, printer)]}


@router.get("/v1/maintenance/due")
def all_due(conn: ReadConn, who: Reader, status: Literal["due", "soon", "ok", "no_counter"] | None = None) -> dict[str, Any]:
    return {"items": [i.to_dict() for i in _due(conn, None) if status is None or i.status == status]}


@router.post("/v1/maintenance/check")
def run_check(conn: WriteConn, who: Writer, printer: str | None = None) -> dict[str, Any]:
    fleet = _fleet()
    with _domain():
        if printer is not None:
            mt.fleet_printer(fleet, printer)
    return mr.check(conn, _rules(fleet), fleet, counter_provider(), printer=printer, actor=who.name).to_dict()


@router.post("/v1/maintenance/sync-fleet")
def sync_fleet(conn: Db, who: Writer) -> dict[str, Any]:
    from printops.fleet import FleetError

    try:
        report = mt.ensure_seeded(conn, actor=who.name, force=True)
    except FleetError as e:
        raise _unavailable(e) from e
    if report is None:
        raise HTTPException(409, "no fleet file (fleet/printers.yaml) in this 3d-api; nothing to seed")
    return report.to_dict()


@router.get("/v1/printers/{printer}/faults")
def printer_faults(conn: ReadConn, who: Reader, printer: str, status: FaultStatus = "open") -> dict[str, Any]:
    with _domain():
        mt.fleet_printer(_fleet(), printer)
        return {"faults": mt.list_faults(conn, printer, status=None if status == "all" else status)}


@router.get("/v1/faults")
def all_faults(conn: ReadConn, who: Reader, status: FaultStatus = "open") -> dict[str, Any]:
    _fleet()  # a broken fleet file is a 503 here too, not stale data
    with _domain():
        return {"faults": mt.list_faults(conn, None, status=None if status == "all" else status)}


@router.get("/v1/faults/{fault_id}")
def one_fault(conn: ReadConn, who: Reader, fault_id: int) -> dict[str, Any]:
    _fleet()
    with _domain():
        return mt.get_fault(conn, fault_id)


@router.post("/v1/printers/{printer}/faults", status_code=201)
def post_fault(conn: WriteConn, who: Writer, printer: str, body: FaultBody) -> dict[str, Any]:
    fleet = _fleet()
    with _domain():
        return mt.open_fault(conn, fleet, printer, symptom=body.symptom, actor=who.name, head=body.head,
                             component=body.component, at=body.at)


@router.post("/v1/faults/{fault_id}/close")
def close_fault(conn: WriteConn, who: Writer, fault_id: int, body: CloseBody) -> dict[str, Any]:
    """By fault id alone (ids are global), so a write-only token needs no read to find the printer."""
    with _domain():
        return mt.close_fault(conn, fault_id, actor=who.name, status=body.status, root_cause=body.root_cause,
                              fixed_by=body.fixed_by)
