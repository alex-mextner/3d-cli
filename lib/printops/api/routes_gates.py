"""routes_gates.py — the PrintOps state the print gates read (issue #164; domain: printops.gates).

  GET /v1/gates/state?printer=NAME   scope read. One fleet printer: fleet facts + the spool bound
                                     to each tool's slot (remaining interval, drying assessment).
                                     404 unknown printer, 503 no/broken fleet file, 502 Spoolman.
  GET /v1/gates/snapshot             scope read. Every fleet printer's state in one document
                                     (`3d printops gates snapshot` saves it for offline preflight).

Read-only: nothing is written to Spoolman, the DB or a printer. The `3d print-preflight` client
side evaluates the gates (lib/printer_ops/printops_checks.py) so a slicer hook can run them
offline against a saved snapshot.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from printops import gates
from printops.api.deps import Db, get_spoolman, require
from printops.api.routes_drying import humidity_reader
from printops.drying_policy import DryingPolicy
from printops.fleet import Fleet, FleetError, load_fleet
from printops.policy import PolicyError, section
from printops.spoolman import Spoolman, SpoolmanError

router = APIRouter()

Sm = Annotated[Spoolman, Depends(get_spoolman)]


def _fleet() -> Fleet:
    try:
        fleet = load_fleet()
    except FleetError as e:
        raise HTTPException(503, f"fleet registry unreadable: {e.message}") from e
    if fleet is None:
        raise HTTPException(503, "no fleet registry (fleet/printers.yaml)")
    return fleet


def _drying() -> tuple[DryingPolicy | None, str | None]:
    """The drying policy, or (None, why) — a broken drying section must not hide the other gates."""
    try:
        policy: DryingPolicy = section("drying")
    except PolicyError as e:
        return None, f"drying policy unusable: {e}"
    return policy, None


@router.get("/v1/gates/state", dependencies=[Depends(require("read"))])
def state(conn: Db, sm: Sm, printer: Annotated[str, Query(min_length=1, max_length=64)]) -> dict[str, Any]:
    fleet = _fleet()
    fp = fleet.printers.get(printer)
    if fp is None:
        raise HTTPException(404, f"unknown printer {printer!r}; fleet has: {', '.join(sorted(fleet.printers))}")
    policy, why = _drying()
    try:
        return gates.printer_state(conn, sm, fp, drying_policy=policy, drying_error=why, humidity=humidity_reader())
    except SpoolmanError as e:
        raise HTTPException(502, f"Spoolman: {e.message}") from e


@router.get("/v1/gates/snapshot", dependencies=[Depends(require("read"))])
def snapshot(request: Request, conn: Db, sm: Sm) -> dict[str, Any]:
    policy, why = _drying()
    try:
        return gates.snapshot(conn, sm, _fleet(), drying_policy=policy, drying_error=why,
                              humidity=humidity_reader(), api=str(request.base_url).rstrip("/"))
    except SpoolmanError as e:
        raise HTTPException(502, f"Spoolman: {e.message}") from e
