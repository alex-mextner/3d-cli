"""`3d printops spool drift` — does Spoolman still agree with PrintOps? List every divergence, heal it.

An action of `3d printops spool` contributed by its own module (printops.cli.SubAction); the
detection and the healing are 3d-api's (printops.spool_sync), this module only calls and renders."""

from __future__ import annotations

import json
from typing import Any

from printops.cli import SubAction
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops spool drift [--spool ID] [--heal] [--json] [--api URL] [--token TOKEN]
  Check that Spoolman (the web UI, Moonraker's [spoolman]) and PrintOps (ledger, bindings, events)
  agree, spool by spool: remaining weight (ledger vs Spoolman, ± 0.5 g), new / deleted / archived
  spools, location and slot, derived fields. Spoolman holds the current value; a difference means
  "changed in Spoolman, not processed yet" (3d-api's scheduled pass processes it within a minute).
  Read-only without --heal. Exit 0: nothing drifts. Exit 1: drift (listed), or — with --heal —
  something is left that needs a person (listed; it is also an anomaly: `3d printops anomalies`).

Options:
  --spool ID     only this spool
  --heal         apply what is healable: PrintOps catches up by appending ledger rows (a UI weighing
                 becomes an anchor, Moonraker usage a delta), events and notes; the weight is never
                 written back to Spoolman. Needs the `write` scope
  --json         the API's JSON answer ({ok, drift, healable, unhealable, drifts[]}; with --heal
                 {ok, before, healed, failed, after})
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops spool drift
  3d printops spool drift --spool 7 --json
  3d printops spool drift --heal"""


def fmt_drift(d: dict[str, Any]) -> str:
    mark = "heal:" if d["healable"] else "needs you:"
    todo = (
        d["action"]
        if d["healable"]
        else "decide in Spoolman or PrintOps (see `3d printops anomalies --printer spools`)"
    )
    return f"  [{d['kind']}] {d['summary']}\n      {mark} {todo}"


def fmt_report(rep: dict[str, Any]) -> str:
    if rep["ok"]:
        return f"in step: {rep['checked']} spool(s) checked, nothing drifts (tolerance {rep['tolerance_g']:g} g)"
    lines = [
        f"DRIFT: {rep['drift']} difference(s) in {rep['checked']} spool(s) (tolerance {rep['tolerance_g']:g} g)"
    ]
    lines += [fmt_drift(d) for d in rep["drifts"]]
    lines.append(
        f"{rep['healable']} healable (`3d printops spool drift --heal`), {rep['unhealable']} need a person."
    )
    return "\n".join(lines)


def fmt_heal(rep: dict[str, Any]) -> str:
    lines = [f"healed {len(rep['healed'])} difference(s)"]
    lines += [f"  [{d['kind']}] {d['summary']}" for d in rep["healed"]]
    lines += [f"  FAILED #{f['spool_id']}: {f['error']}" for f in rep["failed"]]
    lines.append(fmt_report(rep["after"]))
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub = "spool drift"
    p = parse(
        argv, sub, values={"--api", "--token", "--spool"}, flags={"--json", "--heal"}
    )
    spool = p.int_opt("--spool", sub)
    api = client(p, sub)
    with api_errors(sub):
        if "--heal" in p.flags:
            rep = api.post(
                "/v1/spool-sync/heal", {"spool_id": spool} if spool is not None else {}
            )
            text = fmt_heal(rep)
        else:
            rep = api.get(
                "/v1/spool-sync", **({"spool": spool} if spool is not None else {})
            )
            text = fmt_report(rep)
    print(
        json.dumps(rep, indent=2, ensure_ascii=False) if "--json" in p.flags else text
    )
    return 0 if rep["ok"] else 1


SUB_ACTION = SubAction(
    parent="spool",
    name="drift",
    summary="do Spoolman and PrintOps agree? list the differences (read-only), --heal applies them",
    usage=USAGE,
    run=run,
)
