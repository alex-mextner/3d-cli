"""E2E: a user checks a sliced plate's per-layer head sequence through bin/3d."""
from __future__ import annotations

import json
from pathlib import Path

from head_sequence_fixtures import gcode, project_3mf

from .workflow_helper import run_cli


def test_slice_check_head_sequence_json_flags_object_on_wrong_recipe(tmp_path: Path) -> None:
    """Before a multi-hour colour-mix print, the user learns object B ignores pattern 14."""
    proj = project_3mf(tmp_path / "plate.3mf", {"A": 5, "B": 5, "C": 6})
    layers = [{"A": 1 if i % 2 else 4, "B": 1, "C": 2} for i in range(6)]
    g = gcode(tmp_path / "plate.gcode", layers)

    result = run_cli(tmp_path, "slice-check", str(proj), "--gcode", str(g),
                     "--check", "head-sequence", "--json")

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    hs = payload["checks"][0]
    assert hs["status"] == "FAIL"
    objs = {o["name"]: o for o in hs["objects"]}
    assert objs["A"]["status"] == "ok"
    assert objs["B"]["status"] == "FAIL" and objs["B"]["patterns"] == ["14"]
    assert objs["C"]["status"] == "local-z"


def test_slice_check_head_sequence_passes_correct_plate(tmp_path: Path) -> None:
    """A user confirms a correctly sliced colour-mix plate passes before sending it to print."""
    proj = project_3mf(tmp_path / "plate.3mf", {"A": 5})
    g = gcode(tmp_path / "plate.gcode", [{"A": 1 if i % 2 else 4} for i in range(4)])
    result = run_cli(tmp_path, "slice-check", str(proj), "--gcode", str(g), "--check", "head-sequence")
    assert result.returncode == 0, result.stderr
    assert "ok   A" in result.stdout
