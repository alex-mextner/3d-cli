"""calibration_catalog.py — typed loader and schema of `fleet/calibrations.yaml` (plan §7.2, #167).

ACCESSED VIA: printops.calibrations (requirement engine, records, the `calibration_current`
gate), printops.lint_calibrations, and the tests that keep the committed file valid.
Pure: no DB, no network. The file's own header documents every key.

FILE LOCATION: `$PRINTOPS_CALIBRATIONS`, else `<repo>/fleet/calibrations.yaml` (the 3d-api image
copies fleet/ next to lib/, as for fleet/policy.yaml).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

DIMENSIONS = ("printer", "head", "nozzle", "plate", "filament")
BASELINES = ("required", "assumed")
TRIGGERS = (
    "nozzle_swap",
    "nozzle_diameter",
    "extruder",
    "ptfe",
    "hotend",
    "kinematics",
    "belts",
    "plate",
    "head_swap",
    "move",
    "repair",
)
VALUE_TYPES = ("number", "string")
COMPARES = ("eq", "le")
APPLY_SCHEMES = ("orca", "klipper", "journal")

_KIND_KEYS = (
    "title",
    "scope",
    "lot_sensitive",
    "baseline",
    "invalidated_by",
    "methods",
    "values",
    "apply",
    "estimate",
    "sources",
)
_OPTIONAL_KIND_KEYS = ("order", "gcode", "models")
_VALUE_KEYS = ("type", "min", "max", "unit", "optional")
_GCODE_KEYS = ("key", "compare", "tolerance", "enabled_by")


class CatalogError(ValueError):
    """The catalog file is missing, not YAML, or breaks its schema."""


@dataclass(frozen=True)
class ValueSpec:
    name: str
    type: str  # number | string
    min: float | None
    max: float | None
    unit: str | None
    optional: bool


@dataclass(frozen=True)
class GcodeCheck:
    value: str  # value name in the record
    key: str  # G-code footer key, e.g. filament_flow_ratio
    compare: str  # eq | le (G-code ≤ recorded)
    tolerance: float
    enabled_by: str | None  # footer key that must be truthy for the G-code to set the value


@dataclass(frozen=True)
class Kind:
    id: str
    title: str
    scope: tuple[str, ...]
    lot_sensitive: bool
    baseline: str
    invalidated_by: tuple[str, ...]
    methods: tuple[str, ...]
    values: dict[str, ValueSpec]
    order: tuple[str, ...]
    gcode: tuple[GcodeCheck, ...]
    apply: tuple[str, ...]
    estimate_minutes: float
    estimate_grams: float
    models: tuple[str, ...] | None
    sources: tuple[str, ...]

    @property
    def head_scoped(self) -> bool:
        return "head" in self.scope

    @property
    def printer_scoped(self) -> bool:
        return bool({"printer", "head", "nozzle", "plate"} & set(self.scope))

    def applies_to(self, model: str) -> bool:
        return self.models is None or model in self.models

    def check_values(self, values: Any) -> dict[str, Any]:
        """The values of a result, validated against this kind (CatalogError on a bad one)."""
        if not isinstance(values, dict):
            raise CatalogError(f"{self.id}: values must be an object, got {values!r}")
        unknown = sorted(str(k) for k in values if k not in self.values)
        if unknown:
            raise CatalogError(
                f"{self.id}: unknown value(s) {', '.join(unknown)}; accepted: {', '.join(self.values)}"
            )
        out: dict[str, Any] = {}
        for name, spec in self.values.items():
            v = values.get(name)
            if v is None:
                if not spec.optional:
                    raise CatalogError(f"{self.id}: value {name} is required")
                continue
            if spec.type == "string":
                if not isinstance(v, str) or not v.strip():
                    raise CatalogError(f"{self.id}: {name} must be a non-empty string, got {v!r}")
                out[name] = v.strip()
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise CatalogError(f"{self.id}: {name} must be a number, got {v!r}")
            if (spec.min is not None and v < spec.min) or (spec.max is not None and v > spec.max):
                raise CatalogError(
                    f"{self.id}: {name}={v:g} is outside {spec.min:g}..{spec.max:g}"
                    if spec.min is not None and spec.max is not None
                    else f"{self.id}: {name}={v:g} is out of range"
                )
            out[name] = float(v)
        ordered = [(n, out[n]) for n in self.order if n in out]
        for (a, va), (b, vb) in zip(ordered, ordered[1:], strict=False):
            if va > vb:
                raise CatalogError(f"{self.id}: {a}={va:g} must be ≤ {b}={vb:g}")
        return out


@dataclass(frozen=True)
class Catalog:
    path: Path
    kinds: dict[str, Kind]

    def kind(self, kind_id: str) -> Kind:
        k = self.kinds.get(kind_id)
        if k is None:
            raise CatalogError(f"unknown calibration kind {kind_id!r}; accepted: {', '.join(self.kinds)}")
        return k


def catalog_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("PRINTOPS_CALIBRATIONS")
    return Path(raw).expanduser() if raw else REPO_ROOT / "fleet" / "calibrations.yaml"


def load_catalog(path: Path | None = None) -> Catalog:
    path = path or catalog_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise CatalogError(
            f"calibration catalog {path} not found (set PRINTOPS_CALIBRATIONS or add fleet/calibrations.yaml)"
        ) from e
    except yaml.YAMLError as e:
        raise CatalogError(f"calibration catalog {path} is not valid YAML: {e}") from e
    try:
        return Catalog(path, parse(data))
    except CatalogError as e:
        raise CatalogError(f"{path}: {e}") from e


def parse(data: Any) -> dict[str, Kind]:
    if not isinstance(data, dict) or set(data) != {"kinds"}:
        raise CatalogError("the catalog must be a mapping with exactly one key, `kinds`")
    raw = data["kinds"]
    if not isinstance(raw, dict) or not raw:
        raise CatalogError("kinds must be a non-empty mapping")
    return {str(name): _kind(str(name), body) for name, body in raw.items()}


def _kind(name: str, body: Any) -> Kind:
    where = f"kinds.{name}"
    if not name.replace("_", "").isalnum() or name != name.lower():
        raise CatalogError(f"{where}: kind ids are lower_snake_case")
    if not isinstance(body, dict):
        raise CatalogError(f"{where} must be a mapping")
    missing = [k for k in _KIND_KEYS if k not in body]
    extra = sorted(str(k) for k in body if k not in _KIND_KEYS + _OPTIONAL_KIND_KEYS)
    if missing:
        raise CatalogError(f"{where}: missing key(s) {', '.join(missing)}")
    if extra:
        raise CatalogError(f"{where}: unknown key(s) {', '.join(extra)}")

    scope = _strings(f"{where}.scope", body["scope"], DIMENSIONS, non_empty=True)
    if "nozzle" in scope and "head" not in scope:
        raise CatalogError(f"{where}.scope: nozzle needs head (a nozzle sits on a head)")
    if "head" in scope and "printer" in scope:
        raise CatalogError(f"{where}.scope: head already implies printer; list only head")
    if not isinstance(body["lot_sensitive"], bool):
        raise CatalogError(f"{where}.lot_sensitive must be true or false")
    if body["lot_sensitive"] and "filament" not in scope:
        raise CatalogError(f"{where}.lot_sensitive needs filament in scope")
    if body["baseline"] not in BASELINES:
        raise CatalogError(f"{where}.baseline must be one of {', '.join(BASELINES)}")
    if (body["baseline"] == "assumed") == ("filament" in scope):
        raise CatalogError(
            f"{where}: baseline is `required` exactly for filament-scoped kinds (a new filament is what "
            "makes one missing) and `assumed` for the rest (factory state until invalidated)"
        )

    values = _values(where, body["values"])
    order = _strings(f"{where}.order", body.get("order", []), tuple(values))
    if any(values[n].type != "number" for n in order):
        raise CatalogError(f"{where}.order lists only number values")
    gcode = _gcode(where, body.get("gcode", {}), values)
    apply = _strings(f"{where}.apply", body["apply"], None, non_empty=True)
    for target in apply:
        scheme, _, rest = target.partition(":")
        if scheme not in APPLY_SCHEMES or (scheme != "journal" and not rest):
            raise CatalogError(
                f"{where}.apply: {target!r} must be orca:<key>, klipper:<command> or journal"
            )
    est = body["estimate"]
    if not isinstance(est, dict) or set(est) != {"minutes", "grams"}:
        raise CatalogError(f"{where}.estimate must be {{minutes, grams}}")
    for k in ("minutes", "grams"):
        if isinstance(est[k], bool) or not isinstance(est[k], (int, float)) or est[k] < 0:
            raise CatalogError(f"{where}.estimate.{k} must be a number ≥ 0")
    models = body.get("models")
    return Kind(
        id=name,
        title=_text(f"{where}.title", body["title"]),
        scope=scope,
        lot_sensitive=bool(body["lot_sensitive"]),
        baseline=str(body["baseline"]),
        invalidated_by=_strings(f"{where}.invalidated_by", body["invalidated_by"], TRIGGERS),
        methods=_strings(f"{where}.methods", body["methods"], None, non_empty=True),
        values=values,
        order=order,
        gcode=gcode,
        apply=apply,
        estimate_minutes=float(est["minutes"]),
        estimate_grams=float(est["grams"]),
        models=None if models is None else _strings(f"{where}.models", models, None, non_empty=True),
        sources=_strings(f"{where}.sources", body["sources"], None, non_empty=True),
    )


def _text(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where} must be a non-empty string")
    return value.strip()


def _strings(
    where: str, value: Any, allowed: tuple[str, ...] | None, *, non_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or (non_empty and not value):
        raise CatalogError(f"{where} must be a {'non-empty ' if non_empty else ''}list")
    out = tuple(_text(where, v) for v in value)
    if allowed is not None:
        bad = [v for v in out if v not in allowed]
        if bad:
            raise CatalogError(f"{where}: unknown {', '.join(bad)}; accepted: {', '.join(allowed)}")
    if len(set(out)) != len(out):
        raise CatalogError(f"{where} lists an entry twice")
    return out


def _values(where: str, raw: Any) -> dict[str, ValueSpec]:
    if not isinstance(raw, dict) or not raw:
        raise CatalogError(f"{where}.values must be a non-empty mapping")
    out: dict[str, ValueSpec] = {}
    for name, spec in raw.items():
        w = f"{where}.values.{name}"
        if not isinstance(spec, dict):
            raise CatalogError(f"{w} must be a mapping")
        extra = sorted(str(k) for k in spec if k not in _VALUE_KEYS)
        if extra:
            raise CatalogError(f"{w}: unknown key(s) {', '.join(extra)}")
        vtype = spec.get("type")
        if vtype not in VALUE_TYPES:
            raise CatalogError(f"{w}.type must be one of {', '.join(VALUE_TYPES)}")
        lo: Any = spec.get("min")
        hi: Any = spec.get("max")
        if vtype == "number":
            numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi))
            if not numbers:
                raise CatalogError(f"{w}: min and max are required for a number and must be numbers")
            if lo > hi:
                raise CatalogError(f"{w}: min > max")
        elif lo is not None or hi is not None:
            raise CatalogError(f"{w}: min/max apply to numbers only")
        optional = spec.get("optional", False)
        if not isinstance(optional, bool):
            raise CatalogError(f"{w}.optional must be true or false")
        unit = spec.get("unit")
        out[str(name)] = ValueSpec(
            name=str(name),
            type=str(vtype),
            min=None if lo is None else float(lo),
            max=None if hi is None else float(hi),
            unit=None if unit is None else _text(f"{w}.unit", unit),
            optional=optional,
        )
    return out


def _gcode(where: str, raw: Any, values: dict[str, ValueSpec]) -> tuple[GcodeCheck, ...]:
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}.gcode must be a mapping")
    out: list[GcodeCheck] = []
    for name, spec in raw.items():
        w = f"{where}.gcode.{name}"
        if name not in values or values[name].type != "number":
            raise CatalogError(f"{w}: {name} is not a number value of this kind")
        if not isinstance(spec, dict):
            raise CatalogError(f"{w} must be a mapping")
        extra = sorted(str(k) for k in spec if k not in _GCODE_KEYS)
        if extra:
            raise CatalogError(f"{w}: unknown key(s) {', '.join(extra)}")
        compare = spec.get("compare")
        if compare not in COMPARES:
            raise CatalogError(f"{w}.compare must be one of {', '.join(COMPARES)}")
        tol = spec.get("tolerance")
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) or tol < 0:
            raise CatalogError(f"{w}.tolerance must be a number ≥ 0")
        enabled_by = spec.get("enabled_by")
        out.append(
            GcodeCheck(
                value=str(name),
                key=_text(f"{w}.key", spec.get("key")),
                compare=str(compare),
                tolerance=float(tol),
                enabled_by=None if enabled_by is None else _text(f"{w}.enabled_by", enabled_by),
            )
        )
    return tuple(out)
