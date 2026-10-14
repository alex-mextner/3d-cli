from __future__ import annotations

import json
from pathlib import Path

from halves_fixtures import TREE_LONG_TIER, half_mesh, pair_objects, write_orca_3mf, write_stl

from .workflow_helper import run_cli, run_shell


def test_halves_check_passes_a_matching_pair_and_blocks_a_mismatched_one(tmp_path: Path) -> None:
    """A user who split a spruce in two checks the plate before printing: the good pair passes,
    the pair whose cut faces differ fails with the overhang width and an overlay image."""
    write_orca_3mf(tmp_path / "good.3mf", pair_objects("TA", half_mesh(1), half_mesh(2), colour_split=True))
    write_orca_3mf(tmp_path / "bad.3mf", pair_objects("TB", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)))

    good = run_cli(tmp_path, "halves-check", "good.3mf")
    assert good.returncode == 0, good.stdout + good.stderr
    assert "TA1/TA2" in good.stdout
    assert ">>> HALVES-CHECK: PASS (1 pair)" in good.stdout

    bad = run_cli(tmp_path, "halves-check", "bad.3mf", "--png", "overlays")
    assert bad.returncode == 1
    assert "TB1/TB2" in bad.stdout and "FAIL outline: cut faces differ by up to 3.00 mm" in bad.stdout
    assert (tmp_path / "overlays" / "bad__TB.png").read_bytes()[:4] == b"\x89PNG"


def test_halves_check_json_feeds_a_shell_pipeline(tmp_path: Path) -> None:
    """A pre-print script saves the JSON report and greps the failing pairs out of the table."""
    write_orca_3mf(tmp_path / "bad.3mf", pair_objects("TB", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)))

    result = run_shell(
        "\n".join([
            '"$PYTHON" "$THREED" halves-check bad.3mf --json > report.json || true',
            '"$PYTHON" "$THREED" halves-check bad.3mf | grep "FAIL" > failing.txt || true',
        ]),
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "FAIL"
    assert report["pairs"][0]["pair"] == "TB" and report["pairs"][0]["max_dev_mm"] > 2.5
    assert "TB1/TB2" in (tmp_path / "failing.txt").read_text()


def test_halves_check_stl_pair_and_unmarked_halves(tmp_path: Path) -> None:
    """Exported STL halves in the assembly frame are checked without any metadata; a 3MF whose
    objects are named like halves but carry no pair marks is flagged, and fails when strict."""
    write_stl(tmp_path / "T1.stl", half_mesh(1))
    write_stl(tmp_path / "T2.stl", half_mesh(2))
    stl = run_cli(tmp_path, "halves-check", "--pair", "T1.stl", "T2.stl", "--plane", "y=0")
    assert stl.returncode == 0, stl.stdout + stl.stderr
    assert "T1/T2" in stl.stdout and "PASS" in stl.stdout

    write_orca_3mf(tmp_path / "unmarked.3mf", pair_objects("T", half_mesh(1), half_mesh(2), meta=False))
    lax = run_cli(tmp_path, "halves-check", "unmarked.3mf")
    assert lax.returncode == 0
    assert "WARN unmarked.3mf: objects T1 and T2 look like the two halves" in lax.stdout
    strict = run_cli(tmp_path, "halves-check", "unmarked.3mf", "--strict-names")
    assert strict.returncode == 1

    missing = run_cli(tmp_path, "halves-check", "nope.3mf")
    assert missing.returncode == 2
    assert "file not found: nope.3mf" in missing.stderr
