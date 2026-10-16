"""`3d printops slots` — what each printer slot holds: the printer's live reading vs the bound spool."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops slots [PRINTER] [--json] [--api URL] [--token TOKEN]
  Show every fleet printer (or just PRINTER) slot by slot: what the printer reports loaded
  (material, colour, RFID tag — read by 3d-api's background poller, never by this command), the
  spool bound to the slot in Spoolman, and the slot's open reconciliation anomalies.
  Offline printers (other people's) are listed without a live reading: they are never contacted.
  Needs a token with the `read` scope.

Options:
  --json         the raw GET /v1/printers (or /v1/printers/PRINTER) JSON
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops slots
  3d printops slots u1
  3d printops slots --json | jq '.printers[].slots[] | select(.anomalies != [])'"""


def _live_slot(p: dict[str, Any] | None) -> str:
    if p is None:
        return "?"
    if p["present"] is False:
        return "empty"
    parts = [x for x in (p.get("material"), p.get("color"), p.get("vendor")) if x]
    if p.get("tag_uid"):
        parts.append(f"tag {p['tag_uid']}")
    if not parts:
        return "loaded (type unknown)" if p["present"] else "presence unknown"
    return " ".join(parts)


def _spool(s: dict[str, Any]) -> str:
    color = s["colors"][0] if s.get("colors") else ""
    rem = f" {s['remaining_g']:.0f} g" if s.get("remaining_g") is not None else ""
    return f"spool {s['spool_id']} {s['name']} {s.get('material') or ''} {color}{rem}".replace(
        "  ", " "
    ).strip()


def render(printer: dict[str, Any]) -> list[str]:
    live = printer.get("live")
    if live is None:
        state = (
            "offline (never contacted)"
            if printer["access"] == "offline"
            else "not read yet"
        )
    elif not live["ok"]:
        state = f"UNREADABLE at {live['at']}: {live['error']}"
    else:
        prog = (
            f" {live['progress'] * 100:.0f}%"
            if live.get("progress") is not None and live["state"] == "printing"
            else ""
        )
        job = (
            f" {live['filename']}"
            if live.get("filename") and live["state"] in ("printing", "paused")
            else ""
        )
        state = f"{live['state']}{prog}{job}  (read {live['at']})"
    lines = [f"{printer['name']}  {printer['model']}  [{printer['access']}]  {state}"]
    if printer.get("spools_error"):
        lines.append(f"  spools unknown: {printer['spools_error']}")
    for s in printer["slots"]:
        head = s["head"] + ("" if s["usable"] else f" (unusable: {s['reason']})")
        spools = s["spools"]
        bound = (
            "?"
            if spools is None
            else ("; ".join(_spool(x) for x in spools) or "no spool")
        )
        marks = "  ".join(
            f"!{a['kind']} #{a['id']}" + (" (ack)" if a["status"] == "ack" else "")
            for a in s["anomalies"]
        )
        lines.append(
            f"  {head:<6} printer: {_live_slot(s['printer'])}  |  {bound}"
            + (f"  {marks}" if marks else "")
        )
    return lines


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(
        argv, "slots", values={"--api", "--token"}, flags={"--json"}, n_pos=(0, 1)
    )
    api = client(p, "slots")
    with api_errors("slots"):
        if p.pos:
            data: dict[str, Any] = api.get(f"/v1/printers/{quote(p.pos[0], safe='')}")
            printers = [data]
        else:
            data = api.get("/v1/printers")
            printers = data["printers"]
    if "--json" in p.flags:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not printers:
        print("no printers in fleet/printers.yaml on the 3d-api host")
    for printer in printers:
        print("\n".join(render(printer)))
    return 0


SUB = Sub(
    name="slots",
    summary="printer slots: live reading vs bound spool, anomalies",
    usage=USAGE,
    run=run,
)
