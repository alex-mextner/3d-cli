"""fits_registry.py — bridge from a free-text printer/material name to the registries `fits.resolve` cites.

WHAT: a user types `--printer k1se --material "Rapid PLA+"`. The fit table's correction rows are keyed by
names (a fleet key such as `k1se`, a model name such as `Creality K1 SE`, a material such as `PLA`), so this
module answers two questions: which spellings of the same machine/filament may a correction row use, and is
the name known to the registries at all (the provenance says so; an unknown name is not an error).

ACCESSED VIA: `fits.resolve` (lazily, only when a printer/material is named) and `3d fits resolve`.

INVARIANTS:
  - Printers: the fleet registry (`fleet/printers.yaml`, key -> model) first, then the printer MODEL registry
    (`lib/data/printers.yaml` + user/project layers). A fleet key matches its own rows and its model's rows;
    a model name matches model rows only (rows of one physical unit never leak to another unit of the model).
  - Materials: a name matches a registry material by normalised name, else by one of its words
    ("Elegoo Rapid PLA+" -> `PLA`). A material outside the registry is unknown (`known: false`).
  - No data is invented here: an unreadable registry is reported in the provenance `note` and the name is
    treated as unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from errors import ThreeDError
from fits import norm_name

_WORDS = re.compile(r"[\s_/,.-]+")


@dataclass(frozen=True)
class PrinterIdentity:
    input: str
    key: str | None  # fleet key, when the name is one
    model: str | None  # printer model name, when known
    known: bool
    source: str | None
    note: str | None = None

    def names(self) -> tuple[str, ...]:
        """Every spelling a correction row may use for this printer, the most specific first."""
        return tuple(dict.fromkeys(n for n in (self.input, self.key, self.model) if n))

    def to_provenance(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "kind": "printer",
            "input": self.input,
            "key": self.key,
            "model": self.model,
            "known": self.known,
            "source": self.source,
        }
        if not self.known:
            entry["message"] = (
                f"{self.input!r} is not in the fleet or printer registries: only '*' printer rows can apply"
            )
        if self.note:
            entry["note"] = self.note
        return entry


@dataclass(frozen=True)
class MaterialIdentity:
    input: str
    name: str | None  # registry material the input resolved to
    known: bool
    via: str | None
    shrink_xy_pct: float | None
    shrink_z_pct: float | None
    note: str | None = None

    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for n in (self.input, self.name) if n))

    def to_provenance(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "kind": "material",
            "input": self.input,
            "name": self.name,
            "known": self.known,
            "via": self.via,
            "source": "materials.yaml" if self.known else None,
        }
        if not self.known:
            entry["message"] = (
                f"{self.input!r} is not in the material registry: only '*' material rows can apply"
            )
        if self.note:
            entry["note"] = self.note
        return entry


def _fleet_models() -> tuple[dict[str, str], str | None]:
    """Fleet key -> model, plus a note when the fleet file is unreadable (fleet disabled = empty, no note)."""
    from printops.fleet import load_fleet

    try:
        fleet = load_fleet()
    except ThreeDError as exc:
        return {}, f"fleet registry unreadable: {exc}"
    if fleet is None:
        return {}, None
    return {key: p.model for key, p in fleet.printers.items()}, None


def printer_identity(name: str) -> PrinterIdentity:
    """Look a printer name up in the fleet registry, then in the model registry."""
    from registries.printers import load_printers

    wanted = norm_name(name)
    fleet, note = _fleet_models()
    for key, model in fleet.items():
        if norm_name(key) == wanted:
            return PrinterIdentity(name, key, model, True, "fleet/printers.yaml", note)
    try:
        models = list(load_printers())
    except ThreeDError as exc:
        models = []
        note = f"{note}; " if note else ""
        note += f"printer registry unreadable: {exc}"
    for model in models:
        if norm_name(model) == wanted:
            return PrinterIdentity(name, None, model, True, "printers.yaml", note)
    return PrinterIdentity(name, None, None, False, None, note)


def material_identity(name: str) -> MaterialIdentity:
    """Look a material name up in the material registry (name, else one of its words)."""
    from registries.materials import load_materials

    try:
        registry = load_materials()
    except ThreeDError as exc:
        return MaterialIdentity(
            name, None, False, None, None, None, f"material registry unreadable: {exc}"
        )
    by_norm = {norm_name(key): key for key in registry}
    candidates = [("name", name)] + [
        (f"word {w!r}", w)
        for w in sorted(_WORDS.split(name), key=len, reverse=True)
        if w
    ]
    for via, text in candidates:
        key = by_norm.get(norm_name(text))
        if key is not None:
            material = registry[key]
            return MaterialIdentity(
                name, key, True, via, material.shrink_xy_pct, material.shrink_z_pct
            )
    return MaterialIdentity(name, None, False, None, None, None)
