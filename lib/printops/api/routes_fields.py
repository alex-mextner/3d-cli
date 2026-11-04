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

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from printops import filament_defaults, native_fields
from printops.api.deps import Principal, get_spoolman, require
from printops.spoolman import Spoolman, SpoolmanError

router = APIRouter()

Sm = Annotated[Spoolman, Depends(get_spoolman)]
Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
OptBody = Annotated[dict[str, Any] | None, Body()]


def _migrate(sm: Spoolman, *, apply: bool) -> Any:
    try:
        return native_fields.migrate_native(sm, apply=apply).to_dict()
    except SpoolmanError as e:
        return JSONResponse({"detail": e.message, "info": {}}, status_code=502)


@router.get("/v1/fields/migrate-native", response_model=None)
def migrate_native_dry_run(sm: Sm, who: Reader) -> Any:
    return _migrate(sm, apply=False)


@router.post("/v1/fields/migrate-native", response_model=None)
def migrate_native_apply(sm: Sm, who: Writer, body: OptBody = None) -> Any:
    return _migrate(sm, apply=True)


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
