"""3d printers — the printer registry, one printer's dossier, its filament and its firmware.

WHAT: `list` shows every known printer MODEL with its build volume; `show NAME` is the
  one-screen dossier of a printer: model spec (build volume, nozzle, firmware, default
  material), the machine (owner, access, adapter, IP/ports, heads, AMS, enclosure, axis
  limits, slicer profile, settings files), where its web UI answers (Fluidd/Mainsail/Moonraker
  API/built-in), and — read-only, live — its state and temperatures, job queue, and job
  history with lifetime totals.

WHY: a project's 3d.yaml references a printer BY NAME (`printer: Prusa MK4`), so you need the
  menu (`list`) and a spec sheet (`show`) to know whether a 240 mm part fits. The machine on
  the desk is more than its model: its address, its web UI, what it is printing, what is
  queued and what it printed before were scattered over `printer list/status` and the
  printer's own pages.

Examples:
  3d printers list                          # valid model names + bed sizes at a glance
  3d printers show "Prusa MK4"              # spec sheet of a model (+ every fleet machine of it)
  3d printers show u1                       # dossier of the fleet printer `u1`: spec + machine + live
  3d printers show u1 --no-live --json      # static facts only, machine-readable, no network
  3d printers show k1se --history 10 --secs 3
  3d printers spools                        # which spool is in which printer, active-spool agreement
  3d printers swap 9 u1/T2 --to WH:default  # exchange a slot's spool
  3d printers fw plan u1 --to extended      # what a firmware change would do (never executes)

Self-registering subs (lib/printers_cli/<sub>.py, found by `printers_cli.discover()`; no edit here
  to add one): `spools`/`load`/`unload`/`swap`/`filament` (the filament in the printers), `fw`
  (firmware / root / Klipper-unlock per vendor profile).
  `list` and `show` stay in this module.

ROADMAP §2a: "Materials & printers — shared, cross-cutting vocabularies.
  Single canonical registries materials.yaml + printers.yaml (built-in defaults +
  user/project overrides), referenced BY NAME everywhere."

ACCESSED VIA: `3d printers list` and `3d printers show <name>`. A thin CLI frontend: the model
  registry is lib/printers.py (merged built-in + user + project), the machines are
  fleet/printers.yaml + printer-devices.yaml, the dossier is printer_ops/dossier.py (data) and
  printer_ops/dossier_render.py (text), reads go through the printer adapters.

INVARIANTS: stdlib-only at import time (the registry contract); everything else is imported
  lazily inside run(). `show` only ever reads (GET): an unreachable printer never fails the
  command (its live sections say `unreachable: <reason>`, exit 0); `--no-live` and offline
  fleet printers make no network call. Errors come from lib/errors.py.
"""
from __future__ import annotations

from cli.registry import Command
from errors import UsageError

USAGE = """3d printers <subcommand>
  Inspect the printer registry — the machines `3d` knows by name (build volume, nozzle,
  firmware, default material) — and see everything about one printer on one screen.

  list                    list every known printer model with its build volume
  show <name> [--json] [--no-live] [--history N] [--secs S]
                          dossier of one printer: model spec / machine (owner, access, IP, ports,
                          heads, AMS, axis limits, slicer profile, settings files) / web UIs /
                          live status / job queue / job history. <name> is a model from
                          `3d printers list` (all fleet machines of that model are shown) or a
                          fleet printer from `3d printer list` (`u1`, `k1se`, ...).
    --json                the whole dossier as JSON (one object: model + printers[]; every live
                          section has status/error/note)
    --no-live             static facts only, no network (offline fleet printers get this
                          automatically: "never contacted")
    --history N           how many past jobs to list (default 5; 0 = only the lifetime totals)
    --secs S              seconds to wait per read / per probe (default 10; web UI probe 2).
                          Same flag as `3d printer status`; for creality-stock it also sets the
                          WebSocket listen window

Why: you reference a printer BY NAME in 3d.yaml; `list` shows the valid names and `show`
lets you check a part against a bed before slicing ("does a 240mm part fit?") and see the
machine itself: where its web UI is, what it is printing, what is queued, what it printed.
The model registry merges built-in + ~/.config/3d-cli/printers.yaml + ./printers.yaml, so
you can add or correct a machine without editing the shipped data. `show` only reads; a
printer that does not answer prints `unreachable: <reason>` in its live sections (exit 0).
Fluidd/Mainsail/Moonraker URLs are what actually answered (probed ports 80, 4408, 4409 and
the Moonraker port; `THREED_WEBUI_PORTS=80,4408` overrides the probed web ports).

Examples:
  3d printers list
  3d printers show "Prusa MK4"             # spec sheet of a model
  3d printers show u1                      # full dossier of the fleet printer u1
  3d printers show u1 --json               # same, machine-readable
  3d printers show u1 --no-live            # no network: static facts only
  3d printers show k1se --history 10       # the last 10 jobs instead of 5
  3d printers show k1se --secs 3           # give up on a silent printer after 3 s
"""

_SHOW_VALUES = ("--history", "--secs")
_SHOW_FLAGS = ("--json", "--no-live")


def _print_list() -> int:
    from printers import load_printers  # lazy: keep module import-light (registry contract)

    printers = load_printers(command="printers")
    if not printers:
        print("(no printers in the registry)")
        return 0
    print("Known printers (3d.yaml `printer:` names):")
    for name in sorted(printers.keys()):
        p = printers[name]
        x, y, z = p.bed
        fw = p.firmware or "?"
        print(f"  - {name:<20} bed {x:g}x{y:g}x{z:g} mm   nozzle {p.nozzle_mm:g} mm   {fw}")
    print()
    print('Detail:  3d printers show "<name>"   (a fleet printer such as u1: live status, web UI, queue, history)')
    return 0


def _history_count(opts: dict[str, str]) -> int | None:
    from errors import InvalidArgument

    raw = opts.get("--history")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        raise InvalidArgument("--history", raw, ["a whole number 0-999999, e.g. 10"], command="printers")
    return int(raw)


def _seconds(opts: dict[str, str]) -> float | None:
    from errors import InvalidArgument

    raw = opts.get("--secs")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not 0 < value < float("inf"):  # also false for nan
        raise InvalidArgument("--secs", raw, ["a number of seconds > 0, e.g. 3"], command="printers")
    return value


def _print_show(argv: list[str]) -> int:
    import json

    from printer_ops.dossier import DEFAULT_HISTORY, collect, load_registry, resolve, to_json
    from printer_ops.dossier_render import render

    from printops.cli._common import parse  # the argv splitter `3d calibrate` and `3d printops` share

    args = parse(argv, "show", values=set(_SHOW_VALUES), flags=set(_SHOW_FLAGS), n_pos=1, prefix="printers")
    name, opts = args.pos[0], {**args.opts, **dict.fromkeys(args.flags, "1")}
    history, secs = _history_count(opts), _seconds(opts)
    registry = load_registry()
    resolved = resolve(name, registry)  # raises InvalidArgument listing models + fleet printers
    dossiers = [
        collect(m, live="--no-live" not in opts, history_n=DEFAULT_HISTORY if history is None else history,
                secs=secs)
        for m in resolved.machines
    ]
    if "--json" in opts:
        print(json.dumps(to_json(resolved, dossiers, registry.error), indent=1, ensure_ascii=False, default=str))
    else:
        print(render(resolved, dossiers, registry.error.message if registry.error else None))
    return 0


def _usage() -> str:
    """USAGE plus one line per self-registering sub (`printers_cli/<sub>.py`)."""
    from printers_cli import discover

    subs = discover()
    if not subs:
        return USAGE
    width = max(len(s.name) for s in subs.values())
    lines = [f"  {s.name:<{width}}  {s.summary}" + (f"  (alias: {', '.join(s.aliases)})" if s.aliases else "")
             for s in subs.values()]
    return (USAGE.rstrip("\n") + "\n\nMore subcommands (`3d printers <sub> --help` for each):\n"
            + "\n".join(lines) + "\n")


def run(argv: list[str]) -> int:
    if not argv:
        print(_usage())
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        print(_usage())
        return 0

    if sub == "list":
        return _print_list()
    if sub == "show":
        if any(a in ("-h", "--help") for a in argv[1:]):
            print(USAGE)
            return 0
        return _print_show(argv[1:])

    from printers_cli import discover, dispatch, resolve

    if resolve(sub) is None:
        raise UsageError(
            f"unknown subcommand '{sub}'",
            command="printers",
            remediation=[f"Use list, show or one of {', '.join(discover())} — see `3d printers --help`."],
        )
    return dispatch(argv)


COMMAND = Command(
    name="printers",
    group="LIBRARIES",
    summary="printer registry + dossier + fleet actions: list / show / spools / load / fw",
    usage=USAGE,
    run=run,
)
