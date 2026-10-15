"""`3d printops events` — read (or follow live) the PrintOps event log through 3d-api."""
from __future__ import annotations

import json
import sys
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops events [--since ID | --last N] [--follow] [--json] [--api URL] [--token TOKEN]
  Show PrintOps events (slot.mismatch, spool.measured, drying.due, …) from 3d-api, oldest first.
  Needs a token with the `read` scope.

Options:
  --last N       the newest N events (default 20 when --since is not given)
  --since ID     events after event #ID (the cursor printed by a previous run)
  --follow       keep streaming new events (SSE) until Ctrl-C; with --since, replay from there first
  --json         one JSON object per line (JSONL), for jq / scripts
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops events
  3d printops events --last 100 --json | jq -r 'select(.kind == "slot.mismatch") | .subject'
  3d printops events --since 42
  3d printops events --follow
  3d printops events --follow --since 42 --json > events.jsonl"""


def line(e: dict[str, Any]) -> str:
    payload = json.dumps(e.get("payload") or {}, ensure_ascii=False, separators=(",", ":"))
    return f"#{e['id']}  {e['at']}  {e['kind']}  {e['subject']}  by {e['actor']}  {payload}"


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(argv, "events", values={"--since", "--last", "--api", "--token"}, flags={"--follow", "--json"})
    since, last = p.int_opt("--since", "events"), p.int_opt("--last", "events")
    if last is not None and last < 1:
        raise UsageError(f"--last needs a positive count, got {last}", command="printops events")
    if since is not None and last is not None:
        raise UsageError("give either --since or --last, not both", command="printops events")
    if "--follow" in p.flags and last is not None:
        raise UsageError("--follow takes --since (a cursor), not --last", command="printops events")
    as_json = "--json" in p.flags
    api = client(p, "events")

    def show(e: dict[str, Any]) -> None:
        print(json.dumps(e, ensure_ascii=False) if as_json else line(e), flush=True)

    with api_errors("events"):
        if "--follow" in p.flags:
            try:
                for e in api.follow_events(since):
                    show(e)
            except KeyboardInterrupt:
                return 0
            return 0
        page = api.events(since=since, last=None if since is not None else (last or 20))
    for e in page["events"]:
        show(e)
    if not as_json:
        print(f"cursor {page['cursor']}  (next: 3d printops events --since {page['cursor']})", file=sys.stderr)
    return 0


SUB = Sub(name="events", summary="show or --follow the PrintOps event log", usage=USAGE, run=run)
