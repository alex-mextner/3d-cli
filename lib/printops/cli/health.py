"""`3d printops health` — is 3d-api up, is its DB migrated, is Spoolman reachable with our fields,
and does what Spoolman holds still agree with PrintOps (spool drift)?"""

from __future__ import annotations

import json
from typing import Any

from printops.cli import Sub
from printops.cli._common import api_errors, client, parse
from printops.client import ApiClient, ApiError

USAGE = """3d printops health [--json] [--api URL] [--token TOKEN]
  Ask 3d-api for its health: DB migrations, Spoolman reachability + version, whether every
  PrintOps extra field is registered in Spoolman, whether a backup disk is registered (a stack
  without one takes NO backups; that line never changes the exit code), and the spool sync — how
  many spools differ between Spoolman and PrintOps right now (`3d printops spool drift` lists
  them), when the scheduled pass last ran and how many anomalies it could not fix. No token
  needed. Exit 0 when everything is "ok" and nothing drifts, 1 when "degraded", unreachable, or
  any spool drifts.

Options:
  --json         print the raw /v1/health JSON, plus `spool_sync` (the /v1/spool-sync/status answer)
  --api URL      3d-api base URL (default: $PRINTOPS_API, else `api:` in ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else `token:` in the same file)

Examples:
  PRINTOPS_API=http://192.168.0.39:8765 3d printops health
  3d printops health --json | jq '.spoolman.extra_fields.missing'
  3d printops health --json | jq '.spool_sync.last_pass.drift'"""


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


def spool_sync_status(api: ApiClient) -> dict[str, Any] | None:
    """The scheduled sync's state, None when this 3d-api predates it (no such endpoint)."""
    try:
        got = api.get("/v1/spool-sync/status")
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    return got if isinstance(got, dict) else None


def spool_sync_ok(s: dict[str, Any] | None) -> bool:
    """Nothing drifts and nothing is stuck: a disabled or never-run sync has nothing to report."""
    if s is None:
        return True
    last = s.get("last_pass") or {}
    return not (last.get("drift") or last.get("error") or s.get("anomalies_open"))


def render_spool_sync(s: dict[str, Any] | None) -> str:
    if s is None:
        return "  spool sync  not available (this 3d-api has no /v1/spool-sync)"
    last = s.get("last_pass")
    if not s.get("enabled"):
        return "  spool sync  OFF (PRINTOPS_SPOOL_SYNC_S=0): nothing reconciles Spoolman with the ledger by itself"
    if last is None:
        return f"  spool sync  starting (a pass every {s['interval_s']:g} s)"
    bits = [
        f"{last.get('drift', 0)} drift",
        f"last pass {last.get('at') or '-'}",
        f"every {s['interval_s']:g} s",
    ]
    if s.get("anomalies_open"):
        bits.append(
            f"{s['anomalies_open']} anomaly(ies) it could not fix (3d printops anomalies --printer spools)"
        )
    if last.get("error"):
        bits.append(f"LAST PASS FAILED: {last['error']}")
    verdict = "ok" if spool_sync_ok(s) else "DRIFT"
    hint = "" if verdict == "ok" else "  → 3d printops spool drift"
    return f"  spool sync  {verdict}  " + ", ".join(bits) + hint


def render(
    h: dict[str, Any], api: str, spool_sync: dict[str, Any] | None = None
) -> str:
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
    if sp.get("legacy_fields"):
        lines.append(
            f"  legacy    {', '.join(sp['legacy_fields'])} still defined in Spoolman → {sp.get('message', '')}"
        )
    lines.append(render_spool_sync(spool_sync))
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(argv, "health", values={"--api", "--token"}, flags={"--json"})
    api = client(p, "health")
    with api_errors("health"):
        h = api.health()
        sync = spool_sync_status(api)
    if "--json" in p.flags:
        print(json.dumps({**h, "spool_sync": sync}, indent=2))
    else:
        print(render(h, api.base_url, sync))
    return 0 if h.get("status") == "ok" and spool_sync_ok(sync) else 1


SUB = Sub(
    name="health",
    summary="3d-api + DB + Spoolman (+ extra fields) health",
    usage=USAGE,
    run=run,
)
