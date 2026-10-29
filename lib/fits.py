"""fits.py — the shared fit/tolerance table of `3d`: load, validate and resolve (schema `3d.fits/1`).

WHAT: `lib/data/fits.yaml` holds the fit types (snap, sliding, tight, press, glue), their per-face
clearance / interference / tolerance, aliases (`free_running`, `snug`, `normal`, `loose`, ...) and a list of
printer x material x nozzle x layer corrections. `resolve()` turns "I want a sliding fit on a round feature
printed in PETG on k1se with a 0.4 nozzle" into a number plus the provenance that explains it.

ACCESSED VIA: `niche_fit.resolve_clearance` (`3d fit-niche --fit`), the `3d fits` command (S1), the fit
coupon (`3d calibrate fit`, S2: writes `fit_clearance` records that `corrections_from_fit_clearance` turns
into correction rows) and the assembly runner (S3).

INVARIANTS:
  - The table file is the only clearance table in the code base. A legacy label is an alias row, never a
    second dict.
  - `clearance_mm` is per mating face (radial for round) and SIGNED: base clearance - interference +
    correction; negative means designed overlap.
  - Exactly one correction row is applied per `resolve` (the most specific match, see `select_correction`);
    the others are listed as `superseded`, so the number can always be reproduced by hand.
  - A caller who names a printer/material the table has no exact row for gets the `*` row and
    `fallback: true` in the provenance instead of an error: a fit estimate is still better than none.
"""

from __future__ import annotations

import functools
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from errors import InvalidArgument

SCHEMA = "3d.fits/1"
TABLE_PATH = Path(__file__).resolve().parent / "data" / "fits.yaml"
SOURCES = ("calibration", "manual", "default")
FIT_RESULTS = ("free", "press", "fused")
CLEARANCE_FIT_TYPES = (
    "snap",
    "sliding",
    "tight",
)  # types whose coupon reads a free/press/fused gap
WILDCARD = "*"
_NUMBER = (int, float)
_NOZZLE_EPS = 0.005  # mm: 0.4 vs 0.40 and nozzle sizes are never closer than 0.05


class FitTableError(ValueError):
    """`fits.yaml` (or a calibration record) does not match schema `3d.fits/1`."""


@dataclass(frozen=True)
class FitType:
    name: str
    clearance_mm_per_face: float
    interference_mm: float
    tol_mm: float
    applies_to: tuple[str, ...]
    features: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class Alias:
    name: str
    fit_type: str
    extra_mm: float = 0.0


@dataclass(frozen=True)
class Correction:
    """One printer x material x nozzle x layer delta; `WILDCARD`/None dimensions match anything."""

    delta_mm: float
    source: str
    printer: str = WILDCARD
    material: str = WILDCARD
    nozzle_mm: float | None = None
    layer_height_mm: float | None = None
    shape: str | None = None
    fit_type: str | None = None
    measured_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer": self.printer,
            "material": self.material,
            "nozzle_mm": self.nozzle_mm,
            "layer_height_mm": self.layer_height_mm,
            "shape": self.shape,
            "fit_type": self.fit_type,
            "delta_mm": self.delta_mm,
            "source": self.source,
            "measured_at": self.measured_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class FitTable:
    shapes: tuple[str, ...]
    fit_types: Mapping[str, FitType]
    aliases: Mapping[str, Alias]
    corrections: tuple[Correction, ...]

    def names(self) -> list[str]:
        """Every accepted fit-type spelling: canonical names, then aliases."""
        return [*self.fit_types, *self.aliases]


@dataclass(frozen=True)
class Resolution:
    """The answer of `resolve`: numbers plus the provenance that explains them."""

    fit_type: str
    requested: str
    shape: str
    printer: str | None
    material: str | None
    nozzle_mm: float | None
    layer_height_mm: float | None
    clearance_mm: float
    interference_mm: float
    tol_mm: float
    fallback: bool
    provenance: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "fit_type": self.fit_type,
            "requested": self.requested,
            "shape": self.shape,
            "printer": self.printer,
            "material": self.material,
            "nozzle_mm": self.nozzle_mm,
            "layer_height_mm": self.layer_height_mm,
            "clearance_mm": self.clearance_mm,
            "interference_mm": self.interference_mm,
            "tol_mm": self.tol_mm,
            "fallback": self.fallback,
            "provenance": [dict(p) for p in self.provenance],
        }


# --------------------------------------------------------------------------- loading / validation
def _num(value: Any, where: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, _NUMBER):
        raise FitTableError(f"{where}: expected a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise FitTableError(f"{where}: {value} is below the minimum {minimum}")
    return float(value)


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise FitTableError(
            f"{where}: expected a list of non-empty strings, got {value!r}"
        )
    return tuple(value)


_CORRECTION_KEYS = frozenset(
    {
        "printer",
        "material",
        "nozzle_mm",
        "layer_height_mm",
        "shape",
        "fit_type",
        "delta_mm",
        "source",
        "measured_at",
        "note",
    }
)
_FIT_TYPE_KEYS = frozenset(
    {
        "clearance_mm_per_face",
        "interference_mm",
        "tol_mm",
        "applies_to",
        "features",
        "notes",
    }
)


def _check_correction_row(raw: Any, where: str) -> None:
    """Shape of one correction row: a mapping with known keys, the required ones and a valid source."""
    if not isinstance(raw, dict):
        raise FitTableError(f"{where}: expected a mapping")
    unknown = set(raw) - _CORRECTION_KEYS
    if unknown:
        raise FitTableError(f"{where}: unknown keys {sorted(unknown)}")
    for key in ("printer", "material", "delta_mm", "source"):
        if key not in raw:
            raise FitTableError(f"{where}: missing required key {key!r}")
    if raw["source"] not in SOURCES:
        raise FitTableError(
            f"{where}.source: {raw['source']!r} is not one of {list(SOURCES)}"
        )
    for key in ("printer", "material"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise FitTableError(
                f"{where}.{key}: expected a non-empty string or {WILDCARD!r}"
            )


def _optional_num(
    raw: Mapping[str, Any], key: str, where: str, minimum: float
) -> float | None:
    value = raw.get(key)
    return None if value is None else _num(value, f"{where}.{key}", minimum=minimum)


def _parse_correction(
    raw: Any, index: int, shapes: Sequence[str], fit_names: Sequence[str]
) -> Correction:
    where = f"corrections[{index}]"
    _check_correction_row(raw, where)
    source = raw["source"]
    shape = raw.get("shape")
    fit_type = raw.get("fit_type")
    if shape is not None and shape not in shapes:
        raise FitTableError(f"{where}.shape: {shape!r} is not one of {list(shapes)}")
    if fit_type is not None and fit_type not in fit_names:
        raise FitTableError(f"{where}.fit_type: {fit_type!r} is not a fit type")
    measured_at = raw.get("measured_at")
    if source != "default" and not isinstance(measured_at, str):
        raise FitTableError(
            f"{where}.measured_at: a {source} row needs an ISO-8601 UTC timestamp"
        )
    return Correction(
        delta_mm=_num(raw["delta_mm"], f"{where}.delta_mm"),
        source=source,
        printer=raw["printer"],
        material=raw["material"],
        nozzle_mm=_optional_num(raw, "nozzle_mm", where, 0.05),
        layer_height_mm=_optional_num(raw, "layer_height_mm", where, 0.01),
        shape=shape,
        fit_type=fit_type,
        measured_at=measured_at,
        note=str(raw.get("note", "")),
    )


def _parse_fit_type(name: str, raw: Any, shapes: Sequence[str]) -> FitType:
    where = f"fit_types.{name}"
    if not isinstance(raw, dict):
        raise FitTableError(f"{where}: expected a mapping")
    missing = _FIT_TYPE_KEYS - set(raw)
    if missing:
        raise FitTableError(f"{where}: missing keys {sorted(missing)}")
    applies = _str_list(raw["applies_to"], f"{where}.applies_to")
    if not set(applies) <= set(shapes):
        raise FitTableError(
            f"{where}.applies_to: {sorted(set(applies) - set(shapes))} not in shapes"
        )
    return FitType(
        name=name,
        clearance_mm_per_face=_num(
            raw["clearance_mm_per_face"], f"{where}.clearance_mm_per_face", minimum=0
        ),
        interference_mm=_num(
            raw["interference_mm"], f"{where}.interference_mm", minimum=0
        ),
        tol_mm=_num(raw["tol_mm"], f"{where}.tol_mm", minimum=0),
        applies_to=applies,
        features=_str_list(raw["features"], f"{where}.features"),
        notes=str(raw["notes"]).strip(),
    )


def _parse_fit_types(
    data: Mapping[str, Any], shapes: Sequence[str]
) -> dict[str, FitType]:
    raw_types = data.get("fit_types")
    if not isinstance(raw_types, dict) or not raw_types:
        raise FitTableError("fit_types: expected a non-empty mapping")
    fit_types = {
        name: _parse_fit_type(name, raw, shapes) for name, raw in raw_types.items()
    }
    missing_types = {"snap", "sliding", "tight", "press", "glue"} - set(fit_types)
    if missing_types:
        raise FitTableError(f"fit_types: missing {sorted(missing_types)}")
    return fit_types


def _parse_aliases(
    data: Mapping[str, Any], fit_types: Mapping[str, FitType]
) -> dict[str, Alias]:
    aliases: dict[str, Alias] = {}
    for name, raw in (data.get("aliases") or {}).items():
        where = f"aliases.{name}"
        if name in fit_types:
            raise FitTableError(f"{where}: shadows the fit type of the same name")
        if not isinstance(raw, dict) or raw.get("fit_type") not in fit_types:
            raise FitTableError(
                f"{where}.fit_type: must name one of {sorted(fit_types)}"
            )
        aliases[name] = Alias(
            name, raw["fit_type"], _num(raw.get("extra_mm", 0.0), f"{where}.extra_mm")
        )
    return aliases


def _is_default_row(c: Correction) -> bool:
    return (
        c.printer == WILDCARD
        and c.material == WILDCARD
        and c.nozzle_mm is None
        and c.layer_height_mm is None
        and c.shape is None
        and c.fit_type is None
    )


def parse_table(data: Any) -> FitTable:
    """Validate a decoded `fits.yaml` document and return it typed; raises `FitTableError`."""
    if not isinstance(data, dict):
        raise FitTableError("fits table must be a mapping")
    if data.get("schema") != SCHEMA:
        raise FitTableError(f"schema: expected {SCHEMA!r}, got {data.get('schema')!r}")
    shapes = _str_list(data.get("shapes"), "shapes")
    fit_types = _parse_fit_types(data, shapes)
    aliases = _parse_aliases(data, fit_types)
    raw_corrections = data.get("corrections")
    if not isinstance(raw_corrections, list):
        raise FitTableError("corrections: expected a list")
    corrections = tuple(
        _parse_correction(row, i, shapes, [*fit_types, *aliases])
        for i, row in enumerate(raw_corrections)
    )
    if not any(_is_default_row(c) for c in corrections):
        raise FitTableError(
            "corrections: the fully wildcard default row (printer '*', material '*') is required"
        )
    return FitTable(shapes, fit_types, aliases, corrections)


def load_table(path: str | Path | None = None) -> FitTable:
    """The validated fit table (`lib/data/fits.yaml` unless `path` is given); the default is cached."""
    if path is None:
        return _default_table()
    return _read(Path(path))


def _read(path: Path) -> FitTable:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise FitTableError(f"{path}: not valid YAML: {e}") from e
    return parse_table(data)


@functools.lru_cache(maxsize=1)
def _default_table() -> FitTable:
    return _read(TABLE_PATH)


# --------------------------------------------------------------------------- resolve
def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _same_name(wanted: str, got: str | None) -> bool:
    return got is not None and _norm(wanted) == _norm(got)


def _same_dim(wanted: float | None, got: float | None) -> bool:
    return wanted is not None and got is not None and abs(wanted - got) <= _NOZZLE_EPS


def _matches(
    correction: Correction,
    shape: str,
    fit_names: tuple[str, str | None],
    printer: str | None,
    material: str | None,
    nozzle_mm: float | None,
    layer_height_mm: float | None,
) -> int | None:
    """Specificity score when `correction` applies to the query, None when it does not."""
    # (the row constrains this dimension, the query satisfies it, specificity weight)
    constraints = (
        (correction.printer != WILDCARD, _same_name(correction.printer, printer), 32),
        (
            correction.material != WILDCARD,
            _same_name(correction.material, material),
            16,
        ),
        (
            correction.nozzle_mm is not None,
            _same_dim(correction.nozzle_mm, nozzle_mm),
            8,
        ),
        (
            correction.layer_height_mm is not None,
            _same_dim(correction.layer_height_mm, layer_height_mm),
            4,
        ),
        (correction.shape is not None, correction.shape == shape, 2),
        (correction.fit_type is not None, correction.fit_type in fit_names, 1),
    )
    score = 0
    for constrained, satisfied, weight in constraints:
        if constrained:
            if not satisfied:
                return None
            score += weight
    return score


def select_correction(
    corrections: Iterable[Correction],
    *,
    shape: str,
    fit_type: str,
    alias: str | None = None,
    printer: str | None = None,
    material: str | None = None,
    nozzle_mm: float | None = None,
    layer_height_mm: float | None = None,
) -> tuple[Correction, list[Correction]]:
    """The one correction to apply and the matching rows it supersedes.

    Order: specificity (printer > material > nozzle > layer > shape > fit_type), then source
    (calibration > manual > default), then newest `measured_at`. A row's `fit_type` matches the canonical
    `fit_type` or the `alias` the caller asked for (an alias-tagged row stays specific to that alias).
    Raises `FitTableError` without a match (the table loader guarantees a default row, so only
    caller-supplied lists can trigger it).
    """
    scored = []
    for c in corrections:
        score = _matches(
            c, shape, (fit_type, alias), printer, material, nozzle_mm, layer_height_mm
        )
        if score is not None:
            scored.append((score, -SOURCES.index(c.source), c.measured_at or "", c))
    if not scored:
        raise FitTableError(
            "no correction row matches (the table needs its default '*' row)"
        )
    scored.sort(key=lambda t: t[:3], reverse=True)
    return scored[0][3], [t[3] for t in scored[1:]]


def _fit_type_provenance(
    base: FitType, alias: Alias | None, shape: str
) -> tuple[float, list[dict[str, Any]]]:
    """The fit row's per-face clearance (alias extra included) and the provenance entries that explain it."""
    provenance: list[dict[str, Any]] = [
        {
            "kind": "fit_type",
            "id": base.name,
            "source": f"{TABLE_PATH.name}:fit_types.{base.name}",
            "clearance_mm_per_face": base.clearance_mm_per_face,
            "interference_mm": base.interference_mm,
            "tol_mm": base.tol_mm,
        }
    ]
    clearance = base.clearance_mm_per_face
    if alias:
        clearance += alias.extra_mm
        provenance.append(
            {
                "kind": "alias",
                "id": alias.name,
                "source": f"{TABLE_PATH.name}:aliases.{alias.name}",
                "fit_type": alias.fit_type,
                "extra_mm": alias.extra_mm,
            }
        )
    if shape not in base.applies_to:
        provenance.append(
            {
                "kind": "note",
                "id": "shape_outside_applies_to",
                "message": f"{base.name} is meant for {list(base.applies_to)}; {shape} is outside that list",
            }
        )
    return clearance, provenance


def _correction_entry(
    chosen: Correction, fallback: bool, printer: str | None, material: str | None
) -> dict[str, Any]:
    """The provenance entry of the applied correction; a fallback names the dimensions it fell back on."""
    entry: dict[str, Any] = {
        "kind": "correction",
        **chosen.to_dict(),
        "fallback": fallback,
    }
    if fallback:
        entry["fallback_reason"] = [
            dim
            for dim, asked, got in (
                ("printer", printer, chosen.printer),
                ("material", material, chosen.material),
            )
            if asked is not None and got == WILDCARD
        ]
    return entry


def resolve(
    fit_type: str,
    shape: str,
    printer: str | None = None,
    material: str | None = None,
    nozzle: float | None = None,
    layer: float | None = None,
    *,
    extra_corrections: Sequence[Correction] = (),
    table: FitTable | None = None,
) -> Resolution:
    """Resolve a fit to a per-face clearance (signed), tolerance and provenance.

    `fit_type` is a canonical name or an alias; `shape` one of the table's shapes. `extra_corrections`
    (e.g. from `corrections_from_fit_clearance`) compete with the table rows under `select_correction`.
    Unknown printer/material is not an error: the `*` row applies and `fallback` is True.
    """
    tbl = table or load_table()
    requested = fit_type
    alias = tbl.aliases.get(fit_type)
    name = alias.fit_type if alias else fit_type
    base = tbl.fit_types.get(name)
    if base is None:
        raise InvalidArgument("fit_type", requested, tbl.names(), command="fits")
    if shape not in tbl.shapes:
        raise InvalidArgument("shape", shape, list(tbl.shapes), command="fits")

    clearance, provenance = _fit_type_provenance(base, alias, shape)
    chosen, superseded = select_correction(
        [*extra_corrections, *tbl.corrections],
        shape=shape,
        fit_type=name,
        alias=alias.name if alias else None,
        printer=printer,
        material=material,
        nozzle_mm=nozzle,
        layer_height_mm=layer,
    )
    fallback = (printer is not None and chosen.printer == WILDCARD) or (
        material is not None and chosen.material == WILDCARD
    )
    provenance.append(_correction_entry(chosen, fallback, printer, material))
    provenance.extend(
        {"kind": "correction_superseded", **c.to_dict()}
        for c in superseded
        if c.delta_mm != 0.0 or c.source != "default"
    )

    return Resolution(
        fit_type=name,
        requested=requested,
        shape=shape,
        printer=printer,
        material=material,
        nozzle_mm=nozzle,
        layer_height_mm=layer,
        clearance_mm=round(clearance - base.interference_mm + chosen.delta_mm, 4),
        interference_mm=base.interference_mm,
        tol_mm=base.tol_mm,
        fallback=fallback,
        provenance=tuple(provenance),
    )


# --------------------------------------------------------------------------- calibration records
def validate_fit_clearance(
    record: Mapping[str, Any], table: FitTable | None = None
) -> dict[str, Any]:
    """Check one `fit_clearance` calibration record `{fit_type, shape, gap_mm, result}`; returns a clean copy."""
    tbl = table or load_table()
    extra = set(record) - {"fit_type", "shape", "gap_mm", "result"}
    if extra:
        raise FitTableError(f"fit_clearance: unknown keys {sorted(extra)}")
    fit_type = record.get("fit_type")
    if fit_type not in tbl.fit_types:
        raise FitTableError(
            f"fit_clearance.fit_type: {fit_type!r} is not one of {list(tbl.fit_types)}"
        )
    shape = record.get("shape")
    if shape not in tbl.shapes:
        raise FitTableError(
            f"fit_clearance.shape: {shape!r} is not one of {list(tbl.shapes)}"
        )
    result = record.get("result")
    if result not in FIT_RESULTS:
        raise FitTableError(
            f"fit_clearance.result: {result!r} is not one of {list(FIT_RESULTS)}"
        )
    gap = _num(record.get("gap_mm"), "fit_clearance.gap_mm", minimum=0)
    return {"fit_type": fit_type, "shape": shape, "gap_mm": gap, "result": result}


def corrections_from_fit_clearance(
    records: Iterable[Mapping[str, Any]],
    *,
    printer: str,
    material: str,
    nozzle_mm: float,
    measured_at: str,
    layer_height_mm: float | None = None,
    table: FitTable | None = None,
) -> list[Correction]:
    """Turn coupon readings into correction rows (source `calibration`), one per fit type x shape.

    For every clearance fit type (snap, sliding, tight) the smallest gap that read `free` is the measured
    real clearance; the correction is that gap minus the table's base clearance, so the resolved number moves
    to what the coupon proved. A group with no `free` gap yields no row (the coupon did not bracket the fit).
    press/glue records are validated but not converted: a press result needs an interference sweep.
    """
    tbl = table or load_table()
    free_gaps: dict[tuple[str, str], list[float]] = {}
    for record in records:
        rec = validate_fit_clearance(record, tbl)
        if rec["fit_type"] in CLEARANCE_FIT_TYPES and rec["result"] == "free":
            free_gaps.setdefault((rec["fit_type"], rec["shape"]), []).append(
                rec["gap_mm"]
            )
    rows = []
    for (fit_type, shape), gaps in sorted(free_gaps.items()):
        delta = round(min(gaps) - tbl.fit_types[fit_type].clearance_mm_per_face, 4)
        rows.append(
            Correction(
                delta_mm=delta,
                source="calibration",
                printer=printer,
                material=material,
                nozzle_mm=nozzle_mm,
                layer_height_mm=layer_height_mm,
                shape=shape,
                fit_type=fit_type,
                measured_at=measured_at,
                note=f"smallest free gap {min(gaps)} mm of {len(gaps)} free reading(s)",
            )
        )
    return rows


def median_delta(rows: Iterable[Correction]) -> float | None:
    """Median `delta_mm` of calibration rows (a printer-wide bias estimate); None when empty."""
    deltas = [r.delta_mm for r in rows]
    return round(statistics.median(deltas), 4) if deltas else None
