"""`3d printops gates` — the PrintOps state the print gates read, and its offline snapshot (#164)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops gates snapshot [--out FILE] [--api URL] [--token TOKEN]
3d printops gates state PRINTER [--json] [--api URL] [--token TOKEN]

  The material gates of `3d print-preflight` (filament temperature window, loaded spool,
  remaining weight, drying, nozzle, maintenance, enclosure, plate) read each fleet printer's
  PrintOps state from 3d-api: the spool bound to every slot, its filament data, remaining-weight
  interval and drying assessment. Read-only; needs the `read` scope.

Subcommands:
  snapshot  save every printer's state to FILE for `3d print-preflight --offline` (the slicer
            post-processing hook runs without a network). Refresh it after loading spools;
            preflight WARNs when it is older than fleet/policy.yaml `gates.snapshot_max_age_h`.
  state     show one printer's state: tool → slot → spool, remaining, drying.

Options:
  --out FILE     snapshot path (default: $PRINTOPS_SNAPSHOT, else ~/.cache/3d-cli/printops-snapshot.json)
  --json         print the API's JSON
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops gates snapshot
  3d printops gates state u1
  3d printops gates state u1 --json | jq '.tools[] | {slot, spool: .spool.label}'"""

COMMON_VALUES = {"--api", "--token"}


def _tool_line(tool: dict[str, Any]) -> str:
    sp = tool.get("spool")
    if sp is None:
        return f"  tool {tool['tool']}  {tool['slot']:<10} —"
    rem = sp.get("remaining") or {}
    left = (f"{rem.get('mid_g')} ± {rem.get('sigma_g')} g" if rem.get("anchored")
            else f"≤{rem.get('hi_g')} g (never weighed)")
    dry = (sp.get("drying") or {}).get("state") or "?"
    return f"  tool {tool['tool']}  {tool['slot']:<10} {sp.get('label')}  {left}  drying {dry}"


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    verb, rest, sub = argv[0], argv[1:], "gates"
    if verb == "snapshot":
        from printops import gates

        p = parse(rest, sub, values={*COMMON_VALUES, "--out"})
        api = client(p, sub)
        with api_errors(sub):
            snap = api.get("/v1/gates/snapshot")
        out = Path(p.opts["--out"]).expanduser() if "--out" in p.opts else gates.snapshot_path()
        gates.write_snapshot(out, snap)
        bound = sum(t.get("spool") is not None for st in snap["printers"].values() for t in st["tools"])
        print(f"saved {out}: {len(snap['printers'])} printer(s), {bound} bound spool(s), taken {snap['taken_at']}")
        return 0
    if verb == "state":
        p = parse(rest, sub, values=COMMON_VALUES, flags={"--json"}, n_pos=1)
        with api_errors(sub):
            st = client(p, sub).get("/v1/gates/state", printer=p.pos[0])
        if "--json" in p.flags:
            print(json.dumps(st, indent=2))
            return 0
        print(f"{st['printer']} ({st['model']})  enclosure {st.get('enclosure') or '?'}  "
              f"plates {', '.join(st.get('plates') or []) or '?'}  taken {st['taken_at']}")
        for tool in st["tools"]:
            print(_tool_line(tool))
        return 0
    raise UsageError(f"unknown `printops gates` subcommand {verb!r}", command="printops gates",
                     remediation=["One of: snapshot, state. See `3d printops gates --help`."])


SUB = Sub(name="gates", summary="PrintOps state for the print gates; offline snapshot for the slicer hook",
          usage=USAGE, run=run)
