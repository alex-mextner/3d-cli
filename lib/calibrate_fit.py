"""calibrate_fit.py — logic of `3d calibrate fit`: plan the coupon, read a result, pick the gap, check staleness.

WHAT: the pure side of the quick fit calibration (the CLI is `calibrate_fit_cli.py`, the store is
  `calibrate_fit_store.py`, the geometry `fit_coupon.py`):
    plan_doc      the coupon for a printer x material: ladders, their gaps, bed fit, print time and the exact
                  readout instruction per ladder (`--json` of `plan`)
    parse_result  `--result step=3` / `free,press,...` -> one reading per step
    from_measured `--measured` caliper diameters -> readings (the step whose REAL gap reaches the table's target)
    pick          readings of one ladder -> the gap to trust: the smallest `free` gap (a clearance fit) or the
                  largest `press` gap (a press fit), with the warnings of an odd readout
    check_records per printer x material: MISSING / STALE (age, nozzle) / PARTIAL / OK
WHY: S0's `fits.corrections_from_fit_clearance` turns readings into correction rows; this module is how a
  user's words ("step 3 fits") or caliper numbers become those readings without a spreadsheet.
READING RULES (the contract of the tests):
  - Results are ordered by gap; a looser result at a larger gap is expected (fused < press < free).
  - Two readings of one gap keep the TIGHTER result (a repeat that sticks outranks a repeat that slid).
  - A ladder whose steps never reach the answer (all fused / all press for a clearance fit, nothing grips for a
    press fit) is NOT recorded: `pick` returns ok=False with the advice to widen the range (`--gap-offset`).
  - A reading that is looser at a smaller gap than a tighter one is non-monotonic: it is recorded with a warning.
INVARIANTS: stdlib + `fits`, `fit_coupon`, `fits_registry`, `registries` (lazy, never at import by a command).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument, UsageError

import fit_coupon
from fit_coupon import Ladder

if TYPE_CHECKING:
    from calibrate_fit_store import StoredRecord

CMD = "calibrate fit"
RANK = {"fused": 0, "press": 1, "free": 2}
DEFAULT_MAX_AGE_DAYS = 180
LEVELS = ("OK", "PARTIAL", "STALE", "MISSING")  # worst last


# --------------------------------------------------------------------------- printer / material
@dataclass(frozen=True)
class PrinterInfo:
    name: str  # fleet key when the input was one, else the input
    model: str
    bed_mm: tuple[float, float, float]
    nozzle_mm: float
    head: str | None  # the first usable head label of the fleet entry (K1 SE: T1)
    default_material: str | None
    in_fleet: bool

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for n in (self.name, self.model) if n))


def fleet_printers() -> dict[str, str]:
    """Fleet key -> model (empty when no fleet registry is configured)."""
    from printops.fleet import load_fleet

    fleet = load_fleet()
    return {k: p.model for k, p in fleet.printers.items()} if fleet else {}


def printer_info(name: str, *, nozzle_mm: float | None = None) -> PrinterInfo:
    """Resolve a fleet key or printer model to its bed, nozzle and head; unknown names list what is accepted."""
    from fits_registry import printer_identity
    from printops.fleet import load_fleet
    from registries.printers import load_printers

    identity = printer_identity(name)
    models = load_printers()
    if not identity.known or identity.model not in models:
        raise InvalidArgument(
            "--printer",
            name,
            [*fleet_printers(), *models],
            command=CMD,
            extra="Fleet keys come from fleet/printers.yaml; models from `3d printers list`.",
        )
    registry = models[identity.model]
    head: str | None = None
    nozzle = nozzle_mm or registry.nozzle_mm
    fleet = load_fleet()
    entry = fleet.printers.get(identity.key) if fleet and identity.key else None
    if entry is not None:
        usable = next((h for h in entry.heads if h.usable), None)
        if usable is not None:
            head = usable.label
            if nozzle_mm is None and usable.nozzle is not None:
                nozzle = usable.nozzle.diameter_mm
    bed = tuple(float(v) for v in registry.bed)
    return PrinterInfo(
        identity.key or name,
        identity.model,
        (bed[0], bed[1], bed[2] if len(bed) > 2 else 0.0),
        float(nozzle),
        head,
        registry.material,
        entry is not None,
    )


def material_name(text: str) -> str:
    """The registry name of a material (`Rapid PLA+` -> `PLA`), else the text upper-cased."""
    from fits_registry import material_identity

    identity = material_identity(text)
    return identity.name or text.strip().upper()


def csv(text: str | None, accepted: Sequence[str], flag: str) -> tuple[str, ...] | None:
    """`--fit snap,sliding` -> a validated tuple; None/empty = no filter."""
    if not text:
        return None
    out = tuple(t.strip() for t in text.split(",") if t.strip())
    for t in out:
        if t not in accepted:
            raise InvalidArgument(flag, t, list(accepted), command=CMD)
    return out


def select_ladders(
    fit_types: tuple[str, ...] | None,
    shapes: tuple[str, ...] | None,
    gap_offset: float = 0.0,
) -> tuple[Ladder, ...]:
    """The ladders of a coupon, their gaps shifted by `gap_offset` (to widen the range)."""
    chosen = fit_coupon.ladders(fit_types, shapes)
    if not chosen:
        raise UsageError(
            f"no ladder for --fit {','.join(fit_types or ())} --shape {','.join(shapes or ())}",
            command=CMD,
            remediation=[
                "Coupon ladders: "
                + ", ".join(f"{f}/{s}" for f, s in fit_coupon.LADDER_SPECS)
            ],
        )
    if gap_offset == 0.0:
        return chosen
    out = []
    for ladder in chosen:
        steps = []
        for st in ladder.steps:
            gap = fit_coupon._r(st.gap_mm + gap_offset)
            if gap < 0 and ladder.fit_type != "press":
                raise UsageError(
                    f"--gap-offset {gap_offset:g} takes the {ladder.id} ladder below a zero gap ({gap:g} mm)",
                    command=CMD,
                    remediation=["Use a --gap-offset of at least "
                                 f"{-ladder.steps[0].gap_mm:g} for this ladder, or drop that fit with --fit."],
                )
            steps.append(fit_coupon.Step(st.step, gap, fit_coupon._r(ladder.mating_mm + 2 * gap)))
        out.append(
            Ladder(ladder.number, ladder.fit_type, ladder.shape, ladder.table_mm, tuple(steps), ladder.mating_mm)
        )
    return tuple(out)


# --------------------------------------------------------------------------- plan
def readout_text(ladder: Ladder) -> str:
    """The exact words that tell the user how to read this ladder and what to pass to `record`."""
    w = ladder.words()
    n = len(ladder.steps)
    decide = (
        "N = the LAST step that still grips (press)"
        if ladder.fit_type == "press"
        else "N = the FIRST step that is free"
    )
    what = "hole diameters" if ladder.shape == "round" else "slot widths"
    return (
        f"Ladder {ladder.number} ({ladder.fit_type}/{ladder.shape}): push the {ladder.mating_mm:g} mm "
        f"{ladder.mating} into steps 1..{n} (digit under the step, smallest gap first). "
        f"free: {w['free']}. press: {w['press']}. fused: {w['fused']}. "
        f"Report --result step=N ({decide}), or {n} words, one per step, or measure the {what} "
        f"with calipers: --measured D1,...,D{n}."
    )



def record_command(
    ladder: Ladder, printer: str, material: str, gap_offset: float = 0.0
) -> str:
    offset = f" --gap-offset {gap_offset:g}" if gap_offset else ""
    return (
        f"3d calibrate fit record --printer {printer} --material {material} --fit {ladder.fit_type} "
        f"--shape {ladder.shape} --result step=N{offset} --yes"
    )


def plan_doc(
    printer: PrinterInfo,
    material: str,
    ladders: tuple[Ladder, ...],
    gap_offset: float = 0.0,
) -> dict[str, Any]:
    """The plan: coupons, gaps, size/bed fit, print time and readout per ladder (`plan --json`)."""
    lay = fit_coupon.layout(ladders)
    est = fit_coupon.estimate(lay.objects)
    coupons = []
    for ladder in ladders:
        placed = next(o for o in lay.objects if o.ladder is ladder)
        coupons.append(
            {
                "ladder": ladder.number,
                "id": ladder.id,
                "fit_type": ladder.fit_type,
                "shape": ladder.shape,
                "table_clearance_mm": ladder.table_mm,
                "mating": {"part": ladder.mating, "nominal_mm": ladder.mating_mm},
                "steps": [
                    {"step": s.step, "gap_mm": s.gap_mm, "hole_mm": s.hole_mm}
                    for s in ladder.steps
                ],
                "bbox_mm": [round(v, 2) for v in placed.bbox],
                "results": ladder.words(),
                "readout": readout_text(ladder),
                "record": record_command(ladder, printer.name, material, gap_offset),
            }
        )
    return {
        "schema": fit_coupon.SCHEMA,
        "command": "plan",
        "printer": printer.name,
        "model": printer.model,
        "material": material,
        "nozzle_mm": printer.nozzle_mm,
        "head": printer.head,
        "bed_mm": list(printer.bed_mm),
        "gap_offset_mm": gap_offset,
        "coupon": {
            "footprint_mm": list(lay.size[:2]),
            "height_mm": lay.size[2],
            "fits_bed": fit_coupon.fits_bed(lay.size, printer.bed_mm),
            "margin_mm": fit_coupon.MARGIN_MM,
            "objects": [o.name for o in lay.objects],
        },
        "estimate": est,
        "coupons": coupons,
        "next": [
            f"3d calibrate fit gen --printer {printer.name} --material {material} --out coupon.3mf",
            "print it with the usual flow (3d prep / 3d arrange / 3d retarget for the printer)",
            f"3d calibrate fit record --printer {printer.name} --material {material} --fit F --shape S --result step=N --yes",
        ],
    }


# --------------------------------------------------------------------------- readings
@dataclass(frozen=True)
class Reading:
    fit_type: str
    shape: str
    gap_mm: float
    result: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fit_type": self.fit_type,
            "shape": self.shape,
            "gap_mm": self.gap_mm,
            "result": self.result,
        }


def _int(token: str, flag: str, hi: int) -> int:
    try:
        n = int(token)
    except ValueError:
        raise UsageError(f"{flag}: step {token!r} is not a whole number", command=CMD) from None
    if not 1 <= n <= hi:
        raise UsageError(f"{flag}: step {n} is outside 1..{hi}", command=CMD)
    return n


def parse_result(text: str, ladder: Ladder) -> list[Reading]:
    """`step=N`, one word per step (`fused,press,free,...`) or `K=word` pairs -> readings.

    `step=N`: a clearance ladder reads N as the FIRST `free` step (smaller steps `press`, larger `free`);
    a press ladder reads N as the LAST step that still grips (`press`; larger steps `free`, smaller `press`)."""
    n = len(ladder.steps)
    tokens = [t for t in re.split(r"[,\s]+", text.strip()) if t]
    if not tokens:
        raise UsageError("--result is empty", command=CMD)

    def reading(step: int, result: str) -> Reading:
        return Reading(ladder.fit_type, ladder.shape, ladder.steps[step - 1].gap_mm, result)

    if len(tokens) == 1 and tokens[0].startswith("step="):
        first = _int(tokens[0][5:], "--result step=", n)
        if ladder.fit_type == "press":
            return [reading(s, "press" if s <= first else "free") for s in range(1, n + 1)]
        return [reading(s, "free" if s >= first else "press") for s in range(1, n + 1)]
    words = tuple(RANK)
    if all("=" not in t for t in tokens):
        if len(tokens) != n:
            raise UsageError(
                f"--result lists {len(tokens)} word(s) but the ladder has {n} steps",
                command=CMD,
                remediation=[
                    f"One of {', '.join(words)} per step, smallest gap first, or `step=N`, or `K=word` pairs."
                ],
            )
        pairs = list(enumerate(tokens, start=1))
    else:
        pairs = []
        for t in tokens:
            k, sep, word = t.partition("=")
            if not sep or k == "step":
                raise UsageError(
                    f"--result {t!r}: mix of forms; use `step=N` alone, bare words, or `K=word` pairs",
                    command=CMD,
                )
            pairs.append((_int(k, "--result", n), word))
    out = []
    seen: set[int] = set()
    for step, word in pairs:
        if word not in RANK:
            raise InvalidArgument("--result", word, list(words), command=CMD)
        if step in seen:
            raise UsageError(f"--result names step {step} twice", command=CMD)
        seen.add(step)
        out.append(reading(step, word))
    return out


def from_measured(
    measured: Sequence[float], ladder: Ladder, mating_mm: float | None = None
) -> list[Reading]:
    """Caliper numbers -> readings. `measured[i]` is the measured hole Ø / slot width of step i+1.

    The REAL gap per face is (measured - mating) / 2 (the measured pin/tab, else its nominal). A clearance step
    reads `free` when its real gap reaches the table's target, `fused` when it is closed (<= 0), else `press`;
    a press step reads `press` when its real interference reaches the target, else `free`. The smallest design
    gap that reaches the target is therefore the gap that really delivers the table's clearance."""
    n = len(ladder.steps)
    if len(measured) != n:
        raise UsageError(
            f"--measured needs {n} numbers for the {ladder.id} ladder (one per step), got {len(measured)}",
            command=CMD,
        )
    mating = ladder.mating_mm if mating_mm is None else mating_mm
    target = ladder.table_mm
    out = []
    for step, hole in zip(ladder.steps, measured, strict=True):
        real = round((hole - mating) / 2, 4)
        if ladder.fit_type == "press":
            result = "press" if real <= target else "free"
        elif real >= target:
            result = "free"
        elif real <= 0:
            result = "fused"
        else:
            result = "press"
        out.append(Reading(ladder.fit_type, ladder.shape, step.gap_mm, result))
    return out


@dataclass
class Pick:
    fit_type: str
    shape: str
    readings: list[Reading]
    ok: bool
    gap_mm: float | None = None
    delta_mm: float | None = None
    table_mm: float | None = None
    problem: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fit_type": self.fit_type,
            "shape": self.shape,
            "ok": self.ok,
            "gap_mm": self.gap_mm,
            "delta_mm": self.delta_mm,
            "table_mm": self.table_mm,
            "problem": self.problem,
            "warnings": list(self.warnings),
            "readings": [r.to_dict() for r in self.readings],
        }


def merge_ties(readings: Iterable[Reading]) -> tuple[list[Reading], list[str]]:
    """One reading per gap (tighter result wins), sorted by gap; the ties are reported."""
    by_gap: dict[float, Reading] = {}
    notes: list[str] = []
    for r in readings:
        old = by_gap.get(r.gap_mm)
        if old is None:
            by_gap[r.gap_mm] = r
        elif old.result != r.result:
            kept = old if RANK[old.result] <= RANK[r.result] else r
            notes.append(
                f"gap {r.gap_mm:g} mm was read twice ({old.result} and {r.result}): kept the tighter `{kept.result}`"
            )
            by_gap[r.gap_mm] = kept
    return [by_gap[g] for g in sorted(by_gap)], notes


def pick(
    readings: Iterable[Reading], fit_type: str, shape: str, table: Any = None
) -> Pick:
    """Pick the gap to trust from the readings of ONE ladder (module doc, READING RULES)."""
    import fits

    tbl = table or fits.load_table()
    base = tbl.fit_types[fit_type]
    target = round(base.clearance_mm_per_face - base.interference_mm, 4)
    merged, warnings = merge_ties(readings)
    out = Pick(fit_type, shape, merged, False, table_mm=target, warnings=warnings)
    if not merged:
        out.problem = "no readings"
        return out
    worst = None
    for i, a in enumerate(merged):
        for b in merged[i + 1 :]:
            if RANK[b.result] < RANK[a.result]:
                worst = (a, b)
                break
        if worst:
            break
    if worst:
        a, b = worst
        out.warnings.append(
            f"non-monotonic readout: gap {a.gap_mm:g} mm read `{a.result}` but the larger gap {b.gap_mm:g} mm "
            f"read `{b.result}` (a larger gap should never fit tighter); re-check those steps"
        )
    press = fit_type == "press"
    wanted = "press" if press else "free"
    hits = [r for r in merged if r.result == wanted]
    if not hits:
        results = {r.result for r in merged}
        if press:
            if results == {"free"}:
                out.problem = (
                    "no step grips: every step is loose; widen the range toward more interference "
                    "(print the coupon with a negative --gap-offset, e.g. --gap-offset -0.1)"
                )
            else:
                out.problem = (
                    "no step grips: every step is fused or cracked; widen the range toward larger gaps "
                    "(print the coupon with a positive --gap-offset, e.g. --gap-offset 0.1)"
                )
        elif results == {"fused"}:
            out.problem = (
                f"every step is fused: the largest gap ({merged[-1].gap_mm:g} mm) is still too small; widen the "
                f"range (print the coupon with a positive --gap-offset, e.g. --gap-offset "
                f"{max(0.1, round(merged[-1].gap_mm, 2)):g})"
            )
        else:
            out.problem = (
                f"no step reads free (best: {max(results, key=RANK.__getitem__)}): the largest gap "
                f"({merged[-1].gap_mm:g} mm) is still too small; widen the range "
                "(print the coupon with a positive --gap-offset, e.g. --gap-offset 0.1)"
            )
        return out
    gap = max(r.gap_mm for r in hits) if press else min(r.gap_mm for r in hits)
    out.ok, out.gap_mm = True, gap
    out.delta_mm = round(gap - target, 4)
    if press and gap == merged[-1].gap_mm and all(r.result == "press" for r in merged):
        out.warnings.append(
            "every step grips: the loosest step is still a press fit, so the real optimum may be looser "
            "than the ladder; print it again with a positive --gap-offset"
        )
    if not press and all(r.result == "free" for r in merged):
        out.warnings.append(
            f"every step is free: the smallest gap {merged[0].gap_mm:g} mm is already loose, so the real "
            "clearance may be smaller than the ladder; print it again with a negative --gap-offset"
        )
    return out


# --------------------------------------------------------------------------- records
def readings_json(readings: Iterable[Reading | Mapping[str, Any]]) -> str:
    """The value of a record's `readings`: compact JSON of fit_clearance records, sorted by fit/shape/gap."""
    import json

    rows = [r.to_dict() if isinstance(r, Reading) else dict(r) for r in readings]
    rows.sort(key=lambda r: (r["fit_type"], r["shape"], r["gap_mm"]))
    return json.dumps(rows, separators=(",", ":"), sort_keys=True)


def merge_readings(
    old: Iterable[Mapping[str, Any]], new: Iterable[Reading]
) -> list[dict[str, Any]]:
    """The readings of the next record: the new ladders replace the old readings of their fit x shape."""
    fresh = list(new)
    replaced = {(r.fit_type, r.shape) for r in fresh}
    kept = [dict(r) for r in old if (r["fit_type"], r["shape"]) not in replaced]
    return kept + [r.to_dict() for r in fresh]


# --------------------------------------------------------------------------- check
def _age_days(stamp: str, now: datetime.datetime) -> float:
    at = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    return (now - at).total_seconds() / 86400


def _same(a: str | None, b: str | None) -> bool:
    import fits

    return a is not None and b is not None and fits.norm_name(a) == fits.norm_name(b)


@dataclass
class PairStatus:
    printer: str
    material: str
    nozzle_mm: float
    level: str
    reasons: list[str]
    records: list[str]
    covered: list[str]
    missing: list[str]
    newest: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer": self.printer,
            "material": self.material,
            "nozzle_mm": self.nozzle_mm,
            "level": self.level,
            "reasons": self.reasons,
            "records": self.records,
            "covered": self.covered,
            "missing": self.missing,
            "newest": self.newest,
            "fix": f"3d calibrate fit quick --printer {self.printer} --material {self.material}"
            if self.level in ("MISSING", "STALE")
            else None,
        }


def check_pair(
    printer: PrinterInfo,
    material: str,
    records: Sequence[StoredRecord],
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: datetime.datetime | None = None,
) -> PairStatus:
    """MISSING (no record), STALE (older than `max_age_days` or taken with another nozzle), PARTIAL (a coupon
    ladder never recorded), else OK, for one printer x material."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    mine = [
        r
        for r in records
        if any(_same(r.printer, n) for n in printer.names) and _same(r.material, material)
    ]
    wanted = [f"{f}/{s}" for f, s in fit_coupon.LADDER_SPECS]
    if not mine:
        return PairStatus(
            printer.name, material, printer.nozzle_mm, "MISSING",
            [f"no fit_clearance record for {printer.name} x {material}: the fit table's defaults apply"],
            [], [], wanted, None,
        )
    mine.sort(key=lambda r: r.measured_at, reverse=True)
    newest = mine[0]
    covered = sorted({f"{x['fit_type']}/{x['shape']}" for r in mine for x in r.readings})
    reasons: list[str] = []
    level = "OK"
    age = _age_days(newest.measured_at, now)
    if age > max_age_days:
        reasons.append(f"{age:.0f} days old (limit {max_age_days:g}): {newest.id}, {newest.measured_at[:10]}")
        level = "STALE"
    nozzles = {r.nozzle_mm for r in mine if r.nozzle_mm is not None}
    if nozzles and printer.nozzle_mm not in nozzles:
        reasons.append(
            f"taken with a {', '.join(f'{n:g}' for n in sorted(nozzles))} mm nozzle; the printer has "
            f"{printer.nozzle_mm:g} mm now"
        )
        level = "STALE"
    missing = [w for w in wanted if w not in covered]
    if missing and level == "OK":
        reasons.append(f"no reading for {', '.join(missing)}: those fits use the table's value")
        level = "PARTIAL"
    return PairStatus(
        printer.name, material, printer.nozzle_mm, level, reasons,
        [r.id for r in mine], covered, missing, newest.measured_at,
    )


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
