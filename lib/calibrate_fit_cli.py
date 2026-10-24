"""calibrate_fit_cli.py — `3d calibrate fit plan|gen|quick|read|record|check`: the quick fit calibration.

WHAT: argument parsing and printing only (`calibrate.py` forwards `fit` here); the logic is in calibrate_fit.py
  (plan, result reader, pick, staleness), the geometry in fit_coupon.py / fit_coupon_export.py, the store in
  calibrate_fit_store.py. Every subcommand takes --json (schema `3d.calibrate_fit/1`); `record` and `quick` are
  dry runs until --yes.
INVARIANTS: stdlib-only at import (command discovery imports calibrate.py on every `3d` call); the heavy modules
  are imported inside the functions. Errors are structured (`errors.py`): bad input exit 2, a readout that cannot
  be used (all fused) exit 1 with the advice to widen the range, a missing OpenSCAD exit 127.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

if TYPE_CHECKING:
    from calibrate_fit import PairStatus, PrinterInfo, Reading
    from fit_coupon import Ladder

CMD = "calibrate fit"
SUBS = ("plan", "gen", "quick", "read", "record", "check")

USAGE = """3d calibrate fit plan|gen|quick|read|record|check  — quick fit calibration (issue #573)
  Print ONE small coupon (about 10 min), say which step fitted, and the fit table learns your printer and plastic:
  `3d fits resolve --printer P --material M` then shows `calibrated` with the date and the record id.
  Every subcommand takes --json (schema `3d.calibrate_fit/1`). record and quick change nothing without --yes.

  3d calibrate fit plan --printer P [--material M] [--fit snap,sliding,tight,press] [--shape round,slot]
                        [--nozzle MM] [--gap-offset MM]
  3d calibrate fit gen --printer P [--material M] --out coupon.3mf [--fit ..] [--shape ..] [--gap-offset MM]
  3d calibrate fit quick --printer P [--material M] [--out FILE.3mf] [--yes]
  3d calibrate fit read --printer P [--material M] --fit F --shape S (--result R | --measured D1,..,D7)
                        [--mating-mm MM] [--gap-offset MM] | --from-readout FILE.json
  3d calibrate fit record  (the same flags as read) [--head T1] [--spool ID | --filament ID] [--coupon NAME]
                        [--note TEXT] [--nozzle MM] [--yes]
  3d calibrate fit check [--printer P] [--material M] [--max-age-days N] [--nozzle MM]
  common: [--json]   3d-api: [--api URL] [--token TOKEN] (read/record/check)

Subcommands:
  plan    the coupon for a printer x material: 6 ladders of 7 steps (sliding/tight/press holes, sliding/snap/press
          slots) with their gaps around the fit table's value, footprint and bed fit, print time (<= ~10 min,
          estimate) and the exact readout instruction + `record` command per ladder.
  gen     write the coupon as NAME.3mf + .stl + .scad + .json (map) in bed coordinates for the printer (centred),
          rendered with OpenSCAD. For G-code with the printer's slicer presets use `3d calibrate gen fit`.
  quick   plan + gen in one command with the defaults (all ladders, nozzle and head of the printer, the printer's
          material): shows what it would write; --yes writes fit-coupon-P-M.3mf (and the .stl/.scad/.json).
  read    the RESULT READER: from `--result` words/step or `--measured` caliper numbers, pick the gap per ladder
          (the smallest `free` step; for press the largest `press` step), compute delta_mm against the fit table and
          show the clearance before/after. Changes nothing.
  record  read, then store a `fit_clearance` calibration record (3d-api, else the offline buffer) that
          `3d fits resolve`, `3d fit-check --fit` and `3d fit-niche --fit` pick up. Dry run unless --yes.
  check   per printer x material: MISSING (no record), STALE (older than --max-age-days, default 180, or taken with
          another nozzle), PARTIAL (a ladder never read) or OK. Exit 1 when any pair is MISSING or STALE.

Reading the coupon:
  Digit 1..7 = smallest to largest gap, the ladder number sits at the left of its row. Push the spare pin (round
  holes) or tab (slots) into each step and say, per step:
    free   goes in and out by hand without force          press  needs a push, then holds
    fused  will not go in at all (hole closed) or cracked
  --result step=N     N = the first `free` step (press ladders: the last step that still grips); smaller steps
                      are then `press`, larger ones `free`
  --result free,free,press,...   one word per step, smallest gap first; or `3=free,4=free` pairs
  --measured D1,..,D7  caliper diameters of the holes (widths of the slots) per step, in mm; --mating-mm is the
                      measured pin/tab (default: the nominal 4.00 / 2.00). A step reads `free` when its REAL gap
                      ((D - pin)/2) reaches the table's value.
  Odd readouts: two readings of one gap keep the tighter result; a looser step below a tighter one is recorded
  with a non-monotonic warning; no `free` step at all (all fused) stops with exit 1 - widen the range with
  `3d calibrate fit gen --gap-offset 0.1` and read the new coupon (the same --gap-offset goes to record).

Options:
  --printer P        fleet key (k1se, u1) or printer model; the bed, nozzle and head come from the registries
  --material M       PLA, PETG, ... (default: the printer model's material)
  --fit LIST         snap, sliding, tight, press (plan/gen/quick: comma list; read/record: one)
  --shape LIST       round, slot (plan/gen/quick: comma list; read/record: one)
  --gap-offset MM    shift every gap of the coupon (negative = tighter); give record the same value
  --nozzle MM        nozzle diameter if not the head's
  --out FILE         gen/quick: coupon.3mf (the other formats are written next to it)
  --result R         step=N | one word per step | K=word pairs
  --measured LIST    caliper numbers per step
  --from-readout F   JSON `{"printer","material","ladders":[{"fit","shape","result"|"measured"}]}`
  --head T           head of the record (default: the printer's first usable head)
  --spool ID | --filament ID  the Spoolman spool/filament the coupon was printed with (default: the spool bound
                      to the head)
  --coupon NAME      free text kept with the record (which coupon was printed)
  --max-age-days N   check: older records are STALE (default 180)
  --yes              record/quick: perform the write (dry run without it)

Examples:
  3d calibrate fit plan --printer k1se --material PLA --json
  3d calibrate fit gen --printer k1se --material PLA --out coupon.3mf
  3d calibrate fit quick --printer k1se --material PLA --yes
  3d calibrate fit read --printer k1se --material PLA --fit sliding --shape round --result step=3
  3d calibrate fit record --printer k1se --material PLA --fit sliding --shape round --result step=3 --yes
  3d calibrate fit record --printer k1se --material PLA --fit tight --shape round --measured 4.02,4.07,4.12,4.17,4.22,4.27,4.32 --yes
  3d calibrate fit record --printer k1se --material PLA --from-readout readout.json --yes
  3d calibrate fit check --printer k1se --material PLA
  3d calibrate fit check --printer k1se --material PLA --nozzle 0.6 --max-age-days 90 --api http://192.168.0.39:8765
  3d calibrate fit record --printer k1se --material PLA --fit snap --shape slot --result step=5 --filament 7 --yes
  3d calibrate fit plan --printer u1 --material PETG --fit sliding,snap --shape slot
  3d calibrate fit gen --printer k1se --material PLA --out wide.3mf --fit tight --gap-offset 0.3
  3d calibrate fit read --printer k1se --material PLA --fit sliding --shape round --measured 4.1,4.2,4.3,4.4,4.5,4.6,4.7 --mating-mm 4.05
  3d calibrate fit record --printer k1se --material PLA --fit press --shape round --result step=3 --spool 12 --coupon "k1se-pla-2026-10-24" --note "Elegoo Rapid PLA+ grey" --head T1 --yes
  3d fits resolve --fit sliding --shape round --printer k1se --material PLA"""


class _Parser(argparse.ArgumentParser):
    def __init__(self, sub: str) -> None:
        super().__init__(prog=f"3d calibrate fit {sub}", add_help=False)
        self._sub = sub

    def error(self, message: str) -> NoReturn:
        raise UsageError(
            message,
            command=f"{CMD} {self._sub}",
            remediation=["See `3d calibrate --help` (section `calibrate fit`)."],
        )


def _emit(doc: dict[str, Any]) -> None:
    import fit_coupon

    print(json.dumps({"schema": fit_coupon.SCHEMA, **doc}, indent=2))


def _offset(value: float | None) -> float:
    return 0.0 if value is None else round(float(value), 4)


def _plan_args(p: _Parser) -> None:
    p.add_argument("--printer")
    p.add_argument("--material")
    p.add_argument("--fit")
    p.add_argument("--shape")
    p.add_argument("--nozzle", type=float)
    p.add_argument("--gap-offset", type=float)
    p.add_argument("--json", action="store_true")


def _context(a: argparse.Namespace) -> tuple[PrinterInfo, str, tuple[Ladder, ...]]:
    import calibrate_fit as cf
    import fit_coupon

    if not a.printer:
        raise UsageError("--printer is required", command=f"{CMD} {a.sub}")
    info = cf.printer_info(a.printer, nozzle_mm=a.nozzle)
    material = cf.material_name(a.material or info.default_material or "PLA")
    fit_types = cf.csv(a.fit, sorted({f for f, _ in fit_coupon.LADDER_SPECS}), "--fit")
    shapes = cf.csv(a.shape, ["round", "slot"], "--shape")
    return info, material, cf.select_ladders(fit_types, shapes, _offset(a.gap_offset))


def _print_plan(doc: dict[str, Any]) -> None:
    c = doc["coupon"]
    e = doc["estimate"]
    print(
        f"fit coupon for {doc['printer']} ({doc['model']}, bed {doc['bed_mm'][0]:g}x{doc['bed_mm'][1]:g}) x "
        f"{doc['material']}, nozzle {doc['nozzle_mm']:g} mm"
    )
    print(
        f"  {c['footprint_mm'][0]:g} x {c['footprint_mm'][1]:g} mm, "
        f"{'fits the bed' if c['fits_bed'] else 'DOES NOT FIT the bed'}; ~{e['minutes']:g} min, ~{e['grams']:g} g PLA "
        "(estimate, without heating)"
    )
    for k in doc["coupons"]:
        gaps = " ".join(f"{s['gap_mm']:g}" for s in k["steps"])
        print(
            f"  {k['ladder']}  {k['fit_type']}/{k['shape']:<5} table {k['table_clearance_mm']:g} mm  gaps {gaps}"
        )
        print(f"       {k['readout']}")
    for line in doc["next"]:
        print(f"  next: {line}")


# --------------------------------------------------------------------------- plan / gen / quick
def _plan(argv: list[str]) -> int:
    import calibrate_fit as cf

    p = _Parser("plan")
    _plan_args(p)
    a = p.parse_args(argv)
    a.sub = "plan"
    info, material, ladders = _context(a)
    doc = cf.plan_doc(info, material, ladders, _offset(a.gap_offset))
    if a.json:
        _emit(doc)
    else:
        _print_plan(doc)
    return 0


def _build(
    a: argparse.Namespace, *, write: bool, out: Path
) -> tuple[dict[str, Any], list[Path]]:
    import calibrate_fit as cf
    import fit_coupon
    import fit_coupon_export as export

    info, material, ladders = _context(a)
    offset = _offset(a.gap_offset)
    doc = cf.plan_doc(info, material, ladders, offset)
    size = fit_coupon.layout(ladders).size
    if not doc["coupon"]["fits_bed"]:
        raise GateFailure(
            f"the coupon ({size[0]:g}x{size[1]:g} mm) does not fit the {info.bed_mm[0]:g}x{info.bed_mm[1]:g} bed "
            f"with a {fit_coupon.MARGIN_MM:g} mm margin",
            command=f"{CMD} {a.sub}",
            remediation=["Print fewer ladders: --fit sliding,tight or --shape round."],
        )
    origin = fit_coupon.centred(size, (info.bed_mm[0], info.bed_mm[1]))
    lay = fit_coupon.layout(ladders, origin)
    doc["coupon"]["origin_mm"] = list(origin)
    doc["coupon"]["objects"] = [
        {"name": o.name, "bbox_mm": [round(v, 3) for v in o.bbox]} for o in lay.objects
    ]
    stem = out.with_suffix("")
    doc["files"] = [str(stem.with_suffix(f".{f}")) for f in export.FORMATS]
    if not write:
        return doc, []
    title = f"fit coupon {info.name} {material} nozzle {info.nozzle_mm:g}"
    paths, meshes = export.export(
        lay,
        out,
        title=title,
        metadata={
            "Printer": info.model,
            "PrinterKey": info.name,
            "Material": material,
            "Nozzle": f"{info.nozzle_mm:g}",
            "GapOffset": f"{offset:g}",
            "Head": info.head or "",
        },
        doc=doc,
    )
    doc["written"] = [str(p) for p in paths]
    return doc, paths


def _gen(argv: list[str]) -> int:
    p = _Parser("gen")
    _plan_args(p)
    p.add_argument("--out", "-o")
    a = p.parse_args(argv)
    a.sub = "gen"
    if not a.out:
        raise UsageError("--out is required, e.g. --out coupon.3mf", command=f"{CMD} gen")
    doc, paths = _build(a, write=True, out=Path(a.out))
    doc["command"] = "gen"
    if a.json:
        _emit(doc)
        return 0
    for path in paths:
        print(f"wrote {path}")
    c = doc["coupon"]
    print(
        f"  {len(c['objects'])} objects centred on the bed of {doc['printer']} "
        f"({c['footprint_mm'][0]:g} x {c['footprint_mm'][1]:g} mm, origin {c['origin_mm'][0]:g},{c['origin_mm'][1]:g}), "
        f"~{doc['estimate']['minutes']:g} min estimated"
    )
    print(f"  next: slice {Path(a.out).with_suffix('.3mf')} with the printer's profile (3d prep / 3d arrange / 3d retarget),")
    print(
        f"        then `3d calibrate fit plan --printer {doc['printer']} --material {doc['material']}` has the readout of every ladder"
    )
    return 0


def _quick(argv: list[str]) -> int:
    p = _Parser("quick")
    _plan_args(p)
    p.add_argument("--out", "-o")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    a.sub = "quick"
    if not a.printer:
        raise UsageError("--printer is required", command=f"{CMD} quick")
    # the default name carries printer and material, resolved like the coupon's own
    import calibrate_fit as cf

    info = cf.printer_info(a.printer, nozzle_mm=a.nozzle)
    material = cf.material_name(a.material or info.default_material or "PLA")
    out = Path(a.out) if a.out else Path(f"fit-coupon-{info.name}-{material.lower()}.3mf")
    doc, paths = _build(a, write=a.yes, out=out)
    doc["command"] = "quick"
    doc["dry_run"] = not a.yes
    if a.json:
        _emit(doc)
        return 0
    _print_plan(doc)
    if a.yes:
        for path in paths:
            print(f"wrote {path}")
    else:
        names = [out.name, *(Path(f).name for f in doc["files"] if Path(f).name != out.name)]
        print(f"dry run: would write {', '.join(names)}; add --yes")
    return 0


# --------------------------------------------------------------------------- read / record
def _read_args(p: _Parser, *, record: bool) -> None:
    p.add_argument("--printer")
    p.add_argument("--material")
    p.add_argument("--fit")
    p.add_argument("--shape")
    p.add_argument("--result")
    p.add_argument("--measured")
    p.add_argument("--mating-mm", type=float)
    p.add_argument("--from-readout")
    p.add_argument("--nozzle", type=float)
    p.add_argument("--gap-offset", type=float)
    p.add_argument("--api")
    p.add_argument("--token")
    p.add_argument("--json", action="store_true")
    if record:
        p.add_argument("--head")
        p.add_argument("--spool", type=int)
        p.add_argument("--filament", type=int)
        p.add_argument("--coupon")
        p.add_argument("--note")
        p.add_argument("--yes", action="store_true")


def _numbers(text: str, flag: str) -> list[float]:
    tokens = text.replace(",", " ").split()
    try:
        return [float(t) for t in tokens]
    except ValueError as e:
        raise UsageError(f"{flag}: {e}", command=CMD) from None


def _file_ladders(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    p = Path(path)
    if not p.is_file():
        raise InputNotFound(path, command=f"{CMD} read")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise UsageError(f"{path}: not JSON ({e})", command=f"{CMD} read") from e
    ladders = doc.get("ladders") if isinstance(doc, dict) else None
    if not isinstance(ladders, list) or not ladders or not all(isinstance(x, dict) for x in ladders):
        raise UsageError(
            f"{path}: needs a `ladders` list of {{fit, shape, result|measured}} objects",
            command=f"{CMD} read",
        )
    return doc, ladders


def _readings(
    ladder: Ladder, spec: dict[str, Any], mating: float | None
) -> list[Reading]:
    import calibrate_fit as cf

    if spec.get("measured") is not None:
        nums = spec["measured"]
        measured = nums if isinstance(nums, list) else _numbers(str(nums), "--measured")
        return cf.from_measured([float(x) for x in measured], ladder, spec.get("mating_mm", mating))
    result = spec.get("result")
    if result is None:
        raise UsageError(
            f"{ladder.id}: pass --result (step=N or one word per step) or --measured",
            command=f"{CMD} read",
        )
    return cf.parse_result(",".join(result) if isinstance(result, list) else str(result), ladder)


def _before_after(
    info: PrinterInfo, material: str, picks: Sequence[Any], readings_all: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    import fits
    import fits_sources

    out = []
    measured_at = _now()
    for pk in picks:
        rows = fits.corrections_from_fit_clearance(
            [r for r in readings_all if r["fit_type"] == pk.fit_type and r["shape"] == pk.shape],
            printer=info.name,
            material=material,
            nozzle_mm=info.nozzle_mm,
            measured_at=measured_at,
            note="this readout",
        )
        before = fits_sources.resolve(
            pk.fit_type, pk.shape, info.name, material, info.nozzle_mm, command=CMD
        )
        existing = fits_sources.collect(
            fits_sources.Query(info.name, material, info.nozzle_mm, None)
        )
        after = fits.resolve(
            pk.fit_type,
            pk.shape,
            info.name,
            material,
            info.nozzle_mm,
            extra_corrections=[*existing, *rows],
            command=CMD,
        )
        out.append(
            {
                "fit_type": pk.fit_type,
                "shape": pk.shape,
                "before_mm": before.clearance_mm,
                "after_mm": after.clearance_mm,
                "change_mm": round(after.clearance_mm - before.clearance_mm, 4),
                "before_status": _status(before),
                "after_status": _status(after),
            }
        )
    return out


def _status(res: Any) -> str:
    entry: dict[str, Any] = next((p for p in res.provenance if p["kind"] == "correction"), {})
    return str(entry.get("status", "?"))


def _now() -> str:
    import calibrate_fit as cf

    return cf.utc_now()


def _gather(a: argparse.Namespace, sub: str) -> tuple[
    PrinterInfo, str, list[tuple[Ladder, list[Reading], Any]]
]:
    import calibrate_fit as cf
    import fit_coupon

    file_doc: dict[str, Any] = {}
    specs: list[dict[str, Any]]
    if a.from_readout:
        if a.result or a.measured or a.fit or a.shape:
            raise UsageError(
                "--from-readout carries the ladders itself: drop --fit/--shape/--result/--measured",
                command=f"{CMD} {sub}",
            )
        file_doc, specs = _file_ladders(a.from_readout)
        for spec in specs:
            if "fit" not in spec or "shape" not in spec:
                raise UsageError(f"{a.from_readout}: every ladder needs `fit` and `shape`", command=f"{CMD} {sub}")
    else:
        if not a.fit or not a.shape:
            raise UsageError(
                f"`{sub}` needs --fit and --shape (or --from-readout), e.g. --fit sliding --shape round",
                command=f"{CMD} {sub}",
            )
        spec = {"fit": a.fit, "shape": a.shape}
        if a.measured:
            spec["measured"] = _numbers(a.measured, "--measured")
        if a.result:
            spec["result"] = a.result
        if a.measured and a.result:
            raise UsageError("pass --result or --measured, not both", command=f"{CMD} {sub}")
        specs = [spec]
    printer = a.printer or file_doc.get("printer")
    if not printer:
        raise UsageError("--printer is required", command=f"{CMD} {sub}")
    info = cf.printer_info(str(printer), nozzle_mm=a.nozzle or file_doc.get("nozzle_mm"))
    material = cf.material_name(
        a.material or file_doc.get("material") or info.default_material or "PLA"
    )
    offset = _offset(a.gap_offset if a.gap_offset is not None else file_doc.get("gap_offset_mm"))
    fit_names = sorted({f for f, _ in fit_coupon.LADDER_SPECS})
    out: list[tuple[Ladder, list[Reading], Any]] = []
    for spec in specs:
        fit, shape = str(spec["fit"]), str(spec["shape"])
        if fit not in fit_names:
            raise InvalidArgument("--fit", fit, fit_names, command=f"{CMD} {sub}")
        if shape not in ("round", "slot"):
            raise InvalidArgument("--shape", shape, ["round", "slot"], command=f"{CMD} {sub}")
        ladder = next(iter(cf.select_ladders((fit,), (shape,), offset)), None)
        if ladder is None:
            raise UsageError(
                f"no {fit}/{shape} ladder on the coupon",
                command=f"{CMD} {sub}",
                remediation=["Ladders: " + ", ".join(f"{f}/{s}" for f, s in fit_coupon.LADDER_SPECS)],
            )
        out.append((ladder, _readings(ladder, spec, a.mating_mm), spec))
    return info, material, out


def _pick_report(
    a: argparse.Namespace, sub: str, info: PrinterInfo, material: str,
    items: list[tuple[Ladder, list[Reading], Any]],
) -> tuple[list[Any], dict[str, Any]]:
    import calibrate_fit as cf

    picks = [cf.pick(rs, ladder.fit_type, ladder.shape) for ladder, rs, _ in items]
    doc: dict[str, Any] = {
        "command": sub,
        "printer": info.name,
        "material": material,
        "nozzle_mm": info.nozzle_mm,
        "picks": [p.to_dict() for p in picks],
    }
    return picks, doc


def _print_picks(doc: dict[str, Any]) -> None:
    print(f"{doc['printer']} x {doc['material']} (nozzle {doc['nozzle_mm']:g} mm)")
    ba = {(x["fit_type"], x["shape"]): x for x in doc.get("before_after", [])}
    for pk in doc["picks"]:
        head = f"  {pk['fit_type']}/{pk['shape']}:"
        if not pk["ok"]:
            print(f"{head} NOT USABLE - {pk['problem']}")
            continue
        line = f"{head} gap {pk['gap_mm']:g} mm (table {pk['table_mm']:g}) -> delta {pk['delta_mm']:+g} mm"
        x = ba.get((pk["fit_type"], pk["shape"]))
        if x:
            line += (
                f"; clearance {x['before_mm']:g} ({x['before_status']}) -> {x['after_mm']:g} mm "
                f"({x['after_status']})"
            )
        print(line)
    for pk in doc["picks"]:
        for w in pk["warnings"]:
            print(f"  warning ({pk['fit_type']}/{pk['shape']}): {w}")


def _read(argv: list[str]) -> int:
    p = _Parser("read")
    _read_args(p, record=False)
    a = p.parse_args(argv)
    info, material, items = _gather(a, "read")
    picks, doc = _pick_report(a, "read", info, material, items)
    return _finish(a, "read", doc, picks, info, material, items, store=None)


def _finish(
    a: argparse.Namespace,
    sub: str,
    doc: dict[str, Any],
    picks: list[Any],
    info: PrinterInfo,
    material: str,
    items: list[tuple[Ladder, list[Reading], Any]],
    *,
    store: dict[str, Any] | None,
) -> int:
    bad = [p for p in picks if not p.ok]
    if not bad and "before_after" not in doc:
        readings = [r.to_dict() for _, rs, _ in items for r in rs]
        doc["before_after"] = _before_after(info, material, picks, readings)
    if store is not None:
        doc.update(store)
    if a.json:
        doc["ok"] = not bad
        _emit(doc)
    else:
        _print_picks(doc)
        if store is not None:
            _print_store(store)
    if bad:
        raise GateFailure(
            f"{', '.join(f'{p.fit_type}/{p.shape}' for p in bad)}: the readout cannot be used",
            command=f"{CMD} {sub}",
            silent=a.json,
            remediation=[p.problem for p in bad if p.problem],
        )
    return 0


def _print_store(store: dict[str, Any]) -> None:
    if store.get("stored"):
        s = store["stored"]
        print(f"recorded {s['id']} in {s['origin']} ({s['where']})")
        if s["origin"] == "buffer":
            print(
                "  3d-api was not configured/reachable: the record is in the offline buffer, `3d fits resolve` already "
                "reads it; replay it into 3d-api with `3d settings import --spool "
                f"{s['uid']}=<spool id>` when it is back"
            )
    elif store.get("dry_run"):
        print(f"dry run: would store a fit_clearance record in {store['destination']}; add --yes")


def _record(argv: list[str]) -> int:
    import calibrate_fit as cf
    import calibrate_fit_store as store

    p = _Parser("record")
    _read_args(p, record=True)
    a = p.parse_args(argv)
    info, material, items = _gather(a, "record")
    picks, doc = _pick_report(a, "record", info, material, items)
    if any(not pk.ok for pk in picks):
        return _finish(a, "record", doc, picks, info, material, items, store=None)
    client = store.api_client(a.api, a.token)
    existing = store.read_all(client, warn=False)
    scope = next(
        (
            r
            for r in existing.records
            if any(cf._same(r.printer, n) for n in info.names)
            and cf._same(r.material, material)
            and (r.nozzle_mm is None or r.nozzle_mm == info.nozzle_mm)
        ),
        None,
    )
    fresh = [r for pk in picks for r in pk.readings]
    merged = cf.merge_readings(scope.readings if scope else [], fresh)
    unreachable = any("unreachable" in n for n in existing.notes)
    use_api = client is not None and not unreachable
    destination = (
        f"3d-api {client.base_url}"
        if use_api and client is not None
        else f"the offline buffer ({_buffer_hint(info.name)})"
    )
    method = "caliper" if any(spec.get("measured") is not None for _, _, spec in items) else "fit_coupon"
    result: dict[str, Any] = {
        "dry_run": not a.yes,
        "destination": destination,
        "replaces": scope.id if scope else None,
        "kept_readings": len(merged) - len(fresh),
    }
    # the clearance BEFORE the write: afterwards the stored record is part of "before"
    doc["before_after"] = _before_after(
        info, material, picks, [r.to_dict() for _, rs, _ in items for r in rs]
    )
    if a.yes:
        written = store.write(
            store.NewRecord(
                info.name,
                a.head or info.head,
                material,
                info.nozzle_mm,
                merged,
                method=method,
                coupon=a.coupon,
                note=a.note,
                spool_id=a.spool,
                filament_id=a.filament,
            ),
            client if use_api else None,
        )
        result["stored"] = {
            "id": written.id,
            "origin": written.origin,
            "where": written.where,
            "uid": written.uid,
        }
        result["dry_run"] = False
    return _finish(a, "record", doc, picks, info, material, items, store=result)


def _buffer_hint(printer: str) -> str:
    from printops import history_buffer

    return str(history_buffer.buffer_dir() / f"{printer}.jsonl")


# --------------------------------------------------------------------------- check
def _check(argv: list[str]) -> int:
    import calibrate_fit as cf
    import calibrate_fit_store as store

    p = _Parser("check")
    p.add_argument("--printer")
    p.add_argument("--material")
    p.add_argument("--nozzle", type=float)
    p.add_argument("--max-age-days", type=float, default=cf.DEFAULT_MAX_AGE_DAYS)
    p.add_argument("--api")
    p.add_argument("--token")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    names = [a.printer] if a.printer else sorted(cf.fleet_printers())
    if not names:
        raise UsageError(
            "no printer to check: pass --printer P (no fleet registry is configured)",
            command=f"{CMD} check",
        )
    infos = [cf.printer_info(n, nozzle_mm=a.nozzle) for n in names]
    read = store.read_all(store.api_client(a.api, a.token))
    pairs: list[PairStatus] = []
    for info in infos:
        if a.material:
            materials = [cf.material_name(a.material)]
        else:
            seen = {
                cf.material_name(r.material)
                for r in read.records
                if r.material and any(cf._same(r.printer, n) for n in info.names)
            }
            materials = sorted(seen | ({cf.material_name(info.default_material)} if info.default_material else set()))
        pairs.extend(
            cf.check_pair(info, material, read.records, max_age_days=a.max_age_days)
            for material in materials
        )
    worst = max((cf.LEVELS.index(x.level) for x in pairs), default=0)
    doc = {
        "command": "check",
        "max_age_days": a.max_age_days,
        "overall": cf.LEVELS[worst],
        "pairs": [x.to_dict() for x in pairs],
        "notes": read.notes,
    }
    if a.json:
        _emit(doc)
    else:
        for x in pairs:
            print(f"{x.level:<8} {x.printer} x {x.material} (nozzle {x.nozzle_mm:g} mm)")
            for reason in x.reasons:
                print(f"           {reason}")
            if x.level in ("MISSING", "STALE"):
                print(f"           fix: 3d calibrate fit quick --printer {x.printer} --material {x.material} --yes")
        print(f"overall {cf.LEVELS[worst]}")
    if worst >= cf.LEVELS.index("STALE"):
        raise GateFailure(
            "fit calibration is missing or stale",
            command=f"{CMD} check",
            silent=True,
        )
    return 0


_RUN = {
    "plan": _plan,
    "gen": _gen,
    "quick": _quick,
    "read": _read,
    "record": _record,
    "check": _check,
}


def run(argv: list[str]) -> int:
    """`3d calibrate fit <sub> …` (argv = everything after `fit`)."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    fn = _RUN.get(argv[0])
    if fn is None:
        raise UsageError(
            f"unknown `calibrate fit` subcommand {argv[0]!r}; accepted: {', '.join(SUBS)}",
            command=CMD,
            remediation=["Run `3d calibrate fit --help`."],
        )
    if any(x in ("-h", "--help") for x in argv[1:]):
        print(USAGE)
        return 0
    return fn(argv[1:])
