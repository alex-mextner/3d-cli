"""routes_calibrations.py — 3d-api calibration records, requirements and the `calibration_current` gate (#167).

  GET  /v1/calibrations?printer=&head=&kind=&status=&current=&limit=     read   records, newest first
  GET  /v1/calibrations/required?printer=&head=|slot=&spool=&plate=&all=  read   what the head(s) need
  GET  /v1/calibrations/slicer-preset?printer=&head=&spool=               read   {printer, head, spool_id, preset|null,
                                                                          scope spool|filament|null, bound}: the slicer
                                                                          filament preset of the spool on the head (#294)
  POST /v1/calibrations/check   {printer, footer, used_tools, spools?, accept?, reason?, plate?,
                                 gcode_sha?, file?}                       read (+write with accept)
  POST /v1/calibrations/runs    {kind, method, printer?, head?, spool_id?|filament_id?, lot_nr?,
                                 plate?, params?, gcode_path?, gcode_sha?, note?}   write  → planned
  POST /v1/calibrations         run fields + {values, evidence?, valid_until?}       write  manual record
  GET  /v1/calibrations/{id}                                              read
  POST /v1/calibrations/{id}/result  {values, evidence?, note?, valid_until?}       write  planned → recorded

`{id}` is the calibration id: a run IS the calibration row while it is `planned`.
`check` body: `footer` = the G-code's `; key = value` settings (at least the catalog's gcode keys
and filament_settings_id/filament_type), `used_tools` = 0-based tool indexes, `spools` = {tool:
spool_id} overriding the slot binding, `accept` = ["calibration:<kind>", …] with one `reason`
(needs the write scope: every acceptance is logged as a `calibration.accepted` event).
Domain logic: printops.calibrations. Errors: {"detail": message, "info": {...}} with
CalibrationError.status (404 unknown, 409 state, 422 invalid, 502 Spoolman, 503 config).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from printops import calibrations as cal
from printops.api.deps import Db, Principal, get_state, require
from printops.calibration_catalog import CatalogError, load_catalog
from printops.calibration_policy import CalibrationPolicy
from printops.fleet import FleetError, load_fleet
from printops.policy import PolicyError, section

router = APIRouter()

Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
JsonBody = Annotated[dict[str, Any], Body()]


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except cal.CalibrationError as e:
        return JSONResponse({"detail": e.message, "info": e.detail}, status_code=e.status)


def _ctx(request: Request, conn: Any, who: Principal) -> cal.Ctx:
    """Catalog, policy and fleet are re-read per request: editing the files needs no restart."""
    try:
        catalog = load_catalog()
        policy: CalibrationPolicy = section("calibrations")
        fleet = load_fleet()
    except (CatalogError, PolicyError, FleetError) as e:
        raise HTTPException(503, f"calibration engine unusable: {e}") from e
    if fleet is None:
        raise HTTPException(503, "no fleet registry (fleet/printers.yaml): calibrations need printer heads")
    return cal.Ctx(conn=conn, spoolman=get_state(request).spoolman, actor=who.name,
                   catalog=catalog, policy=policy, fleet=fleet)


@router.get("/v1/calibrations", response_model=None)
def list_records(
    conn: Db,
    who: Reader,
    printer: str | None = None,
    head: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    mix: str | None = None,
    current: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> Any:
    return _call(lambda: {"calibrations": [
        c.to_dict() for c in cal.list_calibrations(conn, printer=printer, head=head, kind=kind,
                                                   status=status, mix=mix, current=current, limit=limit)
    ]})


@router.get("/v1/calibrations/required", response_model=None)
def required(
    request: Request,
    conn: Db,
    who: Reader,
    printer: str,
    head: str | None = None,
    slot: str | None = None,
    spool: int | None = None,
    plate: str | None = None,
    all: bool = False,  # noqa: A002 — the public query parameter name
) -> Any:
    if head is not None and slot is not None and head != slot:
        return JSONResponse({"detail": "pass head or slot (the same thing), not both", "info": {}},
                            status_code=422)
    ctx = _ctx(request, conn, who)
    return _call(lambda: cal.requirements(ctx, printer, head=head or slot, spool_id=spool,
                                          plate=plate).to_dict(all_items=all))


@router.get("/v1/calibrations/slicer-preset", response_model=None)
def slicer_preset(
    request: Request, conn: Db, who: Reader, printer: str, head: str | None = None, spool: int | None = None
) -> Any:
    ctx = _ctx(request, conn, who)
    return _call(lambda: cal.slicer_preset(ctx, printer, head=head, spool_id=spool))


def _footer(body: dict[str, Any]) -> dict[str, str]:
    raw = body.get("footer")
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise cal.CalibrationError(422, "footer must be an object of G-code `; key = value` strings")
    return dict(raw)


def _tools(body: dict[str, Any]) -> list[int]:
    raw = body.get("used_tools")
    if not isinstance(raw, list) or not raw or not all(
        isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in raw
    ):
        raise cal.CalibrationError(422, "used_tools must be a non-empty list of 0-based tool indexes")
    return list(raw)


def _spools(body: dict[str, Any]) -> dict[int, int]:
    raw = body.get("spools") or {}
    if not isinstance(raw, dict):
        raise cal.CalibrationError(422, "spools must be an object {tool index: spool id}")
    out: dict[int, int] = {}
    for k, v in raw.items():
        if not str(k).isdigit() or isinstance(v, bool) or not isinstance(v, int):
            raise cal.CalibrationError(422, f"spools: {k!r}: {v!r} must be tool index → spool id")
        out[int(k)] = v
    return out


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    v = body.get(key)
    if v is not None and not isinstance(v, str):
        raise cal.CalibrationError(422, f"{key} must be a string")
    return v


@router.post("/v1/calibrations/check", response_model=None)
def check(request: Request, conn: Db, who: Reader, body: JsonBody) -> Any:
    ctx = _ctx(request, conn, who)

    def go() -> Any:
        printer = _opt_str(body, "printer")
        if not printer:
            raise cal.CalibrationError(422, "printer is required")
        raw_accept = body.get("accept") or []
        if not isinstance(raw_accept, list) or not all(isinstance(a, str) for a in raw_accept):
            raise cal.CalibrationError(422, "accept must be a list like [\"calibration:flow_ratio\"]")
        accept = cal.parse_accept(raw_accept, _opt_str(body, "reason"), ctx.catalog)
        if accept and "write" not in who.scopes:
            raise cal.CalibrationError(403, f"token {who.name!r} lacks scope 'write' (accepting a FAIL is logged)")
        return cal.check_gcode(
            ctx, printer, _footer(body), _tools(body),
            spools=_spools(body), accept=accept, plate=_opt_str(body, "plate"),
            gcode_sha=_opt_str(body, "gcode_sha"), file=_opt_str(body, "file"),
        ).to_dict()

    return _call(go)


@router.post("/v1/calibrations/runs", response_model=None, status_code=201)
def start_run(request: Request, conn: Db, who: Writer, body: JsonBody) -> Any:
    ctx = _ctx(request, conn, who)

    def go() -> Any:
        for key in ("values", "evidence", "valid_until"):
            if key in body:
                raise cal.CalibrationError(422, f"{key} belongs to the result (POST /v1/calibrations/{{id}}/result)")
        return cal.start_run(ctx, cal.RunRequest.from_dict(body)).to_dict()

    return _call(go)


@router.post("/v1/calibrations", response_model=None, status_code=201)
def record(request: Request, conn: Db, who: Writer, body: JsonBody) -> Any:
    ctx = _ctx(request, conn, who)
    return _call(lambda: cal.record(ctx, cal.RunRequest.from_dict(body), body).to_dict())


@router.get("/v1/calibrations/{cal_id}", response_model=None)
def show(conn: Db, who: Reader, cal_id: int) -> Any:
    return _call(lambda: cal.get(conn, cal_id).to_dict())


@router.post("/v1/calibrations/{cal_id}/result", response_model=None)
def result(request: Request, conn: Db, who: Writer, cal_id: int, body: JsonBody) -> Any:
    ctx = _ctx(request, conn, who)

    def go() -> Any:
        unknown = sorted(k for k in body if k not in ("values", "evidence", "note", "valid_until"))
        if unknown:
            raise cal.CalibrationError(422, f"unknown field(s) {', '.join(unknown)}")
        return cal.record_result(ctx, cal_id, body).to_dict()

    return _call(go)
