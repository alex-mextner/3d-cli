"""lint_calibrations.py — calibration data rules of `3d lint --printops` (plan §9.3, issue #167).

RULES:
  calibrations/catalog       error  fleet/calibrations.yaml (as 3d-api sees it) breaks its schema —
                                    /v1/calibrations/required and the gate answer 503 until fixed
  calibrations/policy        error  fleet/policy.yaml `calibrations:` names a kind the catalog does not
                                    know or a printer the fleet does not know (a typo would silently
                                    fall back to the defaults)
  calibrations/unknown-kind  error  a stored calibration's kind is no longer in the catalog
  calibrations/stale-run     warn   a run still `planned` STALE_RUN_DAYS after it started: record its
                                    result or abandon it
"""

from __future__ import annotations

import datetime

from printops import policy
from printops.calibration_catalog import Catalog, CatalogError, load_catalog
from printops.datalint import ERROR, WARN, Finding, LintContext, parse_utc
from printops.fleet import FleetError, load_fleet

STALE_RUN_DAYS = 14


def lint(ctx: LintContext) -> list[Finding]:
    try:
        catalog = load_catalog()
    except CatalogError as e:
        return [Finding("calibrations/catalog", ERROR, "fleet/calibrations.yaml", str(e))]
    findings = _policy(catalog)
    known = set(catalog.kinds)
    rows = ctx.conn.execute("SELECT id, kind, status, created_at FROM calibration").fetchall()
    for r in rows:
        subject = f"calibration:{r['id']}"
        if r["kind"] not in known:
            findings.append(Finding("calibrations/unknown-kind", ERROR, subject,
                                    f"kind {r['kind']!r} is not in fleet/calibrations.yaml"))
        if r["status"] == "planned":
            age = ctx.now - parse_utc(str(r["created_at"]))
            if age > datetime.timedelta(days=STALE_RUN_DAYS):
                findings.append(Finding("calibrations/stale-run", WARN, subject,
                                        f"{r['kind']} run planned {age.days} d ago has no result "
                                        "(POST /v1/calibrations/{id}/result)"))
    return findings


def _policy(catalog: Catalog) -> list[Finding]:
    try:
        cpolicy = policy.section("calibrations")
    except policy.PolicyError:
        return []  # the schema problem itself is reported by drying/policy (policy.check covers every section)
    path = str(policy.policy_path())
    out = [
        Finding("calibrations/policy", ERROR, path,
                f"calibrations: unknown kind {kind!r}; catalog: {', '.join(catalog.kinds)}")
        for kind in sorted(cpolicy.kind_names() - set(catalog.kinds))
    ]
    try:
        fleet = load_fleet()
    except FleetError:
        return out  # fleet/schema reports it
    if fleet is not None:
        out.extend(
            Finding("calibrations/policy", ERROR, path, f"calibrations.printers: unknown printer {printer!r}")
            for printer in sorted(set(cpolicy.printers) - set(fleet.printers))
        )
    return out
