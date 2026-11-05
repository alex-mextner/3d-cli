"""`3d printops health` — is 3d-api up, is its DB migrated, is Spoolman reachable with our fields?"""

from __future__ import annotations

import json
from typing import Any

from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops health [--json] [--api URL] [--token TOKEN]
  Ask 3d-api for its health: DB migrations, Spoolman reachability + version, whether every
  PrintOps extra field is registered in Spoolman, and whether a backup disk is registered (a stack
  without one takes NO backups; that line never changes the exit code). No token needed.
  Exit 0 when "ok", 1 when "degraded" or unreachable.

Options:
  --json         print the raw /v1/health JSON
  --api URL      3d-api base URL (default: $PRINTOPS_API, else `api:` in ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else `token:` in the same file)

Examples:
  PRINTOPS_API=http://192.168.0.39:8765 3d printops health
  3d printops health --json | jq '.spoolman.extra_fields.missing'"""


def _backup_line(backup: Any) -> str:
    """No backup target means NO backups are being taken (HA's schedule has nothing to run): say so loudly."""
    if not isinstance(backup, dict):
        return "  backup    unknown (this 3d-api predates the backup report)"
    if not backup.get("configured"):
        why = f" ({backup['error']})" if backup.get("error") else ""
        return (
            "  backup    NOT CONFIGURED: no backup disk is registered, so NO backups are taken"
            + why
            + ". Set one up: `3d init --env --stage-only --spoolman <target>`"
        )
    last = backup.get("last") or {}
    outcome = (
        f"last run {last.get('status', '?')} {last.get('at', '')}".strip()
        if last
        else "no run recorded yet"
    )
    return f"  backup    targets {', '.join(backup.get('targets', []))}  {outcome}" + (
        f"  ({last['error']})" if last.get("error") else ""
    )


def render(h: dict[str, Any], api: str) -> str:
    db, sp = h.get("db", {}), h.get("spoolman", {})
    lines = [f"3d-api {api}  {h.get('status', '?')}  (version {h.get('version', '?')})"]
    if db.get("ok"):
        lines.append(
            f"  db        ok  migrations {','.join(str(v) for v in db.get('migrations', []))}"
            f"  last event #{db.get('last_event_id', 0)}"
        )
    else:
        lines.append(f"  db        FAIL  {db.get('error', '')}")
    fields = sp.get("extra_fields") or {}
    if sp.get("ok"):
        lines.append(f"  spoolman  ok  v{sp.get('version', '?')}  extra fields ok")
    elif "version" in sp:
        lines.append(
            f"  spoolman  v{sp.get('version')}  extra fields {fields.get('status', '?')}"
            f"  missing: {', '.join(fields.get('missing') or []) or '-'}"
            + (f"  ({fields['error']})" if fields.get("error") else "")
        )
    else:
        lines.append(f"  spoolman  UNREACHABLE  {sp.get('error', '')}")
    lines.append(_backup_line(h.get("backup")))
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(argv, "health", values={"--api", "--token"}, flags={"--json"})
    api = client(p, "health")
    with api_errors("health"):
        h = api.health()
    print(json.dumps(h, indent=2) if "--json" in p.flags else render(h, api.base_url))
    return 0 if h.get("status") == "ok" else 1


SUB = Sub(
    name="health",
    summary="3d-api + DB + Spoolman (+ extra fields) health",
    usage=USAGE,
    run=run,
)
