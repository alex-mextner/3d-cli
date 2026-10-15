"""Fake slicer data dirs for the `3d profiles` tests (issue #180).

The files mirror what Snapmaker Orca 2.x / Bambu Studio write (4-space indent, sorted keys,
trailing newline) and what upstream OrcaSlicer main writes (tab indent); the `.info` layout is
Preset::save_info's five `key = value` lines.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

Preset = dict[str, str | list[str]]

U1_PROCESS: Preset = {
    "from": "User",
    "inherits": "0.28mm Standard @Snapmaker U1 (0.4 nozzle)",
    "initial_layer_speed": ["150"],
    "layer_height": "0.32",
    "name": "0.32mm Standard @Snapmaker U1 (0.4 nozzle)",
    "print_settings_id": "0.32mm Standard @Snapmaker U1 (0.4 nozzle)",
    "version": "2.3.1.10",
}

K1_MACHINE: Preset = {
    "from": "User",
    "host_type": "crealityprint",
    "inherits": "Creality K1 SE 0.4 nozzle",
    "name": "Creality K1 SE 0.4 nozzle - Copy",
    "print_host": "192.168.0.27",
    "printer_settings_id": "Creality K1 SE 0.4 nozzle - Copy",
    "version": "2.3.1.10",
}

PLA_FILAMENT: Preset = {
    "filament_flow_ratio": ["0.98"],
    "from": "User",
    "inherits": "Generic PLA @U1",
    "name": "My PLA",
    "pressure_advance": ["0.02"],
    "version": "2.3.1.10",
}


def slicer_json(obj: Mapping[str, object], indent: int | str = 4) -> str:
    """The bytes an Orca-family slicer writes for a preset (nlohmann dump + std::endl)."""
    return json.dumps(obj, indent=indent, ensure_ascii=False) + "\n"


def slicer_info(
    *,
    sync_info: str = "",
    user_id: str = "",
    setting_id: str = "",
    base_id: str = "",
    updated_time: int = 1791558457,
) -> str:
    return (
        f"sync_info = {sync_info}\nuser_id = {user_id}\nsetting_id = {setting_id}\n"
        f"base_id = {base_id}\nupdated_time = {updated_time}\n"
    )


def make_snapmaker(config_home: Path, *, preset_folder: str = "") -> Path:
    """A Snapmaker Orca data dir under XDG_CONFIG_HOME with three user presets; returns
    the user preset dir (`user/default`)."""
    data = config_home / "Snapmaker_Orca"
    data.mkdir(parents=True, exist_ok=True)
    conf = {"app": {"preset_folder": preset_folder, "user_mode": "advanced"}}
    (data / "Snapmaker_Orca.conf").write_text(
        json.dumps(conf, indent=4) + "\n", encoding="utf-8"
    )
    user = data / "user" / (preset_folder or "default")
    for kind in ("filament", "process", "machine"):
        (user / kind).mkdir(parents=True, exist_ok=True)
    proc = user / "process" / f"{U1_PROCESS['name']}.json"
    proc.write_text(slicer_json(U1_PROCESS), encoding="utf-8")
    proc.with_suffix(".info").write_text(slicer_info(base_id="GP006"), encoding="utf-8")
    mach = user / "machine" / f"{K1_MACHINE['name']}.json"
    mach.write_text(slicer_json(K1_MACHINE), encoding="utf-8")
    mach.with_suffix(".info").write_text(
        slicer_info(sync_info="update", base_id="GM001"), encoding="utf-8"
    )
    fil = user / "filament" / "base" / "My PLA.json"
    fil.parent.mkdir(parents=True, exist_ok=True)
    fil.write_text(slicer_json(PLA_FILAMENT), encoding="utf-8")
    return user
