"""`3d printops spool …` — the spool lifecycle through 3d-api: receive (also from a label photo),
scan, ls, show, move, load, unload, use, measure, empty, import-owned (issues #160, #163, #379).
Domain logic lives in 3d-api (printops.spools); this module only parses, calls HTTP and renders.
Label photos are read locally (printops.ocr + printops.spool_label) and nothing is written
without the user's confirmation."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import InputNotFound, MissingDependency, ThreeDError, UsageError
from printops.cli import Sub
from printops.cli._common import Parsed, api_errors, client, parse
from printops.client import ApiClient, ApiError
from printops.spool_label import Found, Proposal, clean, parse_label

if TYPE_CHECKING:
    from printops.ocr import OcrText

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
  receive --from-photo IMG|TXT [any receive flag …] [--yes]
      the same, filled from the spool label: the photo is OCR'd (tesseract), the rules read
      vendor, material, colour, net, diameter, lot (and nozzle/bed temperatures, kept in the
      comment); a flag overrides what was read. The proposal is printed and the spool is created
      only after a yes at the terminal or --yes. A .txt file is taken as the label text itself.
  scan IMG|TXT [IMG|TXT …] [--location LOC]
      read label photos into proposals — each field with the label line it came from, a ready
      `receive` command per spool, and a table of the batch. Writes nothing.
  move ID LOCATION
      store or move a spool (a loaded spool is unloaded on the way)
  load ID PRINTER/SLOT [--replace-to LOCATION]
      bind a spool to a printer head (u1/T3) or AMS slot (p1s/ams1/2); refuses heads the
      fleet marks unusable and occupied slots (unless --replace-to says where the old spool goes)
  unload PRINTER/SLOT [--to LOCATION] [--gross G]
      take the spool out of a slot (even an unusable head); --gross weighs it first (a failed
      weighing leaves it loaded)
  use ID GRAMS [--source S] [--confidence G] [--job JOB] [--note TEXT]
      book usage (Spoolman /use + ledger). Sources: gcode_estimate moonraker_live printer_reported
      voice manual purge (default manual); negative GRAMS gives grams back (correction)
  measure ID GROSS_G [--tare G [--tare-source TEXT]] [--scale-sigma G] [--note TEXT]
      weigh the spool WITH its empty spool: Spoolman /measure + an absolute ledger anchor; the
      tare comes from the tare chain (`3d printops tare show ID`) unless --tare is given
  empty ID [--note TEXT]
      the spool is used up: remaining 0, unloaded, archived
  import-owned FILE [--location LOC] [--project NAME] [--apply]
      spools from a project's gen/plates.json `owned` (+ `bought`; the vendor is matched against
      the tare DB's vendors, so "Bambu Lab …" splits right): DRY RUN by default (prints the
      diff); --apply creates them. Idempotent: keys already imported are left alone.

Options:
  --json         print the API's JSON answer (scan / receive --from-photo: the proposal too)
  --yes          receive --from-photo: create without asking (after checking the proposal)
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops spool ls --material PLA
  3d printops spool receive --vendor Elegoo --material PLA+ --name "RAPID PLA+ Green" --color #4FE44D --net 1000 --price 1499 --location WH:default
  3d printops spool scan ~/.cache/spool-labels/*.jpg --location WH:default
  3d printops spool scan label.txt --json
  3d printops spool receive --from-photo ~/.cache/spool-labels/k1-black.jpg --name "чёрный (K1)" --location WH:default
  3d printops spool receive --from-photo label.jpg --net 750 --price 1299 --location WH:default --yes
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


# receive flag ↔ body key; one table for parsing the flags and for printing a proposal back as
# the receive command that creates it.
RECEIVE_STR = (
    ("--vendor", "vendor"),
    ("--material", "material"),
    ("--name", "name"),
    ("--color", "color_hex"),
    ("--tare-source", "tare_source"),
    ("--spool-kind", "spool_kind"),
    ("--lot", "lot_nr"),
    ("--location", "location"),
    ("--comment", "comment"),
)
RECEIVE_NUM = (
    ("--density", "density"),
    ("--diameter", "diameter"),
    ("--net", "net_g"),
    ("--remaining", "remaining_g"),
    ("--confidence", "remaining_confidence_g"),
    ("--tare", "tare_g"),
    ("--price", "price"),
)
RECEIVE_VALUES = {"--filament-id"} | {o for o, _ in RECEIVE_STR + RECEIVE_NUM}


def _receive_fields(p: Parsed, sub: str) -> dict[str, Any]:
    """The receive body fields given as flags (nothing checked yet)."""
    body: dict[str, Any] = {}
    fid = p.int_opt("--filament-id", sub)
    if fid is not None:
        body["filament_id"] = fid
    for opt, key in RECEIVE_STR:
        if opt in p.opts:
            body[key] = p.opts[opt]
    for opt, key in RECEIVE_NUM:
        v = _num(p, opt, sub)
        if v is not None:
            body[key] = v
    if "--opened" in p.flags:
        body["opened"] = True
    return body


def receive_command(body: dict[str, Any]) -> str:
    """The `3d printops spool receive …` line that sends `body`."""
    args = ["3d", "printops", "spool", "receive"]
    if "filament_id" in body:
        args += ["--filament-id", str(body["filament_id"])]
    for opt, key in RECEIVE_STR + RECEIVE_NUM:
        v = body.get(key)
        if v is not None:
            args += [opt, f"{v:g}" if isinstance(v, float) else str(v)]
    if body.get("opened"):
        args.append("--opened")
    return shlex.join(args)


def _check_receive(body: dict[str, Any], sub: str, *, hint: str = "") -> dict[str, Any]:
    if "filament_id" not in body and not (body.get("material") and body.get("name")):
        raise UsageError(
            "receive needs --filament-id, or --material and --name" + hint,
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
        values=COMMON_VALUES | RECEIVE_VALUES | {"--from-photo"},
        flags=COMMON_FLAGS | {"--opened", "--yes"},
    )
    api = client(p, sub)
    given = _receive_fields(p, sub)
    if "--from-photo" not in p.opts:
        if "--yes" in p.flags:
            raise UsageError(
                "--yes confirms a --from-photo proposal; a plain receive needs no confirmation",
                command=f"printops {sub}",
            )
        return _post(p, sub, api, "/v1/spools", _check_receive(given, sub), "received ")
    prop, ocr = read_proposal(
        Path(p.opts["--from-photo"]).expanduser(), _vendors(api), sub
    )
    label = prop.receive_body()
    if "filament_id" in given:
        # the filament is chosen: the label only adds what belongs to the spool
        label = {k: v for k, v in label.items() if k not in FILAMENT_KEYS}
    body = {**label, **given}
    as_json = "--json" in p.flags
    # the proposal is always shown before the prompt; with --json on stderr, so stdout stays
    # one JSON document
    print(fmt_proposal(prop, ocr, body), file=sys.stderr if as_json else sys.stdout)
    proposal = _proposal_json(prop, ocr, body)
    missing = ", ".join(prop.missing)
    try:
        _check_receive(
            body, sub, hint=f" (not read from the label: {missing})" if missing else ""
        )
        _confirm(p, sub)
        with api_errors(sub):
            res = api.post("/v1/spools", body)
    except ThreeDError:
        if as_json:
            print(
                json.dumps(
                    {"proposal": proposal, "spool": None}, indent=2, ensure_ascii=False
                )
            )
        raise
    _out(p, {"proposal": proposal, "spool": res}, "received " + fmt_spool(res))
    return 0


FILAMENT_KEYS = frozenset(
    {"vendor", "material", "name", "color_hex", "diameter", "density"}
)


def _confirm(p: Parsed, sub: str) -> None:
    """`--yes`, or a yes typed at the terminal; anything else writes nothing."""
    if "--yes" in p.flags:
        return
    fix = (
        "Check the proposal above; correct any field with its flag (--vendor, --material, --name, "
        "--color, --net, --lot, --location …) and re-run with --yes."
    )
    if not sys.stdin.isatty():
        raise UsageError(
            "receive --from-photo creates the spool only after confirmation; nothing written",
            command=f"printops {sub}",
            remediation=[fix],
        )
    answer = input("Create this spool in Spoolman? [y/N] ").strip().lower()
    if answer not in ("y", "yes", "д", "да"):
        raise ThreeDError(
            "not confirmed; nothing written",
            command=f"printops {sub}",
            remediation=[fix],
        )


def _vendors(api: ApiClient | None) -> list[str]:
    """Vendor names 3d-api knows (its tare DB); the built-in list alone when it is unreachable."""
    if api is None:
        return []
    try:
        return [t["vendor"] for t in api.get("/v1/tares")["tares"]]
    except ApiError as e:
        why = e.message
    except (KeyError, TypeError) as e:
        why = f"unexpected /v1/tares answer ({e!r})"
    print(f"note: vendor names: built-in list only (3d-api: {why})", file=sys.stderr)
    return []


def read_proposal(
    path: Path, vendors: list[str], sub: str
) -> tuple[Proposal, OcrText | None]:
    """The proposal for one label: a photo is OCR'd; a .txt file is the label text itself."""
    if path.suffix.lower() == ".txt":
        if not path.is_file():
            raise InputNotFound(str(path), command=f"printops {sub}")
        try:
            text, ocr = path.read_text(encoding="utf-8"), None
        except UnicodeDecodeError as e:
            raise UsageError(
                f"{path} is not UTF-8 text ({e.reason})",
                command=f"printops {sub}",
                remediation=["Save the label text as UTF-8, or pass the photo itself."],
            ) from e
    else:
        from printops.ocr import read_label

        ocr = read_label(path, command=f"printops {sub}")
        text = ocr.text
    return parse_label(text, vendors, source=path.name), ocr


def _found(f: Found | None, show: str) -> tuple[str, str]:
    return (show, f'"{f.line}"') if f is not None else ("?", "")


def fmt_proposal(prop: Proposal, ocr: OcrText | None, body: dict[str, Any]) -> str:
    """One label: each field with the label line it came from, then the receive command."""
    how = f"OCR, turned {ocr.rotation}°, score {ocr.score:g}" if ocr else "label text"
    c = prop.colour.value if prop.colour else None
    rows = [
        ("vendor", *_found(prop.vendor, str(prop.vendor and prop.vendor.value))),
        (
            "material",
            *_found(prop.material, str(prop.material and prop.material.value)),
        ),
        (
            "colour",
            *_found(prop.colour, f"{c['name']} {c['hex']} ({c['ru']})" if c else ""),
        ),
        ("name", prop.name or "?", ""),
        ("net", *_found(prop.net_g, f"{prop.net_g.value:g} g" if prop.net_g else "")),
        (
            "diameter",
            *_found(
                prop.diameter, f"{prop.diameter.value:g} mm" if prop.diameter else ""
            ),
        ),
        ("nozzle", *_found(prop.nozzle_c, _deg(prop.nozzle_c))),
        ("bed", *_found(prop.bed_c, _deg(prop.bed_c))),
        ("lot", *_found(prop.lot, str(prop.lot and prop.lot.value))),
    ]
    width = max(len(v) for _, v, _ in rows)
    lines = [f"{prop.source}  ({how})"]
    lines += [f"  {k:<9} {v:<{width}}  {src}".rstrip() for k, v, src in rows]
    if prop.missing:
        lines.append(
            f"  MISSING   {', '.join(prop.missing)} — not read from the label; pass the flag"
        )
    lines.append(f"  → {receive_command(body)}")
    return "\n".join(lines)


def _deg(f: Found | None) -> str:
    if f is None:
        return ""
    lo, hi = f.value
    return f"{lo} °C" if lo == hi else f"{lo}–{hi} °C"


def _proposal_json(
    prop: Proposal, ocr: OcrText | None, body: dict[str, Any]
) -> dict[str, Any]:
    d = prop.to_dict()
    d["ocr"] = {"rotation": ocr.rotation, "score": ocr.score} if ocr else None
    d["receive"] = body
    d["receive_command"] = receive_command(body)
    return d


def fmt_scan_table(rows: list[tuple[Proposal, dict[str, Any]]]) -> str:
    """The batch at a glance: one line per label."""
    head = ("#", "photo", "vendor", "material", "colour", "net", "lot", "missing")
    table = [head] + [
        (
            str(i),
            prop.source,
            body.get("vendor") or "?",
            body.get("material") or "?",
            f"{prop.colour.value['name']} {body['color_hex']}" if prop.colour else "?",
            f"{body['net_g']:g} g" if "net_g" in body else "?",
            body.get("lot_nr") or "-",
            ", ".join(prop.missing) or "-",
        )
        for i, (prop, body) in enumerate(rows, 1)
    ]
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip()
        for r in table
    )


def _scan(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES | {"--location"},
        flags=COMMON_FLAGS,
        n_pos=(1, 1000),
    )
    try:
        api: ApiClient | None = client(p, sub)
    except UsageError as e:
        print(f"note: vendor names: built-in list only ({e})", file=sys.stderr)
        api = None
    vendors = _vendors(api)
    extra = {"location": p.opts["--location"]} if "--location" in p.opts else {}
    done = []
    failed: list[dict[str, str]] = []
    for raw in p.pos:
        path = Path(raw).expanduser()
        try:
            prop, ocr = read_proposal(path, vendors, sub)
        except MissingDependency:
            raise  # no photo can be read: stop at once with the install line
        except (
            ThreeDError
        ) as e:  # one unreadable photo does not cost the rest of the batch
            failed.append({"source": clean(path.name), "error": e.message})
            continue
        done.append((prop, ocr, {**prop.receive_body(), **extra}))
    if "--json" in p.flags:
        print(
            json.dumps(
                {"proposals": [_proposal_json(*d) for d in done], "failed": failed},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1 if failed else 0
    print("\n\n".join(fmt_proposal(*d) for d in done))
    if len(done) > 1:
        print()
        print(fmt_scan_table([(prop, body) for prop, _, body in done]))
    for f in failed:
        print(f"\nFAILED {f['source']}: {f['error']}")
    print("\nNothing written: run the receive line(s) above, corrected where needed.")
    return 1 if failed else 0


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
    api = client(p, sub)
    with api_errors(sub):
        vendors = {t["vendor"] for t in api.get("/v1/tares")["tares"]}
    try:
        items = plates_owned(
            path,
            project=p.opts.get("--project"),
            location=p.opts.get("--location"),
            vendors=vendors,
        )
    except ImportFormatError as e:
        raise UsageError(str(e), command=f"printops {sub}") from e
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
    "scan": _scan,
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
    summary="spool lifecycle: receive/scan/ls/show/move/load/unload/use/measure/empty/import-owned",
    usage=USAGE,
    run=run,
)
