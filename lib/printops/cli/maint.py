"""`3d printops maint …` — printer components, maintenance log, faults and due rules through 3d-api
(issue #166; routes_maintenance.py). Domain logic lives in 3d-api (printops.maintenance); this
module only parses, calls HTTP and renders."""

from __future__ import annotations

import datetime
import json
import urllib.parse
from collections.abc import Callable
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import Parsed, api_errors, client, parse
from printops.maintenance import one_line

USAGE = """3d printops maint <action> [args] [--json] [--api URL] [--token TOKEN]
  Printer components (nozzle per head, PTFE, hotend, extruder, plates, …), the maintenance log,
  faults, and the maintenance rules of fleet/maintenance-rules.yaml. TARGET is PRINTER or
  PRINTER/HEAD (u1, u1/T3). A nozzle swap and component replacements invalidate the head's
  calibrations (event calibration.invalidated, read by the calibration engine). Reads need the
  `read` scope, changes `write`. Printers are never contacted, except READ-ONLY job-history
  reads (Moonraker GET /server/history) for the counters of `due`.

Actions:
  components PRINTER [--removed]
      heads (usable, open faults, nozzle) and mounted components; --removed adds the history
  nozzle swap PRINTER/HEAD --reason TEXT [--diameter MM] [--material M] [--type std|hf]
              [--label TEXT] [--at WHEN] [--notes TEXT] [--parts A,B]
      a new nozzle on a head; unset diameter/material/type/label are kept from the old one.
      Materials: brass stainless hardened ruby tungsten
  log TARGET (--rule ID | --component C --kind K) [--at WHEN] [--notes TEXT] [--parts A,B]
             [--label TEXT] [--invalidates KIND,KIND | --no-invalidate] [--fixes FAULT[,FAULT]]
      record maintenance work. --rule restarts that rule's counter (component/kind come from
      it). Kind replace also records the new component (--label = the new part).
      Components: nozzle hotend ptfe extruder plate belts kinematics fan head other printer
      Kinds: clean lube replace tighten repair inspect firmware_update move
  history PRINTER [--limit N]
      the maintenance log, newest first
  due [PRINTER] [--status due|soon|ok|no_counter] [--check]
      every rule with its status and usage since it was last done; --check also announces newly
      due ones (event maintenance.due)
  fault ls [PRINTER] [--status open|fixed|wontfix|all]
      faults (default: open ones)
  fault open TARGET --symptom TEXT [--component C] [--at WHEN]
      record something broken
  fault close ID [--root-cause TEXT] [--fixed-by MAINT_ID] [--wontfix]
      close a fault as fixed (by a logged maintenance) or won't fix
  sync-fleet
      seed nozzles, plates and faults of `usable: false` heads from fleet/printers.yaml now

Options:
  --at WHEN      ISO date/time it happened (default now); without an offset it is local time
  --json         print the API's JSON answer
  --api URL      3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN  bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops maint components u1
  3d printops maint nozzle swap u1/T3 --reason "clogged" --material hardened
  3d printops maint log u1 --rule u1-linear-shafts --notes "IPA + white lithium grease"
  3d printops maint log u1/T3 --component ptfe --kind replace --label "Capricorn XS" --fixes 4
  3d printops maint due --check
  3d printops maint fault open u1/T3 --symptom "under-extrusion after 2 h" --component extruder
  3d printops maint fault close 4 --root-cause "worn PTFE" --fixed-by 12
  3d printops maint due u1 --json | jq '.[] | select(.status == "due") | .log_command'"""

COMMON_VALUES = {"--api", "--token"}
COMMON_FLAGS = {"--json"}


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    """A request body without the options the user did not give."""
    return {k: v for k, v in body.items() if v is not None}


def _target(raw: str, sub: str, *, need_head: bool) -> tuple[str, str | None]:
    """PRINTER or PRINTER/HEAD; need_head=True refuses a bare PRINTER."""
    printer, _, hd = raw.partition("/")
    if not printer or "/" in hd or raw.endswith("/"):
        raise UsageError(f"{raw!r} is not PRINTER or PRINTER/HEAD (e.g. u1/T3)", command=f"printops {sub}")
    if need_head and not hd:
        raise UsageError(f"{raw!r} names no head: use PRINTER/HEAD, e.g. u1/T3", command=f"printops {sub}")
    return printer, hd or None


def _at(p: Parsed, sub: str) -> str | None:
    raw = p.opts.get("--at")
    if raw is None:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise UsageError(f"--at needs an ISO date/time (2026-10-14 or 2026-10-14T18:30), got {raw!r}",
                         command=f"printops {sub}") from e
    return (dt if dt.tzinfo else dt.astimezone()).isoformat()


def _list(p: Parsed, opt: str) -> list[str]:
    return [s.strip() for s in p.opts.get(opt, "").split(",") if s.strip()]


def _ints(p: Parsed, opt: str, sub: str) -> list[int]:
    try:
        return [int(s) for s in _list(p, opt)]
    except ValueError as e:
        raise UsageError(f"{opt} needs ids like 3 or 3,4", command=f"printops {sub}") from e


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _nozzle(n: dict[str, Any] | None) -> str:
    if not n:
        return "no nozzle recorded"
    when = _day(n.get("installed_at"), "unknown date")
    return f"nozzle #{n['id']} {n['diameter_mm']:g} mm {n.get('material') or '?'} {n.get('type') or '?'} (since {when})"


def _local(at: str) -> datetime.datetime:
    """An API timestamp (UTC, `Z`) in this machine's time zone."""
    return datetime.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone()


def _day(at: str | None, missing: str = "") -> str:
    return _local(at).strftime("%Y-%m-%d") if at else missing


def _minute(at: str) -> str:
    return _local(at).strftime("%Y-%m-%d %H:%M")


def _invalidated(res: dict[str, Any]) -> None:
    for inv in res.get("invalidated") or []:
        kinds = ", ".join(inv["kinds"]) if inv["kinds"] else "every kind the catalog ties to it"
        where = f"{inv['printer']}/{inv['head']}" if inv["head"] else inv["printer"]
        print(f"calibrations invalidated on {where}: {inv['trigger']} ({kinds})")


def _components(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES, flags=COMMON_FLAGS | {"--removed"}, n_pos=1)
    with api_errors(sub):
        data = client(p, sub).get(f"/v1/printers/{_q(p.pos[0])}/components",
                                  removed="true" if "--removed" in p.flags else None)
    if "--json" in p.flags:
        _print_json(data)
        return 0
    for h in data["heads"]:
        state = "usable" if h["usable"] else "UNUSABLE"
        why = []
        if not h["fleet_usable"]:
            why.append(f"fleet: {h['fleet_reason']}")
        if h["open_faults"]:
            why.append("open fault " + ", ".join(f"#{i}" for i in h["open_faults"]))
        print(f"{data['printer']}/{h['label']}  {state}{' (' + '; '.join(why) + ')' if why else ''}  {_nozzle(h['nozzle'])}")
    others = [c for c in data["components"] if c["kind"] != "nozzle" or c["removed_at"]]
    for c in others:
        where = f"{c['printer']}/{c['head']}" if c["head"] else c["printer"]
        spec = f" {c['diameter_mm']:g} mm {c['material'] or '?'} {c['type'] or '?'}" if c["kind"] == "nozzle" else ""
        label = f" {c['label']}" if c["label"] else ""
        span = f"{_day(c['installed_at'], 'unknown date')} → {_day(c['removed_at'], 'now')}"
        print(f"  {where}  {c['kind']}{spec}{label}  #{c['id']}  {span}")
    return 0


def _nozzle_action(argv: list[str], sub: str) -> int:
    if not argv or argv[0] != "swap":
        raise UsageError("nozzle actions: swap", command=f"printops {sub}",
                         remediation=["3d printops maint nozzle swap u1/T3 --reason clogged"])
    sub = f"{sub} swap"
    p = parse(argv[1:], sub, values=COMMON_VALUES | {"--reason", "--diameter", "--material", "--type", "--label",
                                                     "--at", "--notes", "--parts"}, flags=COMMON_FLAGS, n_pos=1)
    printer, head = _target(p.pos[0], sub, need_head=True)
    if not p.opts.get("--reason"):
        raise UsageError("--reason is required (clogged, worn, abrasive filament, diameter change, …)",
                         command=f"printops {sub}")
    body: dict[str, Any] = {"head": head, "reason": p.opts["--reason"], "at": _at(p, sub),
                            "material": p.opts.get("--material"), "type": p.opts.get("--type"),
                            "label": p.opts.get("--label"), "notes": p.opts.get("--notes"),
                            "parts_used": _list(p, "--parts")}
    if "--diameter" in p.opts:
        try:
            body["diameter_mm"] = float(p.opts["--diameter"].replace(",", "."))
        except ValueError as e:
            raise UsageError(f"--diameter needs mm, got {p.opts['--diameter']!r}", command=f"printops {sub}") from e
    with api_errors(sub):
        res = client(p, sub).post(f"/v1/printers/{_q(printer)}/components/nozzle-swap",
                                  _compact(body))
    if "--json" in p.flags:
        _print_json(res)
        return 0
    if res["removed"]:
        print(f"removed {_nozzle(res['removed'])}")
    kept = f"  (kept: {', '.join(res['inherited'])})" if res["inherited"] else ""
    print(f"installed {_nozzle(res['installed'])} on {printer}/{head}{kept}  — maintenance #{res['maintenance']['id']}")
    _invalidated(res)
    return 0


def _log(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--rule", "--component", "--kind", "--at", "--notes", "--parts",
                                                 "--label", "--invalidates", "--fixes"},
              flags=COMMON_FLAGS | {"--no-invalidate"}, n_pos=1)
    printer, head = _target(p.pos[0], sub, need_head=False)
    if "--rule" not in p.opts and not ({"--component", "--kind"} <= set(p.opts)):
        raise UsageError("log needs --rule ID, or --component and --kind", command=f"printops {sub}",
                         remediation=["See the rule ids: 3d printops maint due " + printer])
    if "--invalidates" in p.opts and "--no-invalidate" in p.flags:
        raise UsageError("--invalidates and --no-invalidate exclude each other", command=f"printops {sub}")
    body: dict[str, Any] = {"rule": p.opts.get("--rule"), "component": p.opts.get("--component"),
                            "kind": p.opts.get("--kind"), "head": head, "at": _at(p, sub),
                            "notes": p.opts.get("--notes"), "parts_used": _list(p, "--parts"),
                            "label": p.opts.get("--label"),
                            "invalidates": _list(p, "--invalidates") if "--invalidates" in p.opts else None,
                            "no_invalidate": "--no-invalidate" in p.flags, "fixes": _ints(p, "--fixes", sub)}
    with api_errors(sub):
        res = client(p, sub).post(f"/v1/printers/{_q(printer)}/maintenance",
                                  _compact(body))
    if "--json" in p.flags:
        _print_json(res)
        return 0
    m = res["maintenance"]
    where = f"{m['printer']}/{m['head']}" if m["head"] else m["printer"]
    rule = f" (rule {m['rule_id']})" if m["rule_id"] else ""
    print(f"logged #{m['id']} {m['component']} {m['kind']} on {where} at {_minute(m['at'])}{rule}")
    if res["removed"]:
        print(f"removed {res['removed']['kind']} #{res['removed']['id']} {res['removed']['label'] or ''}".rstrip())
    if res["installed"]:
        print(f"installed {res['installed']['kind']} #{res['installed']['id']} {res['installed']['label'] or ''}".rstrip())
    _invalidated(res)
    for f in res["fixed"]:
        print(f"fault #{f['id']} fixed: {f['symptom']}")
    return 0


def _history(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--limit"}, flags=COMMON_FLAGS, n_pos=1)
    with api_errors(sub):
        rows = client(p, sub).get(f"/v1/printers/{_q(p.pos[0])}/maintenance",
                                  limit=p.int_opt("--limit", sub))["maintenance"]
    if "--json" in p.flags:
        _print_json(rows)
        return 0
    if not rows:
        print(f"no maintenance logged on {p.pos[0]} yet")
    for m in rows:
        where = f"/{m['head']}" if m["head"] else ""
        extra = [f"rule {m['rule_id']}"] if m["rule_id"] else []
        extra += [f"parts: {', '.join(m['parts_used'])}"] if m["parts_used"] else []
        extra += [f"invalidated: {', '.join(t['trigger'] for t in m['triggers_calibrations'])}"] \
            if m["triggers_calibrations"] else []
        print(f"#{m['id']}  {_minute(m['at'])}  {m['printer']}{where}  {m['component']} {m['kind']}  by {m['actor']}"
              + (f"  — {m['notes']}" if m["notes"] else "") + ("  [" + "; ".join(extra) + "]" if extra else ""))
    return 0


def _due(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--status"}, flags=COMMON_FLAGS | {"--check"}, n_pos=(0, 1))
    status = p.opts.get("--status")
    if status is not None and status not in ("due", "soon", "ok", "no_counter"):
        raise UsageError(f"--status {status!r}: one of due, soon, ok, no_counter", command=f"printops {sub}")
    api = client(p, sub)
    announced: list[dict[str, Any]] = []
    with api_errors(sub):
        if "--check" in p.flags:
            res = api.request("POST", "/v1/maintenance/check", params={"printer": p.pos[0]} if p.pos else None,
                              json_body={})
            items, announced = res["items"], res["announced"]
        elif p.pos:
            items = api.get(f"/v1/printers/{_q(p.pos[0])}/maintenance/due")["items"]
        else:
            items = api.get("/v1/maintenance/due")["items"]
    items = [i for i in items if (not p.pos or i["printer"] == p.pos[0]) and (status is None or i["status"] == status)]
    if "--json" in p.flags:
        _print_json({"items": items, "announced": announced} if "--check" in p.flags else items)
        return 0
    if not items:
        print("no maintenance rules match (fleet/maintenance-rules.yaml)")
    for i in items:
        used = ", ".join(f"{k} {'?' if v is None else f'{v:g}'}/{i['every'][k]:g}" for k, v in i["used"].items())
        since = f"since #{i['baseline']['maintenance_id']} {_day(i['baseline']['at'])}" if i["baseline"] else "never logged"
        print(f"{i['status'].upper():<10} {i['subject']:<8} {i['rule']}  {i['title']}  [{used}; {since}]")
        for r in i["reasons"]:
            print(f"           {r}")
        if i["status"] in ("due", "soon"):
            print(f"           done? {i['log_command']}")
    for a in announced:
        print(f"announced maintenance.due #{a['event_id']}: {a['rule']} on {a['printer']}" + (f"/{a['head']}" if a["head"] else ""))
    return 0


def _fault(argv: list[str], sub: str) -> int:
    if not argv or argv[0] not in _FAULT_ACTIONS:
        raise UsageError("fault actions: ls, open, close", command=f"printops {sub}",
                         remediation=["See `3d printops maint --help`."])
    return _FAULT_ACTIONS[argv[0]](argv[1:], f"{sub} {argv[0]}")


def _fault_ls(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--status"}, flags=COMMON_FLAGS, n_pos=(0, 1))
    path = f"/v1/printers/{_q(p.pos[0])}/faults" if p.pos else "/v1/faults"
    with api_errors(sub):
        faults = client(p, sub).get(path, status=p.opts.get("--status"))["faults"]
    if "--json" in p.flags:
        _print_json(faults)
        return 0
    if not faults:
        print("no faults")
    for f in faults:
        print(_fault_line(f))
    return 0


def _fault_open(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--symptom", "--component", "--at"}, flags=COMMON_FLAGS, n_pos=1)
    printer, head = _target(p.pos[0], sub, need_head=False)
    if not p.opts.get("--symptom"):
        raise UsageError("--symptom is required (what is wrong, as observed)", command=f"printops {sub}")
    body = {"symptom": p.opts["--symptom"], "head": head, "component": p.opts.get("--component"), "at": _at(p, sub)}
    with api_errors(sub):
        f = client(p, sub).post(f"/v1/printers/{_q(printer)}/faults", _compact(body))
    _show_fault(f, "opened", p)
    return 0


def _fault_close(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES | {"--root-cause", "--fixed-by"}, flags=COMMON_FLAGS | {"--wontfix"},
              n_pos=1)
    fid = _fault_id(p.pos[0], sub)
    fixed_by = p.int_opt("--fixed-by", sub)
    if fixed_by is not None and "--wontfix" in p.flags:
        raise UsageError("--fixed-by and --wontfix exclude each other", command=f"printops {sub}")
    close: dict[str, Any] = {"status": "wontfix" if "--wontfix" in p.flags else "fixed",
                             "root_cause": p.opts.get("--root-cause"), "fixed_by": fixed_by}
    with api_errors(sub):
        f = client(p, sub).post(f"/v1/faults/{fid}/close", _compact(close))
    _show_fault(f, "closed", p)
    return 0


_FAULT_ACTIONS = {"ls": _fault_ls, "open": _fault_open, "close": _fault_close}


def _show_fault(f: dict[str, Any], verb: str, p: Parsed) -> None:
    if "--json" in p.flags:
        _print_json(f)
    else:
        print(f"{verb} {_fault_line(f)}")


def _fault_id(raw: str, sub: str) -> int:
    try:
        return int(raw.lstrip("#"))
    except ValueError as e:
        raise UsageError(f"fault id must be a number, got {raw!r}", command=f"printops {sub}") from e


FAULT_TEXT_CHARS = 300  # one terminal line per fault in `fault ls`; --json keeps the full text


def _fault_line(f: dict[str, Any]) -> str:
    where = f"{f['printer']}/{f['head']}" if f["head"] else f["printer"]
    comp = f" [{f['component']}]" if f["component"] else ""
    tail = ""
    if f["status"] != "open":
        tail = f"  — {f['status']} {_day(f['closed_at'])}"
        if f["root_cause"]:
            tail += f": {one_line(f['root_cause'], FAULT_TEXT_CHARS)}"
        if f["fixed_by_maintenance_id"]:
            tail += f" (maintenance #{f['fixed_by_maintenance_id']})"
    symptom = one_line(f["symptom"], FAULT_TEXT_CHARS)  # rows seeded or stored before validation too
    return f"#{f['id']}  {f['status'].upper()}  {where}{comp}  {symptom}  (since {_day(f['at'])}){tail}"


def _sync_fleet(argv: list[str], sub: str) -> int:
    p = parse(argv, sub, values=COMMON_VALUES, flags=COMMON_FLAGS)
    with api_errors(sub):
        res = client(p, sub).post("/v1/maintenance/sync-fleet")
    if "--json" in p.flags:
        _print_json(res)
        return 0
    for line in res["installed"]:
        print(f"installed      {line}")
    for line in res["faults_opened"]:
        print(f"fault opened   {line}")
    for d in res["drift"]:
        print(f"drift          {d['subject']} {d['what']}: fleet/printers.yaml {d['fleet']} vs recorded {d['recorded']}")
    if not any(res.values()):
        print("components and faults already reflect fleet/printers.yaml")
    return 0


ACTIONS: dict[str, Callable[[list[str], str], int]] = {
    "components": _components, "nozzle": _nozzle_action, "log": _log, "history": _history, "due": _due,
    "fault": _fault, "sync-fleet": _sync_fleet,
}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    handler = ACTIONS.get(action)
    if handler is None:
        raise UsageError(f"unknown maint action {action!r}; accepted: {', '.join(ACTIONS)}",
                         command="printops maint", remediation=["Run `3d printops maint --help`."])
    if any(a in ("-h", "--help") for a in rest):
        print(USAGE)
        return 0
    return handler(rest, f"maint {action}")


SUB = Sub(name="maint", summary="components, nozzle swaps, maintenance log, faults, due maintenance rules",
          usage=USAGE, run=run)
