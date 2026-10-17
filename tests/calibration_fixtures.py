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
"""

from __future__ import annotations

import json
from pathlib import Path

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
