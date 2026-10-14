"""routes_core.py — 3d-api core endpoints: health, the event log (JSON + SSE), and the WebSocket.

  GET /v1/live     no auth. Liveness: 200 {"ok": true} when the API serves and its DB opens.
  GET /v1/health   no auth. 200 {"status": "ok"|"degraded", db, spoolman, version};
                   503 when the PrintOps DB itself is unusable.
  GET /v1/events   scope read. `Accept: application/json` → one page
                   {"events": [...], "cursor": N} (`since=N` → id > N, or `last=K` → newest K).
                   Otherwise Server-Sent Events: `id:`/`event: <kind>`/`data: <event json>` frames
                   from `since` (or `Last-Event-ID`, else only new events), `: ping` every
                   HEARTBEAT_S. Reconnecting EventSource clients resume exactly via Last-Event-ID.
  WS  /v1/ws       first client message {"type": "auth", "token": "po_…", "since": N?} (tokens
                   never ride in URLs, which end up in access logs) → {"type": "auth_ok",
                   "cursor": N} then {"type": "event", "event": {...}} frames; a bad token gets
                   {"type": "auth_invalid"} and close code 4401.

Streams re-check their token before every read from the log: once a token is revoked (or
loses `read`), the SSE stream sends `event: auth_revoked` and ends, and the WebSocket sends
{"type": "auth_revoked"} and closes with 4401 — at the next event or heartbeat (≤ HEARTBEAT_S).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from printops import events as ev
from printops.api.deps import authenticate, bearer_token, get_state, require
from printops.api.state import ApiState
from printops.db import applied_versions, open_db
from printops.spoolman import SpoolmanError, missing_extra_fields
from printops.tokens import TokenRecord

router = APIRouter()

HEARTBEAT_S = 15.0
STREAM_BATCH = 500
WS_AUTH_TIMEOUT_S = 10.0


def _db_health(state: ApiState) -> dict[str, Any]:
    try:
        conn = open_db(state.settings.db_path)
        try:
            versions = sorted(applied_versions(conn))
            return {"ok": True, "migrations": versions, "last_event_id": ev.latest_id(conn)}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}


def _spoolman_health(state: ApiState) -> dict[str, Any]:
    sp = state.spoolman
    if sp is None:
        return {"ok": False, "error": "SPOOLMAN_URL is not set"}
    try:
        info = sp.info()
        missing = missing_extra_fields(sp)
    except SpoolmanError as e:
        return {"ok": False, "error": e.message, "extra_fields": state.fields}
    fields = dict(state.fields)
    fields["missing"] = missing
    return {"ok": not missing, "version": info.get("version"), "extra_fields": fields}


@router.get("/v1/live")
def live(request: Request) -> JSONResponse:
    """Liveness for the container healthcheck: the API serves and its DB opens. No Spoolman call,
    so a slow or down Spoolman never marks the API itself unhealthy (that is /v1/health's job)."""
    db = _db_health(get_state(request))
    return JSONResponse({"ok": db["ok"]}, status_code=200 if db["ok"] else 503)


@router.get("/v1/health")
def health(request: Request) -> JSONResponse:
    state = get_state(request)
    db = _db_health(state)
    spoolman = _spoolman_health(state)
    status = "ok" if db["ok"] and spoolman["ok"] else "degraded"
    body = {"status": status, "version": state.settings.version, "db": db, "spoolman": spoolman}
    return JSONResponse(body, status_code=200 if db["ok"] else 503)


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/event-stream" not in accept


def _sse_frame(event: ev.Event) -> str:
    return f"id: {event.id}\nevent: {event.kind}\ndata: {json.dumps(event.to_dict(), separators=(',', ':'))}\n\n"


def _authorized(state: ApiState, token: str, scope: str) -> Callable[[], bool]:
    """Re-check for long-lived streams: a revoked token (or one that lost the scope) must stop
    receiving events, not keep the stream it opened before the revocation."""

    def check() -> bool:
        record = state.tokens.verify(token)
        return record is not None and scope in record.scopes

    return check


async def _follow(state: ApiState, cursor: int | None,
                  authorized: Callable[[], bool]) -> AsyncIterator[ev.Event | None]:
    """Every event after `cursor` (None = after the current newest) while `authorized()` holds;
    None = heartbeat. Ends (returns) as soon as the token is no longer valid."""
    listener = ev.BUS.listener()  # before the first read, so no wake-up can slip between
    try:
        conn = open_db(state.settings.db_path)
    except BaseException:
        listener.close()
        raise
    try:
        if cursor is None:
            cursor = await run_in_threadpool(ev.latest_id, conn)
        while True:
            if not authorized():
                return
            batch = await run_in_threadpool(ev.since, conn, cursor, STREAM_BATCH)
            for event in batch:
                if not authorized():  # also between events of a backlog batch
                    return
                cursor = event.id
                yield event
            if len(batch) == STREAM_BATCH:
                continue
            if not await listener.wait(HEARTBEAT_S) and authorized():
                yield None
    finally:
        listener.close()
        conn.close()


@router.get("/v1/events", response_model=None)
async def list_events(
    request: Request,
    who: Annotated[TokenRecord, Depends(require("read"))],
    since: Annotated[int | None, Query(ge=0, le=ev.MAX_CURSOR)] = None,
    last: Annotated[int | None, Query(ge=1, le=ev.MAX_PAGE)] = None,
    limit: Annotated[int, Query(ge=1, le=ev.MAX_PAGE)] = 100,
) -> JSONResponse | StreamingResponse:
    state = get_state(request)
    if _wants_json(request):
        if since is not None and last is not None:
            raise HTTPException(422, "give either since or last, not both")

        def page() -> dict[str, Any]:
            conn = open_db(state.settings.db_path)
            try:
                rows = ev.last(conn, last) if last is not None else ev.since(conn, since or 0, limit)
                if rows:
                    cursor = rows[-1].id
                elif last is not None:
                    cursor = ev.latest_id(conn)
                else:
                    cursor = since or 0
                return {"events": [e.to_dict() for e in rows], "cursor": cursor}
            finally:
                conn.close()

        return JSONResponse(await run_in_threadpool(page))

    cursor = since
    last_event_id = request.headers.get("last-event-id")
    # len() first: int() of thousands of digits raises (CPython's int-conversion limit);
    # isascii(): isdigit() also accepts '²', which int() rejects
    if (cursor is None and last_event_id and len(last_event_id) <= 19 and last_event_id.isascii()
            and last_event_id.isdigit()
            and int(last_event_id) <= ev.MAX_CURSOR):
        cursor = int(last_event_id)

    token = bearer_token(request.headers.get("authorization")) or ""
    authorized = _authorized(state, token, "read")

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        async for event in _follow(state, cursor, authorized):
            if await request.is_disconnected():
                return
            yield ": ping\n\n" if event is None else _sse_frame(event)
        # The token was revoked: end the stream; a reconnect then gets a 401.
        yield f"event: auth_revoked\ndata: {json.dumps({'name': who.name})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/v1/ws")
async def websocket_events(ws: WebSocket) -> None:
    state: ApiState = ws.app.state.printops
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), WS_AUTH_TIMEOUT_S)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await _ws_close(ws, 4401, "expected {\"type\": \"auth\", \"token\": ...} first")
        return
    token = hello.get("token") if isinstance(hello, dict) and hello.get("type") == "auth" else None
    try:
        who = authenticate(state, f"Bearer {token}" if isinstance(token, str) else None, "read")
    except HTTPException as e:
        await ws.send_json({"type": "auth_invalid", "message": e.detail})
        await _ws_close(ws, 4401 if e.status_code == 401 else 4403, str(e.detail))
        return
    since = hello.get("since")
    valid = isinstance(since, int) and not isinstance(since, bool) and 0 <= since <= ev.MAX_CURSOR
    cursor = since if valid else None
    if cursor is None:
        conn = open_db(state.settings.db_path)
        try:
            cursor = ev.latest_id(conn)
        finally:
            conn.close()
    await ws.send_json({"type": "auth_ok", "name": who.name, "cursor": cursor})

    authorized = _authorized(state, str(token), "read")

    async def pump() -> None:
        async for event in _follow(state, cursor, authorized):
            if event is not None:
                await ws.send_json({"type": "event", "event": event.to_dict()})
            else:
                await ws.send_json({"type": "ping"})
        await ws.send_json({"type": "auth_revoked"})
        await _ws_close(ws, 4401, "token revoked")

    async def drain() -> None:
        # Client messages are not commands (yet); reading them is how a disconnect is noticed.
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await ws.receive_text()

    pump_task = asyncio.create_task(pump())
    drain_task = asyncio.create_task(drain())
    done, pending = await asyncio.wait({pump_task, drain_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await task
    for task in done:
        exc = task.exception()
        if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
            raise exc


async def _ws_close(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await ws.close(code=code, reason=reason[:120])

