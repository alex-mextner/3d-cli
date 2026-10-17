"""routes_reconcile.py — printers, their slots and reconciliation anomalies (#161, plan §5.3, §11.1).

  GET  /v1/printers                 scope read. Every fleet printer: owner/access/heads, the latest
                                    live reading (state, job, per-slot material/colour/tag) and per
                                    slot the spools bound in Spoolman + its open anomalies.
  GET  /v1/printers/{name}          scope read. One of those entries (404 unknown printer).
  GET  /v1/printers/{name}/slots    scope read. Just its slots.
  GET  /v1/anomalies                scope read. ?status=open,ack (default) | resolved | all,
                                    ?printer=NAME (scope), ?limit=N (≤ 1000, default 200); newest first.
  POST /v1/anomalies/{id}/ack       scope write. {"note"?} open → ack ("known, leave it").
  POST /v1/anomalies/{id}/resolve   scope write. {"note"?} open/ack → resolved (409 otherwise).

Responses never trigger a printer request: they show what the background poller read last.

BACKGROUND (router lifespan, merged into the app's by FastAPI's include_router — app.py stays
untouched): one task per pollable fleet printer (adapter moonraker / creality-stock, access not
offline) reads its slots every PRINTOPS_POLL_S seconds (default 30, floor MIN_POLL_S = 10 — the
"≤ 1 request / 10 s / printer" budget; 0 disables polling) and reconciles the fleet; a second
task re-reconciles from the cached readings whenever slot.loaded / slot.unloaded / spool.* events
are committed, so a load shows up without waiting for the next poll. PRINTOPS_COLOR_DE_MAX
(default 15) is the CIEDE2000 colour threshold. The fleet is fleet/printers.yaml
(THREED_FLEET overrides; the container copies it to /app/fleet).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import sqlite3
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from errors import ThreeDError
from printops import events as ev
from printops import reconcile as rc
from printops.api.deps import Db, get_state, require
from printops.db import open_db
from printops.fleet import Fleet, FleetPrinter, load_fleet
from printops.slots import READERS, Reading, read_printer
from printops.spoolman import SpoolmanError
from printops.tokens import TokenRecord

log = logging.getLogger("printops.reconcile")

MIN_POLL_S = 10.0
DEFAULT_POLL_S = 30.0
TRIGGER_KINDS = ("slot.loaded", "slot.unloaded", *rc.WRITE_EVENTS)
TRIGGER_PREFIXES = ("spool.",)


@dataclass(frozen=True)
class ReconcileSettings:
    poll_s: float = DEFAULT_POLL_S  # 0 = no background polling
    max_delta_e: float = rc.DEFAULT_MAX_DELTA_E
    listen_s: float = 3.0  # Creality ws listen window per read

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ReconcileSettings:
        env = os.environ if env is None else env
        try:
            poll = float(env.get("PRINTOPS_POLL_S") or DEFAULT_POLL_S)
        except ValueError:
            poll = math.nan
        if not math.isfinite(
            poll
        ):  # nan/inf/garbage would kill or freeze the poller tasks
            log.warning(
                "PRINTOPS_POLL_S=%r is not a finite number; using %ss",
                env.get("PRINTOPS_POLL_S"),
                DEFAULT_POLL_S,
            )
            poll = DEFAULT_POLL_S
        if 0 < poll < MIN_POLL_S:
            log.warning(
                "PRINTOPS_POLL_S=%s is below the %ss per-printer floor; using %ss",
                poll,
                MIN_POLL_S,
                MIN_POLL_S,
            )
            poll = MIN_POLL_S
        try:
            de_max = float(env.get("PRINTOPS_COLOR_DE_MAX") or rc.DEFAULT_MAX_DELTA_E)
        except ValueError:
            de_max = math.nan
        # nan disables every `de > max` check; negative flags identical colours
        if not math.isfinite(de_max) or de_max < 0:
            log.warning(
                "PRINTOPS_COLOR_DE_MAX=%r is not a finite non-negative number; using %s",
                env.get("PRINTOPS_COLOR_DE_MAX"),
                rc.DEFAULT_MAX_DELTA_E,
            )
            de_max = rc.DEFAULT_MAX_DELTA_E
        return cls(poll_s=max(poll, 0.0), max_delta_e=de_max)


def pollable(printer: FleetPrinter) -> bool:
    return printer.contactable and printer.device.get("adapter") in READERS


def fleet_reader(fleet: Fleet | None, listen_s: float) -> rc.ReadFn:
    from printer_ops.devices import fleet_devices

    devices = fleet_devices(fleet)

    def read(printer: FleetPrinter) -> Reading:
        return read_printer(
            devices[printer.name],
            tuple(h.label for h in printer.heads),
            listen_s=listen_s,
        )

    return read


async def poll_printer(
    rec: rc.Reconciler, name: str, interval: float, delay: float = 0.0
) -> None:
    """Forever: read `name` once, reconcile, sleep `interval` (errors are logged, never fatal)."""
    await asyncio.sleep(delay)
    while True:
        try:
            reading = await run_in_threadpool(rec.poll, name)
            if not reading.ok:
                log.warning("%s: slots unreadable: %s", name, reading.error)
        except (SpoolmanError, sqlite3.Error) as e:
            rec.last_error = str(e)
            log.warning("reconcile after polling %s failed: %s", name, e)
        except Exception as e:  # a background loop must outlive any one bad pass
            rec.last_error = f"{type(e).__name__}: {e}"
            log.exception("slot poll of %s failed", name)
        await asyncio.sleep(interval)


def _triggered(db_path: Any, cursor: int) -> tuple[int, bool]:
    conn = open_db(db_path)
    try:
        hit = False
        while batch := ev.since(conn, cursor, ev.MAX_PAGE):
            cursor = batch[-1].id
            hit = hit or any(
                e.kind in TRIGGER_KINDS or e.kind.startswith(TRIGGER_PREFIXES)
                for e in batch
            )
        return cursor, hit
    finally:
        conn.close()


async def follow_events(rec: rc.Reconciler, db_path: Any) -> None:
    """Re-reconcile (cached readings, no printer request) after slot/spool events are committed."""
    conn = open_db(db_path)
    try:
        cursor = ev.latest_id(conn)
    finally:
        conn.close()
    listener = ev.BUS.listener()
    try:
        while True:
            await listener.wait(60.0)
            cursor, hit = await run_in_threadpool(_triggered, db_path, cursor)
            if not hit:
                continue
            try:
                await run_in_threadpool(rec.reconcile)
            except (SpoolmanError, sqlite3.Error) as e:
                rec.last_error = str(e)
                log.warning("event-triggered reconcile failed: %s", e)
            except Exception as e:  # see poll_printer
                rec.last_error = f"{type(e).__name__}: {e}"
                log.exception("event-triggered reconcile failed")
    finally:
        listener.close()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = app.state.printops
    cfg = ReconcileSettings.from_env()
    fleet: Fleet | None = None
    fleet_error: str | None = None
    read: rc.ReadFn | None = None
    try:
        fleet = load_fleet()
        read = fleet_reader(fleet, cfg.listen_s)
    except ThreeDError as e:
        # A broken fleet file must not take the rest of 3d-api down: /v1/printers reports it (503).
        fleet, fleet_error = None, "; ".join([e.message, *e.remediation])
        log.error("fleet: %s", fleet_error)
    rec = rc.Reconciler(
        state.settings.db_path,
        state.spoolman,
        fleet,
        read or fleet_reader(None, cfg.listen_s),
        max_delta_e=cfg.max_delta_e,
    )
    app.state.reconcile = rec
    app.state.reconcile_fleet_error = fleet_error
    tasks: list[asyncio.Task[None]] = []
    if cfg.poll_s > 0 and fleet is not None:
        names = [n for n, p in rec.printers.items() if pollable(p)]
        for i, name in enumerate(names):
            # spread the printers over the interval instead of reading them all at once
            tasks.append(
                asyncio.create_task(
                    poll_printer(
                        rec, name, cfg.poll_s, cfg.poll_s * i / max(len(names), 1)
                    )
                )
            )
        log.info(
            "slot poller: %s every %ss",
            ", ".join(names) or "no pollable printers",
            cfg.poll_s,
        )
    if fleet is not None:
        # binding changes reconcile against cached readings; no printer contact
        tasks.append(asyncio.create_task(follow_events(rec, state.settings.db_path)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


router = APIRouter(lifespan=lifespan)


def _reconciler(request: Request) -> rc.Reconciler:
    rec: rc.Reconciler | None = getattr(request.app.state, "reconcile", None)
    if rec is None:
        raise HTTPException(503, "reconciler not started (app lifespan did not run)")
    error = getattr(request.app.state, "reconcile_fleet_error", None)
    if error:
        raise HTTPException(503, f"fleet/printers.yaml unusable: {error}")
    return rec


def _spools(request: Request) -> tuple[list[rc.Binding] | None, str | None]:
    sp = get_state(request).spoolman
    if sp is None:
        return None, "SPOOLMAN_URL is not set"
    try:
        return rc.bindings(sp.find("spool")), None
    except SpoolmanError as e:
        return None, e.message


def printer_view(
    printer: FleetPrinter,
    reading: Reading | None,
    binds: list[rc.Binding] | None,
    anomalies: list[dict[str, Any]],
    spools_error: str | None,
) -> dict[str, Any]:
    slots = []
    for head in printer.heads:
        slot = f"{printer.name}/{head.label}"
        live = reading.slot(head.label) if reading is not None and reading.ok else None
        slots.append(
            {
                "slot": slot,
                "head": head.label,
                "index": head.index,
                "usable": head.usable,
                "reason": head.reason,
                "printer": None
                if live is None
                else {
                    "present": live.present,
                    "material": live.material,
                    "color": live.color,
                    "vendor": live.vendor,
                    "sub_type": live.sub_type,
                    "tag_uid": live.tag_uid,
                    "source": live.source,
                },
                "spools": None
                if binds is None
                else [b.to_dict() for b in binds if b.slot == slot],
                "anomalies": [
                    {"id": a["id"], "kind": a["kind"], "status": a["status"]}
                    for a in anomalies
                    if a["subject"] == f"slot:{slot}"
                    or a["evidence"].get("spool", {}).get("slot") == slot
                ],
            }
        )
    return {
        "name": printer.name,
        "model": printer.model,
        "owner": printer.owner,
        "access": printer.access,
        "adapter": printer.device.get("adapter"),
        "polled": pollable(printer),
        "live": None
        if reading is None
        else {k: v for k, v in reading.to_dict().items() if k != "slots"},
        "slots": slots,
        "spools_error": spools_error,
        "anomalies_open": sum(1 for a in anomalies if a["status"] == "open"),
    }


def _views(
    request: Request, conn: sqlite3.Connection, only: str | None = None
) -> list[dict[str, Any]]:
    rec = _reconciler(request)
    printers = rec.printers
    if only is not None and only not in printers:
        raise HTTPException(
            404,
            f"unknown printer {only!r}; fleet: {', '.join(sorted(printers)) or '(empty)'}",
        )
    binds, spools_error = _spools(request)
    out = []
    for name, printer in printers.items():
        if only is not None and name != only:
            continue
        anomalies = rc.list_anomalies(conn, scope=name, limit=ev.MAX_PAGE)
        out.append(
            printer_view(printer, rec.reading(name), binds, anomalies, spools_error)
        )
    return out


READ = [Depends(require("read"))]
Writer = Annotated[TokenRecord, Depends(require("write"))]


@router.get("/v1/printers", dependencies=READ)
def printers(request: Request, conn: Db) -> dict[str, Any]:
    rec = _reconciler(request)
    return {"printers": _views(request, conn), "last_error": rec.last_error}


@router.get("/v1/printers/{name}", dependencies=READ)
def printer(name: str, request: Request, conn: Db) -> dict[str, Any]:
    return _views(request, conn, name)[0]


@router.get("/v1/printers/{name}/slots", dependencies=READ)
def printer_slots(name: str, request: Request, conn: Db) -> dict[str, Any]:
    view = _views(request, conn, name)[0]
    return {
        "printer": name,
        "live": view["live"],
        "slots": view["slots"],
        "spools_error": view["spools_error"],
    }


@router.get("/v1/anomalies", dependencies=READ)
def anomalies(
    conn: Db, status: str = "open,ack", printer: str | None = None, limit: int = 200
) -> dict[str, Any]:
    wanted = (
        rc.STATUSES
        if status == "all"
        else tuple(s.strip() for s in status.split(",") if s.strip())
    )
    bad = [s for s in wanted if s not in rc.STATUSES]
    if bad or not wanted:
        raise HTTPException(
            422,
            f"status must be a comma list of {', '.join(rc.STATUSES)} or 'all', got {status!r}",
        )
    if not 1 <= limit <= ev.MAX_PAGE:
        raise HTTPException(422, f"limit must be 1..{ev.MAX_PAGE}, got {limit}")
    return {
        "anomalies": rc.list_anomalies(
            conn, statuses=wanted, scope=printer, limit=limit
        )
    }


class NoteBody(BaseModel):
    note: str | None = None


def _transition(
    conn: sqlite3.Connection,
    anomaly_id: int,
    status: str,
    who: TokenRecord,
    body: NoteBody | None,
) -> dict[str, Any]:
    try:
        row = rc.set_status(
            conn, anomaly_id, status, who.name, body.note if body else None
        )
    except rc.TransitionError as e:
        raise HTTPException(409, str(e)) from e
    if row is None:
        raise HTTPException(404, f"no anomaly #{anomaly_id}")
    return row


@router.post("/v1/anomalies/{anomaly_id}/ack")
def ack(
    anomaly_id: int, conn: Db, who: Writer, body: NoteBody | None = None
) -> dict[str, Any]:
    return _transition(conn, anomaly_id, "ack", who, body)


@router.post("/v1/anomalies/{anomaly_id}/resolve")
def resolve(
    anomaly_id: int, conn: Db, who: Writer, body: NoteBody | None = None
) -> dict[str, Any]:
    return _transition(conn, anomaly_id, "resolved", who, body)
