"""calibration_policy.py — the `calibrations:` section of fleet/policy.yaml (plan §8.1, #167).

ACCESSED VIA: `printops.policy.section("calibrations")` (registered in policy.SECTION_PARSERS)
and printops.calibrations. Pure: no DB, no network.

A requirement the engine finds carries a REASON; the policy maps (printer, kind, reason) to a
LEVEL: block (gate FAIL), warn (WARN) or off (not reported).

REASONS:
  new_material_type  no record of this kind for this material type on this scope (D6: block)
  new_vendor         records exist for the material type, none from this vendor (D6: block)
  new_filament       records exist for the vendor + material, not for this filament product
  new_nozzle         records for this filament exist only for another nozzle
  new_lot            a record for this filament exists but for another (or unknown) lot (D6: warn)
  invalidated        the matching record is older than a `calibration.invalidated` event
                     (maintenance, nozzle swap, …) for its printer/head
  expired            the matching record is past its `valid_until`
  gcode_mismatch     the G-code footer carries a value other than the recorded one

SCHEMA:
  levels:   {REASON: block|warn|off}           every reason, required
  kinds:    {KIND: LEVEL | {REASON: LEVEL}}    optional per-kind overrides
  printers: {PRINTER: LEVEL | {KIND: LEVEL | {REASON: LEVEL}}}   optional; KIND may be `all`
PRECEDENCE (most specific wins): printers[p][kind][reason] > printers[p][kind] >
  printers[p].all > printers[p] (a bare level) > kinds[kind][reason] > kinds[kind] > levels[reason].
Kind and printer names are checked against the catalog and the fleet by `3d lint --printops`
(calibrations/policy), not here: this parser sees only the section.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from printops.policy import PolicyError

LEVELS = ("block", "warn", "off")
REASONS = (
    "new_material_type",
    "new_vendor",
    "new_filament",
    "new_nozzle",
    "new_lot",
    "invalidated",
    "expired",
    "gcode_mismatch",
)
ALL = "all"  # wildcard key: every kind of a printer
_ANY = "*"  # internal wildcard for "every reason"
_TOP = ("levels", "kinds", "printers")


@dataclass(frozen=True)
class CalibrationPolicy:
    levels: dict[str, str]
    kinds: dict[str, dict[str, str]]  # kind -> reason|* -> level
    printers: dict[str, dict[str, dict[str, str]]]  # printer -> kind|all -> reason|* -> level

    def level(self, printer: str | None, kind: str, reason: str) -> str:
        if reason not in REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        rules = self.printers.get(printer or "", {})
        for key in (kind, ALL):
            by_reason = rules.get(key)
            if by_reason:
                hit = by_reason.get(reason) or by_reason.get(_ANY)
                if hit:
                    return hit
        by_reason = self.kinds.get(kind, {})
        return by_reason.get(reason) or by_reason.get(_ANY) or self.levels[reason]

    def disabled(self, printer: str | None, kind: str) -> bool:
        """Every reason is `off` for this kind on this printer: the engine skips the kind."""
        return all(self.level(printer, kind, r) == "off" for r in REASONS)

    def kind_names(self) -> set[str]:
        """Every kind the section names (for the catalog cross-check)."""
        names = set(self.kinds)
        for rules in self.printers.values():
            names |= {k for k in rules if k != ALL}
        return names


def _yaml_off(value: Any) -> Any:
    """YAML 1.1 (PyYAML) reads a bare `off` as False; in this section it can only mean the level."""
    return "off" if value is False else value


def _level(where: str, value: Any) -> str:
    value = _yaml_off(value)
    if value not in LEVELS:
        raise PolicyError(f"{where} must be one of {', '.join(LEVELS)}, got {value!r}")
    return str(value)


def _by_reason(where: str, value: Any) -> dict[str, str]:
    """LEVEL or {REASON: LEVEL} → {reason|*: level}."""
    value = _yaml_off(value)
    if isinstance(value, str):
        return {_ANY: _level(where, value)}
    if not isinstance(value, dict) or not value:
        raise PolicyError(f"{where} must be a level or a non-empty {{reason: level}} mapping")
    out: dict[str, str] = {}
    for reason, lvl in value.items():
        if reason not in REASONS:
            raise PolicyError(f"{where}: unknown reason {reason!r}; accepted: {', '.join(REASONS)}")
        out[str(reason)] = _level(f"{where}.{reason}", lvl)
    return out


def parse(body: Mapping[str, Any]) -> CalibrationPolicy:
    extra = sorted(str(k) for k in body if k not in _TOP)
    if extra:
        raise PolicyError(f"unknown key(s) {', '.join(extra)}; accepted: {', '.join(_TOP)}")
    raw_levels = body.get("levels")
    if not isinstance(raw_levels, dict):
        raise PolicyError("levels must be a mapping {reason: block|warn|off}")
    missing = [r for r in REASONS if r not in raw_levels]
    unknown = sorted(str(r) for r in raw_levels if r not in REASONS)
    if missing:
        raise PolicyError(f"levels: missing reason(s) {', '.join(missing)}")
    if unknown:
        raise PolicyError(f"levels: unknown reason(s) {', '.join(unknown)}")
    levels = {r: _level(f"levels.{r}", raw_levels[r]) for r in REASONS}

    raw_kinds = body.get("kinds") or {}
    if not isinstance(raw_kinds, dict):
        raise PolicyError("kinds must be a mapping {kind: level | {reason: level}}")
    kinds = {str(k): _by_reason(f"kinds.{k}", v) for k, v in raw_kinds.items()}

    raw_printers = body.get("printers") or {}
    if not isinstance(raw_printers, dict):
        raise PolicyError("printers must be a mapping {printer: level | {kind: level | {reason: level}}}")
    printers: dict[str, dict[str, dict[str, str]]] = {}
    for p, raw_rules in raw_printers.items():
        rules = _yaml_off(raw_rules)
        if isinstance(rules, str):
            printers[str(p)] = {ALL: {_ANY: _level(f"printers.{p}", rules)}}
            continue
        if not isinstance(rules, dict) or not rules:
            raise PolicyError(f"printers.{p} must be a level or a non-empty {{kind: …}} mapping")
        printers[str(p)] = {str(k): _by_reason(f"printers.{p}.{k}", v) for k, v in rules.items()}
    return CalibrationPolicy(levels=levels, kinds=kinds, printers=printers)
