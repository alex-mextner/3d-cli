"""`3d printops anomalies` — list, ack and resolve reconciliation anomalies through 3d-api."""

from __future__ import annotations

import json
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops anomalies [--status open,ack|resolved|all] [--printer NAME] [--limit N] [--json]
3d printops anomalies ack ID [--note TEXT]
3d printops anomalies resolve ID [--note TEXT]
  Reconciliation anomalies: a printer slot that disagrees with the spool bound to it in Spoolman
  (type_mismatch, color_mismatch — CIEDE2000 above the threshold, tag_mismatch,
  empty_with_spool, unbound_filament) or a binding that cannot be right (multiple_spools,
  unusable_head, unknown_slot, location_conflict), or a printer head that `spool load` could not
  write (printer_write_failed; re-run the load to retry). 3d-api opens and resolves them by itself;
  `ack` marks one as known ("leave it") until it clears, `resolve` closes it now (if the
  condition is still there, the next pass opens a new one). Listing needs scope `read`,
  ack/resolve scope `write`.

Options:
  --status S     comma list of open, ack, resolved, or `all` (default open,ack)
  --printer NAME only anomalies of this fleet printer (`fleet` = bindings to unknown printers)
  --limit N      newest N (default 200, max 1000)
  --note TEXT    why (stored on the anomaly and in the event)
  --json         raw JSON
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops anomalies
  3d printops anomalies --printer u1 --status all
  3d printops anomalies ack 7 --note "T3 is PLA-CF on purpose"
  3d printops anomalies resolve 7"""


def line(a: dict[str, Any]) -> str:
    ev = a["evidence"]
    bits = []
    seen = ev.get("printer")
    if isinstance(seen, dict):
        shown = " ".join(
            str(x)
            for x in (seen.get("material"), seen.get("color"), seen.get("tag_uid"))
            if x
        )
        bits.append(
            f"printer: {shown or ('empty' if seen.get('present') is False else 'loaded')}"
        )
    spool = ev.get("spool")
    if isinstance(spool, dict):
        colors = ",".join(spool.get("colors") or [])
        bits.append(
            f"spool {spool['spool_id']}: {spool.get('material') or '?'} {colors}".rstrip()
        )
    bits.extend(f"spool {s['spool_id']}" for s in ev.get("spools") or [])
    if "delta_e2000" in ev:
        bits.append(f"ΔE2000 {ev['delta_e2000']} > {ev['max_delta_e']}")
    bits.extend(
        f"{key}: {ev[key]}" for key in ("why", "location", "reason") if ev.get(key)
    )
    tail = (
        f"  [{a['status']}"
        + (f" by {a['resolved_by'] or a['acked_by']}" if a["status"] != "open" else "")
        + "]"
    )
    return (
        f"#{a['id']}  {a['at']}  {a['kind']}  {a['subject']}  " + "; ".join(bits) + tail
    )


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv and argv[0] in ("ack", "resolve"):
        action = argv[0]
        p = parse(
            argv[1:],
            "anomalies",
            values={"--note", "--api", "--token"},
            flags={"--json"},
            n_pos=1,
        )
        try:
            anomaly_id = int(p.pos[0])
        except ValueError as e:
            raise UsageError(
                f"anomaly id must be a number, got {p.pos[0]!r}",
                command="printops anomalies",
            ) from e
        api = client(p, "anomalies")
        body = {"note": p.opts["--note"]} if "--note" in p.opts else {}
        with api_errors("anomalies"):
            row = api.post(f"/v1/anomalies/{anomaly_id}/{action}", body)
        print(json.dumps(row, ensure_ascii=False) if "--json" in p.flags else line(row))
        return 0
    p = parse(
        argv,
        "anomalies",
        values={"--status", "--printer", "--limit", "--api", "--token"},
        flags={"--json"},
    )
    api = client(p, "anomalies")
    with api_errors("anomalies"):
        data = api.get(
            "/v1/anomalies",
            status=p.opts.get("--status"),
            printer=p.opts.get("--printer"),
            limit=p.int_opt("--limit", "anomalies"),
        )
    if "--json" in p.flags:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    rows = data["anomalies"]
    if not rows:
        print("no anomalies")
    for a in rows:
        print(line(a))
    return 0


SUB = Sub(
    name="anomalies",
    summary="list / ack / resolve slot reconciliation anomalies",
    usage=USAGE,
    run=run,
)
