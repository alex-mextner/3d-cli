"""routes_mix.py — 3d-api lookup of a plate's colour mixes: the `mix_calibration` gate (#418).

  POST /v1/calibrations/mix-check  {printer, mixes: [{key, filament_ids}], accept?: [mix key],
                                    reason?, gcode_sha?, file?, notes?}  read (+write with accept)

Answers, per mix key, whether a current `mix_color` record exists for the printer (current /
never / expired / stale — printops.mix_gate). `accept` names mix keys the owner prints as they are
despite the finding; it needs the write scope and a reason, and every acceptance is logged as a
`calibration.accepted` event. Errors as routes_calibrations: {"detail", "info"} with the
CalibrationError status (404 unknown printer, 422 invalid body, 403 scope).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request

from printops import calibrations as cal
from printops import mix_gate
from printops.api.deps import Db, Principal, require
from printops.api.routes_calibrations import _call, _ctx, _opt_str

router = APIRouter()

Reader = Annotated[Principal, Depends(require("read"))]
JsonBody = Annotated[dict[str, Any], Body()]


@router.post("/v1/calibrations/mix-check", response_model=None)
def mix_check(request: Request, conn: Db, who: Reader, body: JsonBody) -> Any:
    ctx = _ctx(request, conn, who)

    def go() -> Any:
        printer = _opt_str(body, "printer")
        if not printer:
            raise cal.CalibrationError(422, "printer is required")
        cal.fleet_printer(ctx.fleet, printer)
        raw_accept = body.get("accept") or []
        if not isinstance(raw_accept, list) or not all(
            isinstance(a, str) for a in raw_accept
        ):
            raise cal.CalibrationError(422, "accept must be a list of mix keys")
        if raw_accept and "write" not in who.scopes:
            raise cal.CalibrationError(
                403,
                f"token {who.name!r} lacks scope 'write' (accepting a mix is logged)",
            )
        notes = body.get("notes") or {}
        if not isinstance(notes, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in notes.items()
        ):
            raise cal.CalibrationError(422, "notes must map a mix key to text")
        facts = mix_gate.lookup(conn, printer, mix_gate.parse_asks(body.get("mixes")))
        accepted = (
            mix_gate.accept_mixes(
                conn,
                printer,
                facts,
                raw_accept,
                reason=_opt_str(body, "reason"),
                actor=who.name,
                gcode_sha=_opt_str(body, "gcode_sha"),
                file=_opt_str(body, "file"),
                notes=notes,
            )
            if raw_accept
            else []
        )
        return mix_gate.facts_dict(facts, printer, accepted)

    return _call(go)
