"""A Bambu Studio plate through the real CLI (issue https://github.com/alex-mextner/3d-cli/issues/184):
`3d print-preflight` and `3d gcode-overhangs` read Bambu's object labels (`; start printing
object` / M624 blocks) and its vendor blocks, on an excerpt of a real A1 mini G-code."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from head_sequence_fixtures import project_3mf

from .workflow_helper import run_cli

EXCERPT = Path(__file__).resolve().parents[1] / "data" / "bambu_a1mini_path.gcode"
OBJECTS = ("PTA — PTA", "PTB — PTB", "SW — SW", "SWF — SWF", "SWK — SWK")


def test_preflight_attributes_a_bambu_plate_to_its_objects(tmp_path: Path) -> None:
    """A user preflights an A1 mini plate offline: the head sequence finds every project
    object, overhangs judge each object on its raft, and the waste-chute trips of tool changes,
    timelapse and end G-code are not plate moves."""
    shutil.copy(EXCERPT, tmp_path / "plate.gcode")
    project_3mf(
        tmp_path / "plate.3mf",
        {"PTA — PTA": [1, 2], "PTB — PTB": [1, 2], "SW — SW": [4, 1, 2, 3], "SWF — SWF": 1, "SWK — SWK": 1},
        mixed="",
        raft_layers=4,
    )

    pre = run_cli(tmp_path, "print-preflight", "plate.gcode", "--axis-limits", "-13.5:187,-4:186", "--json")
    over = run_cli(tmp_path, "gcode-overhangs", "plate.gcode", "--json", "--no-png")

    checks = {c["name"]: c for c in json.loads(pre.stdout)["checks"]}
    assert checks["head seq"]["status"] == "PASS", checks["head seq"]
    assert "5 object(s) on their recipes" in checks["head seq"]["detail"]
    assert checks["print area"]["status"] == "PASS", checks["print area"]
    assert checks["axis travel"]["status"] == "PASS" and "x -13.50.." in checks["axis travel"]["detail"]
    assert checks["overhangs"]["status"] != "FAIL", checks["overhangs"]
    assert over.returncode == 0, over.stderr
    names = [o["name"] for o in json.loads(over.stdout)["objects"]]
    assert sorted(n.split(" id:")[0] for n in names) == sorted(OBJECTS)
