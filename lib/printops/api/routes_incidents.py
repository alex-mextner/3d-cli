"""routes_incidents.py — 3d-api incidents, their auto-context, rates and exposure (#165).

  POST /v1/incidents               write  {kind, severity, printer?, head?, spool_id?, at?, note?,
                                          tags?, photos?, reported_by?, confidence?, draft?,
                                          auto_context? (default true), <any snapshot field>}
                                          → 201 {incident, context}; reporter fields win over auto
  GET  /v1/incidents               read   ?status=open,draft&printer=&head=&kind=&spool=&lot=&since=90d&limit=
  GET  /v1/incidents/stats         read   ?by=vendor|filament|lot|printer|head&since=90d&kind=
                                          → rates per kg and per print-hour (+ parent group rate)
  GET  /v1/incidents/context       read   ?printer=&head=&spool= → what a report would auto-fill
  POST /v1/incidents/from-watch    write  a `3d printer watch` event JSON → draft (or merged/skipped)
  GET  /v1/incidents/{id}          read
  PATCH /v1/incidents/{id}         write  {field: value, add_tags?, remove_tags?, add_photos?}
  POST /v1/incidents/{id}/confirm  write  draft → open, optional corrections
  POST /v1/incidents/{id}/resolve  write  {resolution_kind, text?, calibration_id?, at?}
  POST /v1/incidents/{id}/dismiss  write  {reason?}
  GET  /v1/exposure                read   ?printer=&since=&limit= → rows + totals
  POST /v1/exposure                write  manual exposure {printer, head?, spool_id?, hours, grams?, started_at?, note?}
  POST /v1/exposure/sync           write  {printer, limit?} → Moonraker job history (read-only GET)

Domain: printops.incidents / incident_context / exposure / watch_incidents. Errors:
IncidentError.status → {"detail": message} (404 unknown, 409 state conflict, 422 invalid,
502 printer/Spoolman unreadable). Context readers: `app.state.incident_readers` when a test sets
it, else built from the environment per request (incident_context.Readers.from_env).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from printops import exposure, incidents, watch_incidents
from printops.api.deps import Db, Principal, get_state, require
from printops.incident_context import Readers, resolve
from printops.incidents import IncidentError, Report
from printops.spoolman import Spoolman

router = APIRouter()

Reader = Annotated[Principal, Depends(require("read"))]
Writer = Annotated[Principal, Depends(require("write"))]
JsonBody = Annotated[dict[str, Any], Body()]
OptBody = Annotated[dict[str, Any] | None, Body()]


def _call(fn: Callable[[], Any], status: int = 200) -> Any:
    try:
        out = fn()
    except IncidentError as e:
        return JSONResponse({"detail": e.message}, status_code=e.status)
    return JSONResponse(out, status_code=status) if status != 200 else out


def readers(request: Request) -> Readers:
    override = getattr(request.app.state, "incident_readers", None)
    if isinstance(override, Readers):
        return override
    return Readers.from_env(get_state(request).settings)


def spoolman_of(request: Request) -> Spoolman | None:
    return get_state(request).spoolman


Rd = Annotated[Readers, Depends(readers)]
Sm = Annotated[Spoolman | None, Depends(spoolman_of)]


def _since(raw: str | None) -> Any:
    return incidents.parse_since(raw) if raw else None


def _int_field(body: dict[str, Any], key: str) -> int | None:
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise IncidentError(422, f"{key} must be an integer, got {v!r}")
    return v


@router.post("/v1/incidents", response_model=None)
def report(conn: Db, sm: Sm, rd: Rd, who: Writer, body: JsonBody) -> Any:
    def go() -> Any:
        rep = Report.from_dict(body)
        auto = body.get("auto_context", True)
        if not isinstance(auto, bool):
            raise IncidentError(422, f"auto_context must be true/false, got {auto!r}")
        ctx = None
        if auto:
            ctx = resolve(conn, sm, printer=rep.given.get("printer"), head=rep.given.get("head"),
                          spool_id=rep.given.get("spool_id"), readers=rd)
            # the resolver canonicalises the head label (t3 → T3)
            if "head" in rep.given and ctx.fields.get("head"):
                rep.given["head"] = ctx.fields["head"]
        inc = incidents.create(conn, rep, actor=who.name, auto=ctx.fields if ctx else None,
                               context=ctx.to_dict() if ctx else {"auto_context": False})
        return {"incident": inc.to_dict(), "context": inc.context}

    return _call(go, 201)


@router.get("/v1/incidents", response_model=None)
def list_incidents(
    conn: Db,
    who: Reader,
    status: str | None = None,
    printer: str | None = None,
    head: str | None = None,
    kind: str | None = None,
    spool: int | None = None,
    lot: str | None = None,
    since: str | None = None,
    limit: Annotated[int, Query(ge=1, le=incidents.MAX_LIST)] = 50,
) -> Any:
    def go() -> Any:
        rows = incidents.list_incidents(
            conn, status=[s.strip() for s in status.split(",") if s.strip()] if status else None,
            printer=printer, head=head, kind=kind, spool_id=spool, lot=lot, since=_since(since), limit=limit)
        return {"incidents": [i.to_dict() for i in rows]}

    return _call(go)


@router.get("/v1/incidents/stats", response_model=None)
def stats(conn: Db, who: Reader, by: str = "lot", since: str | None = "90d", kind: str | None = None) -> Any:
    return _call(lambda: incidents.stats(conn, by, since=_since(since), kind=kind))


@router.get("/v1/incidents/context", response_model=None)
def context(conn: Db, sm: Sm, rd: Rd, who: Reader, printer: str, head: str | None = None,
            spool: int | None = None) -> Any:
    return _call(lambda: resolve(conn, sm, printer=printer, head=head, spool_id=spool, readers=rd).to_dict())


@router.post("/v1/incidents/from-watch", response_model=None)
def from_watch(conn: Db, sm: Sm, rd: Rd, who: Writer, body: JsonBody) -> Any:
    def go() -> Any:
        out = watch_incidents.draft_from_watch(conn, sm, body, readers=rd, actor=who.name)
        return JSONResponse(out.to_dict(), status_code=201 if out.created else 200)

    return _call(go)


@router.get("/v1/incidents/{incident_id}", response_model=None)
def show(conn: Db, who: Reader, incident_id: int) -> Any:
    return _call(lambda: incidents.get(conn, incident_id).to_dict())


@router.patch("/v1/incidents/{incident_id}", response_model=None)
def edit(conn: Db, who: Writer, incident_id: int, body: JsonBody) -> Any:
    return _call(lambda: incidents.update(conn, incident_id, body, actor=who.name).to_dict())


@router.post("/v1/incidents/{incident_id}/confirm", response_model=None)
def confirm(conn: Db, who: Writer, incident_id: int, body: OptBody = None) -> Any:
    return _call(lambda: incidents.confirm(conn, incident_id, body or {}, actor=who.name).to_dict())


@router.post("/v1/incidents/{incident_id}/resolve", response_model=None)
def resolve_incident(conn: Db, who: Writer, incident_id: int, body: JsonBody) -> Any:
    def go() -> Any:
        unknown = sorted(set(body) - {"resolution_kind", "text", "calibration_id", "at"})
        if unknown:
            raise IncidentError(422, f"unknown field(s): {', '.join(unknown)}")
        kind, text, at = body.get("resolution_kind"), body.get("text"), body.get("at")
        if not isinstance(kind, str):
            raise IncidentError(422, f"resolution_kind must be one of {', '.join(incidents.RESOLUTIONS)}, got {kind!r}")
        if text is not None and not isinstance(text, str):
            raise IncidentError(422, f"text must be a string, got {text!r}")
        if at is not None:
            try:
                at = incidents.iso(incidents.parse_ts(at)) if isinstance(at, str) else None
            except ValueError:
                at = None
            if at is None:
                raise IncidentError(422, f"at must be an ISO date/time, got {body.get('at')!r}")
        return incidents.resolve(conn, incident_id, kind, actor=who.name, text=text,
                                 calibration_id=_int_field(body, "calibration_id"), at=at).to_dict()

    return _call(go)


@router.post("/v1/incidents/{incident_id}/dismiss", response_model=None)
def dismiss(conn: Db, who: Writer, incident_id: int, body: OptBody = None) -> Any:
    def go() -> Any:
        reason = (body or {}).get("reason")
        if reason is not None and not isinstance(reason, str):
            raise IncidentError(422, f"reason must be a string, got {reason!r}")
        return incidents.dismiss(conn, incident_id, actor=who.name, reason=reason).to_dict()

    return _call(go)


@router.get("/v1/exposure", response_model=None)
def list_exposure(conn: Db, who: Reader, printer: str | None = None, since: str | None = None,
                  limit: Annotated[int, Query(ge=1, le=5000)] = 100) -> Any:
    def go() -> Any:
        when = _since(since)
        rows = exposure.list_exposure(conn, printer=printer, since=when, limit=limit)
        return {"exposure": [r.to_dict() for r in rows], "totals": exposure.totals(conn, since=when)}

    return _call(go)


@router.post("/v1/exposure", response_model=None)
def add_exposure(conn: Db, sm: Sm, who: Writer, body: JsonBody) -> Any:
    return _call(lambda: exposure.add_manual(conn, sm, body, actor=who.name).to_dict(), 201)


@router.post("/v1/exposure/sync", response_model=None)
def sync_exposure(conn: Db, sm: Sm, who: Writer, body: JsonBody) -> Any:
    def go() -> Any:
        printer = body.get("printer")
        if not isinstance(printer, str) or not printer:
            raise IncidentError(422, "printer is required")
        limit = _int_field(body, "limit") or exposure.DEFAULT_LIMIT
        return exposure.sync_moonraker(conn, sm, printer, actor=who.name, limit=limit).to_dict()

    return _call(go)
