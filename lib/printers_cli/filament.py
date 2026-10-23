"""`3d printers filament` — what each head of a printer shows it holds, and set it directly.

WHAT: `show [PRINTER ...]` reads the filament of every slot straight from the printers (adapter
  `status().slots`: colour, material, vendor, sub-type); `set PRINTER[/HEAD] --color #RRGGBB
  --material M [--vendor V] [--sub-type S]` writes one head through `Adapter.set_head_filament`
  (verified by reading it back) on every adapter that implements it (Moonraker/Snapmaker U1; the
  others refuse with an UnsupportedOperation, exit 2).
WHY: `3d printers load` keeps Spoolman and the printer in step; this is the escape hatch for a
  head whose filament is not a Spoolman spool (a sample, a test roll) or whose screen drifted.
INVARIANTS: `show` and `--dry-run` only read; `set` refuses a printer that is not `access: full`,
  a printer that is printing/paused/unreadable, and a head with official RFID filament without
  --force-official (the adapter enforces the last two; the dry run predicts them). Capability is
  "the adapter overrides set_head_filament": nothing is keyed on a printer name.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from errors import GateFailure, InvalidArgument, ThreeDError, UsageError

from printers_cli import Sub
from printers_cli._common import (
    PrinterRef,
    emit_json,
    known_printers,
    parse,
    parse_secs,
    require_printer,
    wants_help,
)
from printers_cli._spool_view import resolve_slot

if TYPE_CHECKING:
    from printer_ops.base import Adapter, Slot

USAGE = """3d printers filament <show|set> ...
  What a printer's heads show they hold, and writing one head's filament directly.

  show [PRINTER ...] [--secs S] [--json]
      read every slot of the printers (default: every contactable fleet printer) from the printer
      itself: colour, material, vendor, sub-type, present or empty. Never writes. A printer that
      cannot be read prints `unreachable: <reason>` (exit 0); offline fleet printers are never
      contacted.
  set PRINTER[/HEAD] --color #RRGGBB --material M [--vendor V] [--sub-type S] [--force-official]
      [--dry-run] [--secs S] [--json]
      make the head show this filament and read it back (the Snapmaker U1 writes
      SET_PRINT_FILAMENT_CONFIG and RESETS the head's pressure advance to the firmware default: the
      answer prints the K it now has). Vendor defaults to Generic, sub-type to Basic. Refused:
      a printer that is not `access: full`, one that is printing/paused/unreadable, a head with
      official RFID filament (unless --force-official), an adapter without head-filament writes
      (exit 2). Spoolman is NOT touched: a bound spool keeps its binding and PrintOps may report a
      slot mismatch; for a Spoolman spool use `3d printers load`.
      --dry-run   read-only: print the head before and after, and which rule would refuse the write
                  (exit 1 when one would)

Exit codes: 0 ok, 1 refused / failed / dry run predicts a refusal, 2 usage or unsupported adapter.

Examples:
  3d printers filament show
  3d printers filament show u1 --json | jq '.printers[0].slots[] | select(.present | not)'
  3d printers filament set u1/T2 --color '#C62828' --material PETG --vendor Elegoo
  3d printers filament set u1/T2 --color '#C62828' --material PETG --dry-run
  3d printers filament set u1/T1 --color '#FFFFFF' --material PLA --force-official"""

_HEX = re.compile(r"#?[0-9A-Fa-f]{6}")
SET_CMD = "printers filament set"
CREALITY_LISTEN_S = 3.0  # the push window of creality-stock; --secs is a per-read timeout, not a reason to idle 10 s
_TEXT = re.compile(r"[A-Za-z0-9+._ -]{1,32}")  # what travels into a firmware G-code argument


def _slot_json(s: Slot) -> dict[str, Any]:
    return {
        "index": s.index,
        "label": s.label,
        "present": s.present,
        "color": s.color,
        "material": s.material,
        "vendor": s.vendor,
        "sub_type": s.sub_type,
    }


def _adapter(ref: PrinterRef, secs: float) -> Adapter:
    from printer_ops import make_adapter

    if ref.device is None:
        raise ThreeDError(
            f"{ref.name} is never contacted (access {ref.access})",
            command="printers filament",
        )
    return make_adapter(ref.device, timeout=secs, listen_s=min(secs, CREALITY_LISTEN_S))


def _show(argv: list[str]) -> int:
    p = parse(argv, "filament show", values={"--secs"}, flags={"--json"}, n_pos=(0, 64))
    secs = parse_secs(p.opts.get("--secs"), "filament show")
    refs = known_printers()
    chosen = (
        [require_printer(n, "filament show") for n in p.pos]
        if p.pos
        else [r for r in refs.values() if r.contactable]
    )
    out: list[dict[str, Any]] = []
    for ref in chosen:
        row: dict[str, Any] = {
            "name": ref.name,
            "access": ref.access,
            "status": "ok",
            "error": None,
            "slots": [],
        }
        if not ref.contactable:
            row.update(
                status="skipped",
                error=f"{ref.name} is never contacted (access {ref.access})",
            )
        else:
            try:
                st = _adapter(ref, secs).status()
                row["slots"] = [_slot_json(s) for s in st.slots]
                row["state"] = st.state
            except ThreeDError as e:
                row.update(status="unreachable", error=e.message)
        out.append(row)
    if "--json" in p.flags:
        emit_json({"printers": out})
        return 0
    for row in out:
        if row["status"] != "ok":
            print(f"{row['name']}: {row['status']}: {row['error']}")
            continue
        print(f"{row['name']}  [{row['access']}]  {row['state']}")
        if not row["slots"]:
            print("  (this printer reports no per-head filament)")
        for s in row["slots"]:
            what = (
                " ".join(
                    x
                    for x in (s["material"], s["color"], s["vendor"], s["sub_type"])
                    if x
                )
                or "type unknown"
            )
            print(f"  {s['label']:<4} {what if s['present'] is not False else 'empty'}")
    return 0


def _supports_head_write(adapter: Adapter) -> bool:
    from printer_ops.base import Adapter as Base

    return type(adapter).set_head_filament is not Base.set_head_filament


def _set(argv: list[str]) -> int:
    from printer_ops.base import HeadFilament, PrinterRefused, UnsupportedOperation

    p = parse(
        argv,
        "filament set",
        values={"--color", "--material", "--vendor", "--sub-type", "--secs"},
        flags={"--json", "--dry-run", "--force-official"},
        n_pos=1,
    )
    cmd = SET_CMD
    for need in ("--color", "--material"):
        if need not in p.opts:
            raise UsageError(
                f"set needs {need}",
                command=cmd,
                remediation=[
                    "3d printers filament set u1/T2 --color '#C62828' --material PETG"
                ],
            )
    if not _HEX.fullmatch(p.opts["--color"]):
        raise InvalidArgument(
            "--color", p.opts["--color"], ["#RRGGBB, e.g. '#C62828'"], command=cmd
        )
    secs = parse_secs(p.opts.get("--secs"), "filament set")
    for opt in ("--material", "--vendor", "--sub-type"):
        if opt in p.opts and not _TEXT.fullmatch(p.opts[opt]):
            raise InvalidArgument(opt, p.opts[opt], ["1-32 of letters, digits, space and + . _ -"], command=cmd)
    target = resolve_slot(p.pos[0], "filament set")
    if target.head_index is None:
        raise UsageError(
            f"{target.key} is not a toolhead of the fleet entry: only heads have a writable filament",
            command=cmd,
        )
    ref = target.ref
    if ref.device is None or ref.access != "full":
        raise PrinterRefused(
            f"{ref.name} is {ref.access}: filament writes need access `full`",
            command=cmd,
            remediation=["`3d printers show` lists each printer's access."],
        )
    want = HeadFilament(
        color="#" + p.opts["--color"].lstrip("#").upper(),
        material=p.opts["--material"],
        vendor=p.opts.get("--vendor", "Generic"),
        sub_type=p.opts.get("--sub-type", "Basic"),
    )
    adapter = _adapter(ref, secs)
    if not _supports_head_write(adapter):
        raise UnsupportedOperation(
            f"adapter '{adapter.kind}' (printer {ref.name}) does not support writing a head's filament",
            command=cmd,
            remediation=["`3d printers filament show` reads what the printer reports."],
        )
    force = "--force-official" in p.flags
    if "--dry-run" in p.flags:
        return _dry_run(
            adapter, ref, target.head_index, want, force, "--json" in p.flags
        )
    res = adapter.set_head_filament(target.head_index, want, force_official=force)
    body = {
        "printer": ref.name,
        "head": target.slot,
        "extruder": res.extruder,
        "before": _slot_json(res.before),
        "after": _slot_json(res.after),
        "pressure_advance": res.pressure_advance,
    }
    if "--json" in p.flags:
        emit_json(body)
        return 0
    a = res.after
    print(
        f"{ref.name}/{target.slot} now shows {a.material} {a.color} {a.vendor} {a.sub_type} (read back)"
    )
    k = res.pressure_advance
    print(
        f"  pressure advance of {res.extruder}: {'K=' + format(k, 'g') if k is not None else 'unknown'}"
        " (the firmware resets it on this write)"
    )
    return 0


def _dry_run(
    adapter: Adapter, ref: PrinterRef, index: int, want: Any, force: bool, as_json: bool
) -> int:
    st = adapter.status()
    before = next((s for s in st.slots if s.index == index), None)
    refusals: list[str] = []
    if st.busy:
        refusals.append(
            f"{ref.name} is {st.state} [{st.raw_state}]: a head write is never done during a job"
        )
    if before is None:
        refusals.append(f"{ref.name} reports no head with index {index}")
    official = (st.raw.get("print_task_config") or {}).get("filament_official")
    if (not force and isinstance(official, list) and index < len(official) and official[index] is True):
        refusals.append(f"{ref.name} head index {index} holds official RFID filament: pass --force-official to overwrite it")
    plan = {
        "dry_run": True,
        "printer": ref.name,
        "index": index,
        "before": None if before is None else _slot_json(before),
        "after": {
            "color": want.color,
            "material": want.material,
            "vendor": want.vendor,
            "sub_type": want.sub_type,
        },
        "force_official": force,
        "refusals": refusals,
    }
    if as_json:
        emit_json(plan)
    else:
        print(
            f"DRY RUN filament set {ref.name} head index {index} (nothing is written)"
        )
        print(f"  before: {plan['before']}")
        print(f"  after:  {plan['after']}")
        for r in refusals:
            print(f"  WOULD BE REFUSED: {r}")
    if refusals:
        raise GateFailure(
            f"filament set on {ref.name} would be refused: {refusals[0]}",
            command=SET_CMD,
            silent=True,
        )
    return 0


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    if rest and wants_help(rest):
        print(USAGE)
        return 0
    if action == "show":
        return _show(rest)
    if action == "set":
        return _set(rest)
    raise UsageError(
        f"unknown filament action {action!r}; accepted: show, set",
        command="printers filament",
        remediation=["Run `3d printers filament --help`."],
    )


SUB = Sub(
    name="filament",
    summary="what each head shows it holds; set one head's filament directly",
    usage=USAGE,
    run=run,
)
