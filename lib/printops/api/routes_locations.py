"""routes_locations.py — the location tree over HTTP (issue #159; domain: printops.locations).

  GET    /v1/locations                  read   {"tree": [...nested], "count": N}; ?flat=true →
                                               {"locations": [...]}; ?archived=true includes archived
  GET    /v1/locations/resolve?q=TEXT   read   the location a scanned QR/NFC text, tag id or UID names
  POST   /v1/locations                  write  {path, kind, capacity?, humidity_sensor_entity?,
                                               tag_uid?, note?} → 201 location
  POST   /v1/locations/sync-fleet       write  re-materialise slot:<printer>/… from fleet/printers.yaml
  POST   /v1/locations/resync           write  rewrite Spoolman `location` where it drifted from the path
  GET    /v1/locations/{ref}            read   location + ancestors + children (ref = path or id)
  GET    /v1/locations/{ref}/contents   read   {"location", "spools"}; ?recursive=true includes the subtree
  PATCH  /v1/locations/{ref}            write  {path?} moves the subtree (+ Spoolman re-sync); other
                                               keys (kind, capacity, humidity_sensor_entity, tag_uid,
                                               qr, note, archived:false) update it
  DELETE /v1/locations/{ref}            write  archive; 409 while it holds spools or active children

Paths contain `:` and `/` and go into the URL as they are (`/v1/locations/WH:A/S2/contents`);
the segment `contents` is reserved by the grammar so the routes never clash. Printer slots
follow the fleet file automatically: the first request after the file changed (a deploy)
re-syncs them.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from printops import locations as locs
from printops.api.deps import Db, get_spoolman, get_state, require
from printops.spoolman import Spoolman, SpoolmanError
from printops.tokens import TokenRecord

router = APIRouter()
log = logging.getLogger("printops.api.locations")

Reader = Annotated[TokenRecord, Depends(require("read"))]
Writer = Annotated[TokenRecord, Depends(require("write"))]
Sm = Annotated[Spoolman, Depends(get_spoolman)]


def _ensure_fleet(request: Request, conn: Db) -> sqlite3.Connection:
    """Dependency: re-sync printer slots when the fleet file changed since the last sync."""
    from printops.fleet import FleetError

    try:
        report = locs.ensure_fleet_slots(conn, get_state(request).spoolman)
    except (FleetError, locs.LocationError) as e:  # a broken fleet file must not take the tree down
        log.error("fleet slot sync failed: %s", getattr(e, "message", e))
        return conn
    if report is not None and (report.changed or report.kept):
        log.info("fleet slots synced: %s", report.to_dict())
    return conn


Conn = Annotated[sqlite3.Connection, Depends(_ensure_fleet)]


def _http(e: locs.LocationNotFound | locs.LocationError | SpoolmanError) -> HTTPException:
    if isinstance(e, locs.LocationNotFound):
        return HTTPException(404, f"no location {e.ref!r}")
    if isinstance(e, locs.LocationError):
        return HTTPException(e.status, e.message)
    return HTTPException(502, e.message)


def _get(conn: sqlite3.Connection, ref: str) -> locs.Location:
    try:
        return locs.get(conn, ref)
    except locs.LocationNotFound as e:
        raise _http(e) from e


@router.get("/v1/locations")
def list_locations(conn: Conn, who: Reader, flat: bool = False, archived: bool = False) -> dict[str, Any]:
    rows = locs.all_locations(conn, include_archived=archived)
    if flat:
        return {"locations": [r.to_dict() for r in rows]}
    return {"tree": locs.tree(rows), "count": len(rows)}


@router.get("/v1/locations/resolve")
def resolve(conn: Conn, who: Reader, q: Annotated[str, Query(min_length=1, max_length=200)]) -> dict[str, Any]:
    try:
        return locs.resolve(conn, q).to_dict()
    except locs.LocationNotFound as e:
        raise _http(e) from e


@router.post("/v1/locations", status_code=201)
def create(conn: Conn, who: Writer, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    allowed = {"path", "kind", "capacity", "humidity_sensor_entity", "tag_uid", "note"}
    extra = sorted(set(body) - allowed)
    if extra:
        raise HTTPException(422, f"unknown field(s) {', '.join(extra)}; accepted: {', '.join(sorted(allowed))}")
    if not isinstance(body.get("path"), str) or not isinstance(body.get("kind"), str):
        raise HTTPException(422, "path and kind are required strings")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(422, "note must be a string")
    try:
        loc = locs.add(conn, body["path"], body["kind"], actor=who.name, capacity=body.get("capacity"),
                       humidity_sensor_entity=body.get("humidity_sensor_entity"), tag_uid=body.get("tag_uid"),
                       note=note)
    except locs.LocationError as e:
        raise _http(e) from e
    return loc.to_dict()


@router.post("/v1/locations/sync-fleet")
def sync_fleet(request: Request, conn: Db, who: Writer) -> dict[str, Any]:
    from printops.fleet import FleetError

    try:
        report = locs.ensure_fleet_slots(conn, get_state(request).spoolman, actor=who.name, force=True)
    except FleetError as e:
        raise HTTPException(422, f"{e}: {'; '.join(e.remediation)}") from e
    except locs.LocationError as e:
        raise _http(e) from e
    if report is None:
        raise HTTPException(409, "no fleet file (fleet/printers.yaml) in this 3d-api; nothing to sync")
    return report.to_dict()


@router.post("/v1/locations/resync")
def resync(conn: Conn, who: Writer, sp: Sm) -> dict[str, Any]:
    try:
        return locs.resync(conn, sp).to_dict()
    except SpoolmanError as e:
        raise _http(e) from e


@router.get("/v1/locations/{ref:path}/contents")
def contents(conn: Conn, who: Reader, sp: Sm, ref: str, recursive: bool = False) -> dict[str, Any]:
    loc = _get(conn, ref)
    try:
        spools = locs.contents(conn, sp, loc, recursive=recursive)
    except SpoolmanError as e:
        raise _http(e) from e
    return {"location": loc.to_dict(), "recursive": recursive, "spools": spools}


@router.get("/v1/locations/{ref:path}")
def show(conn: Conn, who: Reader, ref: str) -> dict[str, Any]:
    loc = _get(conn, ref)
    return {**loc.to_dict(),
            "ancestors": [a.to_dict() for a in locs.ancestors(conn, loc)],
            "children": [c.to_dict() for c in locs.children(conn, loc, include_archived=True)],
            "humidity_sensor": locs.humidity_sensor(conn, loc)}


@router.patch("/v1/locations/{ref:path}")
def patch(request: Request, conn: Conn, who: Writer, ref: str,
          body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    loc = _get(conn, ref)
    changes = dict(body)
    new_path = changes.pop("path", None)
    if new_path is not None and not isinstance(new_path, str):
        raise HTTPException(422, "path must be a string")
    if new_path is not None and changes:
        # two transactions (update, then move + Spoolman re-sync) could half-apply; keep them apart
        raise HTTPException(422, "move (path) and attribute changes go in separate PATCH requests")
    result: dict[str, Any] = {"renamed": [], "spoolman": {"updated": [], "failed": []}}
    try:
        if changes:
            loc = locs.update(conn, loc, changes, actor=who.name)
        if new_path is not None:
            moved = locs.move(conn, get_spoolman(request), loc, new_path, actor=who.name)
            loc = moved.location
            result.update(renamed=[list(r) for r in moved.renamed], spoolman=moved.spoolman.to_dict())
    except (locs.LocationError, SpoolmanError) as e:
        raise _http(e) from e
    # The move is committed even if some Spoolman writes failed: they are listed under
    # "spoolman.failed", and `POST /v1/locations/resync` repairs them.
    return {"location": loc.to_dict(), **result}


@router.delete("/v1/locations/{ref:path}")
def delete(request: Request, conn: Conn, who: Writer, ref: str) -> dict[str, Any]:
    loc = _get(conn, ref)
    try:
        return locs.archive(conn, get_state(request).spoolman, loc, actor=who.name).to_dict()
    except (locs.LocationError, SpoolmanError) as e:
        raise _http(e) from e
