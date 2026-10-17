"""3d calibrate — calibration requirements, records and the `calibration_current` gate via 3d-api.

WHAT: `plan` lists the calibrations a printer head needs for the spool loaded on it (plan §8.2
  step 1), `record` stores a measured value, `ls`/`show` list records, `check` runs the
  `calibration_current` gate on a G-code (missing/stale calibration, footer values ≠ recorded),
  `gen` writes a calibration print locally (PA tower, flow tiles; shaper console commands).
WHY: a new material type or vendor on a head prints with someone else's flow and pressure
  advance; after a nozzle swap every old value is stale. Issue #167 (decision D6).
INVARIANTS: stdlib-only at import time (registry contract); the domain logic runs in 3d-api
  (printops.calibrations), this module only parses arguments and speaks HTTP — except `gen`,
  which slices locally (slicing.calibration_gen) and needs no API.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError
from topcam import photo_cli

USAGE = """3d calibrate plan --printer P [--head T] [--spool ID] [--plate PLATE] [--all]
3d calibrate record KIND --printer P [--head T] [--spool ID | --filament ID] [--lot LOT] [--plate PLATE]
                    --values NAME=V[,NAME=V…] [--method M] [--note TEXT] [--evidence URL[,URL…]]
                    [--valid-until DATE]
3d calibrate ls [--printer P] [--head T] [--kind KIND] [--status planned|recorded|abandoned] [--all] [--limit N]
3d calibrate show ID
3d calibrate check FILE.gcode --printer P [--spools T1=ID,…] [--plate PLATE]
                   [--accept calibration:KIND[,…] --reason TEXT]
3d calibrate gen pa|flow|combo|temp|shaper --printer P -o OUT [--material PLA] [--color #RRGGBB] [--nozzle-temp C]
  common (all but gen): [--json] [--api URL] [--token TOKEN]

  Calibrations per printer head and filament (fleet/calibrations.yaml: flow_ratio, pressure_advance,
  temp_tower, max_volumetric_speed, retraction, input_shaper, z_offset, bed_mesh, head_offsets,
  rotation_distance, belt_tension). 3d-api compares what is loaded on a head (the spool bound with
  `3d printops spool load`) with the records: a new material type, vendor or filament blocks flow
  ratio and pressure advance, a new lot warns, a nozzle swap or maintenance makes old records stale
  (fleet/policy.yaml `calibrations:` sets block/warn/off per reason, kind and printer).

Subcommands:
  plan    what the head(s) need now, with time and filament estimates; exit 0 (a report)
  record  store a measured value (manual entry: a run and its result in one step)
  ls      records, newest first (current ones only unless --all)
  show    one record as JSON
  check   the calibration_current gate for a G-code: every used head's requirements plus the
          footer values (`; pressure_advance`, `; filament_flow_ratio`, `; filament_max_volumetric_speed`)
          against the records. FAIL exits 1; --accept turns a FAIL into a logged WARN.
  gen     a calibration print for a Klipper printer, sliced with the Orca CLI from the printer's
          system presets: pa = hollow tower with Klipper TUNING_TOWER (PA = Z × 0.005); flow =
          5 tiles at 90…110 % extrusion (M221 per tile); combo = one plate: PA lines on the first
          layer (SET_PRESSURE_ADVANCE per line, labels = PA × 1000) plus the flow tiles, for the
          express calibration of a new spool; temp = temperature tower, one labelled 8 mm block per
          5 °C (PLA 230→200, PETG 250→220, M104 per block); shaper = SHAPER_CALIBRATE console commands
          (printers with an accelerometer). The G-code gets thumbnails and a slice-warnings record,
          ready for `3d print-preflight` (temp: filament_temp_range FAILs when the tower's hottest
          block is above the spool's nozzle window; start it with --force). Local only; set SLICER=/path/to/orca.

Options:
  --printer P        fleet printer name (fleet/printers.yaml), e.g. u1, k1se
  --head T           head label T1..Tn (plan: default every usable head)
  --spool ID         Spoolman spool to plan/record for instead of the one bound to the head
  --filament ID      record for a Spoolman filament without a spool (e.g. a temperature tower)
  --lot LOT          lot number of the record (default: the spool's Spoolman lot_nr)
  --plate PLATE      build plate (z_offset, bed_mesh); default: the printer's only plate
  --values N=V,…     result values, e.g. pressure_advance=0.035 or flow_ratio=0.97
  --method M         how it was measured (the kind's methods; default manual)
  --evidence URLs    photo URLs / notes kept with the record
  --valid-until DATE record expires then (ISO date)
  --kind KIND        only this calibration kind
  --status S         ls: only planned | recorded | abandoned
  --all              plan: also PASS items; ls: also superseded and planned records
  --spools T1=ID,…   check: spool per head instead of the slot binding
  --accept LIST      check: calibration:<kind>[,…] to accept despite FAIL (needs --reason; logged)
  --reason TEXT      why the FAIL is accepted
  --limit N          ls: at most N records (default 50)
  --material M       gen: filament preset material (default PLA)
  --color #RRGGBB    gen: filament colour written to the G-code (loaded_filament_match)
  --nozzle-temp C    gen: nozzle temperature instead of the preset's (not for temp: it prints its range)
  -o, --out FILE     gen: output .gcode (shaper: a .txt of console commands)
  --json             print the API's JSON
  --api URL          3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN      bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d calibrate plan --printer u1
  3d calibrate plan --printer u1 --head T3 --spool 42 --all
  3d calibrate record pressure_advance --printer u1 --head T3 --values pressure_advance=0.035 --method pa_pattern
  3d calibrate record flow_ratio --printer k1se --head T1 --values flow_ratio=0.97 --note "tiles 3/5"
  3d calibrate record temp_tower --filament 7 --values nozzle_temp_min=205,nozzle_temp_best=215,nozzle_temp_max=230
  3d calibrate check plate.gcode --printer u1
  3d calibrate check plate.gcode --printer u1 --accept calibration:flow_ratio --reason "test cube, flow irrelevant"
  3d calibrate plan --printer u1 --json | jq '.items[] | {head, kind, status, level}'
  3d calibrate gen pa --printer k1se --color '#E8732A' -o k1se_pa_orange.gcode
  3d calibrate gen flow --printer k1se --color '#000000' -o k1se_flow_black.gcode
  3d calibrate gen combo --printer k1se --color '#000000' -o k1se_combo_black.gcode
  3d calibrate gen temp --printer k1se --color '#E8732A' -o k1se_temp_orange.gcode"""

USAGE += (
    "\n\nTop-camera photo (#342):\n"
    + photo_cli.USAGE
    + "\n\nSubcommands:\n"
    + photo_cli.HELP
    + "\n\nOptions:\n"
    + photo_cli.OPTIONS
    + "\n\nExamples:\n"
    + photo_cli.EXAMPLES
)

PREFIX = "calibrate"
COMMON_VALUES = {"--api", "--token"}
COMMON_FLAGS = {"--json"}
LEVEL_ORDER = {"FAIL": 0, "WARN": 1, "UNVERIFIED": 2, "PASS": 3}


def _int(raw: str, what: str, sub: str) -> int:
    try:
        return int(raw)
    except ValueError as e:
        raise UsageError(
            f"{what} must be an integer, got {raw!r}", command=f"{PREFIX} {sub}"
        ) from e


def _pairs(raw: str, opt: str, sub: str) -> list[tuple[str, str]]:
    out = []
    for part in raw.split(","):
        key, sep, value = part.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise UsageError(
                f"{opt} wants NAME=VALUE pairs, got {part!r}", command=f"{PREFIX} {sub}"
            )
        out.append((key.strip(), value.strip()))
    return out


def _where(item: dict[str, Any]) -> str:
    if item.get("head"):
        return str(item["head"])
    if item.get("tool") is not None:
        return f"T{int(item['tool']) + 1}"
    return "-"


def _item_line(item: dict[str, Any]) -> str:
    kind = item.get("kind") or "-"
    return f"  {item['level']:<10} {_where(item):<4} {kind:<21} {item['status']:<18} {item['detail']}"


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda i: (LEVEL_ORDER.get(i["level"], 9), _where(i), i.get("kind") or ""),
    )


def _plan(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv,
        "plan",
        values={"--printer", "--head", "--spool", "--plate"} | COMMON_VALUES,
        flags={"--all"} | COMMON_FLAGS,
        prefix=PREFIX,
    )
    printer = p.opts.get("--printer")
    if not printer:
        raise UsageError("--printer is required", command=f"{PREFIX} plan")
    spool = _int(p.opts["--spool"], "--spool", "plan") if "--spool" in p.opts else None
    with api_errors("plan", prefix=PREFIX):
        data = client(p, "plan", prefix=PREFIX).get(
            "/v1/calibrations/required",
            printer=printer,
            head=p.opts.get("--head"),
            spool=spool,
            plate=p.opts.get("--plate"),
            all="true" if "--all" in p.flags else None,
        )
    if "--json" in p.flags:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    items = _sorted(data["items"])
    counts = {
        lv: sum(1 for i in items if i["level"] == lv)
        for lv in ("FAIL", "WARN", "UNVERIFIED")
    }
    summary = (
        ", ".join(f"{n} {lv}" for lv, n in counts.items() if n) or "nothing needed"
    )
    est = data["estimate"]
    cost = (
        f"; ~{est['minutes']:g} min, ~{est['grams']:g} g"
        if est["minutes"] or est["grams"]
        else ""
    )
    print(f"{data['printer']}: {summary}{cost}")
    for item in items:
        print(_item_line(item))
    for s in data["skipped"]:
        print(f"  skipped {s['head']}: {s['reason']}")
    return 0


def _record(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv,
        "record",
        values={
            "--printer",
            "--head",
            "--spool",
            "--filament",
            "--lot",
            "--plate",
            "--values",
            "--method",
            "--note",
            "--evidence",
            "--valid-until",
        }
        | COMMON_VALUES,
        flags=COMMON_FLAGS,
        n_pos=1,
        prefix=PREFIX,
    )
    if "--values" not in p.opts:
        raise UsageError(
            "--values is required, e.g. --values pressure_advance=0.035",
            command=f"{PREFIX} record",
        )
    if "--spool" in p.opts and "--filament" in p.opts:
        raise UsageError(
            "pass --spool or --filament, not both", command=f"{PREFIX} record"
        )
    body: dict[str, Any] = {
        "kind": p.pos[0],
        "values": dict(
            _pairs(p.opts["--values"], "--values", "record")
        ),  # typed by the catalog
    }
    for opt, key in (
        ("--printer", "printer"),
        ("--head", "head"),
        ("--lot", "lot_nr"),
        ("--plate", "plate"),
        ("--method", "method"),
        ("--note", "note"),
        ("--valid-until", "valid_until"),
    ):
        if opt in p.opts:
            body[key] = p.opts[opt]
    for opt, key in (("--spool", "spool_id"), ("--filament", "filament_id")):
        if opt in p.opts:
            body[key] = _int(p.opts[opt], opt, "record")
    if "--evidence" in p.opts:
        body["evidence"] = [
            e.strip() for e in p.opts["--evidence"].split(",") if e.strip()
        ]
    with api_errors("record", prefix=PREFIX):
        rec = client(p, "record", prefix=PREFIX).post("/v1/calibrations", body)
    if "--json" in p.flags:
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    print(f"recorded #{rec['id']} {_record_line(rec)}")
    return 0


def _record_line(rec: dict[str, Any]) -> str:
    where = (
        "/".join(x for x in (rec.get("printer"), rec.get("head")) if x) or "any printer"
    )
    fil = rec.get("filament") or {}
    what = " ".join(
        str(x) for x in (fil.get("vendor"), fil.get("name") or fil.get("material")) if x
    )
    lot = f" lot {fil['lot_nr']}" if fil.get("lot_nr") else ""
    vals = ", ".join(
        f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}"
        for k, v in (rec.get("values") or {}).items()
    )
    sup = f" superseded by #{rec['superseded_by']}" if rec.get("superseded_by") else ""
    return (
        f"{rec['kind']} {where}"
        + (f" {what}{lot}" if what else "")
        + (f": {vals}" if vals else "")
        + f" [{rec['status']}, {rec['method']}, "
        f"{(rec.get('recorded_at') or rec['created_at'])[:10]}]{sup}"
    )


def _ls(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv,
        "ls",
        values={"--printer", "--head", "--kind", "--status", "--limit"} | COMMON_VALUES,
        flags={"--all"} | COMMON_FLAGS,
        prefix=PREFIX,
    )
    limit = _int(p.opts["--limit"], "--limit", "ls") if "--limit" in p.opts else 50
    with api_errors("ls", prefix=PREFIX):
        data = client(p, "ls", prefix=PREFIX).get(
            "/v1/calibrations",
            printer=p.opts.get("--printer"),
            head=p.opts.get("--head"),
            kind=p.opts.get("--kind"),
            status=p.opts.get("--status"),
            current=None if "--all" in p.flags or "--status" in p.opts else "true",
            limit=limit,
        )
    if "--json" in p.flags:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    rows = data["calibrations"]
    if not rows:
        print("no calibrations")
    for rec in rows:
        print(f"#{rec['id']:<5} {_record_line(rec)}")
    return 0


def _show(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv, "show", values=COMMON_VALUES, flags=COMMON_FLAGS, n_pos=1, prefix=PREFIX
    )
    cal_id = _int(p.pos[0], "ID", "show")
    with api_errors("show", prefix=PREFIX):
        rec = client(p, "show", prefix=PREFIX).get(f"/v1/calibrations/{cal_id}")
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


def _spool_map(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for head, sid in _pairs(raw, "--spools", "check"):
        label = head.upper()
        if not (label.startswith("T") and label[1:].isdigit() and int(label[1:]) >= 1):
            raise UsageError(
                f"--spools: {head!r} is not a head label like T1",
                command=f"{PREFIX} check",
            )
        out[str(int(label[1:]) - 1)] = _int(sid, "--spools spool id", "check")
    return out


def gcode_inputs(path: Path) -> dict[str, Any]:
    """The parts of a G-code the gate needs: footer settings, used tools, sha256."""
    import hashlib

    from gcode_overhangs import read_config
    from printer_ops.preflight import read_gcode_meta

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {
        "footer": read_config(str(path)),
        "used_tools": read_gcode_meta(path).used_tools,
        "gcode_sha": digest.hexdigest(),
        "file": path.name,
    }


def _check(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv,
        "check",
        values={"--printer", "--spools", "--plate", "--accept", "--reason"}
        | COMMON_VALUES,
        flags=COMMON_FLAGS,
        n_pos=1,
        prefix=PREFIX,
    )
    printer = p.opts.get("--printer")
    if not printer:
        raise UsageError("--printer is required", command=f"{PREFIX} check")
    path = Path(p.pos[0])
    if not path.is_file():
        raise InputNotFound(str(path), command=f"{PREFIX} check")
    accept = [a.strip() for a in p.opts.get("--accept", "").split(",") if a.strip()]
    if accept and not p.opts.get("--reason", "").strip():
        raise UsageError(
            "--accept needs --reason (it is logged)", command=f"{PREFIX} check"
        )
    body = {
        **gcode_inputs(path),
        "printer": printer,
        "plate": p.opts.get("--plate"),
        "spools": _spool_map(p.opts["--spools"]) if "--spools" in p.opts else {},
        "accept": accept,
        "reason": p.opts.get("--reason"),
    }
    with api_errors("check", prefix=PREFIX):
        data = client(p, "check", prefix=PREFIX).post("/v1/calibrations/check", body)
    if "--json" in p.flags:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"CALIBRATION {data['printer']} {path}")
        assumed = [i for i in data["items"] if i["status"] == "assumed"]
        for item in _sorted([i for i in data["items"] if i["status"] != "assumed"]):
            print(_item_line(item))
        if assumed:
            kinds = sorted({str(i["kind"]) for i in assumed})
            print(
                f"  {'PASS':<10} {'-':<4} factory state assumed (never recorded, no maintenance since): "
                f"{', '.join(kinds)}"
            )
        print(f"STATUS: {data['overall']}")
    if data["overall"] == "FAIL":
        raise GateFailure(
            "calibration_current: FAIL", command=f"{PREFIX} check", silent=True
        )
    return 0


def _gen(argv: list[str]) -> int:
    from printops.cli._common import parse

    p = parse(
        argv,
        "gen",
        values={"--printer", "--material", "--color", "--nozzle-temp", "-o", "--out"},
        n_pos=1,
        prefix=PREFIX,
    )
    from slicing.calibration_gen import KINDS, GenError, generate

    kind = p.pos[0]
    if kind not in KINDS:
        raise UsageError(
            f"unknown kind {kind!r} (accepted: {', '.join(KINDS)})",
            command=f"{PREFIX} gen",
        )
    printer = p.opts.get("--printer")
    if not printer:
        raise UsageError("--printer is required", command=f"{PREFIX} gen")
    out = p.opts.get("-o") or p.opts.get("--out")
    if not out:
        raise UsageError(
            "-o OUT is required (a .gcode, or .txt for shaper)", command=f"{PREFIX} gen"
        )
    color = p.opts.get("--color", "")
    if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise UsageError(
            f"--color wants #RRGGBB, got {color!r}", command=f"{PREFIX} gen"
        )
    temp = (
        _int(p.opts["--nozzle-temp"], "--nozzle-temp", "gen")
        if "--nozzle-temp" in p.opts
        else None
    )

    from cli.env import find_slicer
    from printops.fleet import load_fleet

    fleet = load_fleet()
    entry = fleet.printers.get(printer) if fleet else None
    if entry is None:
        raise InputNotFound(
            f"printer {printer!r} is not in fleet/printers.yaml",
            command=f"{PREFIX} gen",
        )
    slicer = find_slicer()
    # A forced SLICER ("custom") is taken as an Orca-family binary: the generator needs Orca's CLI.
    if kind != "shaper" and (slicer is None or slicer[0] not in ("orca", "custom")):
        raise UsageError(
            "`calibrate gen` slices with the Orca CLI; none found (set SLICER=/path/to/orca)",
            command=f"{PREFIX} gen",
        )
    try:
        res = generate(
            kind,
            entry.model,
            slicer[1] if slicer else "",
            Path(out),
            material=p.opts.get("--material", "PLA").upper(),
            color=color,
            temp=temp,
        )
    except GenError as e:
        raise UsageError(str(e), command=f"{PREFIX} gen") from e
    for f in res.files:
        print(f"wrote {f}")
    for note in res.notes:
        print(f"  {note}")
    if kind != "shaper":
        print(f"  next: 3d print-preflight {out} --printer {printer}")
    return 0


SUBS = {
    "plan": _plan,
    "record": _record,
    "ls": _ls,
    "show": _show,
    "check": _check,
    "gen": _gen,
    "photo": photo_cli.photo,
    "rectify": photo_cli.rectify,
}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE)
        return 0
    fn = SUBS.get(argv[0])
    if fn is None:
        raise UsageError(
            f"unknown calibrate subcommand {argv[0]!r}; accepted: {', '.join(SUBS)}",
            command=PREFIX,
            remediation=["Run `3d calibrate --help`."],
        )
    return fn(argv[1:])


COMMAND = Command(
    name="calibrate",
    group="SLICING",
    summary="calibrations per head × filament via 3d-api: plan what is needed, record, gate a G-code",
    usage=USAGE,
    run=run,
)
