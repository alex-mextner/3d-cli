"""3d printops — PrintOps (spools, slots, drying, gates…) through the 3d-api service.

WHAT: a sub-dispatcher. Each `lib/printops/cli/<sub>.py` exposes `SUB = Sub(...)` and is found
  by `printops.cli.discover()`; adding a sub never edits this module (PrintOps contract).
WHY: PrintOps grows one slice at a time (epic #154); every slice brings its own subcommand.
INVARIANTS: stdlib-only at import time (registry contract) — the subs are discovered lazily
  inside run(), so `3d help` does not import them.
"""
from __future__ import annotations

from cli.registry import Command
from errors import UsageError

INTRO = """3d printops <sub> [args]
  PrintOps: filament spools in Spoolman + the PrintOps DB, served by 3d-api on the Odroid.
  The CLI talks HTTP to 3d-api: set PRINTOPS_API (e.g. http://192.168.0.39:8765) and
  PRINTOPS_TOKEN, or `api:`/`token:` in ~/.config/3d-cli/printops.yaml.
  Run `3d printops <sub> --help` for a sub's options and examples.

Subcommands:"""


def usage() -> str:
    from printops.cli import discover

    subs = discover()
    width = max((len(n) for n in subs), default=8)
    lines = [INTRO, *(f"  {s.name:<{width}}  {s.summary}" for s in subs.values())]
    lines += ["", "Examples:", "  3d printops health", "  3d printops events --follow",
              "  3d printops token create bot --scopes read,write --ssh ultra@192.168.0.39"]
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if argv else 1
    from printops.cli import discover

    subs = discover()
    sub = subs.get(argv[0])
    if sub is None:
        raise UsageError(f"unknown printops subcommand {argv[0]!r}; accepted: {', '.join(subs)}",
                         command="printops", remediation=["Run `3d printops --help`."])
    return sub.run(argv[1:])


COMMAND = Command(
    name="printops",
    group="SLICING",
    summary="PrintOps via 3d-api: health, events, tokens, deploy/backup of Spoolman + 3d-api",
    usage=INTRO,
    run=run,
)
