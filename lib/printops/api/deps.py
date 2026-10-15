"""deps.py — FastAPI dependencies every routes_*.py module uses: auth scopes, DB, Spoolman.

Usage in a slice's routes file:

    from fastapi import APIRouter, Depends
    from printops.api.deps import Db, Principal, get_spoolman, require

    router = APIRouter()

    @router.post("/v1/spools/{spool_id}/measure")
    def measure(spool_id: int, conn: Db, who: Principal = Depends(require("write"))) -> dict: ...

INVARIANTS:
  - Every route except `GET /v1/health` requires a bearer token (`Authorization: Bearer po_…`);
    missing/unknown token → 401, token without the route's scope → 403.
  - `Db` is a fresh connection per request (closed afterwards); slices wrap multi-statement
    writes in `printops.db.transaction(conn)`.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from printops.api.state import ApiState
from printops.db import open_db
from printops.spoolman import Spoolman
from printops.tokens import SCOPES, TokenRecord


def get_state(request: Request) -> ApiState:
    state: ApiState = request.app.state.printops
    return state


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def authenticate(state: ApiState, header: str | None, scope: str) -> TokenRecord:
    """The token record for an Authorization header, or HTTPException 401/403."""
    token = bearer_token(header)
    if token is None:
        raise HTTPException(401, "missing bearer token (Authorization: Bearer po_…)",
                            headers={"WWW-Authenticate": "Bearer"})
    record = state.tokens.verify(token)
    if record is None:
        raise HTTPException(401, "unknown or revoked token", headers={"WWW-Authenticate": "Bearer"})
    if scope not in record.scopes:
        raise HTTPException(403, f"token {record.name!r} lacks scope {scope!r} (has: {', '.join(record.scopes)})")
    return record


def require(scope: str) -> Callable[[Request], TokenRecord]:
    """Dependency factory: the caller's token must carry `scope`."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; accepted: {', '.join(SCOPES)}")

    def dependency(request: Request) -> TokenRecord:
        return authenticate(get_state(request), request.headers.get("authorization"), scope)

    return dependency


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = open_db(get_state(request).settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_spoolman(request: Request) -> Spoolman:
    sp = get_state(request).spoolman
    if sp is None:
        raise HTTPException(503, "Spoolman is not configured (set SPOOLMAN_URL)")
    return sp


Db = Annotated[sqlite3.Connection, Depends(get_db)]
Principal = TokenRecord
