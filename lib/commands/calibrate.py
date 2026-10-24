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
from typing import TYPE_CHECKING, Any

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError
from topcam import photo_cli

if TYPE_CHECKING:  # registry contract: stdlib-only at import time
    from printops.cli._common import Parsed
    from printops.fleet import FleetPrinter
    from slicing.calibration_gen import PresetChoice
    from slicing.calibration_plate import UnderlayHeads

USAGE = """3d calibrate plan --printer P [--head T] [--spool ID] [--plate PLATE] [--all]
3d calibrate record KIND --printer P [--head T] [--spool ID | --filament ID] [--lot LOT] [--plate PLATE]
                    [--mix KEY] --values NAME=V[,NAME=V…] [--method M] [--note TEXT]
                    [--evidence URL[,URL…]] [--valid-until DATE]
3d calibrate ls [--printer P] [--head T] [--kind KIND] [--mix KEY] [--status planned|recorded|abandoned] [--all] [--limit N]
3d calibrate show ID
3d calibrate check FILE.gcode --printer P [--spools T1=ID,…] [--plate PLATE]
                   [--accept calibration:KIND[,…] --reason TEXT]
3d calibrate underlay-map PLATE.json --rectify R.json [--image IMG] [--cell-mm N] [--json]
3d calibrate gen pa|flow|combo|temp|shaper|plate1|dense|dense2 --printer P -o OUT [--material PLA] [--color #RRGGBB]
                 [--nozzle-temp C] [--only T,…] [--skip T,…] [--pa-range LOW:HIGH] [--pa-step S] [--no-underlay]
                 [--underlay-tool T] [--head-colours T1=#RRGGBB,…]
                 [--machine-preset NAME] [--process-preset NAME] [--filament-preset NAME]
                 [--head T] [--spool ID] [--api URL] [--token TOKEN]
  common (all but gen, which takes --api/--token only for its default filament preset): [--json] [--api URL] [--token TOKEN]

  Calibrations per printer head and filament (fleet/calibrations.yaml: flow_ratio, pressure_advance,
  temp_tower, max_volumetric_speed, retraction, input_shaper, z_offset, bed_mesh, head_offsets,
  rotation_distance, belt_tension; colour: filament_color, mix_color — read by `3d mix`, never
  required before a print). 3d-api compares what is loaded on a head (the spool bound with
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
  underlay-map  the bare underlay of a rectified plate photo as the first-layer map: per-cell
          brightness and texture, the plate's uniformity, and hints (bed tilt, z-offset, patchy cells)
          for a person to confirm; needs the manifest's `underlay` block (plates made without
          --no-underlay). Exit 1 = too few whole cells in the photo (needs_manual).
  gen     a calibration print for a Klipper printer, sliced with the Orca CLI from the printer's
          system presets: pa = hollow tower with Klipper TUNING_TOWER (PA = Z × 0.005); flow =
          5 tiles at 90…110 % extrusion (M221 per tile); combo = one plate: PA lines on the first
          layer (SET_PRESSURE_ADVANCE per line, labels = PA × 1000) plus the flow tiles, for the
          express calibration of a new spool; temp = temperature tower, one labelled 8 mm block per
          5 °C (PLA 230→200, PETG 250→220, M104 per block); shaper = SHAPER_CALIBRATE console commands
          (printers with an accelerometer); plate1 = the filament plate: every test of a new spool on
          one bed, each object with its own setting (docs/printops/express-calibration.md); dense =
          plate1's tests without PA/flow plus ringing (ACCEL by Z), VFA (speed by Z), size/skew,
          fits, hinges, screw holes, thin walls, first-layer patches (K1 SE bed); dense2 = what
          dense left open: hole variants (plain/polyhole/arc fitting/hole compensation), ovality,
          new fit/screw/slot/hinge/thin-wall values, elephant foot, bridge and overhang on 8 mm
          bases, MVS 24–32, a 7x7 grid with fiducials (K1 SE bed); every plate prints its tests on
          ONE shared underlay (one solid layer under all of them, no brims, no per-test bases; the
          print-in-place, loose and through-hole tests stand in cut-outs of it; the bare underlay is
          the first-layer map, so the first-layer patches are gone), see docs/commands/calibrate.md;
          plates add a JSON map (calib-plate/1) object → parameters, bbox, how to read it, plus the
          `underlay` block. The G-code gets thumbnails and a slice-warnings record,
          ready for `3d print-preflight` (temp: filament_temp_range FAILs when the tower's hottest
          block is above the spool's nozzle window; start needs a Telegram-approved --force). Local (3d-api only for the default --filament-preset); set SLICER=/path/to/orca.

Options:
  --printer P        fleet printer name (fleet/printers.yaml), e.g. u1, k1se
  --head T           head label T1..Tn (plan: default every usable head)
  --spool ID         Spoolman spool to plan/record/gen for instead of the one bound to the head
  --filament ID      record for a Spoolman filament without a spool (e.g. a temperature tower)
  --lot LOT          lot number of the record (default: the spool's Spoolman lot_nr)
  --plate PLATE      build plate (z_offset, bed_mesh); default: the printer's only plate
  --mix KEY          record/ls: the colour mix of a mix_color record — layer height and the Spoolman
                     filament ids of its layer cycle (h0.12:f3.f12.f7; 431 and 314 are one mix) or
                     its Local-Z shares (h0.12:lz:f1=25,f4=75)
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
  -o, --out FILE     gen: output .gcode (shaper: a .txt of console commands; plates also write OUT.json)
  --only T,…         gen plateN: print only these tests (plate1: pa, mvs, flow, bridge, overhang,
                     retraction, temp; dense, dense2: see docs/commands/calibrate.md); plate1
                     packs again, dense and dense2 keep their fixed layout
  --skip T,…         gen plateN: print all tests but these
  --no-underlay      gen plateN: print on the bare bed, with the brim rule (Plate.brim) and the
                     first-layer patches, instead of the shared underlay
  --underlay-tool T  gen plateN, multi-head printers: the head of the underlay (default: the one that
                     contrasts most with the tests', ΔE ≥ 40, #263); the same check applies to it
  --head-colours T1=#RRGGBB,…  gen plateN, multi-head printers: what is loaded on the heads (the
                     contrast check needs it; the tests' head defaults to --color)
  --head T           gen plateN, multi-head printers: the tests' head (default: the first); also the head
                     whose spool gives the default --filament-preset
  --rectify R.json   underlay-map: the `calibrate rectify` output (area_mm, px_per_mm, rectified photo)
  --image IMG        underlay-map: the rectified photo instead of R.json's `rectified`
  --cell-mm N        underlay-map: side of a measuring cell in mm (default 10)
  --pa-range LOW:HIGH gen combo/plateN: PA line range (default by material: PLA 0:0.10, PETG 0:0.15)
  --pa-step S        gen combo/plateN: PA line step (default 0.01; a fine pass: --pa-range 0.03:0.05
                     --pa-step 0.0025, labels like 42.5 = 0.0425)
  --filament-preset N gen (not shaper): the spool's slicer preset (user preset from the repo's
                     profiles/ or Orca's user folder, e.g. 'Orange PLA @K1SE'). Default with 3d-api
                     configured: the `slicer_filament_preset` of the spool bound to --printer
                     [--head T] (or of --spool ID), set with `3d printops spool preset`; no binding or
                     no preset there, or 3d-api down → error. Without 3d-api the flag is required
  --machine-preset N gen: machine preset (default: fleet `slicer.machine` of the printer)
  --process-preset N gen: process preset (default: fleet `slicer.process`). No preset → error,
                     never Orca's stock presets; the G-code ends with `; 3d calibrate presets: …`
  --json             print the API's JSON
  --api URL          3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN      bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d calibrate plan --printer u1
  3d calibrate plan --printer u1 --head T3 --spool 42 --all
  3d calibrate record pressure_advance --printer u1 --head T3 --values pressure_advance=0.035 --method pa_pattern
  3d calibrate record flow_ratio --printer k1se --head T1 --values flow_ratio=0.97 --note "tiles 3/5"
  3d calibrate record temp_tower --filament 7 --values nozzle_temp_min=205,nozzle_temp_best=215,nozzle_temp_max=230
  3d calibrate record filament_color --spool 12 --values hex=#C8902A,spoolman_hex=#F6C700 --method mix_tester_photo
  3d calibrate record mix_color --printer u1 --mix h0.12:f3.f12.f7 --values hex=#B5654F,predicted_hex=#A86A55
  3d calibrate check plate.gcode --printer u1
  3d calibrate check plate.gcode --printer u1 --accept calibration:flow_ratio --reason "test cube, flow irrelevant"
  3d calibrate plan --printer u1 --json | jq '.items[] | {head, kind, status, level}'
  3d calibrate gen pa --printer k1se --filament-preset 'Orange PLA @K1SE' -o k1se_pa_orange.gcode
  3d calibrate gen flow --printer k1se --filament-preset 'Black PLA babushkin @K1SE' -o k1se_flow_black.gcode
  3d calibrate gen combo --printer k1se --filament-preset 'Black PLA babushkin @K1SE' -o k1se_combo_black.gcode
  3d calibrate gen temp --printer k1se --filament-preset 'Orange PLA @K1SE' -o k1se_temp_orange.gcode
  3d calibrate gen plate1 --printer k1se --filament-preset 'Orange PLA @K1SE' --skip pa,flow -o k1se_plate1_orange.gcode
  3d calibrate gen plate1 --printer k1se --filament-preset 'Orange PLA @K1SE' --only pa,flow --pa-range 0.03:0.05 --pa-step 0.0025 -o k1se_pa_fine.gcode
  3d calibrate gen dense --printer k1se --filament-preset 'Orange PLA @K1SE' -o k1se_dense_orange.gcode
  3d calibrate gen dense2 --printer k1se --filament-preset 'Orange PLA @K1SE' -o k1se_dense2_orange.gcode
  3d calibrate gen plate1 --printer k1se --filament-preset 'Orange PLA @K1SE' --no-underlay -o k1se_plate1_bare.gcode
  3d calibrate gen plate1 --printer MULTI --filament-preset 'Orange PLA @K1SE' --color '#000000' --head-colours T1=#000000,T3=#2D9E59,T4=#FFFFFF --underlay-tool T4 --head T1 -o multi_plate1.gcode
  3d calibrate underlay-map k1se_plate1.json --rectify rect/rect.json --cell-mm 5 --json"""

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
            "--mix",
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
        ("--mix", "mix"),
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
    where += f" mix {rec['mix']}" if rec.get("mix") else ""
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
        values={"--printer", "--head", "--kind", "--mix", "--status", "--limit"} | COMMON_VALUES,
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
            mix=p.opts.get("--mix"),
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


def _float(raw: str, opt: str) -> float:
    try:
        value = float(raw)
    except ValueError as e:
        raise UsageError(
            f"{opt} wants a number, got {raw!r}", command=f"{PREFIX} gen"
        ) from e
    if value != value or value in (float("inf"), float("-inf")):
        raise UsageError(
            f"{opt} wants a finite number, got {raw!r}", command=f"{PREFIX} gen"
        )
    return value


def _pa_range(raw: str) -> tuple[float, float]:
    lo, sep, hi = raw.partition(":")
    if not sep:
        raise UsageError(
            f"--pa-range wants LOW:HIGH (e.g. 0.03:0.05), got {raw!r}",
            command=f"{PREFIX} gen",
        )
    return _float(lo, "--pa-range"), _float(hi, "--pa-range")


def _spool_preset(p: Parsed, entry: FleetPrinter) -> tuple[str, str] | None:
    """(preset, source) from the spool bound to the head via 3d-api; None when 3d-api is not
    configured (the flag stays required). A bound spool without a preset, no binding, or an
    unreachable 3d-api refuse: the stock preset is never the fallback (#294)."""
    from printops.client import ApiClient, ApiConfigError, ApiError, load_config

    try:
        url, token = load_config(api=p.opts.get("--api"), token=p.opts.get("--token"))
    except ApiConfigError as e:  # a broken printops.yaml is an error, not "not configured"
        raise UsageError(str(e), command=f"{PREFIX} gen") from e
    if not url:  # 3d-api not configured: the flag stays required
        return None
    head, spool_opt = p.opts.get("--head"), p.opts.get("--spool")
    where = f"{entry.name}/{head}" if head else entry.name
    try:
        data = ApiClient(url, token).get(
            "/v1/calibrations/slicer-preset",
            printer=entry.name,
            head=head,
            spool=_int(spool_opt, "--spool", "gen") if spool_opt else None,
        )
    except ApiError as e:
        fix = {
            0: "3d-api is unreachable (check PRINTOPS_API and that it runs)",
            401: "set PRINTOPS_TOKEN (or `token:` in ~/.config/3d-cli/printops.yaml)",
            403: "the token lacks the `read` scope",
        }.get(e.status, "fix the binding (`3d printops slots`)")
        raise UsageError(
            f"no filament preset for {where}: 3d-api: {e.message}",
            command=f"{PREFIX} gen",
            remediation=[f"Pass --filament-preset NAME, or {fix}."],
        ) from e
    bound_note = f"bound to {where}" if data["bound"] else ""
    if not data.get("preset"):
        raise UsageError(
            f"spool #{data['spool_id']} carries no slicer filament preset" + (f" ({bound_note})" if bound_note else ""),
            command=f"{PREFIX} gen",
            remediation=[
                f"3d printops spool preset {data['spool_id']} 'PRESET NAME'  (then re-run), "
                "or pass --filament-preset NAME."
            ],
        )
    source = f"3d-api spool #{data['spool_id']} slicer_filament_preset on {data['scope']}"
    return str(data["preset"]), source + (f", {bound_note}" if bound_note else "")


def _preset_choice(p: Parsed, entry: FleetPrinter) -> PresetChoice:
    """--machine/--process/--filament-preset, else the fleet entry's `slicer:` presets; the
    filament preset defaults to the spool bound to the head (3d-api), never to a stock preset.
    Missing → usage error."""
    from slicing.calibration_gen import NO_PRESETS_HINT, PresetChoice

    names: dict[str, str] = {}
    sources: list[tuple[str, str]] = []
    for kind in ("machine", "process", "filament"):
        flag = f"--{kind}-preset"
        if flag in p.opts:
            names[kind] = p.opts[flag]
            sources.append((kind, flag))
        elif kind in entry.slicer:
            names[kind] = entry.slicer[kind]
            sources.append((kind, f"fleet {entry.name}.slicer"))
        elif kind == "filament" and "machine" in names and "process" in names and (found := _spool_preset(p, entry)):
            # asked last: a missing machine/process preset is reported before 3d-api is bothered
            names[kind], source = found
            sources.append((kind, source))
    missing = [k for k in ("machine", "process", "filament") if k not in names]
    if missing:
        raise UsageError(
            f"no {', '.join(missing)} preset for {entry.name}: {NO_PRESETS_HINT}",
            command=f"{PREFIX} gen",
        )
    return PresetChoice(
        names["machine"], names["process"], names["filament"], tuple(sources)
    )


U1_MODEL = "Snapmaker U1"
U1_PLATE = "plate1"  # the U1's plate: one Orca object per test, white and black parts (u1_underlay.py)


def u1_plate_args(
    out: str, opts: dict[str, str], flags: set[str], entry: FleetPrinter, date: str | None = None
) -> list[str]:
    """argv of lib/slicing/calibration_plates/u1_underlay.py for `gen plate1 --printer <U1>`. The
    U1's plate is its own generator (the plate engine prints with one tool and the U1 needs a
    contrasting head for the underlay): it takes the underlay's layers from the policy, the head
    from the contrast check or `--underlay-tool`, and what is loaded on T1..T4 from
    `--head-colours`."""
    cmd = f"{PREFIX} gen"
    if "--no-underlay" in flags:
        raise UsageError(
            f"--no-underlay: the {entry.name} plate is the underlay plate; the bare bed needs the K1 engine",
            command=cmd,
        )
    for flag in ("--only", "--skip", "--pa-range", "--pa-step", "--nozzle-temp", "--material", "--machine-preset", "--process-preset", "--filament-preset"):
        if flag in opts:
            raise UsageError(
                f"{flag}: the {entry.name} plate has its own tests and presets (Snapmaker PLA SnapSpeed @U1)",
                command=cmd,
            )
    args = ["-o", out]
    if date:
        args += ["--date", date]
    if "--underlay-tool" in opts:
        args += ["--underlay-tool", opts["--underlay-tool"]]
    if "--head-colours" in opts:
        colours = _head_colours(opts["--head-colours"])
        labels = [h.label for h in entry.heads]
        missing = [label for label in labels if label not in colours]
        unknown = sorted(set(colours) - set(labels))
        if missing or unknown:
            raise UsageError(
                f"--head-colours needs every head of {entry.name} ({', '.join(labels)}): "
                + (f"missing {', '.join(missing)}" if missing else f"no such head {', '.join(unknown)}"),
                command=cmd,
            )
        args += ["--slot-colours", ",".join(colours[label] for label in labels)]
    return args


def _gen_u1(out: str, opts: dict[str, str], flags: set[str], entry: FleetPrinter, slicer: str) -> int:
    """Run the U1 plate generator (real Snapmaker Orca slice, tool change underlay head → T1)."""
    import os
    import subprocess

    from cli.env import repo_root
    from cli.pyrun import tool_argv

    args = u1_plate_args(out, opts, flags, entry)
    env = dict(os.environ)
    env["SLICER"] = slicer
    res = Path(slicer).parent / "resources" / "profiles"
    if "ORCA_RES" not in env and res.is_dir():
        env["ORCA_RES"] = str(res)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(repo_root()) / "lib"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    script = "slicing/calibration_plates/u1_underlay.py"
    argv = tool_argv("trimesh,numpy,pillow,manifold3d", script, args)
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise UsageError(
            f"the {entry.name} plate failed (exit {proc.returncode}): "
            + ((proc.stderr or proc.stdout).strip().splitlines() or ["no output"])[-1],
            command=f"{PREFIX} gen",
        )
    for suffix in (".gcode", ".json", ".png", ".3mf"):
        path = Path(out).with_suffix(suffix)
        if path.exists():
            print(f"wrote {path}")
    print(f"  {entry.name}: underlay head and layers in the map's `underlay` block; next: 3d print-preflight {out} --printer {entry.name}")
    return 0


def _head_colours(raw: str) -> dict[str, str]:
    """`T1=#000000,T4=#FFFFFF` → {label: colour}."""
    out: dict[str, str] = {}
    for part in filter(None, (x.strip() for x in raw.split(","))):
        label, _, colour = part.partition("=")
        if not label or not re.fullmatch(r"#[0-9A-Fa-f]{6}", colour):
            raise UsageError(
                f"--head-colours wants T1=#RRGGBB,… , got {part!r}",
                command=f"{PREFIX} gen",
            )
        out[label.strip()] = colour.upper()
    return out


def _underlay_plan(
    kind: str, entry: FleetPrinter, opts: dict[str, str], flags: set[str], color: str
) -> tuple[int | None, UnderlayHeads | None]:
    """(layers of the plate's shared underlay, the two heads of a multi-head plate) — #320.
    One head: exactly one layer, no tool change. Several heads: `calibrations.underlay_layers`
    (1–2) in the head that contrasts most with the tests' (#263 check), or `--underlay-tool`.
    `--no-underlay`: the bare bed (layers None)."""
    from slicing.calibration_gen import all_plates
    from slicing.calibration_plate import UnderlayHeads
    from slicing.calibration_underlay import layers_for, plan_heads

    cmd = f"{PREFIX} gen"
    no_underlay = "--no-underlay" in flags
    if kind not in all_plates():
        for flag in ("--underlay-tool", "--head-colours"):
            if flag in opts:
                raise UsageError(
                    f"{flag}: only the plates have an underlay, not {kind!r}",
                    command=cmd,
                )
        if no_underlay:
            raise UsageError(
                f"--no-underlay: {kind!r} is a single test (it declares `underlay: cutout`), only the "
                "plates have an underlay",
                command=cmd,
            )
        return 1, None
    if no_underlay or len(entry.heads) <= 1:
        why = (
            "--no-underlay has no underlay head"
            if no_underlay
            else f"{entry.name} has one head, the underlay is the tests' filament"
        )
        for flag in ("--underlay-tool", "--head-colours"):
            if flag in opts:
                raise UsageError(f"{flag}: {why}", command=cmd)
        return (None if no_underlay else 1), None
    from printops import policy
    from slicing.calibration_gen import GenError

    try:
        layers = layers_for(
            len(entry.heads), policy.section("calibrations").underlay_layers
        )
        tests = opts.get("--head", entry.heads[0].label)
        colours = _head_colours(opts.get("--head-colours", ""))
        if color:
            colours.setdefault(tests, color.upper())
        label, tool, delta_e = plan_heads(
            [(h.label, h.index) for h in entry.heads],
            tests,
            colours,
            opts.get("--underlay-tool"),
        )
        tests_tool = f"T{next(h.index for h in entry.heads if h.label == tests)}"
    except (GenError, policy.PolicyError) as e:
        raise UsageError(str(e), command=cmd) from e
    return layers, UnderlayHeads(label, tool, tests, tests_tool, delta_e)


def _read_json(path: Path, label: str, cmd: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise InputNotFound(f"{label} {path}: {e.strerror or e}", command=cmd) from e
    except ValueError as e:
        raise UsageError(f"{label} {path} is not JSON: {e}", command=cmd) from e


def _underlay_map(argv: list[str]) -> int:
    """`calibrate underlay-map PLATE.json --rectify R.json`: the bare underlay of a plate photo as the
    first-layer map, with bed-flatness and z-offset hints (lib/calib_underlay_map.py through pyrun)."""
    import subprocess

    from cli.pyrun import tool_argv
    from printops.cli._common import parse

    p = parse(
        argv,
        "underlay-map",
        values={"--rectify", "--image", "--cell-mm"},
        flags={"--json"},
        n_pos=1,
        prefix=PREFIX,
    )
    cmd = f"{PREFIX} underlay-map"
    manifest_path = Path(p.pos[0])
    if "--rectify" not in p.opts:
        raise UsageError(
            "--rectify R.json (the `calibrate rectify` output) is required", command=cmd
        )
    manifest = _read_json(manifest_path, "manifest", cmd)
    rect = _read_json(Path(p.opts["--rectify"]), "rectify", cmd)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("underlay"), dict):
        raise UsageError(
            f"{manifest_path} has no `underlay` block: the plate was generated on the bare bed "
            "(--no-underlay) or is not a calib-plate/1 manifest, so there is no bare underlay to read",
            command=cmd,
        )
    image_path = Path(
        p.opts.get("--image")
        or (rect.get("rectified") if isinstance(rect, dict) else None)
        or ""
    )
    if not image_path.is_file():
        raise InputNotFound(
            f"rectified photo {image_path or '(none)'}: pass --image or a `calibrate rectify` JSON with `rectified`",
            command=cmd,
        )
    args = [
        str(image_path),
        "--manifest",
        str(manifest_path),
        "--rectify",
        p.opts["--rectify"],
    ]
    if "--cell-mm" in p.opts:
        args += ["--cell-mm", f"{_float(p.opts['--cell-mm'], '--cell-mm'):g}"]
    proc = subprocess.run(
        tool_argv("numpy,pillow", "calib_underlay_map.py", args),
        capture_output=True,
        text=True,
    )
    try:
        result: dict[str, Any] = json.loads(proc.stdout)
    except ValueError as e:
        raise UsageError(
            f"cannot read the underlay: {proc.stderr.strip()[-400:] or e}", command=cmd
        ) from e
    if proc.returncode == 2:
        raise UsageError(str(result.get("reason")), command=cmd)
    if "--json" in p.flags:
        print(json.dumps(result, indent=1))
    else:
        u = result["uniformity"] or {}
        print(
            f"underlay map: {result['status']}, {result['cells_read']}/{result['cells_total']} cells of "
            f"{result['cell_mm']:g} mm, {result['light']} light"
        )
        if u:
            g = u["gradient_per_100mm"]
            print(
                f"  uniformity: cv {u['mean_cv']:.3f}, gradient per 100 mm x {g['x']:+.3f} y {g['y']:+.3f}"
            )
        for h in result["hints"]:
            where = f" at {h['bbox_mm']}" if "bbox_mm" in h else ""
            print(
                f"  hint {h['kind']}{where}: {h['note']} (confidence {h['confidence']:.2f})"
            )
        if result["status"] != "proposed":
            print(f"  {result['reason']}")
    return 0 if result["status"] == "proposed" else 1


def _ids(raw: str) -> frozenset[str]:
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _gen(argv: list[str]) -> int:
    from printops.cli._common import parse

    p = parse(
        argv,
        "gen",
        values={
            "--printer",
            "--material",
            "--color",
            "--nozzle-temp",
            "-o",
            "--out",
            "--only",
            "--skip",
            "--pa-range",
            "--pa-step",
            "--machine-preset",
            "--process-preset",
            "--filament-preset",
            "--head",
            "--spool",
            "--underlay-tool",
            "--head-colours",
        }
        | COMMON_VALUES,
        flags={"--no-underlay"},
        n_pos=1,
        prefix=PREFIX,
    )
    from slicing.calibration_gen import GenError, all_kinds, generate

    kind = p.pos[0]
    if kind not in all_kinds():
        raise UsageError(
            f"unknown kind {kind!r} (accepted: {', '.join(all_kinds())})",
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
    if entry.model == U1_MODEL:
        if kind != U1_PLATE:
            raise UsageError(
                f"{entry.name} ({U1_MODEL}): `gen` makes its {U1_PLATE} only (the single tests and the dense plates "
                "are laid out for the K1 SE)",
                command=f"{PREFIX} gen",
            )
        if slicer is None or slicer[0] not in ("orca", "custom"):
            raise UsageError(
                "`calibrate gen` slices with the Snapmaker Orca CLI; none found (set SLICER=/path/to/orca)",
                command=f"{PREFIX} gen",
            )
        return _gen_u1(out, p.opts, p.flags, entry, slicer[1])
    # A forced SLICER ("custom") is taken as an Orca-family binary: the generator needs Orca's CLI.
    if kind != "shaper" and (slicer is None or slicer[0] not in ("orca", "custom")):
        raise UsageError(
            "`calibrate gen` slices with the Orca CLI; none found (set SLICER=/path/to/orca)",
            command=f"{PREFIX} gen",
        )
    layers, heads = _underlay_plan(kind, entry, p.opts, p.flags, color)
    try:
        res = generate(
            kind,
            entry.model,
            slicer[1] if slicer else "",
            Path(out),
            material=p.opts.get("--material", "PLA").upper(),
            color=color,
            temp=temp,
            only=_ids(p.opts.get("--only", "")),
            skip=_ids(p.opts.get("--skip", "")),
            pa_range=_pa_range(p.opts["--pa-range"])
            if "--pa-range" in p.opts
            else None,
            pa_step=_float(p.opts["--pa-step"], "--pa-step")
            if "--pa-step" in p.opts
            else None,
            presets=None if kind == "shaper" else _preset_choice(p, entry),
            underlay_layers=layers,
            heads=heads,
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
    "underlay-map": _underlay_map,
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
