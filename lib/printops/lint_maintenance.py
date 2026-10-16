"""lint_maintenance.py — maintenance data rules of `3d lint --printops` (plan §9.3; issue #166).

RULES (run by printops.datalint over the PrintOps DB; read-only, no printer contact):
  maintenance/open-fault    warn  a fault is open on a printer (or head) — the planner and the
                                  gates treat that head as unusable until it is closed
  maintenance/fleet-drift   warn  fleet/printers.yaml disagrees with the recorded state: a head
                                  with an open fault still `usable: true`, a `usable: false` head
                                  whose fault was closed, or a nozzle that differs from the one
                                  recorded by the last swap — update the fleet file
The rules file itself (fleet/maintenance-rules.yaml) is checked offline by `3d lint --printops`
(rule maintenance/rules-schema, printops.maintenance_rules.lint_rules_file).
"""

from __future__ import annotations

from dataclasses import asdict

from printops import maintenance as mt
from printops.datalint import WARN, Finding, LintContext


def lint(ctx: LintContext) -> list[Finding]:
    from printops.fleet import FleetError, load_fleet

    out: list[Finding] = []
    for f in mt.list_faults(ctx.conn, status="open"):
        where = f"{f['printer']}/{f['head']}" if f["head"] else f["printer"]
        out.append(Finding("maintenance/open-fault", WARN, f"fault:{f['id']}",
                           f"{where}: open since {f['at'][:10]}: {f['symptom']} — fix it and "
                           f"`3d printops maint fault close {f['id']} --fixed-by MAINT_ID`"))
    try:
        fleet = load_fleet()
    except FleetError:
        return out  # fleet/schema reports it
    if fleet is None:
        return out
    for name, p in sorted(fleet.printers.items()):
        by_head: dict[str | None, list[dict]] = {}
        for f in mt.list_faults(ctx.conn, name, status=None):  # one query per printer, not per head
            by_head.setdefault(f["head"], []).append(f)
        for h in p.heads:
            subject = f"{name}/{h.label}"
            faults = by_head.get(h.label, [])
            open_ids = [f["id"] for f in faults if f["status"] == "open"]
            if h.usable and open_ids:
                out.append(Finding("maintenance/fleet-drift", WARN, subject,
                                   f"{subject} has open fault(s) #{', #'.join(map(str, open_ids))} but "
                                   "fleet/printers.yaml says usable: true — set usable: false with the reason"))
            seeded = next((f for f in faults if f["external_key"] == f"fleet:{subject}"), None)
            if not h.usable and seeded is not None and seeded["status"] != "open":
                out.append(Finding("maintenance/fleet-drift", WARN, subject,
                                   f"{subject}: fault #{seeded['id']} is {seeded['status']} but fleet/printers.yaml "
                                   "still says usable: false — set usable: true (drop the reason)"))
            cur = mt.current_nozzle_install(ctx.conn, name, h.label)
            if h.nozzle is not None and cur is not None:
                recorded = {k: cur[k] for k in ("diameter_mm", "material", "type")}
                if recorded != asdict(h.nozzle):
                    out.append(Finding("maintenance/fleet-drift", WARN, subject,
                                       f"{subject}: nozzle #{cur['id']} is {recorded} (recorded swap) but "
                                       f"fleet/printers.yaml says {asdict(h.nozzle)} — update the fleet file"))
    return out
