"""routes_spools.py — 3d-api spool lifecycle, ledger, weighing and tare endpoints (#160, #163).

  GET  /v1/spools?material=&color=&vendor=&location=&loaded=&archived=     read   list + intervals
  POST /v1/spools                      {Receive fields}                   write  receive
  POST /v1/spools/import               {items: [Receive + external_key], dry_run}  write  idempotent import
  GET  /v1/spools/{id}?ledger=N                                            read   spool + ledger + tare
  GET  /v1/spools/{id}/ledger                                              read   all ledger rows
  POST /v1/spools/{id}/move            {location}                         write
  POST /v1/spools/{id}/use             {grams, source, confidence_g?, job_id?, note?}  write  Spoolman /use + ledger
  POST /v1/spools/{id}/measure         {gross_g, tare_g?, tare_source?, scale_sigma_g?, note?}  write  /measure + anchor
  POST /v1/spools/{id}/empty           {note?}                            write  anchor 0, archive
  POST /v1/printers/{p}/slots/{s}/load    {spool_id, replace_to?}         write  s = head label (T3) or ams-id/n
  POST /v1/printers/{p}/slots/{s}/unload  {to?, gross_g?, spool_id?}      write
  GET  /v1/tares?vendor=                                                   read   the tare DB
  POST /v1/tares                       {vendor, tare_g, source, spool_kind?, net_g?, sigma_g?, note?}  write
  GET  /v1/spools/{id}/tare                                                read   the tare chain for a spool

An AMS slot is addressed as `/v1/printers/p1s/slots/ams1/2/load` (the path segment `{s:path}`).
Domain logic: printops.spools / ledger / tares. Errors: SpoolError.status with {"detail": message,
"info": {...}} (409 occupied / unusable or faulted head / no tare, 404 unknown, 422 invalid, 502 Spoolman).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from printops import ledger, spools, tares
from printops.api.deps import Db, Principal, get_spoolman, require
from printops.spoolman import Spoolman

router = APIRouter()

Sm = Annotated[Spoolman, Depends(get_spoolman)]
Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
JsonBody = Annotated[dict[str, Any], Body()]
OptBody = Annotated[dict[str, Any] | None, Body()]


def _call(fn: Callable[[], Any]) -> Any:
    """Run a domain call; its SpoolError / TareError / LedgerError become a JSON error answer."""
    try:
        return fn()
    except spools.SpoolError as e:
        return JSONResponse(
            {"detail": e.message, "info": e.detail}, status_code=e.status
        )
    except (tares.TareError, ledger.LedgerError) as e:
        return JSONResponse({"detail": str(e), "info": {}}, status_code=422)


def _ctx(conn: Any, sm: Spoolman, who: Principal) -> spools.Ctx:
    return spools.Ctx(conn=conn, spoolman=sm, actor=who.name)


def _num(body: dict[str, Any], key: str, *, required: bool = False) -> float | None:
    v = body.get(key)
    if v is None:
        if required:
            raise spools.SpoolError(422, f"{key} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise spools.SpoolError(422, f"{key} must be a number, got {v!r}")
    return float(v)


def _str(body: dict[str, Any], key: str) -> str | None:
    v = body.get(key)
    if v is not None and not isinstance(v, str):
        raise spools.SpoolError(422, f"{key} must be a string, got {v!r}")
    return v


@router.get("/v1/spools", response_model=None)
def list_spools(
    conn: Db,
    sm: Sm,
    who: Reader,
    material: str | None = None,
    color: str | None = None,
    vendor: str | None = None,
    location: str | None = None,
    loaded: bool | None = None,
    archived: bool = False,
) -> Any:
    return _call(
        lambda: {
            "spools": spools.list_spools(
                _ctx(conn, sm, who),
                material=material,
                color=color,
                vendor=vendor,
                location=location,
                loaded=loaded,
                archived=archived,
            )
        }
    )


@router.post("/v1/spools", response_model=None, status_code=201)
def receive(conn: Db, sm: Sm, who: Writer, body: JsonBody) -> Any:
    return _call(
        lambda: spools.receive(_ctx(conn, sm, who), spools.Receive.from_dict(body))
    )


@router.post("/v1/spools/import", response_model=None)
def import_spools(conn: Db, sm: Sm, who: Writer, body: JsonBody) -> Any:
    def go() -> Any:
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise spools.SpoolError(422, "items must be a list of spool objects")
        return spools.import_spools(
            _ctx(conn, sm, who), items, dry_run=bool(body.get("dry_run", True))
        )

    return _call(go)


@router.get("/v1/spools/{spool_id}", response_model=None)
def show(
    conn: Db,
    sm: Sm,
    who: Reader,
    spool_id: int,
    ledger_rows: Annotated[int, Query(alias="ledger", ge=1, le=10000)] = 50,
) -> Any:
    return _call(
        lambda: spools.show(_ctx(conn, sm, who), spool_id, ledger_rows=ledger_rows)
    )


@router.get("/v1/spools/{spool_id}/ledger", response_model=None)
def spool_ledger(conn: Db, sm: Sm, who: Reader, spool_id: int) -> Any:
    def go() -> Any:
        spools.get_spool(sm, spool_id)  # 404 for a spool Spoolman does not know
        est = ledger.estimate(conn, spool_id)
        return {
            "spool_id": spool_id,
            "rows": [r.to_dict() for r in ledger.history(conn, spool_id)],
            "remaining": est.to_dict() if est.anchored else None,
        }

    return _call(go)


@router.get("/v1/spools/{spool_id}/tare", response_model=None)
def spool_tare(conn: Db, sm: Sm, who: Reader, spool_id: int) -> Any:
    return _call(lambda: tares.resolve(conn, spools.get_spool(sm, spool_id)).to_dict())


@router.post("/v1/spools/{spool_id}/move", response_model=None)
def move(conn: Db, sm: Sm, who: Writer, spool_id: int, body: JsonBody) -> Any:
    def go() -> Any:
        loc = _str(body, "location")
        if not loc:
            raise spools.SpoolError(422, "location is required")
        return spools.move(_ctx(conn, sm, who), spool_id, loc)

    return _call(go)


@router.post("/v1/spools/{spool_id}/use", response_model=None)
def use(conn: Db, sm: Sm, who: Writer, spool_id: int, body: JsonBody) -> Any:
    def go() -> Any:
        grams = _num(body, "grams", required=True)
        assert grams is not None
        return spools.use(
            _ctx(conn, sm, who),
            spool_id,
            grams,
            _str(body, "source") or "manual",
            confidence_g=_num(body, "confidence_g"),
            job_id=_str(body, "job_id"),
            note=_str(body, "note"),
        )

    return _call(go)


@router.post("/v1/spools/{spool_id}/measure", response_model=None)
def measure(conn: Db, sm: Sm, who: Writer, spool_id: int, body: JsonBody) -> Any:
    def go() -> Any:
        gross = _num(body, "gross_g", required=True)
        assert gross is not None
        return spools.measure(
            _ctx(conn, sm, who),
            spool_id,
            gross,
            tare_g=_num(body, "tare_g"),
            tare_source=_str(body, "tare_source"),
            scale_sigma_g=_num(body, "scale_sigma_g"),
            note=_str(body, "note"),
        )

    return _call(go)


@router.post("/v1/spools/{spool_id}/empty", response_model=None)
def empty(conn: Db, sm: Sm, who: Writer, spool_id: int, body: OptBody = None) -> Any:
    return _call(
        lambda: spools.empty(
            _ctx(conn, sm, who), spool_id, note=_str(body or {}, "note")
        )
    )


@router.post("/v1/printers/{printer}/slots/{slot:path}/load", response_model=None)
def load(conn: Db, sm: Sm, who: Writer, printer: str, slot: str, body: JsonBody) -> Any:
    def go() -> Any:
        sid = body.get("spool_id")
        if isinstance(sid, bool) or not isinstance(sid, int):
            raise spools.SpoolError(422, "spool_id (integer) is required")
        return spools.load(
            _ctx(conn, sm, who), printer, slot, sid, replace_to=_str(body, "replace_to")
        )

    return _call(go)


@router.post("/v1/printers/{printer}/slots/{slot:path}/unload", response_model=None)
def unload(
    conn: Db, sm: Sm, who: Writer, printer: str, slot: str, body: OptBody = None
) -> Any:
    def go() -> Any:
        b = body or {}
        sid = b.get("spool_id")
        if sid is not None and (isinstance(sid, bool) or not isinstance(sid, int)):
            raise spools.SpoolError(422, "spool_id must be an integer")
        return spools.unload(
            _ctx(conn, sm, who),
            printer,
            slot,
            to=_str(b, "to"),
            gross_g=_num(b, "gross_g"),
            spool_id=sid,
        )

    return _call(go)


@router.get("/v1/tares", response_model=None)
def list_tares(conn: Db, who: Reader, vendor: str | None = None) -> Any:
    return {"tares": [t.to_dict() for t in tares.list_tares(conn, vendor)]}


@router.post("/v1/tares", response_model=None, status_code=201)
def add_tare(conn: Db, who: Writer, body: JsonBody) -> Any:
    def go() -> Any:
        vendor, source = _str(body, "vendor"), _str(body, "source")
        grams = _num(body, "tare_g", required=True)
        if not vendor or not source or grams is None:
            raise spools.SpoolError(422, "vendor, tare_g and source are required")
        return tares.add_tare(
            conn,
            vendor=vendor,
            tare_g=grams,
            source=source,
            actor=who.name,
            spool_kind=_str(body, "spool_kind"),
            net_g=_num(body, "net_g"),
            sigma_g=_num(body, "sigma_g"),
            note=_str(body, "note"),
        ).to_dict()

    return _call(go)
