"""E2E: a user lints a mixed-colour project before slicing and preflights its G-code (#411)."""

from __future__ import annotations

import json
from pathlib import Path

from head_sequence_fixtures import gcode as head_gcode
from mix_fixtures import orca_project, tile

from .workflow_helper import run_cli


def test_mix_check_fails_a_flat_mixed_tile_and_passes_it_standing(
    tmp_path: Path,
) -> None:
    """The PTA tile lies flat in mix 431: the lint names it; stood up, the same project passes."""
    orca_project(tmp_path / "flat.3mf", [tile("PTA", 5), tile("REF", 1)])
    orca_project(tmp_path / "standing.3mf", [tile("PTA", 5, standing=True)])

    flat = run_cli(tmp_path, "mix", "check", "flat.3mf")
    flat_json = run_cli(tmp_path, "mix", "check", "flat.3mf", "--json")
    standing = run_cli(tmp_path, "mix", "check", "standing.3mf")

    assert flat.returncode == 1, flat.stdout + flat.stderr
    assert "mix check flat.3mf: FAIL" in flat.stdout
    assert (
        "FAIL PTA / PTA: filament 5 (431) — visible face horizontal: top 400 mm²"
        in flat.stdout
    )
    assert "REF" not in flat.stdout  # a physical filament is not a mix
    payload = json.loads(flat_json.stdout)
    assert flat_json.returncode == 1
    assert payload["status"] == "FAIL" and payload["failures"] == 1
    assert [
        (p["object"], p["recipe"], p["top_mm2"], p["side_mm2"])
        for p in payload["parts"]
    ] == [("PTA", "431", 400.0, 240.0)]
    assert standing.returncode == 0, standing.stdout + standing.stderr
    assert "mix check standing.3mf: PASS" in standing.stdout


def test_mix_check_rejects_a_file_that_is_not_a_project(tmp_path: Path) -> None:
    (tmp_path / "part.3mf").write_bytes(b"solid not a zip")
    result = run_cli(tmp_path, "mix", "check", "part.3mf")
    assert result.returncode == 2
    assert "not a readable Orca-family project 3MF" in result.stderr


def test_print_preflight_shows_the_mix_face_line_from_the_sibling_project(
    tmp_path: Path,
) -> None:
    """plate.3mf next to plate.gcode: the flat mixed tile FAILs preflight; standing it PASSes;
    without a project 3MF the line is a SKIP that says what to pass."""
    layers = [{"PTA": int("431"[i % 3])} for i in range(6)]
    orca_project(tmp_path / "plate.3mf", [tile("PTA", 5)])
    head_gcode(tmp_path / "plate.gcode", layers)
    good = tmp_path / "good"
    good.mkdir()
    orca_project(good / "plate.3mf", [tile("PTA", 5, standing=True)])
    head_gcode(good / "plate.gcode", layers)
    bare = tmp_path / "bare"
    bare.mkdir()
    head_gcode(bare / "plate.gcode", layers)

    bad = run_cli(tmp_path, "print-preflight", "plate.gcode")
    ok = run_cli(tmp_path, "print-preflight", "good/plate.gcode", "--json")
    none = run_cli(tmp_path, "print-preflight", "bare/plate.gcode")

    assert bad.returncode == 1
    assert (
        "FAIL  mix face    1 of 1 mixed part(s) show their colour on a horizontal face "
        "(plate.3mf next to the G-code)" in bad.stdout
    )
    assert "FAIL PTA / PTA: filament 5 (431)" in bad.stdout
    checks = {c["name"]: c for c in json.loads(ok.stdout)["checks"]}
    assert checks["mix face"]["status"] == "PASS", checks["mix face"]
    assert checks["head seq"]["status"] == "PASS", checks["head seq"]
    assert "SKIP  mix face    no project 3MF" in none.stdout
