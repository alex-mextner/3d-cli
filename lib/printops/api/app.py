"""app.py — builds the 3d-api FastAPI app: migrate the DB, wire Spoolman + tokens, mount routes.

RUN: `uvicorn --factory printops.api.app:app_from_env` (the container CMD; see
services/3d-api/). Tests call `create_app(settings, spoolman=FakeSpoolman())`.

EXTENSION POINT: every module `lib/printops/api/routes_<name>.py` that defines a module-level
`router` (fastapi.APIRouter) is included automatically, in name order. A slice adds a routes
file; it never edits this one.

INVARIANTS:
  - Migrations run synchronously inside create_app, so a bad migration fails startup loudly
    instead of serving a half-migrated schema.
  - A background task keeps the PrintOps extra fields registered in Spoolman: it retries with
    backoff while Spoolman is down and re-checks every FIELDS_RECHECK_S, so a fresh or restored
    Spoolman is repaired without a redeploy. A type conflict (ExtraFieldConflict) is permanent:
    it is reported by /v1/health and not retried.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import pkgutil
from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI
from starlette.concurrency import run_in_threadpool

import printops.api as api_pkg
from printops.api.state import ApiState
from printops.db import migrate, open_db
from printops.settings import Settings
from printops.spoolman import ExtraFieldConflict, HttpSpoolman, Spoolman, SpoolmanError, ensure_extra_fields
from printops.tokens import TokenStore

__all__ = ["Settings", "app_from_env", "create_app", "route_modules"]

log = logging.getLogger("printops.api")

FIELDS_RECHECK_S = 600.0
FIELDS_RETRY_MAX_S = 60.0


def route_modules() -> list[str]:
    """Names of the auto-included routes_*.py modules, in include order."""
    return sorted(
        f"{api_pkg.__name__}.{m.name}"
        for m in pkgutil.iter_modules(api_pkg.__path__)
        if m.name.startswith("routes_")
    )


async def _keep_fields_registered(state: ApiState) -> None:
    sp = state.spoolman
    if sp is None:
        state.fields = {"status": "error", "error": "SPOOLMAN_URL is not set"}
        return
    delay = 1.0
    while True:
        try:
            report = await run_in_threadpool(ensure_extra_fields, sp)
        except ExtraFieldConflict as e:
            state.fields = {"status": "error", "error": e.message}
            log.error("extra fields: %s", e.message)
            return
        except SpoolmanError as e:
            state.fields = {"status": "pending", "error": e.message}
            log.warning("extra fields not registered yet (retry in %.0fs): %s", delay, e.message)
            await asyncio.sleep(delay)
            delay = min(delay * 2, FIELDS_RETRY_MAX_S)
            continue
        if report.created or report.updated:
            log.info("extra fields registered: created=%s updated=%s", report.created, report.updated)
        state.fields = {"status": "ok", "error": None, **report.to_dict()}
        delay = 1.0
        await asyncio.sleep(FIELDS_RECHECK_S)


def create_app(settings: Settings, *, spoolman: Spoolman | None = None) -> FastAPI:
    conn = open_db(settings.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    if spoolman is None and settings.spoolman_url:
        spoolman = HttpSpoolman(settings.spoolman_url)
    state = ApiState(settings=settings, spoolman=spoolman, tokens=TokenStore(settings.tokens_file))

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_keep_fields_registered(state))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="PrintOps 3d-api", version=settings.version, lifespan=lifespan)
    app.state.printops = state
    for name in route_modules():
        router = getattr(importlib.import_module(name), "router", None)
        if not isinstance(router, APIRouter):
            raise RuntimeError(f"{name} must define a module-level `router = APIRouter()`")
        app.include_router(router)
    return app


def app_from_env() -> FastAPI:
    """uvicorn --factory entry point: Settings from the process environment."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(Settings.from_env())
