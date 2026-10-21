"""gates_policy.py — the `gates:` section of fleet/policy.yaml: levels and limits of the PrintOps
material gates in `3d print-preflight` (issue #164, plan §9.1).

ACCESSED VIA: `printops.policy.section("gates")` (registered in policy.SECTION_PARSERS), read by
lib/printer_ops/printops_checks.py on every preflight. Pure: no DB, no network.

SCHEMA (all keys required unless noted):
  snapshot_max_age_h: number > 0   an offline snapshot older than this is a WARN (refresh it)
  levels: {CHECK: block|warn|off}  every check in CHECKS. A check's hard finding is FAIL under
                                   `block`, WARN under `warn`; `off` does not run the check.
                                   Soft findings (vendor differs, "weigh the spool") are WARN.
  printers: {NAME: {CHECK: level}} optional per-printer overrides of `levels`
  material_aliases: {NAME: NAME}   optional; Spoolman `material` spellings that mean a G-code
                                   `filament_type` (upper-case both sides: "PLA+": PLA)
  colour: {warn_delta_e, fail_delta_e, projects: [{match, warn_delta_e, fail_delta_e, why}]}
          CIEDE2000 between the G-code's filament_colour and the loaded spool's colour; the
          first project whose shell glob `match` matches the G-code path (absolute) wins.
          0 < warn ≤ fail.
  filament_sufficient: {purge_g_per_tool, margin_g, margin_pct}   need = G-code grams +
          purge_g_per_tool + max(margin_g, margin_pct % of the G-code grams)
  nozzle_compat: {abrasive_ok: [nozzle material], abrasive_markers: [TOKEN]}
          abrasive_ok ⊆ fleet NOZZLE_MATERIALS; a filament is abrasive when Spoolman says so
          (filament extra `abrasive`) or its material/name/type has a marker token (PLA-CF,
          PA12 GF). A false flag cannot hide a positive marker. Independently, a positive
          `required_nozzle_HRC` minimum is compared with the nozzle material's known nominal
          hardness; a nonzero minimum alone does not classify a filament as abrasive.
  enclosure_material: {materials: [NAME]}   materials that want a closed printer (warping, fumes)
  plate_material: [{materials: [NAME], plates: [fleet PLATE_TYPES], why}]   bad pairings
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from printops.fleet import NOZZLE_MATERIALS, PLATE_TYPES
from printops.policy import PolicyError

CHECKS: tuple[str, ...] = (
    "printer_profile_match",
    "nozzle_compat",
    "filament_temp_range",
    "loaded_filament_match",
    "slot_binding_known",
    "filament_sufficient",
    "drying_due",
    "maintenance_due",
    "enclosure_material",
    "plate_material",
)
LEVELS: tuple[str, ...] = ("block", "warn", "off")
_TOP_KEYS = (
    "snapshot_max_age_h",
    "levels",
    "colour",
    "filament_sufficient",
    "nozzle_compat",
    "enclosure_material",
    "plate_material",
)
_OPTIONAL_TOP = ("printers", "material_aliases")


@dataclass(frozen=True)
class ColourRule:
    warn_delta_e: float
    fail_delta_e: float
    match: str = ""  # shell glob on the G-code path; "" = the default rule
    why: str = ""


@dataclass(frozen=True)
class PlateRule:
    materials: tuple[str, ...]
    plates: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class GatesPolicy:
    snapshot_max_age_h: float
    levels: dict[str, str]
    printers: dict[str, dict[str, str]]
    material_aliases: dict[str, str]
    colour: ColourRule
    colour_projects: tuple[ColourRule, ...]
    purge_g_per_tool: float
    margin_g: float
    margin_pct: float
    abrasive_ok: tuple[str, ...]
    abrasive_markers: tuple[str, ...]
    enclosure_materials: tuple[str, ...]
    plate_rules: tuple[PlateRule, ...]

    def level(self, check: str, printer: str | None) -> str:
        """block | warn | off for `check` on `printer` (per-printer override, else `levels`)."""
        return self.printers.get(printer or "", {}).get(check, self.levels[check])

    def colour_rule(self, gcode_path: str) -> ColourRule:
        return next((r for r in self.colour_projects if fnmatch(gcode_path, r.match)), self.colour)

    def material(self, name: str | None) -> str | None:
        """Canonical spelling: upper-case, runs of space/underscore → '-', then the alias table."""
        if not name or not name.strip():
            return None
        canon = re.sub(r"[\s_]+", "-", name.strip().upper())
        return self.material_aliases.get(canon, canon)

    def is_abrasive_material(self, name: str | None) -> bool:
        tokens = set(re.split(r"[^A-Z0-9]+", (name or "").upper()))
        return any(m in tokens for m in self.abrasive_markers)


def _num(where: str, value: Any, *, lo: float = 0.0, lo_open: bool = False) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PolicyError(f"{where} must be a number, got {value!r}")
    if value < lo or (lo_open and value == lo):
        raise PolicyError(f"{where} must be {'>' if lo_open else '≥'} {lo:g}, got {value}")
    return float(value)


def _keys(where: str, body: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> Mapping[str, Any]:
    if not isinstance(body, dict):
        raise PolicyError(f"{where} must be a mapping")
    missing = [k for k in required if k not in body]
    extra = [str(k) for k in body if k not in required and k not in optional]
    if missing:
        raise PolicyError(f"{where}: missing key(s) {', '.join(missing)}")
    if extra:
        raise PolicyError(f"{where}: unknown key(s) {', '.join(extra)}")
    return body


def _names(where: str, value: Any, *, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise PolicyError(f"{where} must be a non-empty list of names")
    if allowed is not None:
        bad = [v for v in value if v not in allowed]
        if bad:
            raise PolicyError(f"{where}: unknown value(s) {', '.join(bad)}; known: {', '.join(allowed)}")
        return tuple(value)
    upper = [v for v in value if v != v.strip().upper()]
    if upper:
        raise PolicyError(f"{where}: names must be upper-case without surrounding spaces: {', '.join(upper)}")
    return tuple(value)


def _levels(where: str, body: Any, *, complete: bool) -> dict[str, str]:
    if not isinstance(body, dict):
        raise PolicyError(f"{where} must be a mapping of check → block|warn|off")
    unknown = [str(k) for k in body if k not in CHECKS]
    if unknown:
        raise PolicyError(f"{where}: unknown check(s) {', '.join(unknown)}; known: {', '.join(CHECKS)}")
    if complete:
        missing = [c for c in CHECKS if c not in body]
        if missing:
            raise PolicyError(f"{where}: missing check(s) {', '.join(missing)}")
    for check, level in body.items():
        if level not in LEVELS:
            raise PolicyError(f"{where}.{check} must be one of {', '.join(LEVELS)}, got {level!r}")
    return {str(k): str(v) for k, v in body.items()}


def _colour_rule(where: str, body: Any, *, project: bool) -> ColourRule:
    keys = ("match", "warn_delta_e", "fail_delta_e", "why") if project else ("warn_delta_e", "fail_delta_e")
    row = _keys(where, body, keys, () if project else ("projects",))
    warn = _num(f"{where}.warn_delta_e", row["warn_delta_e"], lo_open=True)
    fail = _num(f"{where}.fail_delta_e", row["fail_delta_e"], lo_open=True)
    if warn > fail:
        raise PolicyError(f"{where}: warn_delta_e {warn:g} must be ≤ fail_delta_e {fail:g}")
    if project and not (isinstance(row["match"], str) and row["match"].strip()):
        raise PolicyError(f"{where}.match must be a non-empty shell glob")
    return ColourRule(warn, fail, str(row.get("match", "")), str(row.get("why", "")))


def parse(body: Mapping[str, Any]) -> GatesPolicy:
    top = _keys("gates", body, _TOP_KEYS, _OPTIONAL_TOP)
    levels = _levels("levels", top["levels"], complete=True)
    printers_raw = top.get("printers") or {}
    if not isinstance(printers_raw, dict):
        raise PolicyError("printers must be a mapping of printer → {check: level}")
    printers = {str(p): _levels(f"printers.{p}", v, complete=False) for p, v in printers_raw.items()}

    aliases_raw = top.get("material_aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise PolicyError("material_aliases must be a mapping NAME → NAME")
    aliases: dict[str, str] = {}
    for k, v in aliases_raw.items():
        _names(f"material_aliases.{k}", [str(k), v])
        aliases[str(k)] = str(v)

    colour_raw = top["colour"]
    colour = _colour_rule("colour", colour_raw, project=False)
    projects_raw = colour_raw.get("projects", [])
    if not isinstance(projects_raw, list):
        raise PolicyError("colour.projects must be a list")
    projects = tuple(
        _colour_rule(f"colour.projects[{i}]", p, project=True) for i, p in enumerate(projects_raw)
    )

    fs = _keys("filament_sufficient", top["filament_sufficient"], ("purge_g_per_tool", "margin_g", "margin_pct"))
    nz = _keys("nozzle_compat", top["nozzle_compat"], ("abrasive_ok", "abrasive_markers"))
    enc = _keys("enclosure_material", top["enclosure_material"], ("materials",))
    plates_raw = top["plate_material"]
    if not isinstance(plates_raw, list):
        raise PolicyError("plate_material must be a list of {materials, plates, why}")
    plate_rules = []
    for i, raw in enumerate(plates_raw):
        row = _keys(f"plate_material[{i}]", raw, ("materials", "plates", "why"))
        if not (isinstance(row["why"], str) and row["why"].strip()):
            raise PolicyError(f"plate_material[{i}].why must say why the pairing is bad")
        plate_rules.append(
            PlateRule(
                _names(f"plate_material[{i}].materials", row["materials"]),
                _names(f"plate_material[{i}].plates", row["plates"], allowed=PLATE_TYPES),
                row["why"].strip(),
            )
        )
    return GatesPolicy(
        snapshot_max_age_h=_num("snapshot_max_age_h", top["snapshot_max_age_h"], lo_open=True),
        levels=levels,
        printers=printers,
        material_aliases=aliases,
        colour=colour,
        colour_projects=projects,
        purge_g_per_tool=_num("filament_sufficient.purge_g_per_tool", fs["purge_g_per_tool"]),
        margin_g=_num("filament_sufficient.margin_g", fs["margin_g"]),
        margin_pct=_num("filament_sufficient.margin_pct", fs["margin_pct"]),
        abrasive_ok=_names("nozzle_compat.abrasive_ok", nz["abrasive_ok"], allowed=NOZZLE_MATERIALS),
        abrasive_markers=_names("nozzle_compat.abrasive_markers", nz["abrasive_markers"]),
        enclosure_materials=_names("enclosure_material.materials", enc["materials"]),
        plate_rules=tuple(plate_rules),
    )
