"""routes_lint.py — `GET /v1/lint?weigh_days=N` (scope read): the PrintOps data rules.

Runs every `printops/lint_<slice>.py` rule (printops.datalint) over the live Spoolman spools and
the PrintOps DB, read-only. Answers {"findings": [...], "errors": n, "warnings": n, "spools": n};
502 when Spoolman cannot be read (the rules would be meaningless without it).
`3d lint --printops` calls this and merges the findings with the fleet-file rules.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from printops import datalint
from printops.api.deps import Db, Principal, get_spoolman, require
from printops.spoolman import Spoolman, SpoolmanError

router = APIRouter()


@router.get("/v1/lint", response_model=None)
def lint(
    conn: Db,
    sm: Annotated[Spoolman, Depends(get_spoolman)],
    who: Annotated[Principal, Depends(require("read"))],
    weigh_days: Annotated[int, Query(ge=1, le=3650)] = datalint.DEFAULT_WEIGH_DAYS,
) -> Any:
    try:
        spools = sm.find("spool")
    except SpoolmanError as e:
        return JSONResponse(
            {"detail": f"cannot read spools: {e.message}"}, status_code=502
        )
    ctx = datalint.LintContext(
        conn=conn,
        spoolman=sm,
        spools=spools,
        now=datetime.datetime.now(datetime.timezone.utc),
        weigh_days=weigh_days,
    )
    findings = datalint.run(ctx)
    errors = sum(1 for f in findings if f.level == datalint.ERROR)
    return {
        "findings": [f.as_dict() for f in findings],
        "errors": errors,
        "warnings": len(findings) - errors,
        "spools": len(spools),
        "rules": [m.rsplit(".", 1)[-1] for m in datalint.rule_modules()],
    }
