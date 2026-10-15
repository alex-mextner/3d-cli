"""routes_drying.py — 3d-api drying endpoints and the periodic drying check (#162).

  POST /v1/drying/sessions               scope write. {dryer, spools[], temp_c?, hours?, rh_before?,
                                         weights_before?: {spool_id: g}, source?, note?} → 201
                                         {session, warnings}. Missing temp/hours come from the
                                         spools' recipes (fleet/policy.yaml); missing rh_before is
                                         read from the dryer's humidity sensor (HA) when configured.
  POST /v1/drying/sessions/{id}/finish   scope write. {rh_after?, weights_after?} → {session, warnings};
                                         Spoolman last_dried_at + dry_state=fresh, weights → ledger notes.
  GET  /v1/drying/sessions               scope read. ?open=true&spool=ID&limit=N → {sessions}
  GET  /v1/drying/sessions/{id}          scope read.
  GET  /v1/drying/due                    scope read. ?all=true → every spool's assessment, else
                                         only due/wet. Read-only: writes nothing to Spoolman.
  POST /v1/drying/check                  scope write. Run the periodic check now → {checked,
                                         changed, alerts, errors, due}.

BACKGROUND: the router lifespan (merged into the app's by FastAPI) runs `_loop`: every
TIMER_TICK_S it fires elapsed session timers (`drying.finished` trigger=timer); every
`drying.check_every_min` (policy) it runs `drying.check` (dry_state + `drying.due`). Errors are
logged and retried next round; they never stop the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from printops import drying, locations
from printops.api.deps import Db, Principal, get_spoolman, require
from printops.api.state import ApiState
from printops.db import open_db
from printops.drying_policy import DryingPolicy
from printops.homeassistant import HaError, HaStates
from printops.policy import PolicyError, section
from printops.spoolman import Spoolman, SpoolmanError

log = logging.getLogger("printops.drying")

TIMER_TICK_S = 60.0
# The first check waits a little after startup (Spoolman may still be coming up with us).
FIRST_CHECK_DELAY_S = 60.0
FALLBACK_CHECK_MIN = 30


def load_policy() -> DryingPolicy:
    try:
        policy: DryingPolicy = section("drying")
    except PolicyError as e:
        raise HTTPException(503, f"drying policy unusable: {e}") from e
    return policy


def humidity_reader() -> HaStates | None:
    """Home Assistant from PRINTOPS_HA_* (re-read per call so a token change needs no restart)."""
    try:
        return HaStates.from_env()
    except (HaError, OSError) as e:
        log.error("Home Assistant humidity disabled: %s", e)
        return None


@contextlib.contextmanager
def _domain_errors() -> Iterator[None]:
    try:
        yield
    except (drying.SessionNotFound, locations.LocationNotFound) as e:
        raise HTTPException(404, str(e).strip("'\"")) from e
    except drying.SessionConflict as e:
        raise HTTPException(409, str(e)) from e
    except drying.DryingError as e:
        raise HTTPException(400, str(e)) from e
    except SpoolmanError as e:
        raise HTTPException(502, f"Spoolman: {e.message}") from e


async def _loop(state: ApiState) -> None:
    next_check = time.monotonic() + FIRST_CHECK_DELAY_S
    while True:
        run_check = time.monotonic() >= next_check
        try:
            minutes = await run_in_threadpool(_tick, state, run_check)
        except (
            Exception
        ):  # one bad round (Spoolman down, broken policy) must not end the loop
            log.exception("drying round failed (retry next round)")
            minutes = FALLBACK_CHECK_MIN
        if run_check:
            next_check = time.monotonic() + minutes * 60
        await asyncio.sleep(TIMER_TICK_S)


def _tick(state: ApiState, run_check: bool) -> int:
    """Fire elapsed timers and, when due, run the check; returns minutes until the next check."""
    conn = open_db(state.settings.db_path)
    try:
        for event in drying.fire_timers(conn):
            log.info("drying timer elapsed: %s", event.subject)
        if not run_check:
            return FALLBACK_CHECK_MIN
        policy: DryingPolicy = section("drying")
        if state.spoolman is not None:
            report = drying.check(
                conn, state.spoolman, policy, humidity=humidity_reader()
            )
            if report.changed or report.errors:
                log.info(
                    "drying check: changed=%s alerts=%s errors=%s",
                    report.changed,
                    report.alerts,
                    report.errors,
                )
        return policy.check_every_min
    finally:
        conn.close()


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


class StartBody(BaseModel):
    dryer: str = Field(description="dryer location path (e.g. dryer:1) or id")
    spools: list[int] = Field(min_length=1, max_length=16)
    temp_c: float | None = Field(default=None, ge=20, le=150)
    hours: float | None = Field(default=None, gt=0, le=96)
    rh_before: float | None = Field(default=None, ge=0, le=100)
    weights_before: dict[int, float] | None = None
    source: str = "api"
    note: str | None = Field(default=None, max_length=500)


class FinishBody(BaseModel):
    rh_after: float | None = Field(default=None, ge=0, le=100)
    weights_after: dict[int, float] | None = None


@router.post("/v1/drying/sessions", status_code=201)
def start_session(
    body: StartBody,
    conn: Db,
    who: Annotated[Principal, Depends(require("write"))],
    spoolman: Annotated[Spoolman, Depends(get_spoolman)],
) -> dict[str, Any]:
    policy = load_policy()
    dryer: str | int = (
        int(body.dryer) if body.dryer.isascii() and body.dryer.isdigit() else body.dryer
    )
    with _domain_errors():
        session, warnings = drying.start(
            conn,
            spoolman,
            policy,
            dryer=dryer,
            spool_ids=body.spools,
            actor=who.name,
            source=body.source,
            temp_c=body.temp_c,
            hours=body.hours,
            rh_before=body.rh_before,
            weights_before=body.weights_before,
            note=body.note,
            humidity=humidity_reader(),
        )
    return {"session": session.to_dict(), "warnings": warnings}


@router.post("/v1/drying/sessions/{session_id}/finish")
def finish_session(
    session_id: int,
    conn: Db,
    who: Annotated[Principal, Depends(require("write"))],
    spoolman: Annotated[Spoolman, Depends(get_spoolman)],
    body: FinishBody | None = None,
) -> dict[str, Any]:
    body = body or FinishBody()
    with _domain_errors():
        session, warnings = drying.finish(
            conn,
            spoolman,
            session_id=session_id,
            actor=who.name,
            rh_after=body.rh_after,
            weights_after=body.weights_after,
            humidity=humidity_reader(),
        )
    return {"session": session.to_dict(), "warnings": warnings}


@router.get("/v1/drying/sessions", dependencies=[Depends(require("read"))])
def list_sessions(
    conn: Db,
    open_only: Annotated[bool, Query(alias="open")] = False,
    spool: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> dict[str, Any]:
    rows = drying.list_sessions(conn, open_only=open_only, spool_id=spool, limit=limit)
    return {"sessions": [s.to_dict() for s in rows]}


@router.get("/v1/drying/sessions/{session_id}", dependencies=[Depends(require("read"))])
def get_session(session_id: int, conn: Db) -> dict[str, Any]:
    with _domain_errors():
        return drying.get_session(conn, session_id).to_dict()


@router.get("/v1/drying/due", dependencies=[Depends(require("read"))])
def due(
    conn: Db,
    spoolman: Annotated[Spoolman, Depends(get_spoolman)],
    show_all: Annotated[bool, Query(alias="all")] = False,
) -> JSONResponse:
    policy = load_policy()
    with _domain_errors():
        rows = drying.assess_all(conn, spoolman, policy, humidity=humidity_reader())
    shown = rows if show_all else [a for a in rows if a.alert]
    return JSONResponse({"checked": len(rows), "spools": [a.to_dict() for a in shown]})


@router.post("/v1/drying/check")
def run_check(
    conn: Db,
    who: Annotated[Principal, Depends(require("write"))],
    spoolman: Annotated[Spoolman, Depends(get_spoolman)],
) -> dict[str, Any]:
    policy = load_policy()
    with _domain_errors():
        drying.fire_timers(conn)
        report = drying.check(
            conn, spoolman, policy, humidity=humidity_reader(), actor=who.name
        )
    return report.to_dict()
