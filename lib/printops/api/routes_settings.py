"""routes_settings.py — 3d-api settings-change journal: import of the offline buffers and history (#264).

  POST /v1/settings/import   {lines: [str], file?, spools?: {uid: spool_id}, methods?: {uid: method},
                              dry_run?}                                                   write
  GET  /v1/settings/history?printer=&kind=&since=&limit=                                  read

`import` takes one buffer file's text lines (schema printops.settings_change/1) and answers
per line imported / skipped / refused with counts; idempotent by uid. `spools` and `methods`
supply what a calibration line leaves open — the spool it was measured with, the catalog method
(`3d settings import --spool / --method`).
Domain logic and the calibration mapping: printops.settings_history. Errors:
{"detail": message, "info": {...}} (422 malformed buffer line with its number).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from printops import settings_history as sh
from printops.api.deps import Db, Principal, require
from printops.api.routes_calibrations import _ctx

router = APIRouter()

Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
JsonBody = Annotated[dict[str, Any], Body()]


def _error(e: sh.HistoryError) -> JSONResponse:
    return JSONResponse({"detail": e.message, "info": e.detail}, status_code=e.status)


def _spools(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in raw.items()
    ):
        raise sh.HistoryError(422, "spools must be an object {uid: spool id}")
    return dict(raw)


def _methods(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in raw.items()
    ):
        raise sh.HistoryError(422, "methods must be an object {uid: method}")
    return dict(raw)


@router.post("/v1/settings/import", response_model=None)
def import_buffer(request: Request, conn: Db, who: Writer, body: JsonBody) -> Any:
    ctx = _ctx(request, conn, who)
    try:
        unknown = sorted(
            k
            for k in body
            if k not in ("lines", "file", "spools", "methods", "dry_run")
        )
        if unknown:
            raise sh.HistoryError(422, f"unknown field(s) {', '.join(unknown)}")
        lines = body.get("lines")
        if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
            raise sh.HistoryError(422, "lines must be a list of buffer lines (strings)")
        file = body.get("file")
        if file is not None and not isinstance(file, str):
            raise sh.HistoryError(422, "file must be a string")
        dry_run = body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise sh.HistoryError(422, "dry_run must be true or false")
        report = sh.import_lines(
            ctx,
            lines,
            file=file,
            spools=_spools(body.get("spools")),
            methods=_methods(body.get("methods")),
            dry_run=dry_run,
        )
    except sh.HistoryError as e:
        return _error(e)
    return report.to_dict()


@router.get("/v1/settings/history", response_model=None)
def history(
    conn: Db,
    who: Reader,
    printer: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> Any:
    try:
        return {
            "changes": sh.history(
                conn, printer=printer, kind=kind, since=since, limit=limit
            )
        }
    except sh.HistoryError as e:
        return _error(e)
