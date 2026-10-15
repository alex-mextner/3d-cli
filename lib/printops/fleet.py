"""fleet.py — the fleet registry fleet/printers.yaml: typed loader + schema validation.

WHAT: every printer we print on — ours, the neighbour's, Boris's, the hackspace's — with owner,
  access level and topology: heads (label T1..Tn <-> index 0..n-1, Klipper extruder, nozzle,
  usable + reason), AMS-like units, enclosure, chamber heater, build plates (plan §7.1,
  https://github.com/alex-mextner/3d-cli/issues/157). `3d printer list` prints it, the device
  settings of full/read-only entries are the base layer of printer_ops.devices (the user and
  project printer-devices.yaml still replace them per name), `3d lint --printops` checks it.

WHY: which head is broken, who owns a printer and whether 3d may talk to it lived as free text
  in project notes (babushkin-dom gen/queue.json: "голова T2 не работает") or nowhere; the
  gates and the planner need it typed and checked.

INVARIANTS:
  - stdlib + lazy pyyaml (printops package contract).
  - `offline` entries (other people's printers, decision D7) carry no contact settings (only
    STATIC_KEYS: axis_limits, trusted_macros, bridge_max_mm) and are never contacted;
    device_specs() leaves them out, so printer_ops never builds an adapter for them.
  - validate() reports EVERY problem (for lint); load_fleet() raises FleetError (exit 2) with
    all of them. A key absent from an entry means "not known", never a default guess.
  - The file is found at <repo>/fleet/printers.yaml; THREED_FLEET=<path> overrides it and
    THREED_FLEET="" disables the layer (the test suite does this so no test can reach a real
    printer through the repo file).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import MissingDependency, ThreeDError

ENV = "THREED_FLEET"
OWNERS = ("own", "neighbour", "boris", "hackerspace")
ACCESS = ("full", "read-only", "offline")
NOZZLE_MATERIALS = ("brass", "stainless", "hardened", "ruby", "tungsten")
NOZZLE_TYPES = ("std", "hf")
AMS_KINDS = ("ams", "ams_lite", "ams_2_pro", "ams_ht", "cfs", "other")
ENCLOSURES = ("none", "partial", "full")
PLATE_TYPES = (
    "smooth_pei",
    "textured_pei",
    "cool_plate",
    "engineering_plate",
    "high_temp_plate",
    "epoxy",
    "glass",
)
# Settings printer_ops.devices reads (it validates them); fleet only routes them there.
DEVICE_KEYS = frozenset(
    {
        "adapter",
        "host",
        "port",
        "ws_port",
        "axis_limits",
        "trusted_macros",
        "bridge_max_mm",
        "resume",
        "watch",
    }
)
# What an offline printer may carry: facts about the machine used by the G-code gates
# (`3d print-preflight --printer NAME`), nothing that says how to reach it.
STATIC_KEYS = frozenset({"axis_limits", "trusted_macros", "bridge_max_mm"})
FLEET_KEYS = frozenset(
    {
        "model",
        "owner",
        "access",
        "heads",
        "ams",
        "enclosure",
        "chamber_heater",
        "plates",
        "notes",
        "sources",
    }
)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_HEAD_KEYS = frozenset({"label", "index", "extruder", "nozzle", "usable", "reason"})
_NOZZLE_KEYS = frozenset({"diameter_mm", "material", "type"})


class FleetError(ThreeDError):
    """fleet/printers.yaml is unreadable or breaks the schema. Exit 2."""

    exit_code = 2


@dataclass(frozen=True)
class Nozzle:
    diameter_mm: float
    material: str | None = None  # NOZZLE_MATERIALS; None = not known
    type: str | None = None  # std | hf; None = not known


@dataclass(frozen=True)
class Head:
    label: str  # T1..Tn, as the printer's screen and G-code comments name it
    index: int  # 0-based tool index (T1 <-> 0)
    extruder: str | None  # Klipper object name (extruder, extruder1, ...)
    nozzle: Nozzle | None
    usable: bool
    reason: str | None  # why not usable


@dataclass(frozen=True)
class AmsUnit:
    id: str
    kind: str
    slots: int


@dataclass(frozen=True)
class FleetPrinter:
    name: str
    model: str
    owner: str
    access: str
    heads: tuple[Head, ...]
    ams: tuple[AmsUnit, ...] | None  # None = not known; () = none
    enclosure: str | None
    chamber_heater: bool | None
    plates: tuple[str, ...] | None
    notes: str | None
    sources: tuple[str, ...]
    device: dict[str, Any]  # DEVICE_KEYS present in the entry, untouched
    source: Path

    @property
    def contactable(self) -> bool:
        return self.access != "offline"


@dataclass(frozen=True)
class Fleet:
    path: Path
    printers: dict[str, FleetPrinter]

    def device_specs(self) -> dict[str, dict[str, Any]]:
        """printer-devices.yaml-shaped entries of the printers 3d may contact (not offline)."""
        return {
            name: {**p.device, "access": p.access}
            for name, p in self.printers.items()
            if p.contactable
        }


def fleet_path() -> Path | None:
    """The fleet file to read, or None when THREED_FLEET="" disables it."""
    env = os.environ.get(ENV)
    if env is not None:
        return Path(env).expanduser() if env.strip() else None
    from cli.env import repo_root

    return Path(repo_root()) / "fleet" / "printers.yaml"


def _yaml() -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise MissingDependency(
            "pyyaml",
            install="uv sync",
            degrades="fleet/printers.yaml cannot be read",
            command="printer",
        ) from exc
    return yaml


def read_yaml(path: Path) -> Any:
    yaml = _yaml()
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FleetError(f"{path}: cannot read: {exc}", command="printer") from exc
    except yaml.YAMLError as exc:
        raise FleetError(f"{path}: invalid YAML: {exc}", command="printer") from exc


def load_fleet(path: Path | None = None) -> Fleet | None:
    """Parsed fleet; None when the layer is disabled or the default file is absent.
    An explicit `path` (or THREED_FLEET) that does not exist is an error."""
    explicit = path is not None or bool(os.environ.get(ENV, "").strip())
    path = path or fleet_path()
    if path is None:
        return None
    if not path.is_file():
        if explicit:
            raise FleetError(
                f"fleet file not found: {path}",
                command="printer",
                remediation=[
                    f"Fix the path, or unset {ENV} to use <repo>/fleet/printers.yaml."
                ],
            )
        return None
    printers, problems = validate(read_yaml(path), path)
    if problems:
        raise FleetError(
            f"{path}: {len(problems)} schema problem(s)",
            command="printer",
            remediation=[
                *(p.message for p in problems),
                "Run `3d lint --printops` after fixing; see docs/commands/printer.md.",
            ],
        )
    return Fleet(path, printers)


@dataclass(frozen=True)
class Problem:
    subject: str  # printer name; "" for the whole file
    message: str  # "<name>.<key>: what is wrong"


def validate(data: Any, path: Path) -> tuple[dict[str, FleetPrinter], list[Problem]]:
    """All schema problems of a parsed fleet file, plus the entries that had none."""
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, [Problem("", "top level must be a mapping of printer name -> entry")]
    printers: dict[str, FleetPrinter] = {}
    problems: list[Problem] = []
    keys: dict[str, list[Any]] = {}
    for raw_name in data:
        keys.setdefault(str(raw_name), []).append(raw_name)
    for (
        name,
        raws,
    ) in keys.items():  # `1:` and `'1':` are one name: neither may silently win
        if len(raws) > 1:
            problems.append(
                Problem(
                    name,
                    f"{name}: duplicate printer name (YAML keys "
                    f"{' and '.join(map(repr, raws))} are the same name)",
                )
            )
    for raw_name, spec in data.items():
        name = str(raw_name)
        if len(keys[name]) > 1:
            continue
        found: list[str] = []
        printer = _entry(name, spec, path, found)
        problems.extend(
            Problem(name, f"{name}{'' if m.startswith(':') else '.'}{m}") for m in found
        )
        if printer is not None and not found:
            printers[name] = printer
    return printers, problems


def _entry(name: str, spec: Any, path: Path, bad: list[str]) -> FleetPrinter | None:
    if not _NAME.fullmatch(name):
        bad.append(
            ": printer name must be letters/digits/_ . - (starting with a letter or digit)"
        )
    if not isinstance(spec, dict):
        bad.append(": entry must be a mapping")
        return None
    bad.extend(_unknown(spec, FLEET_KEYS | DEVICE_KEYS, ""))
    model = spec.get("model")
    if not isinstance(model, str) or not model.strip():
        bad.append("model: required, a model name from `3d printers list`")
    owner = _choice(spec, "owner", OWNERS, bad, required=True)
    access = _choice(spec, "access", ACCESS, bad, required=True)
    device = {k: spec[k] for k in DEVICE_KEYS if k in spec}
    contact = sorted(set(device) - STATIC_KEYS)
    if access == "offline" and contact:
        bad.append(
            f"access: offline printers are never contacted; remove {', '.join(contact)}"
        )
    elif access in ("full", "read-only"):
        bad.extend(
            f"{k}: required for access {access}"
            for k in ("adapter", "host")
            if not spec.get(k)
        )
    heads = _heads(spec.get("heads"), bad)
    ams = _ams(spec["ams"], bad) if "ams" in spec else None
    enclosure = _choice(spec, "enclosure", ENCLOSURES, bad)
    chamber = spec.get("chamber_heater")
    if chamber is not None and not isinstance(chamber, bool):
        bad.append(f"chamber_heater: {chamber!r} is not true/false")
    plates = _plates(spec["plates"], bad) if "plates" in spec else None
    notes = spec.get("notes")
    if notes is not None and not isinstance(notes, str):
        bad.append("notes: must be text")
    sources = _strings(spec.get("sources", []), "sources", bad)
    if bad:
        return None
    return FleetPrinter(
        name,
        str(model).strip(),
        str(owner),
        str(access),
        heads,
        ams,
        enclosure,
        chamber,
        plates,
        notes,
        sources,
        device,
        path,
    )


def _choice(
    spec: dict[str, Any],
    key: str,
    accepted: tuple[str, ...],
    bad: list[str],
    required: bool = False,
    prefix: str = "",
) -> str | None:
    value = spec.get(key)
    if value is None:
        if required:
            bad.append(f"{prefix}{key}: required, one of {', '.join(accepted)}")
        return None
    if value not in accepted:
        bad.append(f"{prefix}{key}: {value!r} is not one of {', '.join(accepted)}")
        return None
    return str(value)


def _strings(value: Any, key: str, bad: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        bad.append(f"{key}: must be a list of non-empty strings")
        return ()
    return tuple(v.strip() for v in value)


def _unknown(
    mapping: dict[Any, Any], accepted: frozenset[str], prefix: str
) -> list[str]:
    """One problem per key of `mapping` outside `accepted` (typos must not pass silently)."""
    return [
        f"{prefix}{key}: unknown key (accepted: {', '.join(sorted(accepted))})"
        for key in sorted(set(map(str, mapping)) - accepted)
    ]


def _nozzle(value: Any, where: str, bad: list[str]) -> Nozzle | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        bad.append(f"{where}.nozzle: must be a mapping {{diameter_mm, material, type}}")
        return None
    bad.extend(_unknown(value, _NOZZLE_KEYS, f"{where}.nozzle."))
    dia = value.get("diameter_mm")
    if not isinstance(dia, (int, float)) or isinstance(dia, bool) or not 0 < dia <= 2:
        bad.append(
            f"{where}.nozzle.diameter_mm: {dia!r} is not a diameter in mm (0 < d <= 2)"
        )
        return None
    material = _choice(
        value, "material", NOZZLE_MATERIALS, bad, prefix=f"{where}.nozzle."
    )
    kind = _choice(value, "type", NOZZLE_TYPES, bad, prefix=f"{where}.nozzle.")
    return Nozzle(float(dia), material, kind)


def _heads(value: Any, bad: list[str]) -> tuple[Head, ...]:
    if not isinstance(value, list) or not value:
        bad.append("heads: required, a non-empty list (one entry per toolhead)")
        return ()
    heads: list[Head] = []
    for i, h in enumerate(value):
        where = f"heads[{i}]"
        if not isinstance(h, dict):
            bad.append(f"{where}: must be a mapping")
            continue
        bad.extend(_unknown(h, _HEAD_KEYS, f"{where}."))
        label, index = h.get("label"), h.get("index")
        if index != i or isinstance(index, bool):
            bad.append(
                f"{where}.index: {index!r}; heads are listed in tool order, so index must be {i}"
            )
        if label != f"T{i + 1}":
            bad.append(f"{where}.label: {label!r}; tool index {i} is labelled T{i + 1}")
        extruder = h.get("extruder")
        if extruder is not None and (
            not isinstance(extruder, str) or not extruder.strip()
        ):
            bad.append(
                f"{where}.extruder: must be a Klipper object name, e.g. extruder1"
            )
            extruder = None  # reported above; keep it out of the duplicate check
        usable = h.get("usable", True)
        reason = h.get("reason")
        if not isinstance(usable, bool):
            bad.append(f"{where}.usable: {usable!r} is not true/false")
        elif not usable and (not isinstance(reason, str) or not reason.strip()):
            bad.append(
                f"{where}.reason: required when usable is false (what is broken, where it is recorded)"
            )
        elif reason is not None and not isinstance(reason, str):
            bad.append(f"{where}.reason: must be text")
        heads.append(
            Head(
                f"T{i + 1}",
                i,
                extruder,
                _nozzle(h.get("nozzle"), where, bad),
                usable is not False,
                reason.strip() if isinstance(reason, str) else None,
            )
        )
    extruders = [h.extruder for h in heads if h.extruder]
    if len(set(extruders)) != len(extruders):
        bad.append("heads: two heads name the same extruder")
    return tuple(heads)


def _ams(value: Any, bad: list[str]) -> tuple[AmsUnit, ...] | None:
    if not isinstance(value, list):
        bad.append("ams: must be a list of {id, kind, slots} ([] = no AMS)")
        return None
    units: list[AmsUnit] = []
    for i, u in enumerate(value):
        where = f"ams[{i}]"
        if not isinstance(u, dict) or set(map(str, u)) != {"id", "kind", "slots"}:
            bad.append(f"{where}: must be a mapping with exactly id, kind, slots")
            continue
        uid, kind, slots = u["id"], u["kind"], u["slots"]
        if not isinstance(uid, str) or not _NAME.fullmatch(uid):
            bad.append(f"{where}.id: {uid!r} must be letters/digits/_ . -")
        if kind not in AMS_KINDS:
            bad.append(f"{where}.kind: {kind!r} is not one of {', '.join(AMS_KINDS)}")
        if (
            not isinstance(slots, int)
            or isinstance(slots, bool)
            or not 1 <= slots <= 32
        ):
            bad.append(f"{where}.slots: {slots!r} is not a slot count 1-32")
        units.append(
            AmsUnit(str(uid), str(kind), slots if isinstance(slots, int) else 0)
        )
    ids = [u.id for u in units]
    if len(set(ids)) != len(ids):
        bad.append("ams: unit ids must be unique")
    return tuple(units)


def _plates(value: Any, bad: list[str]) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        bad.append(f"plates: must be a list of plate types ({', '.join(PLATE_TYPES)})")
        return None
    bad.extend(
        f"plates: {v!r} is not one of {', '.join(PLATE_TYPES)}"
        for v in value
        if v not in PLATE_TYPES
    )
    return tuple(value)
