"""Fixtures for the K1 SE prep tests: a preset tree, G-code with real feedrates, a fake slicer executable.

The preset tree mirrors the real inheritance (a machine and process "- Copy" over flat parents, the Elegoo filament
over Creality Generic PLA) with only the keys the tests look at. The fake slicer is a real executable the CLI starts
as a subprocess, so the whole `bin/3d prep` path runs without Orca.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
import stat
import sys
from pathlib import Path

import pytest

from prep_gcode_fixtures import gcode_text

MACHINE = "Creality K1 SE 0.4 nozzle - Copy"
PROCESS = "0.20mm Standard @Creality K1 SE - accel 8900"
FILAMENT = "Creality Generic PLA @K1-all"
ELEGOO = "Elegoo RAPID PLA+ @K1-all"

#: speeds (mm/s) the synthetic G-code runs each feature at
FEATURE_SPEEDS = {
    "Outer wall": 250.0,
    "Inner wall": 450.0,
    "Sparse infill": 376.0,
    "Prime tower": 100.0,
    "Support": 60.0,
}
TRAVEL_MM_S = 600.0


def write_preset_tree(root: Path) -> Path:
    """`root/snapmaker-orca/{machine,process,filament}/*.json`; returns `root` (the profiles dir)."""
    base = root / "snapmaker-orca"
    for kind in ("machine", "process", "filament"):
        (base / kind).mkdir(parents=True, exist_ok=True)

    def put(kind: str, name: str, data: Mapping[str, object]) -> None:
        (base / kind / f"{name}.json").write_text(
            json.dumps({"name": name, "type": kind, **data}, indent=1), encoding="utf-8"
        )

    put(
        "machine",
        "Creality K1 SE 0.4 nozzle",
        {
            "printable_area": ["0x0", "220x0", "220x220", "0x220"],
            "machine_max_acceleration_extruding": ["20000", "20000"],
            "machine_max_acceleration_travel": ["20000", "20000"],
            "printer_settings_id": "Creality K1 SE 0.4 nozzle",
        },
    )
    put(
        "machine",
        MACHINE,
        {
            "inherits": "Creality K1 SE 0.4 nozzle",
            "machine_max_acceleration_extruding": ["8900", "8900"],
            "machine_max_acceleration_travel": ["8900", "8900"],
        },
    )
    process = {
        "layer_height": "0.2",
        "wall_loops": "2",
        "sparse_infill_density": "10%",
        "outer_wall_speed": "250",
        "inner_wall_speed": "500",
        "sparse_infill_speed": "550",
        "internal_solid_infill_speed": "300",
        "top_surface_speed": "200",
        "gap_infill_speed": "200",
        "bridge_speed": "60",
        "internal_bridge_speed": "60",
        "small_perimeter_speed": "50%",
        "travel_speed": "600",
        "initial_layer_speed": "30",
        "initial_layer_infill_speed": "30",
        "overhang_1_4_speed": "0",
        "overhang_2_4_speed": "0",
        "overhang_3_4_speed": "0",
        "overhang_4_4_speed": "0",
        "support_speed": "60",
        "default_acceleration": "8000",
        "outer_wall_acceleration": "5000",
        "inner_wall_acceleration": "8000",
        "initial_layer_acceleration": "500",
        "enable_support": "1",
        "brim_type": "auto_brim",
        "compatible_printers": ["Creality K1 SE 0.4 nozzle"],
    }
    put("process", "0.20mm Standard @Creality K1 SE", process)
    put(
        "process",
        PROCESS,
        {"inherits": "0.20mm Standard @Creality K1 SE", "default_acceleration": "8000"},
    )
    put(
        "filament",
        FILAMENT,
        {
            "filament_settings_id": [FILAMENT],
            "filament_max_volumetric_speed": ["22"],
            "filament_flow_ratio": ["1"],
            "pressure_advance": ["0.05"],
            "nozzle_temperature": ["220"],
        },
    )
    elegoo = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "profiles"
            / "snapmaker-orca"
            / "filament"
            / f"{ELEGOO}.json"
        ).read_text(encoding="utf-8")
    )
    put("filament", ELEGOO, {k: v for k, v in elegoo.items() if k not in ("name",)})
    return root


def isolate_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Only the fixture tree is visible to the preset catalog (no slicer data dirs of the machine)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = write_preset_tree(tmp_path / "profiles")
    monkeypatch.setenv("THREED_PROFILES_DIR", str(root))
    return root


def with_feedrates(
    text: str,
    speeds: dict[str, float] | None = None,
    travel: float = TRAVEL_MM_S,
) -> str:
    """`gcode_text` output with an `F` on every feature change and on every travel (mm/s -> mm/min), as Orca writes it."""
    speeds = speeds or FEATURE_SPEEDS
    out: list[str] = []
    current = 0.0
    for line in text.splitlines():
        if line.startswith(";TYPE:"):
            current = speeds.get(line[6:], 100.0)
            out.append(line)
            out.append(f"G1 F{current * 60:g}")
        elif line.startswith("G0 X"):
            out.append(f"{line} F{travel * 60:g}")
            if current:
                out.append(f"G1 F{current * 60:g}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def k1se_gcode(**kw: object) -> str:
    """Three 0.2 mm layers of a disc, walls and infill, with feedrates; the grams and time of the real mole print."""
    kw.setdefault("grams", "13.56")
    kw.setdefault("time_text", "32m 51s")
    kw.setdefault("loops", 3)
    kw.setdefault("centre", (110.0, 110.0))
    kw.setdefault("radius", 20.0)
    return with_feedrates(gcode_text([(0.2, 0)] * 3, **kw))  # type: ignore[arg-type]


_FAKE_SLICER = """#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_SLICER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\\n")
out = args[args.index("--outputdir") + 1]
presets = args[args.index("--load-settings") + 1].split(";") + [args[args.index("--load-filaments") + 1]]
drop = set(filter(None, os.environ.get("FAKE_SLICER_DROP", "").split(",")))  # keys Orca does not know: it ignores them
config = ["; CONFIG_BLOCK_START"]
for path in presets:
    with open(path, encoding="utf-8") as fh:
        for key, value in json.load(fh).items():
            if key not in drop:
                config.append("; %s = %s" % (key, ",".join(value) if isinstance(value, list) else value))
with open({gcode!r}, encoding="utf-8") as src, open(os.path.join(out, "plate_1.gcode"), "w", encoding="utf-8") as dst:
    dst.write(src.read() + "\\n".join(config) + "\\n; CONFIG_BLOCK_END\\n")
with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as fh:
    fh.write("{{}}")
"""


def fake_slicer(tmp_path: Path, gcode: str) -> Path:
    """An executable that stands in for the Orca CLI: logs its argv (to $FAKE_SLICER_LOG) and writes `gcode` followed
    by a config block that echoes every key of the three preset files (minus $FAKE_SLICER_DROP), as Orca does."""
    source = tmp_path / "fake-gcode.gcode"
    source.write_text(gcode, encoding="utf-8")
    exe = tmp_path / "fake-orca"
    exe.write_text(
        _FAKE_SLICER.format(python=sys.executable, gcode=str(source)), encoding="utf-8"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe
