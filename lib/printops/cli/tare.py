"""`3d printops tare …` — the empty-spool weight (tare) database of 3d-api (issue #163, plan F6)."""

from __future__ import annotations

import json
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import api_errors, client, parse

USAGE = """3d printops tare <action> [args] [--json] [--api URL] [--token TOKEN]
  Empty-spool weights (tares) used to turn a scale reading into remaining filament. Each entry
  has a source: the seed comes from SpoolmanDB (https://github.com/Donkie/SpoolmanDB, 151
  vendor/spool-kind/net rows); add your own weighings with `tare add`.

Actions:
  ls [VENDOR]
      the tare DB (all vendors, or one: "Bambu Lab" = "bambulab")
  add VENDOR GRAMS --source TEXT [--kind cardboard|plastic|refill] [--net G] [--sigma G] [--note TEXT]
      record a known empty-spool weight; --source is required (a URL, or e.g. "weighed empty
      spool, kitchen scale"); --sigma defaults to 2 g (a weighing). Our own weighings
      (sigma ≤ 2 g) win over catalogue rows of the same vendor.
  show SPOOL_ID
      the tare chain for one spool: which tare a weighing would subtract, and where it comes from

Options:
  --json         print the API's JSON answer
  --api URL / --token TOKEN   as for every `3d printops` sub

Examples:
  3d printops tare ls elegoo
  3d printops tare add Elegoo 154 --kind cardboard --net 1000 --source "weighed empty RAPID PLA+ spool 2026-10-14"
  3d printops tare show 7"""

VALUES = {"--api", "--token"}


def fmt_row(t: dict[str, Any]) -> str:
    kind = t.get("spool_kind") or "kind ?"
    net = f"net {t['net_g']:g} g" if t.get("net_g") else "net ?"
    note = f"  [{t['note']}]" if t.get("note") else ""
    return f"#{t['id']:<4} {t['vendor']:<20} {t['tare_g']:>6g} ± {t['sigma_g']:g} g  {kind:<9} {net:<12} {t['source']}{note}"


def run(argv: list[str]) -> int:
    if (
        not argv
        or argv[0] in ("-h", "--help", "help")
        or (len(argv) > 1 and argv[1] in ("-h", "--help"))
    ):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    sub = f"tare {action}"
    if action == "ls":
        p = parse(rest, sub, values=VALUES, flags={"--json"}, n_pos=(0, 1))
        api = client(p, sub)
        with api_errors(sub):
            res = api.get("/v1/tares", vendor=p.pos[0] if p.pos else None)
        text = "\n".join(fmt_row(t) for t in res["tares"]) or "no tares"
    elif action == "add":
        p = parse(
            rest,
            sub,
            values=VALUES | {"--source", "--kind", "--net", "--sigma", "--note"},
            flags={"--json"},
            n_pos=2,
        )
        body: dict[str, Any] = {
            "vendor": p.pos[0],
            "tare_g": _num(p.pos[1], "GRAMS", sub),
        }
        if "--source" not in p.opts:
            raise UsageError(
                "tare add needs --source (where the weight comes from)",
                command=f"printops {sub}",
            )
        body["source"] = p.opts["--source"]
        for opt, key in (("--kind", "spool_kind"), ("--note", "note")):
            if opt in p.opts:
                body[key] = p.opts[opt]
        for opt, key in (("--net", "net_g"), ("--sigma", "sigma_g")):
            if opt in p.opts:
                body[key] = _num(p.opts[opt], opt, sub)
        api = client(p, sub)
        with api_errors(sub):
            res = api.post("/v1/tares", body)
        text = "added " + fmt_row(res)
    elif action == "show":
        p = parse(rest, sub, values=VALUES, flags={"--json"}, n_pos=1)
        api = client(p, sub)
        with api_errors(sub):
            res = api.get(f"/v1/spools/{_int(p.pos[0], sub)}/tare")
        t = res.get("tare")
        text = (
            f"tare {t['tare_g']:g} ± {t['sigma_g']:g} g  from {t['origin']}: {t['source']}"
            if t
            else f"no tare: {res.get('reason')}"
        )
        if res.get("candidates") and not t:
            text += "\n" + "\n".join("  " + fmt_row(c) for c in res["candidates"])
    else:
        raise UsageError(
            f"unknown tare action {action!r}; accepted: ls, add, show",
            command="printops tare",
            remediation=["Run `3d printops tare --help`."],
        )
    print(
        json.dumps(res, indent=2, ensure_ascii=False) if "--json" in p.flags else text
    )
    return 0


def _num(raw: str, what: str, sub: str) -> float:
    try:
        return float(raw.replace(",", "."))
    except ValueError as e:
        raise UsageError(
            f"{what} must be a number, got {raw!r}", command=f"printops {sub}"
        ) from e


def _int(raw: str, sub: str) -> int:
    try:
        return int(raw.lstrip("#"))
    except ValueError as e:
        raise UsageError(
            f"spool id must be an integer, got {raw!r}", command=f"printops {sub}"
        ) from e


SUB = Sub(
    name="tare",
    summary="empty-spool weight DB (sourced): ls / add / show the chain for a spool",
    usage=USAGE,
    run=run,
)
