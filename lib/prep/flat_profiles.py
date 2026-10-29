"""flat_profiles.py — flat Orca machine/process/filament files for a single-extruder printer sliced from an STL.

WHAT: `build_profiles(printer, ...)` resolves the fleet presets of a printer (`FLAT_PRINTERS`: K1 SE = the fleet
  machine "Creality K1 SE 0.4 nozzle - Copy", the process "0.20mm Standard @Creality K1 SE - accel 8900" and a
  filament) through `slicing.orca_presets.PresetCatalog` (repo `profiles/`, user presets, system presets), folds each
  `inherits` chain into one standalone JSON, and applies the overrides in a fixed order: the preset, then the
  `3d prep` flags (walls, infill, brim, supports), then the overlay file, then `--set`/`--fset`.
  `speed_limits(process)` turns the resolved process into "feature -> requested mm/s" for the G-code `feedrates` gate.
WHY: the K1 SE has one extruder and no colour mixing, so the 3MF project route of `prep.project3mf` (built for the
  four-head U1) is the wrong tool; the owner's verified recipe is "STL + three flat presets through
  `--load-settings`" (garage-band soil-sensor-case `verify/slice_k1se.py`, whose numbers this reproduces).
  Flat files, because the CLI rejects a process whose `compatible_printers` does not list the machine, and a user
  preset only stores the keys the owner changed.
INVARIANTS:
  * An override key is NOT checked against the base preset: the system presets carry only part of Orca's keys
    (`small_perimeter_speed` is not in the K1 SE chain but is a real setting). The check is made on the result:
    `FlatProfiles.requested` is what the G-code's own config block must echo (`gcode_gates` gate `overrides`), so a
    typo (`outer_wal_speed`) FAILs the gates instead of printing at the old speed.
  * The fleet preset names stay on the files unless a key that `3d print-preflight` `preset_match` compares
    (accelerations, jerk, `accel_to_decel*`) or the machine actually changes value; then the names get " - prep overrides",
    so the G-code never claims a fleet preset it did not use.
  * `speed_limits` only names a feature when every setting that can set its speed parses as a number (a percentage
    only for `small_perimeter_speed`, of `outer_wall_speed`); a limit is the largest of them (and of the first-layer
    speeds), so the gate never fails a correct slice.
  * Stdlib + `slicing.orca_presets` only (imported lazily: it scans the slicer data dirs).
Past bugs: none yet.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from errors import InputNotFound, InvalidArgument

Value = str | int | float | bool | list[Any]


@dataclass(frozen=True)
class FlatPrinter:
    """The fleet presets a printer is sliced with, by Orca preset name."""

    machine: str
    process: str
    filament: str


FLAT_PRINTERS: dict[str, FlatPrinter] = {
    "k1se": FlatPrinter(
        machine="Creality K1 SE 0.4 nozzle - Copy",
        process="0.20mm Standard @Creality K1 SE - accel 8900",
        filament="Creality Generic PLA @K1-all",
    )
}

#: `--supports auto`: easy tear-off supports (overhangs steeper than 45 degrees, build plate only, sparse base,
#: 0.3 mm air gap, two wide-spaced interface layers: a small bond that snaps off by hand). The recipe of the
#: spoon150 K1 SE print with a wider gap.
SUPPORT_AUTO: dict[str, str] = {
    "enable_support": "1",
    "support_type": "normal(auto)",
    "support_style": "snug",
    "support_threshold_angle": "45",
    "support_on_build_plate_only": "1",
    "support_object_xy_distance": "0.4",
    "support_object_first_layer_gap": "0.3",
    "support_top_z_distance": "0.3",
    "support_bottom_z_distance": "0.3",
    "support_interface_top_layers": "2",
    "support_interface_bottom_layers": "0",
    "support_interface_spacing": "0.4",
    "support_interface_pattern": "rectilinear",
    "support_base_pattern": "default",
    "support_base_pattern_spacing": "4",
    "support_remove_small_overhang": "1",
    "support_critical_regions_only": "0",
}

KINDS = ("machine", "process", "filament")
_ACCEL_KEYS = ("accel_to_decel",)
_ACCEL_SUFFIXES = ("_acceleration", "_jerk")
_OVERRIDE_SUFFIX = " - prep overrides"
_IDENTITY = {
    "machine": "printer_settings_id",
    "process": "print_settings_id",
    "filament": "filament_settings_id",
}


@dataclass
class Overlay:
    """Per-kind key/value overrides: an overlay file (`{"process": {...}, "filament": {...}, "machine": {...}}`)."""

    process: dict[str, Value] = field(default_factory=dict)
    filament: dict[str, Value] = field(default_factory=dict)
    machine: dict[str, Value] = field(default_factory=dict)

    def merged(
        self, process: Mapping[str, Value], filament: Mapping[str, Value]
    ) -> Overlay:
        """This overlay with `--set`/`--fset` values on top."""
        return Overlay(
            {**self.process, **process},
            {**self.filament, **filament},
            dict(self.machine),
        )


def load_overlay(path: Path) -> Overlay:
    """Read an overlay file; any other top-level key, or a non-object section, is an InvalidArgument."""
    if not path.is_file():
        raise InputNotFound(str(path), command="prep")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InvalidArgument(
            "--overlay", str(path), ["a JSON file"], command="prep", extra=str(exc)
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidArgument("--overlay", str(path), ["a JSON object"], command="prep")
    unknown = sorted(set(raw) - set(KINDS))
    if unknown:
        raise InvalidArgument(
            "--overlay key", ", ".join(unknown), list(KINDS), command="prep"
        )
    sections: dict[str, dict[str, Value]] = {}
    for kind in KINDS:
        section = raw.get(kind, {})
        if not isinstance(section, dict):
            raise InvalidArgument(
                f"--overlay {kind}",
                str(path),
                ["a JSON object of setting: value"],
                command="prep",
            )
        sections[kind] = section
    return Overlay(**sections)


def parse_assignments(items: list[str], flag: str) -> dict[str, str]:
    """`["a=1", "b=2"]` -> `{"a": "1", "b": "2"}`; an item without `=` is an InvalidArgument."""
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise InvalidArgument(flag, item, ["KEY=VALUE"], command="prep")
        out[key.strip()] = value
    return out


def scalar(value: Value) -> str:
    """One setting value the way Orca stores it: booleans as 1/0, whole floats without the ".0"."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def coerce(existing: Any, value: Value) -> str | list[str]:
    """`value` in the shape the slicer stores `existing`: a list stays a list (a scalar is repeated for every
    entry, e.g. both accelerations of a machine), a string stays a string."""
    if isinstance(value, list):
        return [scalar(v) for v in value]
    if isinstance(existing, list):
        return [scalar(value)] * max(len(existing), 1)
    return scalar(value)


def _apply(base: dict[str, Any], overrides: Mapping[str, Value]) -> list[str]:
    """Write `overrides` into `base`; returns the keys whose value actually changed."""
    changed: list[str] = []
    for key, value in overrides.items():
        new = coerce(base.get(key), value)
        if new != base.get(key):
            changed.append(key)
        base[key] = new
    return changed


def echoed(value: Any) -> str:
    """A setting as the slicer prints it in the G-code config block: list entries joined by commas."""
    return ",".join(str(v) for v in value) if isinstance(value, list) else str(value)


def _is_accel(key: str) -> bool:
    return key.endswith(_ACCEL_SUFFIXES) or key.startswith(_ACCEL_KEYS)


@dataclass(frozen=True)
class FlatProfiles:
    """The three standalone presets, ready to write."""

    machine: dict[str, Any]
    process: dict[str, Any]
    filament: dict[str, Any]
    #: preset names as they appear in the G-code footer
    names: dict[str, str]
    #: True when the files no longer match the fleet preset names
    renamed: bool
    #: setting -> value the G-code config block must show after the slice: every override, the walls/infill/support
    #: switch `3d prep` owns and the three preset names
    requested: dict[str, str]

    def write(self, directory: Path) -> dict[str, Path]:
        """`directory/{machine,process,filament}.json`; returns the paths by kind."""
        directory.mkdir(parents=True, exist_ok=True)
        paths: dict[str, Path] = {}
        for kind in KINDS:
            paths[kind] = directory / f"{kind}.json"
            paths[kind].write_text(
                json.dumps(getattr(self, kind), indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return paths


def _stamp_identity(
    machine: dict[str, Any],
    process: dict[str, Any],
    filament: dict[str, Any],
    names: Mapping[str, str],
) -> None:
    """Give the three presets their (possibly renamed) identity. The system presets list only their stock printer
    as compatible; the machine here carries the fleet name (or the renamed one), so that name is the compatible
    printer."""
    machine.update({"name": names["machine"], _IDENTITY["machine"]: names["machine"]})
    process.update(
        {
            "name": names["process"],
            _IDENTITY["process"]: names["process"],
            "compatible_printers": [names["machine"]],
        }
    )
    filament.update(
        {
            "name": names["filament"],
            _IDENTITY["filament"]: [names["filament"]],
            "compatible_printers": [names["machine"]],
        }
    )
    for kind, preset in (
        ("machine", machine),
        ("process", process),
        ("filament", filament),
    ):
        preset.update({"type": kind, "from": "system", "instantiation": "true"})


def _requested_settings(
    machine: Mapping[str, Any],
    process: Mapping[str, Any],
    filament: Mapping[str, Any],
    overlay: Overlay,
    names: Mapping[str, str],
) -> dict[str, str]:
    """setting -> value the G-code config block must show: the overrides, the settings `3d prep` owns, the names."""
    requested = {
        key: echoed(process[key])
        for key in (
            "wall_loops",
            "sparse_infill_density",
            "enable_support",
            *overlay.process,
        )
        if key in process
    }
    requested.update({key: echoed(filament[key]) for key in overlay.filament})
    requested.update({key: echoed(machine[key]) for key in overlay.machine})
    requested.update(
        {
            "printer_settings_id": names["machine"],
            "print_settings_id": names["process"],
            "filament_settings_id": names["filament"],
        }
    )
    return requested


def _check_inputs(printer: str, supports: str) -> None:
    if printer not in FLAT_PRINTERS:
        raise InvalidArgument(
            "--printer", printer, sorted(FLAT_PRINTERS), command="prep"
        )
    if supports not in ("none", "auto"):
        raise InvalidArgument("--supports", supports, ["none", "auto"], command="prep")


def build_profiles(
    printer: str,
    *,
    process_preset: str | None = None,
    filament_preset: str | None = None,
    process_flags: Mapping[str, Value] | None = None,
    supports: str = "none",
    overlay: Overlay | None = None,
) -> FlatProfiles:
    """Resolve and flatten the presets of `printer` and apply the overrides (see the module header for the order).

    `process_flags` are the settings `3d prep` itself owns (walls, infill, brim, layer height); `supports` is
    `none` or `auto`; `overlay` holds the overlay file merged with `--set`/`--fset`."""
    from slicing.orca_presets import PresetCatalog

    _check_inputs(printer, supports)
    fleet = FLAT_PRINTERS[printer]
    overlay = overlay or Overlay()
    catalog = PresetCatalog(command="prep")

    def resolve(kind: str, name: str) -> dict[str, Any]:
        return catalog.flatten(catalog.find(kind, name))

    machine = resolve("machine", fleet.machine)
    process = resolve("process", process_preset or fleet.process)
    filament = resolve("filament", filament_preset or fleet.filament)

    for key, value in {
        **(process_flags or {}),
        **(SUPPORT_AUTO if supports == "auto" else {"enable_support": "0"}),
    }.items():
        process[key] = coerce(process.get(key), value)
    changed_process = _apply(process, overlay.process)
    _apply(filament, overlay.filament)
    renamed_machine = bool(_apply(machine, overlay.machine))
    renamed_process = any(_is_accel(k) for k in changed_process) or renamed_machine
    machine_name = fleet.machine + (_OVERRIDE_SUFFIX if renamed_machine else "")
    process_name = (process_preset or fleet.process) + (
        _OVERRIDE_SUFFIX if renamed_process else ""
    )
    filament_name = str(
        filament.get("filament_settings_id", [filament_preset or fleet.filament])[0]
    )
    names = {
        "machine": machine_name,
        "process": process_name,
        "filament": filament_name,
    }
    _stamp_identity(machine, process, filament, names)
    requested = _requested_settings(machine, process, filament, overlay, names)
    return FlatProfiles(
        machine, process, filament, names, renamed_machine or renamed_process, requested
    )


# ---------------------------------------------------------------- requested speeds

#: feature name in the G-code (`;TYPE:`) -> the process settings that can set its speed (mm/s)
_FEATURE_SPEED_KEYS: dict[str, tuple[str, ...]] = {
    "Outer wall": ("outer_wall_speed", "small_perimeter_speed"),
    "Inner wall": ("inner_wall_speed", "small_perimeter_speed"),
    "Overhang wall": (
        "overhang_1_4_speed",
        "overhang_2_4_speed",
        "overhang_3_4_speed",
        "overhang_4_4_speed",
        "outer_wall_speed",
        "inner_wall_speed",
    ),
    "Sparse infill": ("sparse_infill_speed",),
    "Internal solid infill": ("internal_solid_infill_speed",),
    "Bottom surface": ("internal_solid_infill_speed",),
    "Top surface": ("top_surface_speed",),
    "Gap infill": ("gap_infill_speed",),
    "Bridge": ("bridge_speed",),
    "Internal Bridge": ("internal_bridge_speed", "bridge_speed"),
    "Support": ("support_speed",),
    "Support interface": ("support_interface_speed", "support_speed"),
    "Skirt": ("skirt_speed", "initial_layer_speed"),
    "Brim": ("skirt_speed", "initial_layer_speed"),
    "travel": ("travel_speed", "initial_layer_travel_speed"),
}
#: the first layer runs every feature at these, which may exceed the feature's own setting
_FIRST_LAYER_KEYS = ("initial_layer_speed", "initial_layer_infill_speed")


def _speed(process: Mapping[str, Any], key: str) -> float | None:
    """The setting as mm/s; None when absent or not a number. `small_perimeter_speed` may be a percentage of
    `outer_wall_speed` (the slicer's own definition); no other setting is read as one."""
    raw = process.get(key)
    if not isinstance(raw, str):
        return None
    try:
        if raw.endswith("%") and key == "small_perimeter_speed":
            outer = _speed(process, "outer_wall_speed")
            return None if outer is None else float(raw[:-1]) / 100.0 * outer
        return float(raw)
    except ValueError:
        return None


def _feature_limit(
    process: Mapping[str, Any],
    feature: str,
    keys: tuple[str, ...],
    first_layer: list[float],
) -> float | None:
    """The fastest speed one feature can run at, or None when any of its settings is not a plain number."""
    present = [k for k in keys if k in process]
    if not present:
        return None
    values = [_speed(process, k) for k in present]
    known = [v for v in values if v is not None]
    if len(known) != len(values):
        return None
    limit = max(known if feature == "travel" else [*known, *first_layer])
    return limit if limit > 0 else None


def speed_limits(process: Mapping[str, Any]) -> dict[str, float]:
    """Feature -> the fastest speed (mm/s) the process asks for. Features whose settings are not all plain numbers
    are left out (no limit rather than a wrong one)."""
    first = [v for k in _FIRST_LAYER_KEYS if (v := _speed(process, k)) is not None]
    limits = {
        f: _feature_limit(process, f, keys, first)
        for f, keys in _FEATURE_SPEED_KEYS.items()
    }
    return {f: v for f, v in limits.items() if v is not None}
