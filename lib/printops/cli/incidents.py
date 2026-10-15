"""`3d printops incidents` — report, triage and analyse print incidents through 3d-api (#165)."""

from __future__ import annotations

import json
import sys
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import Parsed, api_errors, client, parse
from printops.client import ApiClient

USAGE = """3d printops incidents report KIND --severity S [--printer P] [--head T3] [--spool ID] [--at ISO]
                              [--note TEXT] [--tag T]... [--photo URL]... [--set FIELD=VALUE]... [--draft] [--no-context]
3d printops incidents ls [--status S[,S]] [--printer P] [--head H] [--kind K] [--spool ID] [--lot LOT] [--since 90d] [--limit N]
3d printops incidents show ID
3d printops incidents edit ID [--kind K] [--severity S] [--note TEXT] [--tag T]... [--untag T]... [--photo URL]... [--set FIELD=VALUE]...
3d printops incidents confirm ID [--kind K] [--severity S] [--note TEXT] [--set FIELD=VALUE]...
3d printops incidents resolve ID RESOLUTION [--text TEXT] [--calibration ID] [--at ISO]
3d printops incidents dismiss ID [--reason TEXT]
3d printops incidents stats --by vendor|filament|lot|printer|head [--since 90d] [--kind K]
3d printops incidents context PRINTER [--head T3] [--spool ID]
3d printops incidents from-watch            (reads one `3d printer watch` event JSON from stdin)
3d printops incidents exposure ls [--printer P] [--since 90d] [--limit N]
3d printops incidents exposure add PRINTER --hours H [--grams G] [--head T1] [--spool ID] [--at ISO] [--note TEXT]
3d printops incidents exposure sync PRINTER [--limit N]
  common: [--json] [--api URL] [--token TOKEN]

  Structured incidents per filament, vendor, lot, printer and head (plan §6): what went wrong
  (KIND), how bad (severity), and — filled by 3d-api automatically — what was printing: the job,
  layer and Z from the printer (read-only), temperatures / speed / volumetric flow / fan / slicer
  feature from the job's G-code at the printer's file position, the spool, filament, vendor and lot
  bound to the head, days since the spool was dried and the dry-box humidity. Anything you pass
  with --set wins over the auto-filled value.

  `stats` turns counts into RATES: incidents per kg printed and per print-hour of the same group
  in the same window (exposure = finished jobs from Moonraker history via `exposure sync`, or
  manual `exposure add` rows for offline printers), next to the parent group's rate (a lot vs
  its vendor, a head vs its printer). Only confirmed incidents (open, resolved) count.

  `3d printer watch` can draft incidents automatically: --exec '3d printops incidents from-watch'.
  Drafts wait for `confirm` (correct kind/severity) or `dismiss`.

  `ls`, `show`, `stats`, `context` and `exposure ls` need the `read` scope; the rest `write`.

KIND: clog stringing oozing_blobs warping delamination under_extrusion over_extrusion
  moisture_popping brittleness poor_bed_adhesion layer_shift colour_off runout tangle heat_creep
  nozzle_drag other  (aliases: layer_split, snap, oozing, blobs, jam, color_off)
SEVERITY: cosmetic part_lost print_lost hardware_risk
RESOLUTION: dried temp_change speed_change retraction_change cleaned_nozzle replaced_nozzle
  glue_plate recalibrated discarded_spool none
FIELD (--set): printer head spool_id filament_id vendor_id vendor material filament_name lot_nr
  job_id job_file layer z_mm feature nozzle_temp bed_temp chamber_temp speed_mm_s volumetric_mm3_s
  fan_pct days_since_dried dry_box_rh room_rh

Options:
  --severity S        how bad (required for report)
  --printer P         fleet printer (fleet/printers.yaml); with it 3d-api auto-fills the context
  --head H            head label (T3) or AMS slot (ams1/2); default: the printer's only head, else
                      the G-code's active tool
  --spool ID          the Spoolman spool, when not the one bound to the head
  --at ISO            when it happened (default now)
  --tag T / --untag T add / remove a tag (repeatable)
  --photo URL         a photo link (repeatable)
  --set FIELD=VALUE   set a snapshot field explicitly (repeatable; numbers for numeric fields)
  --draft             store as a draft to confirm later
  --no-context        do not auto-fill anything (no printer/Spoolman/HA reads)
  --status S[,S]      draft, open, resolved, dismissed (ls default: all)
  --since SPAN        90d, 12h, 2w, 6m, 1y or an ISO date (stats default 90d)
  --by GROUP          stats grouping: vendor, filament, lot, printer, head
  --text TEXT         what was done (resolve)
  --calibration ID    the calibration record the fix was (resolve)
  --hours H --grams G exposure add: print time and filament used
  --json              print the API's JSON
  --api URL           3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN       bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops incidents report clog --severity print_lost --printer u1 --head T3
  3d printops incidents report stringing --severity cosmetic --printer k1se --set room_rh=62 --tag petg
  3d printops incidents ls --status draft
  3d printops incidents confirm 12 --kind tangle
  3d printops incidents resolve 12 dried --text "PETG 65 °C 6 h, stringing gone"
  3d printops incidents stats --by lot --since 90d
  3d printops incidents exposure sync u1
  3d printer watch u1 --exec '3d printops incidents from-watch'"""

COMMON_VALUES = {"--api", "--token"}
COMMON_FLAGS = {"--json"}
# Calls that read a printer and possibly scan a 130 MB G-code server-side (incident_context
# DEADLINE_S = 120 s) or page through job history: the client's 10 s default would give up
# while 3d-api still completes the write.
SLOW_TIMEOUT_S = 180.0


def _slow(p: Parsed, sub: str) -> ApiClient:
    api = client(p, sub)
    api.timeout = SLOW_TIMEOUT_S
    return api


REPEATED = {"--tag", "--untag", "--photo", "--set"}
NUMERIC = {
    "spool_id", "filament_id", "vendor_id", "layer", "z_mm", "nozzle_temp", "bed_temp", "chamber_temp",
    "speed_mm_s", "volumetric_mm3_s", "fan_pct", "days_since_dried", "dry_box_rh", "room_rh",
}
INTEGER = {"spool_id", "filament_id", "vendor_id", "layer"}


def _split_repeated(argv: list[str], sub: str) -> tuple[list[str], dict[str, list[str]]]:
    """Pull repeatable `--opt VALUE` pairs out of argv (the shared parser keeps the last only)."""
    rest: list[str] = []
    found: dict[str, list[str]] = {k: [] for k in REPEATED}
    i = 0
    while i < len(argv):
        if argv[i] in REPEATED:
            if i + 1 >= len(argv):
                raise UsageError(f"{argv[i]} needs a value", command=f"printops {sub}")
            found[argv[i]].append(argv[i + 1])
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return rest, found


def _sets(values: list[str], sub: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in values:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise UsageError(f"--set wants FIELD=VALUE, got {item!r}", command=f"printops {sub}")
        if key in NUMERIC:
            try:
                out[key] = int(raw) if key in INTEGER else float(raw)
            except ValueError as e:
                raise UsageError(f"--set {key} needs a number, got {raw!r}", command=f"printops {sub}") from e
        else:
            out[key] = raw
    return out


def _int(raw: str, what: str, sub: str) -> int:
    try:
        return int(raw)
    except ValueError as e:
        raise UsageError(f"{what} must be an integer, got {raw!r}", command=f"printops {sub}") from e


def _float(p: Parsed, name: str, sub: str) -> float | None:
    raw = p.opts.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as e:
        raise UsageError(f"{name} needs a number, got {raw!r}", command=f"printops {sub}") from e


def _print(as_json: bool, data: Any, human: list[str]) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for line in human:
            print(line)


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "?"
    if isinstance(v, float):
        return f"{v:g}{unit}"
    return f"{v}{unit}"


def incident_lines(inc: dict[str, Any], *, full: bool = False) -> list[str]:
    where = "/".join(x for x in (inc.get("printer"), inc.get("head")) if x) or "-"
    spool = ""
    if inc.get("spool_id") is not None:
        fil = " ".join(str(x) for x in (inc.get("vendor"), inc.get("material"), inc.get("filament_name")) if x)
        lot = f" lot {inc['lot_nr']}" if inc.get("lot_nr") else ""
        spool = f"  spool #{inc['spool_id']} {fil}{lot}"
    head = (f"#{inc['id']} [{inc['status']}] {inc['at'][:16].replace('T', ' ')}  {inc['kind']} ({inc['severity']})  "
            f"{where}{spool}")
    lines = [head]
    bits = []
    if inc.get("job_file"):
        bits.append(f"job {inc['job_file']}" + (f" ({inc['job_id']})" if inc.get("job_id") else ""))
    if inc.get("layer") is not None:
        bits.append(f"layer {inc['layer']}" + (f" z {inc['z_mm']:g}" if inc.get("z_mm") is not None else ""))
    if inc.get("feature"):
        bits.append(inc["feature"])
    for label, key, unit in (("nozzle", "nozzle_temp", " °C"), ("bed", "bed_temp", " °C"),
                             ("chamber", "chamber_temp", " °C"), ("speed", "speed_mm_s", " mm/s"),
                             ("flow", "volumetric_mm3_s", " mm³/s"), ("fan", "fan_pct", " %"),
                             ("dried", "days_since_dried", " d ago"), ("dry-box RH", "dry_box_rh", " %"),
                             ("room RH", "room_rh", " %")):
        if inc.get(key) is not None:
            bits.append(f"{label} {_fmt(inc[key], unit)}")
    if bits:
        lines.append("    " + ", ".join(bits))
    if inc.get("status") == "resolved":
        lines.append(f"    resolved {inc['resolved_at'][:16].replace('T', ' ')}: {inc['resolution_kind']}"
                     + (f" — {inc['resolution_text']}" if inc.get("resolution_text") else ""))
    if full:
        meta = [f"reported by {inc['reported_by']} ({inc['actor']}), confidence {inc['confidence']:g}"]
        if inc.get("tags"):
            meta.append("tags " + ", ".join(inc["tags"]))
        lines.append("    " + "; ".join(meta))
        if inc.get("note"):
            lines += [f"    note: {ln}" for ln in str(inc["note"]).splitlines()]
        for photo in inc.get("photos") or []:
            lines.append(f"    photo: {photo}")
        ctx = inc.get("context") or {}
        gcode = ctx.get("gcode") or {}
        if gcode.get("found"):
            lines.append(f"    G-code: {gcode.get('how') or gcode.get('path')}"
                         + (f", line {gcode['line']}" if gcode.get("line") else ""))
        elif gcode:
            lines.append(f"    G-code: not available ({gcode.get('why')})")
        for key, src in sorted((ctx.get("sources") or {}).items()):
            lines.append(f"    {key:<17} ← {src}")
        lines += [f"    warning: {w}" for w in ctx.get("warnings") or []]
        lines += [f"    evidence: {e.get('text')}" for e in ctx.get("evidence") or []]
    return lines


def _rate(v: Any, unit: str, digits: int) -> str:
    return f"{'-':>8}/{unit}" if v is None else f"{v:8.{digits}f}/{unit}"


def _per_kg(v: Any) -> str:
    return _rate(v, "kg", 3)


def _per_hour(v: Any) -> str:
    return _rate(v, "h", 4)


def stats_lines(out: dict[str, Any]) -> list[str]:
    since = f" since {out['since'][:10]}" if out.get("since") else ""
    kind = f", kind {out['kind']}" if out.get("kind") else ""
    o = out["overall"]
    lines = [f"incidents by {out['by']}{since}{kind}: {o['incidents']} incident(s), "
             f"{_fmt(o['kg'], ' kg')} / {o['hours']:g} h printed; overall {_per_kg(o['per_kg']).strip()}  "
             f"{_per_hour(o['per_hour']).strip()}"]
    for g in out["groups"]:
        kinds = ", ".join(f"{k} {n}" for k, n in g["kinds"].items())
        low = "  (little exposure)" if g["low_exposure"] and g["incidents"] else ""
        line = (f"  {_per_kg(g['per_kg'])}  {_per_hour(g['per_hour'])}  {g['incidents']:3d} in "
                f"{_fmt(g['kg'], ' kg'):>9} / {g['hours']:6.1f} h  {g['label']}{low}")
        if kinds:
            line += f"  [{kinds}]"
        parent = g.get("parent")
        if parent and parent.get("per_kg") is not None:
            line += f"  vs {parent['label']} {parent['per_kg']:.3f}/kg"
        lines.append(line)
    lines += [f"  note: {n}" for n in out["notes"]]
    return lines


def context_lines(ctx: dict[str, Any]) -> list[str]:
    lines = [f"printer state: {ctx.get('printer_state') or 'not read'}"]
    for key, value in ctx["fields"].items():
        lines.append(f"  {key:<17} {value!s:<28} ← {ctx['sources'].get(key, '')}")
    lines += [f"  warning: {w}" for w in ctx["warnings"]]
    return lines


def _exposure_line(r: dict[str, Any]) -> str:
    who = "/".join(x for x in (r["printer"], r.get("head")) if x)
    spool = f" spool #{r['spool_id']}" + (f" lot {r['lot_nr']}" if r.get("lot_nr") else "") if r.get("spool_id") else ""
    job = f" job {r['job_id']}" if r.get("job_id") else ""
    return (f"{r['started_at'][:16].replace('T', ' ')}  {who:<10} {r['hours']:6.2f} h  {_fmt(r.get('grams'), ' g'):>9}"
            f"  [{r['source']}]{job}{spool}")


def _report(rest: list[str], sub: str) -> int:
    rest, rep = _split_repeated(rest, sub)
    p = parse(rest, sub, values={*COMMON_VALUES, "--severity", "--printer", "--head", "--spool", "--at", "--note"},
              flags={*COMMON_FLAGS, "--draft", "--no-context"}, n_pos=1)
    if "--severity" not in p.opts:
        raise UsageError("--severity is required (cosmetic, part_lost, print_lost, hardware_risk)",
                         command=f"printops {sub}")
    body: dict[str, Any] = {"kind": p.pos[0], "severity": p.opts["--severity"], "reported_by": "cli"}
    for opt, key in (("--printer", "printer"), ("--head", "head"), ("--at", "at"), ("--note", "note")):
        if opt in p.opts:
            body[key] = p.opts[opt]
    if "--spool" in p.opts:
        body["spool_id"] = _int(p.opts["--spool"], "--spool", sub)
    if rep["--tag"]:
        body["tags"] = rep["--tag"]
    if rep["--photo"]:
        body["photos"] = rep["--photo"]
    body.update(_sets(rep["--set"], sub))
    if "--draft" in p.flags:
        body["draft"] = True
    if "--no-context" in p.flags:
        body["auto_context"] = False
    with api_errors(sub):
        out = _slow(p, sub).post("/v1/incidents", body)
    ctx = out["context"]
    _print("--json" in p.flags, out, [
        "recorded " + incident_lines(out["incident"])[0], *incident_lines(out["incident"])[1:],
        *(f"  warning: {w}" for w in ctx.get("warnings") or []),
    ])
    return 0


def _changes(rest: list[str], sub: str, extra_values: set[str]) -> tuple[Parsed, dict[str, Any]]:
    rest, rep = _split_repeated(rest, sub)
    p = parse(rest, sub, values={*COMMON_VALUES, "--kind", "--severity", "--note", *extra_values},
              flags=COMMON_FLAGS, n_pos=1)
    body: dict[str, Any] = {}
    for opt, key in (("--kind", "kind"), ("--severity", "severity"), ("--note", "note")):
        if opt in p.opts:
            body[key] = p.opts[opt]
    if rep["--tag"]:
        body["add_tags"] = rep["--tag"]
    if rep["--untag"]:
        body["remove_tags"] = rep["--untag"]
    if rep["--photo"]:
        body["add_photos"] = rep["--photo"]
    body.update(_sets(rep["--set"], sub))
    return p, body


def _exposure(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    verb, rest = argv[0], argv[1:]
    sub = f"incidents exposure {verb}"
    if verb == "ls":
        p = parse(rest, sub, values={*COMMON_VALUES, "--printer", "--since", "--limit"}, flags=COMMON_FLAGS)
        with api_errors(sub):
            out = client(p, sub).get("/v1/exposure", printer=p.opts.get("--printer"), since=p.opts.get("--since"),
                                     limit=p.int_opt("--limit", sub))
        t = out["totals"]
        _print("--json" in p.flags, out, [
            *(_exposure_line(r) for r in out["exposure"]),
            f"total: {t['rows']} row(s), {t['hours']:g} h, {_fmt(t['kg'], ' kg')}"
            + (f" ({t['first'][:10]} … {t['last'][:10]})" if t.get("first") else ""),
        ])
        return 0
    if verb == "add":
        p = parse(rest, sub, values={*COMMON_VALUES, "--hours", "--grams", "--head", "--spool", "--at", "--note"},
                  flags=COMMON_FLAGS, n_pos=1)
        hours = _float(p, "--hours", sub)
        if hours is None:
            raise UsageError("--hours is required (print time; 0 when only grams are known)", command=f"printops {sub}")
        body: dict[str, Any] = {"printer": p.pos[0], "hours": hours}
        if (grams := _float(p, "--grams", sub)) is not None:
            body["grams"] = grams
        if "--head" in p.opts:
            body["head"] = p.opts["--head"]
        if "--spool" in p.opts:
            body["spool_id"] = _int(p.opts["--spool"], "--spool", sub)
        if "--at" in p.opts:
            body["started_at"] = p.opts["--at"]
        if "--note" in p.opts:
            body["note"] = p.opts["--note"]
        with api_errors(sub):
            out = client(p, sub).post("/v1/exposure", body)
        _print("--json" in p.flags, out, ["recorded " + _exposure_line(out)])
        return 0
    if verb == "sync":
        p = parse(rest, sub, values={*COMMON_VALUES, "--limit"}, flags=COMMON_FLAGS, n_pos=1)
        body = {"printer": p.pos[0]}
        if (limit := p.int_opt("--limit", sub)) is not None:
            body["limit"] = limit
        api = _slow(p, sub)
        # 3d-api pages the history 50 jobs at a time, 5 s apart, each GET up to 10 s
        # (printops.exposure PAGE / PACE_S): wait for every page, not just the first few
        pages = -(-(limit or 50) // 50)
        api.timeout = max(SLOW_TIMEOUT_S, 30.0 + 15.0 * pages)
        with api_errors(sub):
            out = api.post("/v1/exposure/sync", body)
        lines = [f"{out['printer']}: {out['jobs_seen']} job(s) read, {len(out['added'])} exposure row(s) added, "
                 f"{out['already_synced']} already synced, {out['in_progress']} still printing"]
        for r in out["added"]:
            spool = f" spool #{r['spool_id']}" if r.get("spool_id") else " spool unknown"
            lines.append(f"  job {r['job_id']} {r.get('head') or '-'}: {r['hours']:.2f} h, {_fmt(r.get('grams'), ' g')}{spool}")
        lines += [f"  warning: {w}" for w in out["warnings"]]
        _print("--json" in p.flags, out, lines)
        return 0
    raise UsageError(f"unknown `printops incidents exposure` subcommand {verb!r}", command="printops incidents",
                     remediation=["One of: ls, add, sync. See `3d printops incidents --help`."])


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    verb, rest = argv[0], argv[1:]
    sub = f"incidents {verb}"
    if verb == "report":
        return _report(rest, sub)
    if verb == "ls":
        p = parse(rest, sub, values={*COMMON_VALUES, "--status", "--printer", "--head", "--kind", "--spool", "--lot",
                                     "--since", "--limit"}, flags=COMMON_FLAGS)
        with api_errors(sub):
            out = client(p, sub).get(
                "/v1/incidents", status=p.opts.get("--status"), printer=p.opts.get("--printer"),
                head=p.opts.get("--head"), kind=p.opts.get("--kind"), spool=p.int_opt("--spool", sub),
                lot=p.opts.get("--lot"), since=p.opts.get("--since"), limit=p.int_opt("--limit", sub))
        rows = out["incidents"]
        _print("--json" in p.flags, out, [ln for inc in rows for ln in incident_lines(inc)] or ["no incidents"])
        return 0
    if verb == "show":
        p = parse(rest, sub, values=COMMON_VALUES, flags=COMMON_FLAGS, n_pos=1)
        with api_errors(sub):
            out = client(p, sub).get(f"/v1/incidents/{_int(p.pos[0], 'ID', sub)}")
        _print("--json" in p.flags, out, incident_lines(out, full=True))
        return 0
    if verb in ("edit", "confirm"):
        p, body = _changes(rest, sub, set())
        incident_id = _int(p.pos[0], "ID", sub)
        with api_errors(sub):
            api = client(p, sub)
            out = (api.patch(f"/v1/incidents/{incident_id}", body) if verb == "edit"
                   else api.post(f"/v1/incidents/{incident_id}/confirm", body))
        _print("--json" in p.flags, out, [f"{verb}ed " + incident_lines(out)[0],
                                           *incident_lines(out)[1:]])
        return 0
    if verb == "resolve":
        p = parse(rest, sub, values={*COMMON_VALUES, "--text", "--calibration", "--at"}, flags=COMMON_FLAGS, n_pos=2)
        body = {"resolution_kind": p.pos[1]}
        if "--text" in p.opts:
            body["text"] = p.opts["--text"]
        if "--at" in p.opts:
            body["at"] = p.opts["--at"]
        if "--calibration" in p.opts:
            body["calibration_id"] = _int(p.opts["--calibration"], "--calibration", sub)
        with api_errors(sub):
            out = client(p, sub).post(f"/v1/incidents/{_int(p.pos[0], 'ID', sub)}/resolve", body)
        _print("--json" in p.flags, out, incident_lines(out))
        return 0
    if verb == "dismiss":
        p = parse(rest, sub, values={*COMMON_VALUES, "--reason"}, flags=COMMON_FLAGS, n_pos=1)
        body = {"reason": p.opts["--reason"]} if "--reason" in p.opts else {}
        with api_errors(sub):
            out = client(p, sub).post(f"/v1/incidents/{_int(p.pos[0], 'ID', sub)}/dismiss", body)
        _print("--json" in p.flags, out, ["dismissed " + incident_lines(out)[0]])
        return 0
    if verb == "stats":
        p = parse(rest, sub, values={*COMMON_VALUES, "--by", "--since", "--kind"}, flags=COMMON_FLAGS)
        if "--by" not in p.opts:
            raise UsageError("--by is required: vendor, filament, lot, printer or head", command=f"printops {sub}")
        with api_errors(sub):
            out = client(p, sub).get("/v1/incidents/stats", by=p.opts["--by"], since=p.opts.get("--since", "90d"),
                                     kind=p.opts.get("--kind"))
        _print("--json" in p.flags, out, stats_lines(out))
        return 0
    if verb == "context":
        p = parse(rest, sub, values={*COMMON_VALUES, "--head", "--spool"}, flags=COMMON_FLAGS, n_pos=1)
        with api_errors(sub):
            out = _slow(p, sub).get("/v1/incidents/context", printer=p.pos[0], head=p.opts.get("--head"),
                                    spool=p.int_opt("--spool", sub))
        _print("--json" in p.flags, out, context_lines(out))
        return 0
    if verb == "from-watch":
        p = parse(rest, sub, values=COMMON_VALUES, flags=COMMON_FLAGS)
        raw = sys.stdin.read()
        try:
            event = json.loads(raw)
        except ValueError as e:
            raise UsageError(f"stdin is not a watch event JSON: {e}", command=f"printops {sub}",
                             remediation=["Use it as `3d printer watch P --exec '3d printops incidents from-watch'`."]) from e
        if not isinstance(event, dict):
            raise UsageError("stdin must be one JSON object (a watch event)", command=f"printops {sub}")
        with api_errors(sub):
            out = _slow(p, sub).post("/v1/incidents/from-watch", event)
        inc = out.get("incident")
        lines = [f"{out['reason']}" + (f": {incident_lines(inc)[0]}" if inc else "")]
        _print("--json" in p.flags, out, lines)
        return 0
    if verb == "exposure":
        return _exposure(rest)
    raise UsageError(
        f"unknown `printops incidents` subcommand {verb!r}",
        command="printops incidents",
        remediation=["One of: report, ls, show, edit, confirm, resolve, dismiss, stats, context, from-watch, "
                     "exposure. See `3d printops incidents --help`."],
    )


SUB = Sub(
    name="incidents",
    summary="print incidents: report (auto-context), triage watch drafts, rates per kg / print-hour",
    usage=USAGE,
    run=run,
)
