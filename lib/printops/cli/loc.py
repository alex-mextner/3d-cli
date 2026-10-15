"""`3d printops loc` — the PrintOps location tree (warehouse cells, dry boxes, dryers, printer slots).

Talks to 3d-api (`/v1/locations…`, routes_locations.py); labels are rendered here, locally,
by printops.labels (stdlib PDF/SVG + QR), then their payload is recorded on each location so
`loc show` can tell when a printed label went stale after a move.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from errors import ThreeDError, UsageError
from printops.cli import Sub
from printops.cli._common import Parsed, api_errors, client, parse

ACTIONS = ("tree", "add", "show", "set", "mv", "archive", "unarchive", "label", "resolve", "sync-fleet", "resync")

USAGE = """3d printops loc tree [ROOT] [--archived] [--json]
3d printops loc add PATH --kind KIND [--capacity N] [--humidity-sensor ENTITY] [--tag-uid UID] [--note TEXT]
3d printops loc show REF [--recursive] [--json]
3d printops loc set REF [--kind KIND] [--capacity N] [--humidity-sensor ENTITY] [--tag-uid UID] [--note TEXT]
3d printops loc mv REF NEW_PATH [--json]
3d printops loc archive REF
3d printops loc unarchive REF
3d printops loc label REF... [--subtree] [-o FILE] [--format pdf|svg] [--grid COLSxROWS] [--page a4|letter]
                            [--margin MM] [--start N] [--no-border] [--ntag]
3d printops loc resolve TEXT
3d printops loc sync-fleet
3d printops loc resync
  Where spools live. A location is a path SCHEME:NAME[/NAME…] of at most 64 chars (Spoolman's
  `location` limit), e.g. WH:A/S2/B3/04, dryer:1, drybox:1; its parent must exist first.
  Every spool stored in it carries the path in Spoolman `location` and the id in the extra field
  `printops_location_id`; moving a location rewrites both for every spool inside.
  Printer slots slot:<printer>/<head> (and slot:<printer>/<ams>/<n>) come from
  fleet/printers.yaml and follow it automatically. REF = path or numeric id.
  Kinds: warehouse zone shelf box slot dryer dry_box (printer, ams, printer_slot: fleet only).
  Reads need a token with `read`; changes need `write`.

Actions:
  tree        the tree (or the subtree under ROOT) with kind, capacity, sensor, label state
  add         create a location under an existing parent
  show        one location: ancestors, children and the spools stored in it
  set         change kind / capacity / humidity sensor / NFC tag UID / note ("" clears)
  mv          rename or re-parent a location with its subtree; re-syncs Spoolman.location
  archive     archive an empty location (refused while it holds spools or active children)
  unarchive   revive an archived location (its parent must be active)
  label       printable label sheet: QR (cell:<path>; dryer:/slot: paths as they are) + text;
              records the printed payload on each location
  resolve     which location a scanned QR/NFC text (cell:…, loc-<id>), path, id or tag UID names
  sync-fleet  re-materialise printer slots from fleet/printers.yaml now
  resync      rewrite Spoolman.location of spools whose string drifted from their location's path

Options:
  --kind KIND             location kind (add: required)
  --capacity N            how many spools it holds (unset = unlimited)
  --humidity-sensor E     Home Assistant entity of its RH sensor, e.g. sensor.drybox_1_humidity
  --tag-uid UID           UID of the NTAG213 stuck on it (hex, ':' allowed)
  --note TEXT             free text
  --archived              tree: include archived locations
  --recursive             show: also list spools in the whole subtree
  --subtree               label: every active location under each REF too
  -o FILE                 label: output file (default location-labels.pdf / .svg; SVG pages: FILE, FILE-2.svg, …)
  --format pdf|svg        label: sheet format (default: from -o, else pdf)
  --grid COLSxROWS        label: labels per sheet (default 3x8 = 70×37 mm on A4)
  --page a4|letter        label: paper size (default a4)
  --margin MM             label: page margin around the grid (default 0)
  --start N               label: skip the first N cells of the first sheet (a partly used sheet)
  --no-border             label: no grey cut guides
  --ntag                  label: also print each NTAG213 NDEF image (hex) for writing the cell's tag
  --json                  machine-readable output
  --api URL / --token T   3d-api URL and token (default: $PRINTOPS_API / $PRINTOPS_TOKEN or the config file)

Examples:
  3d printops loc add WH:default --kind warehouse
  3d printops loc add WH:A/S2 --kind shelf
  3d printops loc add drybox:1 --kind dry_box --capacity 4 --humidity-sensor sensor.drybox_1_humidity
  3d printops loc tree
  3d printops loc show slot:u1/T3
  3d printops loc mv WH:A/S2 WH:B/S1
  3d printops loc label WH:A --subtree -o shelf-a.pdf
  3d printops loc label dryer:1 slot:u1/T1 --format svg -o labels.svg --start 5
  3d printops loc resolve cell:WH:A/S2
  3d printops loc archive WH:A/S2"""

_COMMON = {"--api", "--token"}
_ATTRS = {"--kind": "kind", "--capacity": "capacity", "--humidity-sensor": "humidity_sensor_entity",
          "--tag-uid": "tag_uid", "--note": "note"}


def _ref(ref: str) -> str:
    return urllib.parse.quote(ref.strip(), safe=":/")


def _attrs(p: Parsed, sub: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for opt, key in _ATTRS.items():
        if opt in p.opts:
            empty = p.opts[opt] == ""  # "" clears the attribute
            out[key] = None if empty else p.int_opt(opt, sub) if key == "capacity" else p.opts[opt]
    return out


def _flags(loc: dict[str, Any]) -> str:
    bits = [loc["kind"]]
    if loc.get("capacity"):
        bits.append(f"cap {loc['capacity']}")
    if loc.get("humidity_sensor_entity"):
        bits.append(f"RH {loc['humidity_sensor_entity']}")
    if loc.get("tag_uid"):
        bits.append(f"tag {loc['tag_uid']}")
    if loc.get("label_stale"):
        bits.append("label STALE")
    if loc.get("archived"):
        bits.append("ARCHIVED")
    if loc.get("note"):
        bits.append(str(loc["note"]))
    return ", ".join(bits)


def _print_tree(nodes: list[dict[str, Any]], depth: int = 0) -> None:
    for n in nodes:
        name = n["path"] if depth == 0 else n["path"].rsplit("/", 1)[-1]
        print(f"{'  ' * depth}{name}  ({_flags(n)})  #{n['id']}")
        _print_tree(n["children"], depth + 1)


def _find(nodes: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    for n in nodes:
        if n["path"].lower() == path.lower():
            return n
        hit = _find(n["children"], path)
        if hit is not None:
            return hit
    return None


def spool_line(s: dict[str, Any]) -> str:
    fil = s.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    name = " ".join(x for x in (vendor, fil.get("name")) if x) or f"filament #{fil.get('id')}"
    colour = f", #{fil['color_hex']}" if fil.get("color_hex") else ""
    rem = s.get("remaining_weight")
    grams = f"{rem:.0f} g" if isinstance(rem, (int, float)) else "? g"
    where = f"  @ {s['location']}" if s.get("location") else ""
    return f"#{s['id']}  {name} ({fil.get('material', '?')}{colour})  {grams}{where}"


def _label_out(p: Parsed) -> tuple[str, Path]:
    fmt = p.opts.get("--format")
    out = p.opts.get("-o")
    if fmt is None:
        fmt = "svg" if out and out.lower().endswith(".svg") else "pdf"
    if fmt not in ("pdf", "svg"):
        raise UsageError(f"--format {fmt!r}: pdf or svg", command="printops loc label")
    return fmt, Path(out or f"location-labels.{fmt}").expanduser()


def _sheet(p: Parsed) -> Any:
    from printops.labels import Sheet

    grid = p.opts.get("--grid", "3x8")
    try:
        cols, rows = (int(x) for x in grid.lower().split("x"))
        margin = float(p.opts.get("--margin", "0"))
    except ValueError as e:
        raise UsageError(f"--grid needs COLSxROWS (e.g. 3x8) and --margin a number; got {grid!r}",
                         command="printops loc label") from e
    sheet = Sheet(page=p.opts.get("--page", "a4"), cols=cols, rows=rows, margin_mm=margin,
                  start=p.int_opt("--start", "loc label") or 0, border="--no-border" not in p.flags)
    try:
        sheet.validate()
    except ValueError as e:
        raise UsageError(str(e), command="printops loc label") from e
    return sheet


def _label(p: Parsed) -> int:
    from printops.labels import Label, ntag213_ndef, render_pdf, render_svg_pages

    sheet = _sheet(p)
    fmt, out = _label_out(p)
    api = client(p, "loc label")
    with api_errors("loc label"):
        flat = {loc["path"].lower(): loc for loc in api.get("/v1/locations", flat="true")["locations"]}
        chosen: dict[int, dict[str, Any]] = {}
        for ref in p.pos:
            loc = api.get(f"/v1/locations/{_ref(ref)}")
            if loc["archived"]:
                raise UsageError(f"location {loc['path']!r} is archived; unarchive it before labelling",
                                 command="printops loc label")
            chosen[loc["id"]] = loc
            if "--subtree" in p.flags:
                prefix = loc["path"].lower() + "/"
                chosen.update({d["id"]: d for k, d in sorted(flat.items()) if k.startswith(prefix)})
        labels = [Label(loc["id"], loc["path"], loc["kind"], loc["qr_payload"])
                  for loc in sorted(chosen.values(), key=lambda x: x["path"].lower())]
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "pdf":
            out.write_bytes(render_pdf(labels, sheet))
            files = [out]
        else:
            pages = render_svg_pages(labels, sheet)
            files = [out if i == 0 else out.with_name(f"{out.stem}-{i + 1}{out.suffix}") for i in range(len(pages))]
            for f, doc in zip(files, pages, strict=True):
                f.write_text(doc, encoding="utf-8")
        for lb in labels:
            api.patch(f"/v1/locations/{lb.loc_id}", {"qr": lb.payload})
    print(f"wrote {len(labels)} label(s) on {len(files)} page(s): {', '.join(str(f) for f in files)}")
    if "--ntag" in p.flags:
        for lb in labels:
            print(f"ntag213 {lb.path}  {ntag213_ndef(lb).hex()}")
    return 0


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    if action not in ACTIONS:
        raise UsageError(f"unknown loc action {action!r}; accepted: {', '.join(ACTIONS)}",
                         command="printops loc", remediation=["See `3d printops loc --help`."])
    if rest and rest[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub = f"loc {action}"
    if action == "tree":
        p = parse(rest, sub, values=_COMMON, flags={"--archived", "--json"}, n_pos=(0, 1))
        api = client(p, sub)
        with api_errors(sub):
            data = api.get("/v1/locations", archived="true" if "--archived" in p.flags else None)
        nodes = data["tree"]
        if p.pos:
            node = _find(nodes, p.pos[0])
            if node is None:
                raise ThreeDError(f"no location {p.pos[0]!r} in the tree", command="printops loc tree")
            nodes = [node]
        if "--json" in p.flags:
            print(json.dumps(nodes, ensure_ascii=False, indent=2))
        elif not nodes:
            print("no locations yet — add one: 3d printops loc add WH:default --kind warehouse")
        else:
            _print_tree(nodes)
        return 0
    if action in ("add", "set"):
        p = parse(rest, sub, values=_COMMON | set(_ATTRS), n_pos=1)
        body = _attrs(p, sub)
        api = client(p, sub)
        with api_errors(sub):
            if action == "add":
                if not body.get("kind"):
                    raise UsageError("add needs --kind (e.g. --kind shelf)", command="printops loc add")
                loc = api.post("/v1/locations", {"path": p.pos[0], **body})
            else:
                if not body:
                    raise UsageError("set needs at least one of " + ", ".join(_ATTRS), command="printops loc set")
                loc = api.patch(f"/v1/locations/{_ref(p.pos[0])}", body)["location"]
        print(f"{'added' if action == 'add' else 'updated'} {loc['path']}  ({_flags(loc)})  #{loc['id']}")
        return 0
    if action == "show":
        p = parse(rest, sub, values=_COMMON, flags={"--json", "--recursive"}, n_pos=1)
        api = client(p, sub)
        with api_errors(sub):
            loc = api.get(f"/v1/locations/{_ref(p.pos[0])}")
            inside = api.get(f"/v1/locations/{_ref(loc['path'])}/contents",
                             recursive="true" if "--recursive" in p.flags else None)
        if "--json" in p.flags:
            print(json.dumps({**loc, "spools": inside["spools"]}, ensure_ascii=False, indent=2))
            return 0
        print(f"{loc['path']}  #{loc['id']}  ({_flags(loc)})")
        if loc["ancestors"]:
            print("  in       " + " < ".join(a["path"] for a in loc["ancestors"]))
        print(f"  label    {loc['qr_payload']}" + (f"  (printed: {loc['qr']} — STALE, reprint with "
                                                     f"`3d printops loc label {loc['path']}`)" if loc["label_stale"] else
                                                     "" if loc["qr"] else "  (not printed yet)"))
        if loc.get("humidity_sensor"):
            print(f"  RH       {loc['humidity_sensor']}")
        for c in loc["children"]:
            print(f"  child    {c['path']}  ({_flags(c)})")
        spools = inside["spools"]
        cap = f" / {loc['capacity']}" if loc.get("capacity") else ""
        print(f"  spools   {len(spools)}{cap}")
        for s in spools:
            print(f"    {spool_line(s)}")
        return 0
    if action == "mv":
        p = parse(rest, sub, values=_COMMON, flags={"--json"}, n_pos=2)
        api = client(p, sub)
        with api_errors(sub):
            res = api.patch(f"/v1/locations/{_ref(p.pos[0])}", {"path": p.pos[1]})
        if "--json" in p.flags:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            for old, new in res["renamed"]:
                print(f"{old} -> {new}")
            sm = res["spoolman"]
            print(f"Spoolman: {len(sm['updated'])} spool(s) re-synced")
        for f in res["spoolman"]["failed"]:
            what = f"spool #{f['spool_id']}" if f["spool_id"] is not None else "listing spools"
            print(f"Spoolman re-sync FAILED ({what}): {f['error']} — the move is saved; "
                  "run `3d printops loc resync` when Spoolman is back", file=sys.stderr)
        return 1 if res["spoolman"]["failed"] else 0
    if action in ("archive", "unarchive"):
        p = parse(rest, sub, values=_COMMON, n_pos=1)
        api = client(p, sub)
        with api_errors(sub):
            if action == "archive":
                loc = api.delete(f"/v1/locations/{_ref(p.pos[0])}")
            else:
                loc = api.patch(f"/v1/locations/{_ref(p.pos[0])}", {"archived": False})["location"]
        print(f"{action}d {loc['path']}  #{loc['id']}")
        return 0
    if action == "label":
        p = parse(rest, sub, values=_COMMON | {"-o", "--format", "--grid", "--page", "--margin", "--start"},
                  flags={"--subtree", "--no-border", "--ntag"}, n_pos=(1, 500))
        return _label(p)
    if action == "resolve":
        p = parse(rest, sub, values=_COMMON, flags={"--json"}, n_pos=1)
        api = client(p, sub)
        with api_errors(sub):
            loc = api.get("/v1/locations/resolve", q=p.pos[0])
        print(json.dumps(loc, ensure_ascii=False, indent=2) if "--json" in p.flags
              else f"{loc['path']}  #{loc['id']}  ({_flags(loc)})")
        return 0
    p = parse(rest, sub, values=_COMMON, flags={"--json"})
    api = client(p, sub)
    with api_errors(sub):
        res = api.post("/v1/locations/sync-fleet" if action == "sync-fleet" else "/v1/locations/resync")
    if "--json" in p.flags:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if action == "sync-fleet":
        for key in ("created", "revived", "updated", "archived"):
            if res[key]:
                print(f"{key:<9} {' '.join(res[key])}")
        for k in res["kept"]:
            print(f"kept      {k['path']}: {k['reason']}")
        if not any(res[k] for k in ("created", "revived", "updated", "archived", "kept")):
            print("printer slots already match fleet/printers.yaml")
        return 0
    print(f"Spoolman: {len(res['updated'])} spool(s) re-synced" + "".join(f"\n  #{i}" for i in res["updated"]))
    for f in res["failed"]:
        what = f"spool #{f['spool_id']}" if f["spool_id"] is not None else "listing spools"
        print(f"FAILED ({what}): {f['error']}", file=sys.stderr)
    return 1 if res["failed"] else 0


SUB = Sub(name="loc", summary="location tree: add/tree/show/mv/archive + QR label sheets", usage=USAGE, run=run)
