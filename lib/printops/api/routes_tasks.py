"""routes_tasks.py — printer tasks (the to-do list per printer) over HTTP (#281; plan §7.5, §11.1;
domain: printops.printer_tasks).

  GET  /v1/printers/{p}/tasks           read   {printer, summary, tasks, sync}; ?status=open (default:
                                               open, in progress, snoozed) | all | done | dismissed | …
  POST /v1/printers/{p}/tasks           write  {title, why?, type?, head?, slot?, component?, blocks?,
                                               when?, when_note?, duration_min?, needs_human?,
                                               depends_on?: [id], refs?, calibration_kinds?, spool_id?,
                                               note?, source?: manual|calibration_followup} → 201 task
  GET  /v1/tasks                        read   {tasks} of every printer; ?printer= ?status= as above
  GET  /v1/tasks/{ref}                  read   one task by id or uid (404 when unknown)
  POST /v1/tasks/{ref}/done             write  {note?} → task; 409 for a derived task (detail names
                                               the command that records the work)
  POST /v1/tasks/{ref}/dismiss          write  {reason} → task
  POST /v1/tasks/{ref}/snooze           write  {until?, reason?} → task (no until = until reopened)
  POST /v1/tasks/{ref}/reopen           write  → task (snoozed/done/dismissed back to open)
  POST /v1/tasks/sync                   write  derive now; ?printer= → {opened, closed, updated, errors}
  POST /v1/tasks/import                 write  {lines: [str]} buffer lines; ?dry_run=true rolls back →
                                               {created, merged, applied, refused, skipped, items, …}

DERIVATION: every read and write of tasks first syncs the printer(s) concerned (faults, due rules,
calibration requirements → tasks; printops.printer_tasks.sync), and a background round syncs every
printer each SYNC_EVERY_MIN, so printer_task.* events follow the sources without a client asking.
A source that cannot be read (rules file, calibration catalog/policy, Spoolman) leaves its tasks
as they are; the reason is in `sync.errors`.
Errors: TaskError.status (404 unknown printer/task, 409 conflicts, 422 invalid); a broken fleet
file → 503.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from printops import printer_tasks as pt
from printops.api.deps import require
from printops.api.routes_maintenance import ReadConn, WriteConn
from printops.api.state import ApiState
from printops.db import open_db
from printops.spoolman import Spoolman
from printops.tokens import TokenRecord

log = logging.getLogger("printops.tasks")

FIRST_SYNC_DELAY_S = 180.0
SYNC_EVERY_MIN = 15

Reader = Annotated[TokenRecord, Depends(require("read"))]
Writer = Annotated[TokenRecord, Depends(require("write"))]
Status = Literal["open", "all", "in_progress", "snoozed", "done", "dismissed"]


def _fleet() -> Any:
    from printops.fleet import FleetError, load_fleet

    try:
        fleet = load_fleet()
    except FleetError as e:
        raise HTTPException(503, e.message) from e
    if fleet is None:
        raise HTTPException(
            503,
            "no fleet registry (fleet/printers.yaml): printer tasks need the printers",
        )
    return fleet


def sources(
    conn: sqlite3.Connection, fleet: Any, spoolman: Spoolman | None
) -> pt.Sources:
    """The derivation inputs; one that cannot be loaded is None with its reason in `errors`."""
    from printops import maintenance_rules as mr
    from printops.calibration_catalog import CatalogError, load_catalog
    from printops.calibration_policy import CalibrationPolicy
    from printops.calibrations import Ctx
    from printops.policy import PolicyError, section

    out = pt.Sources(fleet=fleet)
    try:
        out.rules = mr.load_rules(printers=fleet.printers)
    except mr.RulesError as e:
        out.errors.append(f"maintenance rules: {e.message}")
    try:
        policy: CalibrationPolicy = section("calibrations")
        out.calibration = Ctx(
            conn=conn,
            spoolman=spoolman,
            actor=pt.DERIVE,
            catalog=load_catalog(),
            policy=policy,
            fleet=fleet,
        )
    except (CatalogError, PolicyError) as e:
        out.errors.append(f"calibration engine: {e}")
    return out


def _sync(
    request: Request, conn: sqlite3.Connection, printer: str | None
) -> tuple[Any, pt.SyncReport]:
    fleet = _fleet()
    if printer is not None:
        with _domain():
            pt.fleet_printer(fleet, printer)
    state: ApiState = request.app.state.printops
    with _domain():
        report = pt.sync(conn, sources(conn, fleet, state.spoolman), printer)
    if report.errors:
        log.warning("printer task sync: %s", "; ".join(report.errors))
    return fleet, report


@contextlib.contextmanager
def _domain() -> Iterator[None]:
    try:
        yield
    except pt.TaskError as e:
        raise HTTPException(e.status, e.message) from e


def _status(status: Status) -> str | None:
    return None if status == "open" else status


async def _loop(state: ApiState) -> None:
    await asyncio.sleep(FIRST_SYNC_DELAY_S)
    while True:
        try:
            await run_in_threadpool(_tick, state)
        except (
            Exception
        ):  # one bad round (Spoolman down, broken file) must not end the loop
            log.exception("printer task sync failed (retry next round)")
        await asyncio.sleep(SYNC_EVERY_MIN * 60)


def _tick(state: ApiState) -> None:
    from printops import maintenance as mt
    from printops.fleet import load_fleet

    fleet = load_fleet()
    if fleet is None:
        return
    conn = open_db(state.settings.db_path)
    try:
        mt.ensure_seeded(conn)
        report = pt.sync(conn, sources(conn, fleet, state.spoolman))
    finally:
        conn.close()
    if report.changed or report.errors:
        log.info("printer tasks: %s", report.to_dict())


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


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskBody(_Body):
    title: str = Field(min_length=1, max_length=500)
    why: str | None = Field(default=None, max_length=4000)
    type: Literal[
        "calibration", "inspect", "maintenance", "repair", "setup", "other"
    ] = "other"
    head: str | None = Field(default=None, max_length=8)
    slot: str | None = Field(default=None, max_length=32)
    component: str | None = None
    blocks: Literal["none", "printer", "head", "slot"] = "none"
    when: Literal["idle", "before_print", "any"] = "any"
    when_note: str | None = Field(default=None, max_length=500)
    duration_min: int | None = Field(default=None, gt=0, le=100000)
    needs_human: bool = True
    depends_on: list[int] = Field(default_factory=list, max_length=50)
    refs: list[str] = Field(default_factory=list, max_length=50)
    calibration_kinds: list[str] = Field(default_factory=list, max_length=20)
    spool_id: int | None = None
    note: str | None = Field(default=None, max_length=4000)
    source: Literal["manual", "calibration_followup"] = "manual"


class NoteBody(_Body):
    note: str | None = Field(default=None, max_length=4000)


class DismissBody(_Body):
    reason: str = Field(min_length=1, max_length=4000)


class SnoozeBody(_Body):
    until: str | None = None
    reason: str | None = Field(default=None, max_length=4000)


class ImportBody(_Body):
    lines: list[str] = Field(max_length=100000)


@router.get("/v1/printers/{printer}/tasks")
def printer_tasks(
    request: Request, conn: ReadConn, who: Reader, printer: str, status: Status = "open"
) -> dict[str, Any]:
    _, report = _sync(request, conn, printer)
    with _domain():
        tasks = pt.list_tasks(conn, printer, status=_status(status))
        active = tasks if status == "open" else pt.list_tasks(conn, printer)
    return {
        "printer": printer,
        "summary": pt.summary(active),
        "tasks": tasks,
        "sync": report.to_dict(),
    }


@router.post("/v1/printers/{printer}/tasks", status_code=201)
def add_task(
    request: Request, conn: WriteConn, who: Writer, printer: str, body: TaskBody
) -> dict[str, Any]:
    fleet, _ = _sync(request, conn, printer)
    fields = body.model_dump(exclude={"title", "type", "source"})
    with _domain():
        return pt.create(
            conn,
            fleet,
            printer,
            actor=who.name,
            title=body.title,
            type_=body.type,
            source=body.source,
            **fields,
        )


@router.get("/v1/tasks")
def all_tasks(
    request: Request,
    conn: ReadConn,
    who: Reader,
    printer: str | None = None,
    status: Status = "open",
) -> dict[str, Any]:
    _sync(request, conn, printer)
    with _domain():
        return {"tasks": pt.list_tasks(conn, printer, status=_status(status))}


@router.post("/v1/tasks/sync")
def sync_now(
    request: Request, conn: WriteConn, who: Writer, printer: str | None = None
) -> dict[str, Any]:
    _, report = _sync(request, conn, printer)
    return report.to_dict()


@router.post("/v1/tasks/import")
def import_buffer(
    request: Request,
    conn: WriteConn,
    who: Writer,
    body: ImportBody,
    dry_run: bool = False,
) -> dict[str, Any]:
    fleet, _ = _sync(
        request, conn, None
    )  # derived tasks first: buffer tasks with their key merge into them
    state: ApiState = request.app.state.printops
    with _domain():
        return pt.import_lines(
            conn,
            fleet,
            body.lines,
            actor=who.name,
            spoolman=state.spoolman,
            dry_run=dry_run,
        ).to_dict()


@router.get("/v1/tasks/{ref}")
def one_task(conn: ReadConn, who: Reader, ref: str) -> dict[str, Any]:
    with _domain():
        return pt.get(conn, ref)


@router.post("/v1/tasks/{ref}/done")
def task_done(conn: WriteConn, who: Writer, ref: str, body: NoteBody) -> dict[str, Any]:
    with _domain():
        return pt.done(conn, ref, actor=who.name, note=body.note)


@router.post("/v1/tasks/{ref}/dismiss")
def task_dismiss(
    conn: WriteConn, who: Writer, ref: str, body: DismissBody
) -> dict[str, Any]:
    with _domain():
        return pt.dismiss(conn, ref, actor=who.name, reason=body.reason)


@router.post("/v1/tasks/{ref}/snooze")
def task_snooze(
    conn: WriteConn, who: Writer, ref: str, body: SnoozeBody
) -> dict[str, Any]:
    with _domain():
        return pt.snooze(
            conn, ref, actor=who.name, until=body.until, reason=body.reason
        )


@router.post("/v1/tasks/{ref}/reopen")
def task_reopen(conn: WriteConn, who: Writer, ref: str) -> dict[str, Any]:
    with _domain():
        return pt.reopen(conn, ref, actor=who.name)
