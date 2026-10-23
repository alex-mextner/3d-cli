"""`3d printers load | unload | swap` — put a spool into a printer slot, take it out, or exchange it.

WHAT: the fleet-addressed front of `3d printops spool load|unload` (same 3d-api calls, same answers,
  same warnings): the slot is `PRINTER[/HEAD]` and the head may be left out for a printer with a
  single slot. Adds what the printops form lacks — `--dry-run` (read-only: what the load would bind,
  replace and write, and which rule would refuse it) and `swap` (the slot's current spool goes to a
  storage location and the new one takes its place, in ONE 3d-api transaction).
WHY: swapping a spool was `unload` + `load --replace-to` by hand, and nothing said beforehand that
  the head was marked unusable or still held another spool.
INVARIANTS: no logic is copied from printops — a real run calls the public
  `printops.cli.spool.run(["load"|"unload", ...])` with the reshaped argv; `--dry-run` only reads
  (GET /v1/printers/P, /v1/spools?loaded=true, /v1/spools/ID, /v1/printers/P/faults) and its
  refusals mirror the 3d-api rules named in `printops.spools.load`. Occupants come from the loaded
  spools' `slot`, so toolheads and AMS slots are seen alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from errors import GateFailure, UsageError

from printers_cli import Sub
from printers_cli._common import (
    Parsed,
    api_errors,
    client,
    emit_json,
    parse,
    wants_help,
)
from printers_cli._spool_view import SlotTarget, resolve_slot

if TYPE_CHECKING:
    from printops.client import ApiClient

_COMMON_VALUES = {"--api", "--token"}
_COMMON_FLAGS = {"--json", "--dry-run"}

USAGE_LOAD = """3d printers load SPOOL PRINTER[/HEAD] [--replace-to LOCATION] [--no-printer-write] [--force-official]
                            [--dry-run] [--json] [--api URL] [--token TOKEN]
  Bind spool SPOOL (a Spoolman id, `7` or `#7`) to a printer head or AMS slot (`u1/T3`, `p1s/ams1/2`;
  HEAD may be left out when the printer has exactly one slot: `3d printers load 2 k1se`). Same call
  as `3d printops spool load`: refuses a head the fleet marks unusable or with an open fault, and an
  occupied slot unless --replace-to says where the old spool goes; then writes the printer head to
  show the spool (Snapmaker U1) and sets Moonraker's active spool on a single-head printer with
  [spoolman] (K1 SE), both read back. Not while the printer prints.
  --dry-run      read-only: print what would be bound/replaced/written and which rule would refuse
                 the load; exit 1 when one would. Nothing is written to 3d-api or the printer.
  --replace-to L the occupant goes to storage location L (`WH:default`)
  --no-printer-write  change Spoolman only; leave the printer's head and active spool alone
  --force-official    also overwrite a head showing official RFID filament
  --json         the 3d-api answer (dry run: the plan) as JSON

Exit codes: 0 done / dry run clean, 1 refused or failed (3d-api rule, printer, dry run predicts a
refusal), 2 usage (unknown printer or slot, bad option).

Examples:
  3d printers load 7 u1/T3
  3d printers load 2 k1se
  3d printers load 7 u1/T3 --replace-to WH:default --dry-run
  3d printers load 7 u1/T3 --no-printer-write --json"""

USAGE_UNLOAD = """3d printers unload PRINTER[/HEAD] [--to LOCATION] [--gross G] [--no-printer-write] [--dry-run]
                              [--json] [--api URL] [--token TOKEN]
  Take the spool out of a slot (even an unusable head). Same call as `3d printops spool unload`:
  --to stores it, --gross weighs it first (a failed weighing leaves it loaded), Moonraker's active
  spool is cleared when it was this spool.
  --dry-run      read-only: print which spool(s) would leave the slot; exit 1 when the slot is empty
  --to L         storage location the spool goes to (`WH:default`)
  --gross G      weigh the spool WITH its empty reel (g) before taking it out
  --no-printer-write  leave the printer's active spool alone
  --json         the 3d-api answer (dry run: the plan) as JSON

Exit codes: 0 done / dry run clean, 1 refused or failed, 2 usage.

Examples:
  3d printers unload u1/T3 --to WH:default --gross 803
  3d printers unload k1se --to WH:default
  3d printers unload u1/T3 --dry-run --json"""

USAGE_SWAP = """3d printers swap SPOOL PRINTER[/HEAD] --to LOCATION [--no-printer-write] [--force-official]
                            [--dry-run] [--json] [--api URL] [--token TOKEN]
  Exchange the spool in a slot: the current spool goes to LOCATION and SPOOL takes its place, in one
  3d-api transaction (`load --replace-to`). An empty slot is a plain load (a note says so). To weigh
  the old spool on the way out, `3d printers unload ... --gross G` first, then `load`.
  --to L         where the spool that leaves goes (required: a spool is never dropped without a place)
  --dry-run      read-only: what would be swapped and which rule would refuse it (exit 1 then)
  --no-printer-write, --force-official, --json   as for `3d printers load`

Exit codes: 0 done / dry run clean, 1 refused or failed, 2 usage.

Examples:
  3d printers swap 9 u1/T2 --to WH:default
  3d printers swap 9 k1se --to WH:default --dry-run
  3d printers swap 9 u1/T2 --to WH:default --json"""


def _spool_id(raw: str, command: str) -> int:
    text = raw.removeprefix("#")
    if not text.isascii() or not text.isdigit():
        raise UsageError(
            f"SPOOL must be a Spoolman spool id (7 or #7), got {raw!r}",
            command=f"printers {command}",
            remediation=["`3d printops spool ls` lists the spool ids."],
        )
    return int(text)


def _printer_path(name: str, suffix: str = "") -> str:
    return f"/v1/printers/{quote(name, safe='')}{suffix}"


def _occupants(api: ApiClient, target: SlotTarget) -> list[dict[str, Any]]:
    """The spools bound to `target` (toolhead or AMS slot), from the loaded spools' `slot`."""
    key = target.key.casefold()
    return [
        s
        for s in api.get("/v1/spools", loaded="true")["spools"]
        if str(s.get("slot") or "").casefold() == key
    ]


def _head_view(
    api: ApiClient, target: SlotTarget
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The printer's 3d-api view and the view of `target` in it (None for an AMS slot: only toolheads are there)."""
    printer = api.get(_printer_path(target.printer))
    slot = next(
        (s for s in printer["slots"] if s["slot"].casefold() == target.key.casefold()),
        None,
    )
    return printer, slot


def _describe(spool: dict[str, Any]) -> str:
    return f"#{spool['id']} {spool.get('name') or ''}".strip()


def _writes_printer(p: Parsed) -> bool:
    return "--no-printer-write" not in p.flags


def _plan_load(
    api: ApiClient,
    target: SlotTarget,
    action: str,
    spool_id: int,
    p: Parsed,
    replace_to: str | None,
) -> dict[str, Any]:
    """What `load`/`swap` would do: facts and the 3d-api rules that would refuse it, from GETs only."""
    printer, slot = _head_view(api, target)
    occupants = _occupants(api, target)
    others = [o for o in occupants if o["id"] != spool_id]
    spool = api.get(f"/v1/spools/{spool_id}")
    refusals: list[str] = []
    notes: list[str] = []
    if spool.get("archived"):
        refusals.append(f"spool #{spool_id} is archived")
    if slot is not None and not slot["usable"]:
        refusals.append(f"{target.key} is marked unusable: {slot.get('reason')}")
    if target.head_index is not None:
        faults = api.get(_printer_path(target.printer, "/faults"))["faults"]
        mine = [
            f
            for f in faults
            if f.get("head") == target.slot and f.get("status", "open") == "open"
        ]
        if mine:
            shown = "; ".join(f"#{f['id']} {f['symptom']}" for f in mine[:3])
            refusals.append(f"{target.key} has {len(mine)} open fault(s): {shown}")
    if others and not replace_to:
        refusals.append(
            f"{target.key} already holds {', '.join(_describe(o) for o in others)}: "
            "pass --replace-to LOCATION (or use `swap --to`)"
        )
    if not others and any(o["id"] == spool_id for o in occupants):
        notes.append(
            f"spool #{spool_id} is already bound to {target.key}: the load only re-writes the printer head"
        )
    steps = [
        f"move spool {_describe(o)} out of {target.key} to {replace_to}"
        for o in others
        if replace_to
    ]
    steps.append(f"bind spool #{spool_id} to {target.key}")
    if _writes_printer(p) and printer.get("adapter") == "moonraker":
        if target.head_index is not None:
            steps.append(
                f"write the printer head {target.slot} to show the spool (when the firmware supports it; "
                "refused while the printer prints)"
            )
        if len(printer["slots"]) == 1:
            steps.append("set Moonraker's active spool")
    return _plan_body(action, target, printer, spool, occupants, steps, notes, refusals)


def _plan_unload(api: ApiClient, target: SlotTarget, p: Parsed) -> dict[str, Any]:
    """What `unload` would do, from GETs only."""
    printer, _ = _head_view(api, target)
    occupants = _occupants(api, target)
    refusals = [] if occupants else [f"{target.key} holds no spool"]
    where = f" to {p.opts['--to']}" if "--to" in p.opts else ""
    steps = [f"unload spool {_describe(o)} from {target.key}{where}" for o in occupants]
    if "--gross" in p.opts and occupants:
        steps.insert(0, f"weigh it first: gross {p.opts['--gross']} g")
    if (
        _writes_printer(p)
        and printer.get("adapter") == "moonraker"
        and len(printer["slots"]) == 1
        and occupants
    ):
        steps.append("clear Moonraker's active spool when it is this spool")
    return _plan_body("unload", target, printer, None, occupants, steps, [], refusals)


def _plan_body(
    action: str,
    target: SlotTarget,
    printer: dict[str, Any],
    spool: dict[str, Any] | None,
    occupants: list[dict[str, Any]],
    steps: list[str],
    notes: list[str],
    refusals: list[str],
) -> dict[str, Any]:
    shown = (
        None
        if spool is None
        else {
            k: spool.get(k)
            for k in ("id", "vendor", "material", "name", "color_hex", "remaining_text")
        }
    )
    return {
        "dry_run": True,
        "action": action,
        "slot": target.key,
        "spool": shown,
        "occupants": [{"spool_id": o["id"], "name": o.get("name")} for o in occupants],
        "steps": steps,
        "notes": notes,
        "refusals": refusals,
        "printer_state": (printer.get("live") or {}).get("state"),
    }


def _print_plan(plan: dict[str, Any], as_json: bool) -> None:
    if as_json:
        emit_json(plan)
        return
    print(f"DRY RUN {plan['action']} {plan['slot']} (nothing is written)")
    if plan["printer_state"]:
        print(f"  printer state: {plan['printer_state']}")
    for line in plan["steps"]:
        print(f"  would: {line}")
    for line in plan["notes"]:
        print(f"  note: {line}")
    for line in plan["refusals"]:
        print(f"  WOULD BE REFUSED: {line}")
    if not plan["refusals"]:
        print("  no refusing rule found")


def _finish_dry_run(plan: dict[str, Any], target: SlotTarget, p: Parsed) -> int:
    _print_plan(plan, "--json" in p.flags)
    if plan["refusals"]:
        raise GateFailure(
            f"{plan['action']} {target.key} would be refused: {plan['refusals'][0]}",
            command=f"printers {plan['action']}",
            silent=True,
        )
    return 0


def _delegate(
    verb: str,
    positional: list[str],
    p: Parsed,
    flags: tuple[str, ...],
    values: tuple[str, ...],
) -> int:
    """Run `3d printops spool VERB` with the options this sub parsed (the printops code is the one real writer)."""
    from printops.cli.spool import run as spool

    rest = [f for f in flags if f in p.flags]
    for opt in ("--api", "--token", *values):
        if opt in p.opts:
            rest += [opt, p.opts[opt]]
    return spool([verb, *positional, *rest])


def run_load(argv: list[str]) -> int:
    if wants_help(argv):
        print(USAGE_LOAD)
        return 0
    p = parse(
        argv,
        "load",
        values=_COMMON_VALUES | {"--replace-to"},
        flags=_COMMON_FLAGS | {"--no-printer-write", "--force-official"},
        n_pos=2,
    )
    spool_id = _spool_id(p.pos[0], "load")
    target = resolve_slot(p.pos[1], "load")
    if "--dry-run" in p.flags:
        with api_errors("load --dry-run"):
            plan = _plan_load(
                client(p, "load"),
                target,
                "load",
                spool_id,
                p,
                p.opts.get("--replace-to"),
            )
        return _finish_dry_run(plan, target, p)
    return _delegate(
        "load",
        [str(spool_id), target.key],
        p,
        ("--json", "--no-printer-write", "--force-official"),
        ("--replace-to",),
    )


def run_unload(argv: list[str]) -> int:
    if wants_help(argv):
        print(USAGE_UNLOAD)
        return 0
    p = parse(
        argv,
        "unload",
        values=_COMMON_VALUES | {"--to", "--gross"},
        flags=_COMMON_FLAGS | {"--no-printer-write"},
        n_pos=1,
    )
    target = resolve_slot(p.pos[0], "unload")
    if "--dry-run" in p.flags:
        with api_errors("unload --dry-run"):
            plan = _plan_unload(client(p, "unload"), target, p)
        return _finish_dry_run(plan, target, p)
    return _delegate(
        "unload", [target.key], p, ("--json", "--no-printer-write"), ("--to", "--gross")
    )


def run_swap(argv: list[str]) -> int:
    if wants_help(argv):
        print(USAGE_SWAP)
        return 0
    p = parse(
        argv,
        "swap",
        values=_COMMON_VALUES | {"--to"},
        flags=_COMMON_FLAGS | {"--no-printer-write", "--force-official"},
        n_pos=2,
    )
    if "--to" not in p.opts:
        raise UsageError(
            "swap needs --to LOCATION: where the spool that leaves the slot goes",
            command="printers swap",
            remediation=[
                "3d printers swap 9 u1/T2 --to WH:default",
                "`3d printops loc ls` lists the locations.",
            ],
        )
    spool_id = _spool_id(p.pos[0], "swap")
    target = resolve_slot(p.pos[1], "swap")
    api = client(p, "swap")
    if "--dry-run" in p.flags:
        with api_errors("swap --dry-run"):
            plan = _plan_load(api, target, "swap", spool_id, p, p.opts["--to"])
        return _finish_dry_run(plan, target, p)
    with api_errors("swap"):
        empty = not [o for o in _occupants(api, target) if o["id"] != spool_id]
    if empty and "--json" not in p.flags:
        print(
            f"note: {target.key} holds no other spool: this is a plain load", flush=True
        )
    # --replace-to is harmless on an empty slot (the 3d-api only reads it when there is an occupant)
    p.opts["--replace-to"] = p.opts["--to"]
    return _delegate("load", [str(spool_id), target.key], p, ("--json", "--no-printer-write", "--force-official"),
                     ("--replace-to",))


SUBS = (
    Sub(
        name="load",
        summary="bind a spool to a printer slot (+ write the head / active spool)",
        usage=USAGE_LOAD,
        run=run_load,
    ),
    Sub(
        name="unload",
        summary="take the spool out of a printer slot",
        usage=USAGE_UNLOAD,
        run=run_unload,
    ),
    Sub(
        name="swap",
        summary="exchange a slot's spool: old one to --to, new one in, one transaction",
        usage=USAGE_SWAP,
        run=run_swap,
    ),
)
