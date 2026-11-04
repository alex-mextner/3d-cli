"""routes_fields.py — migrate the legacy PrintOps extra fields to Spoolman's native fields.

  GET  /v1/fields/migrate-native   scope read    dry run: what `POST` would change; writes nothing
  POST /v1/fields/migrate-native   scope write   {} (body optional) apply it
  GET  /v1/fields/fill-defaults    scope read    dry run: filament max_speed_mm_s where empty
  POST /v1/fields/fill-defaults    scope write   {} (body optional) apply it; never overwrites a value

Answer: printops.native_fields.MigrationReport.to_dict() — `changes[]` per item (field, item_id, item,
action copy|drop|adjust|link|fill|conflict|invalid|failed, legacy, native, detail), `summary`
counts, `fields[]` (the fate of each legacy field definition), `blocked` (a conflict / invalid /
failed item remains), `pending` (dry run: it would still write). 503 without Spoolman, 502 when
Spoolman fails. docs/printops/native-fields.md has the field audit.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from printops import filament_defaults, locations, native_fields
from printops.api.deps import Db, Principal, get_spoolman, require
from printops.spoolman import Spoolman, SpoolmanError

router = APIRouter()

Sm = Annotated[Spoolman, Depends(get_spoolman)]
Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
OptBody = Annotated[dict[str, Any] | None, Body()]


def _location_path(conn: sqlite3.Connection) -> native_fields.LocationPath:
    """PrintOps location id -> the path Spoolman stores (None: no such location)."""

    def resolve(location_id: int) -> str | None:
        loc = locations.find(conn, location_id)
        return None if loc is None else locations.spoolman_path(loc)

    return resolve


def _migrate(sm: Spoolman, conn: sqlite3.Connection, *, apply: bool) -> Any:
    try:
        return native_fields.migrate_native(
            sm, apply=apply, location_path=_location_path(conn)
        ).to_dict()
    except SpoolmanError as e:
        return JSONResponse({"detail": e.message, "info": {}}, status_code=502)


@router.get("/v1/fields/migrate-native", response_model=None)
def migrate_native_dry_run(sm: Sm, conn: Db, who: Reader) -> Any:
    return _migrate(sm, conn, apply=False)


@router.post("/v1/fields/migrate-native", response_model=None)
def migrate_native_apply(sm: Sm, conn: Db, who: Writer, body: OptBody = None) -> Any:
    return _migrate(sm, conn, apply=True)


def _fill(sm: Spoolman, *, apply: bool) -> Any:
    try:
        return filament_defaults.fill_defaults(sm, apply=apply).to_dict()
    except SpoolmanError as e:
        return JSONResponse({"detail": e.message, "info": {}}, status_code=502)


@router.get("/v1/fields/fill-defaults", response_model=None)
def fill_defaults_dry_run(sm: Sm, who: Reader) -> Any:
    return _fill(sm, apply=False)


@router.post("/v1/fields/fill-defaults", response_model=None)
def fill_defaults_apply(sm: Sm, who: Writer, body: OptBody = None) -> Any:
    return _fill(sm, apply=True)
