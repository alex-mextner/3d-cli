"""filament_defaults.py — default values PrintOps gives a filament's extra fields, and the pass that
fills them on existing filaments.

ACCESSED VIA: `printops.spools` (a filament it creates gets the defaults), `3d printops fields
fill-defaults` (printops.cli.fields) and 3d-api `GET|POST /v1/fields/fill-defaults`.

THE RULE (one place; no call site hard-codes a number): `max_speed_mm_s`, the filament's maximum
print speed in mm/s (extra field `max_speed_mm_s`; unrelated to `max_volumetric_mm3_s`, which is
mm³/s). First matching rule of MAX_SPEED_RULES wins, else DEFAULT_MAX_SPEED_MM_S:
  - material "PLA+" (case/space-insensitive) or a name containing "PLA+" (e.g. "Elegoo RAPID PLA+") → 600
  - everything else → 300

INVARIANTS:
  - A value is only ever written where the field is EMPTY: a number typed in the Spoolman UI (any
    number, also a different one) is kept, and a caller-given value at creation wins.
  - Dry run (`apply=False`) only reads. Idempotent: a second apply finds nothing empty and writes
    nothing. A SpoolmanError on one filament marks that row `failed`; the others still run.
  - Nothing in PrintOps consumes max_speed_mm_s yet: it is data for the user and future gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from printops.spoolman import JSON, Spoolman, SpoolmanError, decode_extra, encode_extra

FIELD = "max_speed_mm_s"
DEFAULT_MAX_SPEED_MM_S = 300
PLA_PLUS_MAX_SPEED_MM_S = 600
# (material, name fragment) — a rule matches when the material equals `material` or the name
# contains `fragment`, both compared lower-case with spaces removed. Order = priority.
MAX_SPEED_RULES: tuple[tuple[str, str, int], ...] = (("pla+", "pla+", PLA_PLUS_MAX_SPEED_MM_S),)


def _squash(text: Any) -> str:
    return "".join(str(text or "").lower().split())


def max_speed_default(material: str | None, name: str | None) -> int:
    """The default max print speed (mm/s) for a filament of this material and name."""
    mat, nm = _squash(material), _squash(name)
    for rule_material, fragment, value in MAX_SPEED_RULES:
        if mat == rule_material or fragment in nm:
            return value
    return DEFAULT_MAX_SPEED_MM_S


def creation_extra(spec: Mapping[str, Any], given: int | None) -> dict[str, str]:
    """The extra fields a newly created filament gets: the caller's `given` value, else the default."""
    value = given if given is not None else max_speed_default(spec.get("material"), spec.get("name"))
    return {FIELD: encode_extra(value)}


@dataclass(frozen=True)
class FillRow:
    filament_id: int
    filament: str  # "vendor name"
    material: str
    old: Any  # decoded stored value, None when empty
    new: int | None  # the value this run sets (None for keep)
    action: str  # set | keep | failed
    detail: str

    def to_dict(self) -> JSON:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class FillReport:
    apply: bool
    rows: list[FillRow]

    @property
    def failed(self) -> bool:
        return any(r.action == "failed" for r in self.rows)

    @property
    def pending(self) -> bool:
        return not self.apply and any(r.action == "set" for r in self.rows)

    def to_dict(self) -> JSON:
        return {
            "apply": self.apply,
            "field": f"filament.{FIELD}",
            "rows": [r.to_dict() for r in self.rows],
            "summary": {a: sum(1 for r in self.rows if r.action == a) for a in ("set", "keep", "failed")},
            "failed": self.failed,
            "pending": self.pending,
        }


def _stored(fil: Mapping[str, Any]) -> Any:
    raw = (fil.get("extra") or {}).get(FIELD)
    if raw is None:
        return None
    try:
        return decode_extra(raw)
    except ValueError:
        return raw  # typed by hand and not JSON: still not empty, never overwritten


def _row(spoolman: Spoolman, fil: Mapping[str, Any], *, apply: bool) -> FillRow:
    vendor = (fil.get("vendor") or {}).get("name")
    label = " ".join(str(p) for p in (vendor, fil.get("name")) if p) or f"filament {fil['id']}"
    material = str(fil.get("material") or "")
    old = _stored(fil)
    fid = int(fil["id"])
    if old is not None:
        return FillRow(fid, label, material, old, None, "keep", f"already {old!r}; kept")
    value = max_speed_default(fil.get("material"), fil.get("name"))
    if not apply:
        return FillRow(fid, label, material, None, value, "set", f"{FIELD} := {value}")
    try:
        reply = spoolman.update("filament", fid, {"extra": {FIELD: encode_extra(value)}})
    except SpoolmanError as e:
        return FillRow(fid, label, material, None, value, "failed", f"Spoolman refused the write: {e.message}")
    if _stored(reply) != value:
        return FillRow(fid, label, material, None, value, "failed", f"Spoolman answered {_stored(reply)!r}")
    return FillRow(fid, label, material, None, value, "set", f"{FIELD} := {value}")


def fill_defaults(spoolman: Spoolman, *, apply: bool) -> FillReport:
    """Set max_speed_mm_s on every filament where it is empty (see the module doc)."""
    return FillReport(apply, [_row(spoolman, fil, apply=apply) for fil in spoolman.find("filament")])
