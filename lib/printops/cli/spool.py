"""`3d printops spool …` — the spool lifecycle through 3d-api: receive, ls, show, move, load,
unload, use, measure, empty, import-owned (issues #160, #163). Domain logic lives in 3d-api
(printops.spools); this module only parses, calls HTTP and renders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import Parsed, api_errors, client, parse
from printops.client import ApiClient

USAGE = """3d printops spool <action> [args] [--json] [--api URL] [--token TOKEN]
  Filament spools in Spoolman, with the PrintOps ledger: remaining is shown as an interval
  (mid ± σ g) — the last weighing (or a sealed spool's label net) plus every usage booked since,
  each with its own uncertainty. Weighing resets the error. Reads need the `read` scope, changes
  `write`.

Actions:
  ls [--material M] [--color C] [--vendor V] [--location PREFIX] [--loaded|--unloaded] [--archived]
      list spools with location, slot and remaining interval
  show ID [--ledger N]
      one spool: identity, location/slot, remaining, tare (and where it comes from), ledger
  receive (--filament-id N | --material M --name NAME [--vendor V] [--color #HEX] [--density D]
           [--diameter MM]) [--net G] [--remaining G [--confidence G]] [--tare G [--tare-source TEXT]]
           [--spool-kind cardboard|plastic|refill] [--lot LOT] [--price P] [--location LOC] [--opened]
           [--comment TEXT]
      a new spool: the vendor/filament is matched in Spoolman or created; --net = label net
      (sealed: the ledger is anchored at it), --remaining = already used, roughly this much left
  move ID LOCATION
      store or move a spool (a loaded spool is unloaded on the way)
  load ID PRINTER/SLOT [--replace-to LOCATION]
      bind a spool to a printer head (u1/T3) or AMS slot (p1s/ams1/2); refuses heads the
      fleet marks unusable and occupied slots (unless --replace-to says where the old spool goes)
  unload PRINTER/SLOT [--to LOCATION] [--gross G]
      take the spool out of a slot, optionally weighing it on the way
  use ID GRAMS [--source S] [--confidence G] [--job JOB] [--note TEXT]
      book usage (Spoolman /use + ledger). Sources: gcode_estimate moonraker_live printer_reported
      voice manual purge (default manual); negative GRAMS gives grams back (correction)
  measure ID GROSS_G [--tare G [--tare-source TEXT]] [--scale-sigma G] [--note TEXT]
      weigh the spool WITH its empty spool: Spoolman /measure + an absolute ledger anchor; the
      tare comes from the tare chain (`3d printops tare show ID`) unless --tare is given
  empty ID [--note TEXT]
      the spool is used up: remaining 0, unloaded, archived
  import-owned FILE [--location LOC] [--project NAME] [--apply]
      spools from a project's gen/plates.json `owned` (+ `bought`): DRY RUN by default (prints the
      diff); --apply creates them. Idempotent: keys already imported are left alone.

Options:
  --json         print the API's JSON answer
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops spool ls --material PLA
  3d printops spool receive --vendor Elegoo --material PLA+ --name "RAPID PLA+ Green" --color #4FE44D --net 1000 --price 1499 --location WH:default
  3d printops spool load 7 u1/T3
  3d printops spool use 7 12.4 --source gcode_estimate --job u1-2026-10-14-01
  3d printops spool unload u1/T3 --to WH:default --gross 803
  3d printops spool measure 7 803
  3d printops spool import-owned ~/xp/garage-band/projects/babushkin-dom/gen/plates.json --location WH:default
  3d printops spool import-owned ~/xp/garage-band/projects/babushkin-dom/gen/plates.json --location WH:default --apply"""

COMMON_VALUES = {"--api", "--token"}
COMMON_FLAGS = {"--json"}


def _num(p: Parsed, opt: str, sub: str) -> float | None:
    raw = p.opts.get(opt)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError as e:
        raise UsageError(
            f"{opt} needs a number, got {raw!r}", command=f"printops {sub}"
        ) from e


def _float_arg(raw: str, what: str, sub: str) -> float:
    try:
        return float(raw.replace(",", "."))
    except ValueError as e:
        raise UsageError(
            f"{what} must be a number, got {raw!r}", command=f"printops {sub}"
        ) from e


def _id(raw: str, sub: str) -> int:
    try:
        return int(raw.lstrip("#"))
    except ValueError as e:
        raise UsageError(
            f"spool id must be an integer, got {raw!r}", command=f"printops {sub}"
        ) from e


def _slot(raw: str, sub: str) -> tuple[str, str]:
    printer, _, head = raw.removeprefix("slot:").partition("/")
    if not printer or not head:
        raise UsageError(
            f"slot must look like PRINTER/HEAD (u1/T3) or PRINTER/AMS/N, got {raw!r}",
            command=f"printops {sub}",
        )
    return printer, head


def fmt_spool(s: dict[str, Any]) -> str:
    what = " ".join(
        str(x) for x in (s.get("vendor"), s.get("material"), s.get("name")) if x
    )
    color = f"#{s['color_hex']}" if s.get("color_hex") else ""
    where = s.get("slot") and f"in {s['slot']}" or s.get("location") or "(no location)"
    flags = " [archived]" if s.get("archived") else ""
    return (
        f"#{s['id']:<4} {what} {color}  {where}  {s.get('remaining_text', '')}{flags}"
    )


def fmt_show(s: dict[str, Any]) -> str:
    lines = [fmt_spool(s)]
    r = s["remaining"]
    lines.append(
        f"  remaining  {s['remaining_text']}  (anchor: {r['anchor_source'] or '-'} {r['anchor_at'] or ''}, "
        f"{r['deltas_since_anchor']} booking(s) since; Spoolman says {s.get('spoolman_remaining_g')})"
    )
    lines.append(f"  weighed    {r['last_weighed_at'] or 'never'}")
    t = s.get("tare")
    lines.append(
        f"  tare       {t['tare_g']:g} ± {t['sigma_g']:g} g ({t['source']})"
        if t
        else f"  tare       UNKNOWN — {s.get('tare_problem')}"
    )
    for k, label in (
        ("lot_nr", "lot"),
        ("price", "price"),
        ("opened_at", "opened"),
        ("external_key", "import key"),
        ("comment", "comment"),
    ):
        if s.get(k) not in (None, ""):
            lines.append(f"  {label:<10} {s[k]}")
    if s.get("ledger"):
        lines.append("  ledger:")
        for row in s["ledger"]:
            if row["kind"] == "anchor":
                what = f"= {row['anchor_g']:.1f} g"
                if row.get("gross_g") is not None:
                    what += f" (gross {row['gross_g']:g} − tare {row['tare_g']:g})"
            elif row["kind"] == "note":
                what = "note"
            else:
                what = f"{row['delta_g']:+.1f} g"
            conf = f" ±{row['confidence_g']:g}" if row["kind"] != "note" else ""
            extra = "".join(f"  {k}={row[k]}" for k in ("job_id", "note") if row.get(k))
            lines.append(
                f"    #{row['id']} {row['at']}  {row['source']:<16} {what}{conf}  by {row['actor']}{extra}"
            )
    return "\n".join(lines)


def fmt_import(res: dict[str, Any]) -> str:
    lines = []
    for a in res["actions"]:
        if a["action"] == "create":
            f = a["filament"]
            fil = (
                f"filament #{a['filament_id']}"
                if a.get("filament_id")
                else "NEW filament"
            )
            vendor = a.get("vendor") or "no vendor"
            if a.get("vendor") and not a.get("vendor_exists"):
                vendor += " (NEW vendor)"
            price = f", price {a['price']:g}" if a.get("price") is not None else ""
            lines.append(
                f"+ {a['key']}: {vendor} {f.get('material')} {f.get('name')!r} #{f.get('color_hex')} "
                f"({fil}) → {a.get('location') or '(no location)'}{price}"
            )
        elif a["action"] == "created":
            lines.append(
                f"+ {a['key']}: created spool #{a['spool_id']} (filament #{a['filament_id']}"
                f"{', new' if a.get('filament_created') else ''}) → {a.get('location') or '(no location)'}"
            )
        else:
            lines.append(f"= {a['key']}: spool #{a['spool_id']} already imported")
            lines.extend(f"    ! {d} (not changed)" for d in a.get("differs") or [])
    mode = (
        "DRY RUN — nothing written; re-run with --apply"
        if res["dry_run"]
        else "applied"
    )
    lines.append(
        f"{res['changes']} change(s), {res['counts'].get('exists', 0)} already imported ({mode})"
    )
    return "\n".join(lines)


def _out(p: Parsed, data: Any, text: str) -> None:
    print(
        json.dumps(data, indent=2, ensure_ascii=False) if "--json" in p.flags else text
    )


def _receive_body(p: Parsed, sub: str) -> dict[str, Any]:
    body: dict[str, Any] = {}
    fid = p.int_opt("--filament-id", sub)
    if fid is not None:
        body["filament_id"] = fid
    for opt, key in (
        ("--vendor", "vendor"),
        ("--material", "material"),
        ("--name", "name"),
        ("--color", "color_hex"),
        ("--tare-source", "tare_source"),
        ("--spool-kind", "spool_kind"),
        ("--lot", "lot_nr"),
        ("--location", "location"),
        ("--comment", "comment"),
    ):
        if opt in p.opts:
            body[key] = p.opts[opt]
    for opt, key in (
        ("--density", "density"),
        ("--diameter", "diameter"),
        ("--net", "net_g"),
        ("--remaining", "remaining_g"),
        ("--confidence", "remaining_confidence_g"),
        ("--tare", "tare_g"),
        ("--price", "price"),
    ):
        v = _num(p, opt, sub)
        if v is not None:
            body[key] = v
    if "--opened" in p.flags:
        body["opened"] = True
    if "filament_id" not in body and not (body.get("material") and body.get("name")):
        raise UsageError(
            "receive needs --filament-id, or --material and --name",
            command=f"printops {sub}",
            remediation=["See `3d printops spool --help`."],
        )
    return body


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    sub = f"spool {action}"
    handler = ACTIONS.get(action)
    if handler is None:
        raise UsageError(
            f"unknown spool action {action!r}; accepted: {', '.join(ACTIONS)}",
            command="printops spool",
            remediation=["Run `3d printops spool --help`."],
        )
    if rest and rest[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return handler(rest, sub)


def _ls(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES | {"--material", "--color", "--vendor", "--location"},
        flags=COMMON_FLAGS | {"--loaded", "--unloaded", "--archived"},
    )
    if {"--loaded", "--unloaded"} <= p.flags:
        raise UsageError(
            "--loaded and --unloaded exclude each other", command=f"printops {sub}"
        )
    api = client(p, sub)
    params: dict[str, Any] = {
        k[2:]: v for k, v in p.opts.items() if k not in COMMON_VALUES
    }
    if "--loaded" in p.flags or "--unloaded" in p.flags:
        params["loaded"] = "true" if "--loaded" in p.flags else "false"
    if "--archived" in p.flags:
        params["archived"] = "true"
    with api_errors(sub):
        res = api.get("/v1/spools", **params)
    rows = res["spools"]
    _out(p, res, "\n".join(fmt_spool(s) for s in rows) if rows else "no spools")
    return 0


def _show(argv: list[str], sub: str) -> int:
    p = parse(
        argv, sub, values=COMMON_VALUES | {"--ledger"}, flags=COMMON_FLAGS, n_pos=1
    )
    api = client(p, sub)
    with api_errors(sub):
        res = api.get(
            f"/v1/spools/{_id(p.pos[0], sub)}", ledger=p.int_opt("--ledger", sub)
        )
    _out(p, res, fmt_show(res))
    return 0


def _post(
    p: Parsed,
    sub: str,
    api: ApiClient,
    path: str,
    body: dict[str, Any],
    prefix: str = "",
) -> int:
    with api_errors(sub):
        res = api.post(path, body)
    _out(p, res, prefix + fmt_spool(res) + _drift(res))
    return 0


def _drift(res: dict[str, Any]) -> str:
    """How far the ledger was off, when the answer comes from a weighing (measure, unload --gross)."""
    drift = res.get("drift_g")
    return f"  (ledger was off by {drift:+.0f} g)" if drift is not None else ""


def _receive(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES
        | {
            "--filament-id",
            "--vendor",
            "--material",
            "--name",
            "--color",
            "--density",
            "--diameter",
            "--net",
            "--remaining",
            "--confidence",
            "--tare",
            "--tare-source",
            "--spool-kind",
            "--lot",
            "--price",
            "--location",
            "--comment",
        },
        flags=COMMON_FLAGS | {"--opened"},
    )
    return _post(
        p, sub, client(p, sub), "/v1/spools", _receive_body(p, sub), "received "
    )


def _move(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES, flags=COMMON_FLAGS, n_pos=2)
    return _post(
        p,
        sub,
        client(p, sub),
        f"/v1/spools/{_id(p.pos[0], sub)}/move",
        {"location": p.pos[1]},
        "moved ",
    )


def _load(argv: list[str], sub: str) -> int:
    p = parse(
        argv, sub, values=COMMON_VALUES | {"--replace-to"}, flags=COMMON_FLAGS, n_pos=2
    )
    printer, head = _slot(p.pos[1], sub)
    body: dict[str, Any] = {"spool_id": _id(p.pos[0], sub)}
    if "--replace-to" in p.opts:
        body["replace_to"] = p.opts["--replace-to"]
    return _post(
        p,
        sub,
        client(p, sub),
        f"/v1/printers/{printer}/slots/{head}/load",
        body,
        "loaded ",
    )


def _unload(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES | {"--to", "--gross"},
        flags=COMMON_FLAGS,
        n_pos=1,
    )
    printer, head = _slot(p.pos[0], sub)
    body: dict[str, Any] = {}
    if "--to" in p.opts:
        body["to"] = p.opts["--to"]
    gross = _num(p, "--gross", sub)
    if gross is not None:
        body["gross_g"] = gross
    return _post(
        p,
        sub,
        client(p, sub),
        f"/v1/printers/{printer}/slots/{head}/unload",
        body,
        "unloaded ",
    )


def _use(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES | {"--source", "--confidence", "--job", "--note"},
        flags=COMMON_FLAGS,
        n_pos=2,
    )
    body: dict[str, Any] = {
        "grams": _float_arg(p.pos[1], "GRAMS", sub),
        "source": p.opts.get("--source", "manual"),
    }
    conf = _num(p, "--confidence", sub)
    if conf is not None:
        body["confidence_g"] = conf
    if "--job" in p.opts:
        body["job_id"] = p.opts["--job"]
    if "--note" in p.opts:
        body["note"] = p.opts["--note"]
    return _post(
        p, sub, client(p, sub), f"/v1/spools/{_id(p.pos[0], sub)}/use", body, "booked "
    )


def _measure(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES | {"--tare", "--tare-source", "--scale-sigma", "--note"},
        flags=COMMON_FLAGS,
        n_pos=2,
    )
    body: dict[str, Any] = {"gross_g": _float_arg(p.pos[1], "GROSS_G", sub)}
    for opt, key in (("--tare", "tare_g"), ("--scale-sigma", "scale_sigma_g")):
        v = _num(p, opt, sub)
        if v is not None:
            body[key] = v
    for opt, key in (("--tare-source", "tare_source"), ("--note", "note")):
        if opt in p.opts:
            body[key] = p.opts[opt]
    api = client(p, sub)
    with api_errors(sub):
        res = api.post(f"/v1/spools/{_id(p.pos[0], sub)}/measure", body)
    _out(p, res, "weighed " + fmt_spool(res) + _drift(res))
    return 0


def _empty(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--note"}, flags=COMMON_FLAGS, n_pos=1)
    body = {"note": p.opts["--note"]} if "--note" in p.opts else {}
    return _post(
        p,
        sub,
        client(p, sub),
        f"/v1/spools/{_id(p.pos[0], sub)}/empty",
        body,
        "emptied ",
    )


def _import_owned(argv: list[str], sub: str) -> int:
    from printops.importers import ImportFormatError, plates_owned

    p = parse(
        argv,
        sub,
        values=COMMON_VALUES | {"--location", "--project"},
        flags=COMMON_FLAGS | {"--apply"},
        n_pos=1,
    )
    path = Path(p.pos[0]).expanduser()
    try:
        items = plates_owned(
            path, project=p.opts.get("--project"), location=p.opts.get("--location")
        )
    except ImportFormatError as e:
        raise UsageError(str(e), command=f"printops {sub}") from e
    api = client(p, sub)
    with api_errors(sub):
        res = api.post(
            "/v1/spools/import", {"items": items, "dry_run": "--apply" not in p.flags}
        )
    _out(p, res, fmt_import(res))
    return 0


ACTIONS = {
    "ls": _ls,
    "show": _show,
    "receive": _receive,
    "move": _move,
    "load": _load,
    "unload": _unload,
    "use": _use,
    "measure": _measure,
    "empty": _empty,
    "import-owned": _import_owned,
}

SUB = Sub(
    name="spool",
    summary="spool lifecycle: receive/ls/show/move/load/unload/use/measure/empty/import-owned",
    usage=USAGE,
    run=run,
)
