"""`3d printers load | unload | swap` — put a spool into a printer slot, take it out, or exchange it.

WHAT: the fleet-addressed front of `3d printops spool load|unload` (same 3d-api calls, same answers,
  same warnings): the slot is `PRINTER[/HEAD]` and the head may be left out for a printer with a
  single slot. Adds what the printops form lacks — `--dry-run` (read-only: what the load would bind,
  replace and write, and which rule would refuse it) and `swap` (the slot's current spool goes to a
  storage location and the new one takes its place, in ONE 3d-api transaction).
WHY: swapping a spool was `unload` + `load --replace-to` by hand, and nothing said beforehand that
  the head was marked unusable or still held another spool.
INVARIANTS: no logic is copied from printops — a real run calls `printops.cli.spool._load/_unload`
  with the reshaped argv; `--dry-run` only reads (GET /v1/printers/P, /v1/spools/ID,
  /v1/printers/P/faults) and its refusals mirror the 3d-api rules named in `printops.spools.load`.
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


def _slot_view(api: ApiClient, target: SlotTarget) -> dict[str, Any]:
    printer = api.get(f"/v1/printers/{quote(target.printer, safe='')}")
    for s in printer["slots"]:
        if s["slot"].casefold() == target.key.casefold():
            return {"printer": printer, "slot": s}
    return {"printer": printer, "slot": None}


def _plan(
    api: ApiClient,
    target: SlotTarget,
    action: str,
    spool_id: int | None,
    p: Parsed,
    replace_to: str | None,
) -> dict[str, Any]:
    """What `action` would do to `target`, from GETs only: the facts and the rules that would refuse."""
    view = _slot_view(api, target)
    slot, printer = view["slot"], view["printer"]
    occupants: list[dict[str, Any]] = (
        [] if slot is None else list(slot.get("spools") or [])
    )
    refusals: list[str] = []
    notes: list[str] = []
    spool: dict[str, Any] | None = None
    if (
        slot is None
    ):  # an AMS slot is not a head view of /v1/printers: the 3d-api checks it itself
        notes.append(
            f"{target.key} is not a toolhead of the 3d-api's fleet view: its rules are checked by the real call"
        )
    if spool_id is not None:
        spool = api.get(f"/v1/spools/{spool_id}")
        if spool.get("archived"):
            refusals.append(f"spool #{spool_id} is archived")
    others = [o for o in occupants if o["spool_id"] != spool_id]
    if action in ("load", "swap"):
        if slot is not None and not slot["usable"]:
            refusals.append(f"{target.key} is marked unusable: {slot.get('reason')}")
        if slot is not None and target.head_index is not None:
            faults = api.get(f"/v1/printers/{quote(target.printer, safe='')}/faults")[
                "faults"
            ]
            mine = [f for f in faults if f.get("head") == target.slot]
            if mine:
                shown = "; ".join(f"#{f['id']} {f['symptom']}" for f in mine[:3])
                refusals.append(f"{target.key} has {len(mine)} open fault(s): {shown}")
        if others and not replace_to:
            who = ", ".join(f"#{o['spool_id']} {o['name']}" for o in others)
            refusals.append(
                f"{target.key} already holds {who}: pass --replace-to LOCATION (or use `swap --to`)"
            )
        if (
            spool_id is not None
            and any(o["spool_id"] == spool_id for o in occupants)
            and not others
        ):
            notes.append(
                f"spool #{spool_id} is already bound to {target.key}: the load only re-writes the printer head"
            )
    elif not occupants and slot is not None:
        refusals.append(f"{target.key} holds no spool")
    steps: list[str] = []
    if action == "unload":
        steps += [
            f"unload spool #{o['spool_id']} {o['name']} from {target.key}"
            + (f" to {p.opts['--to']}" if "--to" in p.opts else "")
            for o in occupants
        ]
        if "--gross" in p.opts:
            steps.insert(0, f"weigh it first: gross {p.opts['--gross']} g")
    else:
        steps += [
            f"move spool #{o['spool_id']} {o['name']} out of {target.key} to {replace_to}"
            for o in others
            if replace_to
        ]
        steps.append(f"bind spool #{spool_id} to {target.key}")
    writes_printer = "--no-printer-write" not in p.flags
    adapter, n_slots = printer.get("adapter"), len(printer["slots"])
    if writes_printer and adapter == "moonraker":
        if action != "unload" and target.head_index is not None:
            steps.append(
                f"write the printer head {target.slot} to show the spool (when the firmware supports it; "
                "refused while the printer prints)"
            )
        if n_slots == 1:
            steps.append(
                "set Moonraker's active spool"
                + (" (cleared when it was this spool)" if action == "unload" else "")
            )
    return {
        "dry_run": True,
        "action": action,
        "slot": target.key,
        "spool": None
        if spool is None
        else {
            k: spool.get(k)
            for k in ("id", "vendor", "material", "name", "color_hex", "remaining_text")
        },
        "occupants": occupants,
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


def _dry_run(
    api: ApiClient,
    target: SlotTarget,
    action: str,
    spool_id: int | None,
    p: Parsed,
    replace_to: str | None,
) -> int:
    with api_errors(f"{action} --dry-run"):
        plan = _plan(api, target, action, spool_id, p, replace_to)
    _print_plan(plan, "--json" in p.flags)
    if plan["refusals"]:
        raise GateFailure(
            f"{action} {target.key} would be refused: {plan['refusals'][0]}",
            command=f"printers {action}",
            silent=True,
        )
    return 0


def _forward_flags(p: Parsed, flags: tuple[str, ...]) -> list[str]:
    out = [f for f in flags if f in p.flags]
    for opt in ("--api", "--token"):
        if opt in p.opts:
            out += [opt, p.opts[opt]]
    return out


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
    api = client(p, "load")
    if "--dry-run" in p.flags:
        return _dry_run(api, target, "load", spool_id, p, p.opts.get("--replace-to"))
    from printops.cli.spool import run as spool

    rest = _forward_flags(p, ("--json", "--no-printer-write", "--force-official"))
    if "--replace-to" in p.opts:
        rest += ["--replace-to", p.opts["--replace-to"]]
    return spool(["load", str(spool_id), target.key, *rest])


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
    api = client(p, "unload")
    if "--dry-run" in p.flags:
        return _dry_run(api, target, "unload", None, p, None)
    from printops.cli.spool import run as spool

    rest = _forward_flags(p, ("--json", "--no-printer-write"))
    for opt in ("--to", "--gross"):
        if opt in p.opts:
            rest += [opt, p.opts[opt]]
    return spool(["unload", target.key, *rest])


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
        return _dry_run(api, target, "swap", spool_id, p, p.opts["--to"])
    with api_errors("swap"):
        occupied = bool(
            [
                o
                for o in (_slot_view(api, target)["slot"] or {}).get("spools") or []
                if o["spool_id"] != spool_id
            ]
        )
    if not occupied and "--json" not in p.flags:
        print(
            f"note: {target.key} holds no other spool: this is a plain load", flush=True
        )
    from printops.cli.spool import run as spool

    rest = _forward_flags(p, ("--json", "--no-printer-write", "--force-official"))
    if occupied:
        rest += ["--replace-to", p.opts["--to"]]
    return spool(["load", str(spool_id), target.key, *rest])


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
