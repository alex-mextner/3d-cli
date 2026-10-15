"""lint_drying.py — drying data rules of `3d lint --printops` (plan §9.3, issue #162).

RULES (run by printops.datalint; read-only):
  drying/policy          error  fleet/policy.yaml (as 3d-api sees it) breaks its schema — the
                                periodic drying check and GET /v1/drying/due are off until fixed
  drying/bad-date        error  spool extra `opened_at` / `last_dried_at` is not an ISO date, so
                                its drying clock cannot run
  drying/forgotten       warn   a drying session still open more than FORGOTTEN_H after its
                                target time — finish it (`3d printops dry finish ID`) so the
                                spools get last_dried_at
  drying/state-stale     warn   Spoolman `dry_state` is behind the assessment (the periodic
                                check is not running, or Spoolman refused the write)
"""

from __future__ import annotations

import datetime

from printops import drying, policy
from printops.datalint import ERROR, WARN, Finding, LintContext

FORGOTTEN_H = 24


def lint(ctx: LintContext) -> list[Finding]:
    problems = policy.check()
    out: list[Finding] = [
        Finding("drying/policy", ERROR, str(policy.policy_path()), p) for p in problems
    ]

    for sp in ctx.spools:
        extra = drying.decoded_extra(sp)
        for key in ("opened_at", "last_dried_at"):
            raw = extra.get(key)
            if not raw:
                continue
            try:
                drying.parse_ts(str(raw))
            except ValueError:
                out.append(
                    Finding(
                        "drying/bad-date",
                        ERROR,
                        f"spool:{sp['id']}",
                        f"{key} {raw!r} is not an ISO date; fix it in Spoolman",
                    )
                )

    for session in drying.list_sessions(ctx.conn, open_only=True, limit=1000):
        overdue = ctx.now - drying.parse_ts(session.ends_at)
        if overdue > datetime.timedelta(hours=FORGOTTEN_H):
            out.append(
                Finding(
                    "drying/forgotten",
                    WARN,
                    f"drying:{session.id}",
                    f"session #{session.id} in {session.dryer} ended {overdue.total_seconds() / 3600:.0f} h ago "
                    f"and is still open: 3d printops dry finish {session.id}",
                )
            )

    if not problems:
        dpolicy = policy.section("drying")
        drying_now = drying.spools_drying(ctx.conn)
        for sp in ctx.spools:
            a = drying.assess(
                dpolicy, sp, now=ctx.now, drying=int(sp["id"]) in drying_now
            )
            if a.state in drying.STATES and a.state != a.stored_state:
                out.append(
                    Finding(
                        "drying/state-stale",
                        WARN,
                        f"spool:{a.spool_id}",
                        f"{a.label}: Spoolman dry_state is {a.stored_state or 'unset'}, assessment says {a.state} "
                        "(storage RH not considered here); run 3d printops dry due --check",
                    )
                )
    return out
