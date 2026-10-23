"""`3d printers spools` — which spool is in which printer, how much is left, and whether Moonraker
books the printer's usage to that spool.

ACCESSED VIA: `3d printers spools [PRINTER ...]` (alias `slots`). Read-only: 3d-api GETs plus, for
single-head Moonraker printers, one GET of the printer's own `[spoolman]` state; never a write.
The logic is `printers_cli/_spool_view.py`.
"""

from __future__ import annotations

from urllib.parse import quote

from errors import GateFailure

from printers_cli import Sub
from printers_cli._common import (
    api_errors,
    client,
    emit_json,
    known_printers,
    parse,
    require_printer,
    wants_help,
)
from printers_cli._spool_view import Report, build_report, read_active_spool, render

USAGE = """3d printers spools [PRINTER ...] [--no-live] [--strict] [--secs S] [--json] [--api URL] [--token TOKEN]
  What is loaded where: every fleet printer (or just the PRINTERs given), slot by slot — what
  the printer reports loaded (read by 3d-api's poller), the spool bound to the slot in Spoolman
  with its remaining filament as an interval (mid ± σ g), the slot's open anomalies — and
  whether Moonraker's ACTIVE spool agrees with the bound one. A mismatch means the printer's
  own usage reporting books filament to the wrong spool. Moonraker keeps ONE active spool, so
  the agreement is checked for single-head printers with [spoolman] (a K1 SE); a multi-head
  printer (the U1) says why it is not applicable. Offline fleet printers are listed, never
  contacted. Needs a token with the `read` scope. Reads only; nothing is written anywhere.

Options:
  --no-live      do not ask the printers (no active-spool read); 3d-api data only
  --strict       exit 1 when any printer's active spool disagrees with its bound spool
  --secs S       seconds to wait for each printer read (default 10)
  --json         the whole report as JSON: printers[] {name, model, access, adapter, state,
                 slots[] {slot, head, usable, reason, printer, spools[], anomalies[]},
                 active_spool {status, note, spool_id, bound_spool_id, connected, pending_reports}}
                 + spools_error
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Exit codes: 0 ok (also with a mismatch unless --strict), 1 --strict and a mismatch / 3d-api
unreachable or refused, 2 usage (unknown printer, bad option).

Examples:
  3d printers spools
  3d printers spools k1se --strict
  3d printers spools --no-live --json | jq '.printers[].slots[] | select(.spools == [])'
  3d printers spools --json | jq '.printers[] | select(.active_spool.status == "mismatch") | .name'"""

DEFAULT_SECS = 10.0


def _seconds(raw: str | None) -> float:
    from errors import InvalidArgument

    if raw is None:
        return DEFAULT_SECS
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not 0 < value < float("inf"):
        raise InvalidArgument(
            "--secs",
            raw,
            ["a number of seconds > 0, e.g. 3"],
            command="printers spools",
        )
    return value


def run(argv: list[str]) -> int:
    if wants_help(argv):
        print(USAGE)
        return 0
    p = parse(
        argv,
        "spools",
        values={"--api", "--token", "--secs"},
        flags={"--json", "--no-live", "--strict"},
        n_pos=(0, 64),
    )
    secs = _seconds(p.opts.get("--secs"))
    names = [require_printer(n, "spools").name for n in p.pos]
    api = client(p, "spools")
    with api_errors("spools"):
        if names:
            views = [api.get(f"/v1/printers/{quote(n, safe='')}") for n in names]
        else:
            views = api.get("/v1/printers")["printers"]
        loaded = api.get("/v1/spools", loaded="true")["spools"]
    report = build_report(
        views,
        loaded,
        known_printers(),
        live="--no-live" not in p.flags,
        read=lambda ref: read_active_spool(ref, secs),
        spools_error=next(
            (v["spools_error"] for v in views if v.get("spools_error")), None
        ),
    )
    _print(report, "--json" in p.flags)
    if "--strict" in p.flags and report.mismatches:
        raise GateFailure(
            f"Moonraker's active spool disagrees with the bound spool on: {', '.join(report.mismatches)}",
            command="printers spools",
            silent=True,
        )
    return 0


def _print(report: Report, as_json: bool) -> None:
    if as_json:
        emit_json(report.to_json())
    else:
        print(render(report))


SUB = Sub(
    name="spools",
    summary="what is loaded where: spool, remaining interval, active-spool agreement",
    usage=USAGE,
    run=run,
    aliases=("slots",),
)
