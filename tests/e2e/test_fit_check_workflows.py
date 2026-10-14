from __future__ import annotations

import json
from pathlib import Path

from fit_fixtures import write_insert, write_plate

from .workflow_helper import run_cli, run_shell


def test_fit_check_locks_a_printed_insert_against_a_rebuilt_socket(tmp_path: Path) -> None:
    """A user printed the insert last week; before printing the rebuilt plate they check that its
    pocket still leaves 0.2 mm per side, then a rebuild that narrowed the pocket is blocked."""
    write_insert(tmp_path / "INSERT.stl")
    write_plate(tmp_path / "PLATE__body.stl")
    write_plate(tmp_path / "NARROW.stl", pocket=(20.2, 10.4))

    good = run_cli(tmp_path, "fit-check", "--insert", "INSERT.stl", "--socket", "PLATE__*.stl",
                   "--gap", "0.2", "--printed-size", "20,10,5")
    assert good.returncode == 0, good.stdout + good.stderr
    assert "fit INSERT ↔ PLATE__body: PASS: size 20 x 10 x 5 mm (printed 20 x 10 x 5)" in good.stdout
    assert ">>> FIT-CHECK: PASS (1 fit)" in good.stdout

    bad = run_cli(tmp_path, "fit-check", "--insert", "INSERT.stl", "--socket", "NARROW.stl",
                  "--gap", "0.2", "--printed-size", "20,10,5")
    assert bad.returncode == 1
    assert "FAIL socket gap off 0.2±0.03 mm" in bad.stdout

    equals = run_cli(tmp_path, "fit-check", "--insert=INSERT.stl", "--socket=PLATE__body.stl", "--gap=0.2")
    assert equals.returncode == 0, equals.stdout + equals.stderr
    assert "fit INSERT ↔ PLATE__body: PASS" in equals.stdout

    missing = run_cli(tmp_path, "fit-check", "--insert", "nope.stl", "--socket", "NARROW.stl", "--gap", "0.2")
    assert missing.returncode == 2
    assert "file not found: nope.stl" in missing.stderr


def test_fit_check_spec_json_feeds_a_pre_print_script(tmp_path: Path) -> None:
    """A pre-print script checks every insert of the project from a fits spec, saves the JSON
    report and pulls the failing inserts out with a pipe."""
    (tmp_path / "assembly").mkdir()
    write_insert(tmp_path / "assembly" / "KEY1.stl")
    write_insert(tmp_path / "assembly" / "KEY2.stl", size=(20.1, 10.0, 5.0))
    write_plate(tmp_path / "assembly" / "BASE1__body.stl")
    (tmp_path / "fits.yaml").write_text(
        "gap: 0.2\n"
        "any_turn: true\n"
        "fits:\n"
        "  - {name: KEY1, insert: assembly/KEY1.stl, socket: 'assembly/BASE1__*.stl', printed_size: [10, 20, 5]}\n"
        "  - {name: KEY2, insert: assembly/KEY2.stl, socket: 'assembly/BASE1__*.stl', printed_size: [10, 20, 5]}\n",
        encoding="utf-8")

    result = run_shell(
        '"$PYTHON" "$THREED" fit-check --spec fits.yaml --json > report.json || true\n"$PYTHON" "$THREED" fit-check --spec fits.yaml | grep "FAIL" > failing.txt || true',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "FAIL"
    key1, key2 = report["fits"]
    assert key1["status"] == "PASS" and key1["printed_size_mm"] == [10, 20, 5] and key1["any_turn"] is True
    assert key1["gap_set_mm"] == 0.2 and abs(key1["gap_mm"] - 0.2) < 0.01 and abs(key1["gap_max_mm"] - 0.2) < 0.01
    assert abs(key1["socket_depth_mm"] - 4.0) < 0.01
    assert key2["status"] == "FAIL" and key2["insert_size_mm"][0] == 20.1
    failing = (tmp_path / "failing.txt").read_text()
    assert "KEY2" in failing and "size 20.1 x 10 x 5 mm differs from the printed 10 x 20 x 5 mm in any turn" in failing
