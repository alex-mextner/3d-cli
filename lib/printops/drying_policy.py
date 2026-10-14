"""drying_policy.py — the `drying:` section of fleet/policy.yaml: hygroscopic classes and material defaults.

ACCESSED VIA: `printops.policy.section("drying")` (registered in policy.SECTION_PARSERS) and
`printops.drying`. Pure: no DB, no network.

SCHEMA (all keys required unless noted):
  check_every_min: int ≥ 1        how often 3d-api re-assesses every spool
  fresh_days: number ≥ 0          a spool dried less than this ago is `fresh`, then `ok`
  wet_factor: number > 1          exposure ≥ limit × wet_factor → `wet` (instead of `due`)
  default_class: low|medium|high|extreme   for materials the table does not know
  classes: {low|medium|high|extreme: {max_days_opened, max_days_since_dried, max_rh}}
           every class present; days > 0; max_rh in 0..100 (% RH of the storage location)
  sources: {key: https URL}       where the material numbers come from
  materials: {NAME: {class, temp_c, hours, source}}   NAME upper-case as Spoolman `material`;
           `source` is a key of `sources`

MATERIAL MATCH: exact name, else the longest table name the material starts with followed by a
non-alphanumeric character ("PLA+" → PLA, "PA12-CF" → PA12, "PLA Silk" → PLA, but "PCTG" is
not PC). Filament extra fields `hygroscopic_class`, `drying_temp_c`, `drying_hours` override.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from printops.policy import PolicyError

CLASSES: tuple[str, ...] = ("low", "medium", "high", "extreme")
_CLASS_KEYS = ("max_days_opened", "max_days_since_dried", "max_rh")
_TOP_KEYS = (
    "check_every_min",
    "fresh_days",
    "wet_factor",
    "default_class",
    "classes",
    "sources",
    "materials",
)
_MATERIAL_KEYS = ("class", "temp_c", "hours", "source")


@dataclass(frozen=True)
class ClassLimits:
    max_days_opened: float
    max_days_since_dried: float
    max_rh: float


@dataclass(frozen=True)
class MaterialDefaults:
    name: str
    hygroscopic_class: str
    temp_c: int
    hours: float
    source: str


@dataclass(frozen=True)
class Recipe:
    """What applies to one filament after overrides; `origin` says where each part came from."""

    hygroscopic_class: str
    temp_c: int | None
    hours: float | None
    origin: str  # e.g. "material PLA (bambu+prusa)", "filament extra", "default_class"


@dataclass(frozen=True)
class DryingPolicy:
    check_every_min: int
    fresh_days: float
    wet_factor: float
    default_class: str
    classes: dict[str, ClassLimits]
    sources: dict[str, str]
    materials: dict[str, MaterialDefaults]

    def material(self, material: str | None) -> MaterialDefaults | None:
        if not material:
            return None
        name = material.strip().upper()
        if name in self.materials:
            return self.materials[name]
        best: MaterialDefaults | None = None
        for key, row in self.materials.items():
            if (
                name.startswith(key)
                and not name[len(key)].isalnum()
                and (best is None or len(key) > len(best.name))
            ):
                best = row
        return best

    def recipe(self, filament: Mapping[str, Any]) -> Recipe:
        """Class / temperature / hours for a Spoolman filament (dict with `material`, decoded `extra`)."""
        extra = filament.get("extra") or {}
        row = self.material(filament.get("material"))
        cls = row.hygroscopic_class if row else self.default_class
        temp = row.temp_c if row else None
        hours = row.hours if row else None
        origin = f"material {row.name} ({row.source})" if row else "default_class"
        overridden = []
        if extra.get("hygroscopic_class") in CLASSES:
            cls = str(extra["hygroscopic_class"])
            overridden.append("class")
        if _number(extra.get("drying_temp_c")):
            temp = int(extra["drying_temp_c"])
            overridden.append("temp")
        if _number(extra.get("drying_hours")):
            hours = float(extra["drying_hours"])
            overridden.append("hours")
        if overridden:
            origin += f"; filament extra: {', '.join(overridden)}"
        return Recipe(cls, temp, hours, origin)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _num(
    where: str, value: Any, *, lo: float, hi: float | None = None, lo_open: bool = False
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PolicyError(f"{where} must be a number, got {value!r}")
    if (value <= lo if lo_open else value < lo) or (hi is not None and value > hi):
        bound = f"> {lo}" if lo_open else f"≥ {lo}"
        raise PolicyError(
            f"{where} must be {bound}{f' and ≤ {hi}' if hi is not None else ''}, got {value}"
        )
    return float(value)


def _keys(where: str, body: Any, required: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(body, dict):
        raise PolicyError(f"{where} must be a mapping")
    missing = [k for k in required if k not in body]
    extra = [str(k) for k in body if k not in required]
    if missing:
        raise PolicyError(f"{where}: missing key(s) {', '.join(missing)}")
    if extra:
        raise PolicyError(f"{where}: unknown key(s) {', '.join(extra)}")
    return body


def parse(body: Mapping[str, Any]) -> DryingPolicy:
    top = _keys("drying", body, _TOP_KEYS)
    every = top["check_every_min"]
    if not isinstance(every, int) or isinstance(every, bool) or every < 1:
        raise PolicyError(f"check_every_min must be an integer ≥ 1, got {every!r}")
    default_class = top["default_class"]
    if default_class not in CLASSES:
        raise PolicyError(
            f"default_class must be one of {', '.join(CLASSES)}, got {default_class!r}"
        )

    classes_raw = _keys("classes", top["classes"], CLASSES)
    classes: dict[str, ClassLimits] = {}
    for name in CLASSES:
        row = _keys(f"classes.{name}", classes_raw[name], _CLASS_KEYS)
        classes[name] = ClassLimits(
            max_days_opened=_num(
                f"classes.{name}.max_days_opened",
                row["max_days_opened"],
                lo=0,
                lo_open=True,
            ),
            max_days_since_dried=_num(
                f"classes.{name}.max_days_since_dried",
                row["max_days_since_dried"],
                lo=0,
                lo_open=True,
            ),
            max_rh=_num(f"classes.{name}.max_rh", row["max_rh"], lo=0, hi=100),
        )

    sources_raw = top["sources"]
    if not isinstance(sources_raw, dict) or not sources_raw:
        raise PolicyError("sources must be a non-empty mapping of key → https URL")
    sources: dict[str, str] = {}
    for key, url in sources_raw.items():
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PolicyError(f"sources.{key} must be an https URL, got {url!r}")
        sources[str(key)] = url

    materials_raw = top["materials"]
    if not isinstance(materials_raw, dict) or not materials_raw:
        raise PolicyError("materials must be a non-empty mapping")
    materials: dict[str, MaterialDefaults] = {}
    for raw_name, raw_row in materials_raw.items():
        name = str(raw_name)
        if name != name.strip().upper() or not name:
            raise PolicyError(
                f"materials: name {name!r} must be upper-case without surrounding spaces"
            )
        row = _keys(f"materials.{name}", raw_row, _MATERIAL_KEYS)
        if row["class"] not in CLASSES:
            raise PolicyError(
                f"materials.{name}.class must be one of {', '.join(CLASSES)}, got {row['class']!r}"
            )
        temp = row["temp_c"]
        if not isinstance(temp, int) or isinstance(temp, bool) or not 20 <= temp <= 150:
            raise PolicyError(
                f"materials.{name}.temp_c must be an integer 20..150, got {temp!r}"
            )
        cited = [s.strip() for s in str(row["source"]).split("+")]
        unknown = [s for s in cited if s not in sources]
        if unknown:
            raise PolicyError(
                f"materials.{name}.source {row['source']!r} cites unknown source(s) {', '.join(unknown)}"
            )
        materials[name] = MaterialDefaults(
            name=name,
            hygroscopic_class=str(row["class"]),
            temp_c=temp,
            hours=_num(
                f"materials.{name}.hours", row["hours"], lo=0, hi=96, lo_open=True
            ),
            source=str(row["source"]),
        )
    return DryingPolicy(
        check_every_min=every,
        fresh_days=_num("fresh_days", top["fresh_days"], lo=0),
        wet_factor=_num("wet_factor", top["wet_factor"], lo=1, lo_open=True),
        default_class=str(default_class),
        classes=classes,
        sources=sources,
        materials=materials,
    )
