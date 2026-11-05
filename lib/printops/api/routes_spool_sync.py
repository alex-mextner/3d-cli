"""routes_spool_sync.py — the scheduled Spoolman ↔ PrintOps sync and its drift API (issue #704).

  GET  /v1/spool-sync             scope read. Live detection (one Spoolman listing): every divergence
                                  between Spoolman and PrintOps right now, `?spool=ID` for one spool.
                                  {ok, tolerance_g, checked, drift, healable, unhealable, drifts[],
                                   last_pass, interval_s}
  GET  /v1/spool-sync/status      NO auth (like /v1/health). What the latest scheduled pass saw, no
                                  Spoolman request: {enabled, interval_s, last_pass, age_s, stale,
                                   anomalies_open} (stale: on, but no pass completed for several intervals)
  POST /v1/spool-sync/heal        scope write. {"spool_id"?} heal now (the same pass the schedule
                                  runs); answers {ok, before, healed, failed, after}.
  POST /v1/spool-sync/confirm-deleted
                                  scope write. The owner's decision after `spoolman_empty` (most known
                                  spools missing at once: a wiped Spoolman or a wrong URL, so nothing is
                                  healed): record every spool Spoolman answers 404 for as deleted.
                                  {confirmed[], still_present[], may_be_other_spools[], after}

BACKGROUND (router lifespan): every PRINTOPS_SPOOL_SYNC_S seconds (default 60, floor 10, 0 turns it
off) one pass runs `printops.spool_sync.run_pass` against Spoolman through the one write path: it
books what the Spoolman web UI and Moonraker changed into the ledger, events and bindings, and opens
an anomaly of scope `spools` for what it cannot fix (it closes by itself when the condition clears).
One Spoolman listing per pass; one more request per spool that has something to do. A pass waits for
the PrintOps extra fields to be registered first, and a Spoolman that is down only records the error.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import math
import os
import sqlite3
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from printops import reconcile as rc
from printops import spool_sync as sync
from printops.api.deps import Db, get_state, require
from printops.api.state import ApiState
from printops.datalint import parse_utc
from printops.db import open_db
from printops.db import utc_now as db_utc_now
from printops.spoolman import SpoolmanError
from printops.tokens import TokenRecord

log = logging.getLogger("printops.spool_sync")

DEFAULT_INTERVAL_S = 60.0
MIN_INTERVAL_S = 10.0
FIELDS_WAIT_S = 120.0
STALE_PASSES = 3  # a schedule with no pass for this many intervals (+ the fields wait) is stuck

READ = [Depends(require("read"))]
Writer = Annotated[TokenRecord, Depends(require("write"))]


@dataclass
class SpoolSyncService:
    """What the lifespan keeps for the routes: the interval and the latest pass."""

    interval_s: float
    status: sync.PassStatus = field(default_factory=sync.PassStatus)
    started_at: str = field(default_factory=db_utc_now)

    @property
    def enabled(self) -> bool:
        return self.interval_s > 0

    def age_s(self, now: datetime.datetime | None = None) -> float:
        """Seconds since the last pass that completed (or since start when none did)."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return (now - parse_utc(self.status.at or self.started_at)).total_seconds()

    def stale(self, now: datetime.datetime | None = None) -> bool:
        """The schedule is on but nothing completed for several intervals: a dead loop or passes that
        keep failing. Without this `health` would keep reporting the last good pass as current."""
        return self.enabled and self.age_s(now) > STALE_PASSES * self.interval_s + FIELDS_WAIT_S

    def last_pass(self) -> dict[str, Any] | None:
        return self.status.to_dict() if self.status.at or self.status.error else None


def interval_from_env(env: Mapping[str, str] | None = None) -> float:
    """PRINTOPS_SPOOL_SYNC_S: seconds between passes (0 = off, below the floor = the floor)."""
    env = os.environ if env is None else env
    raw = env.get("PRINTOPS_SPOOL_SYNC_S")
    if raw is None or not raw.strip():
        return DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        log.warning(
            "PRINTOPS_SPOOL_SYNC_S=%r is not a finite number >= 0; using %ss",
            raw,
            DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    if 0 < value < MIN_INTERVAL_S:
        log.warning(
            "PRINTOPS_SPOOL_SYNC_S=%s is below the %ss floor; using %ss",
            value,
            MIN_INTERVAL_S,
            MIN_INTERVAL_S,
        )
        return MIN_INTERVAL_S
    return value


def pass_once(state: ApiState, svc: SpoolSyncService) -> None:
    """One scheduled pass in a worker thread; never raises (a background loop outlives a bad pass)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = open_db(state.settings.db_path)  # inside the try: a locked DB must not end the loop
        sm = state.synced(conn)
        if sm is None:
            svc.status = sync.PassStatus(error="SPOOLMAN_URL is not set")
            return
        _, svc.status = sync.run_pass(conn, sm)
    except SpoolmanError as e:
        log.warning("spool sync: Spoolman unavailable: %s", e.message)
        svc.status = sync.PassStatus(
            at=svc.status.at,
            checked=svc.status.checked,
            drift=svc.status.drift,
            error=e.message,
        )
    except Exception as e:  # a background loop must outlive any one bad pass
        log.exception("spool sync pass failed")
        svc.status = sync.PassStatus(
            at=svc.status.at,
            checked=svc.status.checked,
            drift=svc.status.drift,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        if conn is not None:
            conn.close()


async def _wait_for_fields(state: ApiState) -> None:
    """The heal writes PrintOps extra fields: let the registration finish (or fail) first."""
    waited = 0.0
    while state.fields.get("status") == "pending" and waited < FIELDS_WAIT_S:
        await asyncio.sleep(1.0)
        waited += 1.0


async def _loop(state: ApiState, svc: SpoolSyncService) -> None:
    await _wait_for_fields(state)
    while True:
        await run_in_threadpool(pass_once, state, svc)
        await asyncio.sleep(svc.interval_s)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: ApiState = app.state.printops
    svc = SpoolSyncService(interval_from_env())
    app.state.spool_sync = svc
    task: asyncio.Task[None] | None = None
    if svc.enabled and state.spoolman is not None:
        task = asyncio.create_task(_loop(state, svc))
        log.info("spool sync: every %ss", svc.interval_s)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


router = APIRouter(lifespan=lifespan)


def _service(request: Request) -> SpoolSyncService:
    svc: SpoolSyncService | None = getattr(request.app.state, "spool_sync", None)
    if svc is None:
        raise HTTPException(503, "spool sync not started (app lifespan did not run)")
    return svc


def _spoolman_error(e: SpoolmanError) -> HTTPException:
    return HTTPException(502 if e.status in (0, 500, 502, 503, 504) else 422, e.message)


@router.get("/v1/spool-sync", dependencies=READ)
def drift(request: Request, conn: Db, spool: int | None = None) -> dict[str, Any]:
    state = get_state(request)
    sm = state.synced(conn)
    if sm is None:
        raise HTTPException(503, "Spoolman is not configured (set SPOOLMAN_URL)")
    try:
        report = sync.detect(conn, sm, only=spool)
    except SpoolmanError as e:
        raise _spoolman_error(e) from e
    svc = _service(request)
    return {
        **report.to_dict(),
        "last_pass": svc.last_pass(),
        "interval_s": svc.interval_s,
    }


@router.get("/v1/spool-sync/status")
def status(request: Request, conn: Db) -> dict[str, Any]:
    svc = _service(request)
    return {
        "enabled": svc.enabled,
        "interval_s": svc.interval_s,
        "last_pass": svc.last_pass(),
        "age_s": round(svc.age_s()),
        "stale": svc.stale(),
        "anomalies_open": len(rc.list_anomalies(conn, scope=sync.SCOPE)),
    }


@router.post("/v1/spool-sync/confirm-deleted")
def confirm_deleted(request: Request, conn: Db, who: Writer) -> dict[str, Any]:
    sm = get_state(request).synced(conn)
    if sm is None:
        raise HTTPException(503, "Spoolman is not configured (set SPOOLMAN_URL)")
    try:
        with sync.PASS_LOCK:
            done = sync.confirm_deleted(conn, sm)
            after = sync.detect(conn, sm)
    except SpoolmanError as e:
        raise _spoolman_error(e) from e
    return {**done.to_dict(), "after": after.to_dict()}


@router.post("/v1/spool-sync/heal")
def heal(
    request: Request,
    conn: Db,
    who: Writer,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    state = get_state(request)
    sm = state.synced(conn)
    if sm is None:
        raise HTTPException(503, "Spoolman is not configured (set SPOOLMAN_URL)")
    only = (body or {}).get("spool_id")
    if only is not None and (isinstance(only, bool) or not isinstance(only, int)):
        raise HTTPException(422, "spool_id must be an integer")
    try:
        report, status_ = sync.run_pass(conn, sm, only=only)
    except SpoolmanError as e:
        raise _spoolman_error(e) from e
    if only is None:
        _service(request).status = status_
    return report.to_dict()
