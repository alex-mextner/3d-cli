"""`3d printops fields …` — Spoolman extra fields: the legacy migration to native fields (docs/printops/native-fields.md)."""

from __future__ import annotations

import json
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops fields migrate-native [--apply] [--json] [--api URL] [--token TOKEN]
  One-off, idempotent: move the values PrintOps used to keep in its own Spoolman extra fields into
  Spoolman's native fields, then delete each emptied field definition. DRY RUN by default (one line
  per item, nothing written); --apply writes. What moves:
    vendor spool_tare_g    -> vendor "empty spool weight" (empty_spool_weight)
    spool printops_slot    -> spool location `slot:<printer>/<head>`
    spool printops_location_id -> dropped (the location text is the only placement record)
    spool opened_at        -> spool first_used (the earlier of the two)
    spool tag_uids         -> Spoolman spool tags
    filament nozzle/bed_temp_min+max (kept) -> native single temperatures, as the midpoint, where empty
  Per item: copy / drop (native value already there) / adjust (first_used moved earlier) / link (tags) /
  fill (temperature) are written; conflict (a different native value or a tag held by another spool),
  invalid (not a usable value) and failed (Spoolman refused) are never overwritten. A field definition
  is deleted only when no item still holds a value (Spoolman destroys the values with it).
  Exit 0 when nothing is left to do or the apply fully succeeded, 1 while conflict/invalid/failed remain.

Options:
  --apply        write (the default is a dry run)
  --json         print the API's JSON answer
  --api URL / --token TOKEN   as for every `3d printops` sub (--apply needs a token with scope write)

Examples:
  3d printops fields migrate-native
  3d printops fields migrate-native --apply
  3d printops fields migrate-native --apply --json"""

VALUES = {"--api", "--token"}


def fmt(res: dict[str, Any]) -> str:
    lines = [
        f"{c['action']:<8} {c['field']} {c['item']!r}: {c['detail']}" for c in res["changes"]
    ]
    counts = ", ".join(f"{n} {a}" for a, n in res["summary"].items() if n) or "no item holds a legacy value"
    lines.append(counts)
    lines.extend(f"field {f['field']}: {f['state'].replace('_', ' ')} — {f['detail']}" for f in res["fields"])
    if not res["apply"]:
        lines.append("DRY RUN — nothing written; re-run with --apply" if res["pending"] else "DRY RUN — nothing written; nothing to do")
    elif res["blocked"]:
        lines.append("applied, but the items above still need a human; fix them in Spoolman and re-run")
    else:
        lines.append("applied")
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help") or (len(argv) > 1 and argv[1] in ("-h", "--help")):
        print(USAGE)
        return 0 if argv else 1
    if argv[0] != "migrate-native":
        raise UsageError(
            f"unknown fields action {argv[0]!r}; accepted: migrate-native",
            command="printops fields",
            remediation=["Run `3d printops fields --help`."],
        )
    action = argv[0]
    sub = f"fields {action}"
    p = parse(argv[1:], sub, values=VALUES, flags={"--json", "--apply"})
    api = client(p, sub)
    path = f"/v1/fields/{action}"
    with api_errors(sub):
        res = api.post(path, {}) if "--apply" in p.flags else api.get(path)
    print(json.dumps(res, indent=2, ensure_ascii=False) if "--json" in p.flags else fmt(res))
    return 1 if res.get("blocked") else 0


SUB = Sub(
    name="fields",
    summary="Spoolman extra fields: migrate-native (legacy -> native)",
    usage=USAGE,
    run=run,
)
