"""lint_incidents.py — incident data rules of `3d lint --printops` (plan §9.3, issue #165).

RULES (run by printops.datalint; read-only):
  incidents/draft-stale          warn  an automatic draft (watch) waited more than DRAFT_STALE_DAYS
                                       for confirm/dismiss — unconfirmed drafts are left out of
                                       the rates, so real failures go uncounted
  incidents/hardware-risk-open   warn  a hardware_risk incident still open after HARDWARE_OPEN_DAYS:
                                       the printer may be printing on a known fault
"""

from __future__ import annotations

import datetime

from printops.datalint import WARN, Finding, LintContext
from printops.incidents import iso

DRAFT_STALE_DAYS = 7
HARDWARE_OPEN_DAYS = 1


def lint(ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    stale = iso(ctx.now - datetime.timedelta(days=DRAFT_STALE_DAYS))
    for r in ctx.conn.execute(
        "SELECT id, at, kind, printer, head FROM incident WHERE status = 'draft' AND at < ? ORDER BY at", (stale,)
    ):
        where = "/".join(x for x in (r["printer"], r["head"]) if x) or "?"
        out.append(Finding(
            "incidents/draft-stale", WARN, f"incident:{r['id']}",
            f"draft #{r['id']} ({r['kind']} on {where}, {r['at'][:10]}) is unconfirmed for over "
            f"{DRAFT_STALE_DAYS} d: 3d printops incidents confirm {r['id']} | dismiss {r['id']}",
        ))
    risky = iso(ctx.now - datetime.timedelta(days=HARDWARE_OPEN_DAYS))
    for r in ctx.conn.execute(
        "SELECT id, at, kind, printer, head FROM incident WHERE status = 'open' AND severity = 'hardware_risk' "
        "AND at < ? ORDER BY at", (risky,)
    ):
        where = "/".join(x for x in (r["printer"], r["head"]) if x) or "?"
        out.append(Finding(
            "incidents/hardware-risk-open", WARN, f"incident:{r['id']}",
            f"hardware-risk incident #{r['id']} ({r['kind']} on {where}, {r['at'][:10]}) is still open: "
            f"fix and `3d printops incidents resolve {r['id']} …`",
        ))
    return out
