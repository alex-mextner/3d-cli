"""calibration_fixtures.py — a fake Orca CLI and its system presets for `3d calibrate gen` tests.

Shared by the unit tests (tests/test_calibration_gen.py) and the `bin/3d` e2e
(tests/e2e/test_calibrate_workflows.py). It lives in a *_fixtures.py helper, not in a test
module, so the fast gate (tests/run_gate.py) reaches the e2e when slicing.calibration_gen
changes: the preset names come from calibration_gen.PRESETS.

  fake_orca(tmp_path, ...) -> (profiles_root, orca_path)
      writes the K1 SE machine/process/filament presets as Orca system profiles under
      tmp_path/profiles and an executable stand-in for the Orca CLI at tmp_path/orca: it writes
      plate_1.gcode with START_PRINT, one EXCLUDE_OBJECT block per STL argument, ;Z: layer
      markers up to 60 mm, the end G-code and the relative-E footer.
  write_plate_orca(tmp_path) -> (profiles_root, orca_path)
      the calibration plates' variant (tests/test_calibration_plate.py, the plate e2e): presets
      with the gantry clearances, and a fake that slices every STL into real layers, by layer
      or by object, with a retraction and a wipe per visit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slicing.calibration_gen import PRESETS

FAKE_GCODE = """; thumbnails = 16x16/PNG
START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55
{objects}{layers}{end}
; use_relative_e_distances = {relative_e}
"""
# Orca's layer markers, 0.2 mm layers up to 60 mm (taller than any temperature tower)
FAKE_LAYERS = "".join(
    f";LAYER_CHANGE\n;Z:{i * 0.2:.1f}\nG1 X100 Y100 E0.1\n" for i in range(1, 301)
)


def fake_orca(
    tmp_path: Path,
    *,
    end: str = "END_PRINT",
    relative_e: str = "1",
    accel: str = "12000",
    diameter: str = "1.75",
    retract_length: str = "0.8",
    retract_speed: str = "40",
) -> tuple[Path, Path]:
    p = PRESETS["Creality K1 SE"]
    root = tmp_path / "profiles"
    for kind, name, data in (
        (
            "machine",
            p.machine,
            {
                "bed_exclude_area": ["0x0"],
                "retraction_length": [retract_length],
                "retraction_speed": [retract_speed],
            },
        ),
        (
            "process",
            p.process,
            {
                "default_acceleration": accel,
                "default_jerk": "9",
                "initial_layer_print_height": "0.2",
            },
        ),
        (
            "filament",
            p.filament["PLA"],
            {
                "filament_flow_ratio": ["0.98"],
                "filament_colour": ["#FFFFFF"],
                "filament_diameter": [diameter],
            },
        ),
    ):
        d = root / p.vendor / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps({"name": name, **data}))
    body = (
        FAKE_GCODE.replace("{end}", end)
        .replace("{relative_e}", relative_e)
        .replace("{layers}", FAKE_LAYERS)
    )
    script = tmp_path / "orca"
    script.write_text(f"""#!/usr/bin/env python3
import sys, pathlib
a = sys.argv[1:]
out = pathlib.Path(a[a.index("--outputdir") + 1])
objs = "".join(
    f"EXCLUDE_OBJECT_START NAME={{pathlib.Path(s).name}}_id_{{i}}_copy_0\\nG1 X100 Y100 Z0.2 E1\\n"
    f"EXCLUDE_OBJECT_END NAME={{pathlib.Path(s).name}}_id_{{i}}_copy_0\\n"
    for i, s in enumerate(x for x in a if x.endswith(".stl")))
(out / "plate_1.gcode").write_text({body!r}.replace("{{objects}}", objs))
""")
    script.chmod(0o755)
    return root, script


PLATE_PRESETS: dict[tuple[str, str], dict[str, Any]] = {
    ("machine", "Creality K1 SE 0.4 nozzle"): {
        "bed_exclude_area": ["0x0"],
        "retraction_length": ["0.8"],
        "retraction_speed": ["40"],
        "extruder_clearance_height_to_rod": "45",
        "extruder_clearance_radius": "45",
    },
    ("process", "0.20mm Standard @Creality K1 SE"): {
        "default_acceleration": "12000",
        "default_jerk": "9",
        "initial_layer_print_height": "0.2",
        "layer_height": "0.2",
    },
    ("filament", "Creality Generic PLA @K1-all"): {
        "filament_flow_ratio": ["0.98"],
        "filament_colour": ["#FFFFFF"],
        "filament_diameter": ["1.75"],
        "nozzle_temperature": ["220"],
        "nozzle_temperature_initial_layer": ["220"],
    },
}

# The fake slices every STL into layers of `layer_height` (process preset written by
# slice_models) up to the STL's max Z; per layer and object: START, an extruding move, a
# retraction with a wipe, END. print_sequence by object prints each object's layers in a row.
PLATE_ORCA = r"""#!/usr/bin/env python3
import json, pathlib, re, sys
a = sys.argv[1:]
out = pathlib.Path(a[a.index("--outputdir") + 1])
proc = json.loads(pathlib.Path(a[a.index("--load-settings") + 1].split(";")[1]).read_text())
h = float(proc.get("layer_height", "0.2"))
seq = proc.get("print_sequence") == "by object"
objs = []
for s in (x for x in a if x.endswith(".stl")):
    txt = pathlib.Path(s).read_text()
    zs = [float(m) for m in re.findall(r"vertex \S+ \S+ (\S+)", txt)]
    xs = [float(m) for m in re.findall(r"vertex (\S+) ", txt)]
    ys = [float(m) for m in re.findall(r"vertex \S+ (\S+) ", txt)]
    objs.append((pathlib.Path(s).stem, max(zs), (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
L = ["; thumbnails = 16x16/PNG"]
L += [f"EXCLUDE_OBJECT_DEFINE NAME={n}.stl_id_{i}_copy_0" for i, (n, *_r) in enumerate(objs)]
L += ["START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55", "G90", "M83", "G1 E-.8 F2400"]
def layer(i, n, cx, cy, z):
    return [f"EXCLUDE_OBJECT_START NAME={n}.stl_id_{i}_copy_0", f"G1 X{cx} Y{cy} F30000", f"G1 Z{z:.2f}",
            "G1 E.8 F2400", f"G1 X{cx + 1} Y{cy} E.05", "G1 E-.56 F2400", f"G1 X{cx} Y{cy} E-.24",
            f"EXCLUDE_OBJECT_END NAME={n}.stl_id_{i}_copy_0"]
if seq:
    for i, (n, top, cx, cy) in enumerate(objs):
        z = h
        while z <= top + 1e-6:
            L += [";LAYER_CHANGE", f";Z:{z:.2f}"] + layer(i, n, cx, cy, z)
            z += h
else:
    z = h
    while z <= max(o[1] for o in objs) + 1e-6:
        L += [";LAYER_CHANGE", f";Z:{z:.2f}"]
        for i, (n, top, cx, cy) in enumerate(objs):
            if z <= top + 1e-6:
                L += layer(i, n, cx, cy, z)
        z += h
L += ["END_PRINT", "; estimated printing time (normal mode) = 10m 0s", "; use_relative_e_distances = 1"]
# footer like Orca's: the settings and the preset names it sliced with
mach = json.loads(pathlib.Path(a[a.index("--load-settings") + 1].split(";")[0]).read_text())
fil = json.loads(pathlib.Path(a[a.index("--load-filaments") + 1]).read_text())
first = lambda v: v[0] if isinstance(v, list) else v
L += [f"; default_acceleration = {proc.get('default_acceleration')}",
      f"; filament_flow_ratio = {first(fil.get('filament_flow_ratio'))}",
      f"; pressure_advance = {first(fil.get('pressure_advance', '0'))}",
      f"; printer_settings_id = {mach['name']}", f"; print_settings_id = {proc['name']}",
      f'; filament_settings_id = "{fil["name"]}"',
      f"; compatible_printers = {proc.get('compatible_printers')}"]
(out / "plate_1.gcode").write_text("\n".join(L) + "\n")
"""


def write_plate_orca(tmp_path: Path) -> tuple[Path, Path]:
    """PLATE_PRESETS as Orca system profiles under tmp_path/profiles and PLATE_ORCA as tmp_path/orca."""
    root = tmp_path / "profiles"
    for (kind, name), data in PLATE_PRESETS.items():
        d = root / PRESETS["Creality K1 SE"].vendor / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps({"name": name, **data}))
    (tmp_path / "user").mkdir(exist_ok=True)  # ORCA_USER_PRESETS for the tests that set it
    script = tmp_path / "orca"
    script.write_text(PLATE_ORCA)
    script.chmod(0o755)
    return root, script
