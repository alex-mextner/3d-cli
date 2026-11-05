"""`3d printer bed check NAME --assume-empty "why"` end to end through bin/3d against a loopback fake Moonraker:
the owner's confirmation is recorded (evidence JSON + state record), the preflight `bed` line of `3d printer
start` then passes with the note until a new job shows up in the printer's history, and the flags are validated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_printer_bed_workflows import GCODE, HAND_ROI, jpeg, u1_after_job, write_u1
from .workflow_helper import run_cli

pytest.importorskip("numpy")
pytest.importorskip("cv2")

BED = f"{{enabled: true, interval_s: 0.2, roi: {HAND_ROI}}}"
JOB = {"job_id": "000070", "filename": "old.gcode", "status": "completed", "start_time": 1_700_000_000.0,
       "print_duration": 60.0, "filament_used": 10.0}  # fmt: skip


def test_the_owners_word_is_recorded_and_the_start_gate_passes_until_a_new_job(tmp_path: Path) -> None:
    moon = u1_after_job()
    moon.history = [dict(JOB)]
    (tmp_path / "part.gcode").write_text(GCODE, encoding="utf-8")
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED)
        assert run_cli(tmp_path, "printer", "bed", "reference", "u1", "--capture").returncode == 0
        assert run_cli(tmp_path, "printer", "upload", "u1", "part.gcode").returncode == 0
        moon.camera = jpeg("u1_occupied_1351.jpg")
        refused = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes")
        said = run_cli(
            tmp_path, "printer", "bed", "check", "u1", "--no-move", "--assume-empty", "wiped it, I looked", "--json"
        )
        passed = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes")
        moon.history.insert(0, {**JOB, "job_id": "000071", "filename": "part.gcode"})
        again = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes")

    assert refused.returncode == 1 and "FAIL  bed" in refused.stdout
    assert '3d printer bed check u1 --assume-empty "' in refused.stdout
    assert said.returncode == 0, said.stdout + said.stderr
    rec = json.loads(said.stdout)["assume_empty"]
    assert rec["recorded"] is True
    evidence = json.loads(Path(rec["evidence"]).read_text(encoding="utf-8"))
    assert evidence["why"] == "wiped it, I looked"
    assert evidence["verdict"]["bed"] == "occupied"
    assert evidence["job_marker"]["job_id"] == "000070"
    assert evidence["who"]
    state = next(tmp_path.glob("xdg-state/**/bed/u1.assumed.json"))
    assert json.loads(state.read_text(encoding="utf-8"))["why"] == "wiped it, I looked"
    # the other gates of this bare G-code (thumbnails, slicer warnings) may fail: the `bed` line is the point
    assert "PASS  bed" in passed.stdout
    assert "owner confirmed empty at" in passed.stdout
    assert "wiped it, I looked" in passed.stdout
    assert again.returncode == 1 and "FAIL  bed" in again.stdout
    assert "no longer applies" in again.stdout


def test_assume_empty_flags_are_validated(tmp_path: Path) -> None:
    moon = u1_after_job()
    with moon.serve():
        write_u1(tmp_path, moon, bed=BED)
        blank = run_cli(tmp_path, "printer", "bed", "check", "u1", "--assume-empty", " ")
        ttl_alone = run_cli(tmp_path, "printer", "bed", "check", "u1", "--assume-empty-ttl", "30")
        bad_ttl = run_cli(tmp_path, "printer", "bed", "check", "u1", "--assume-empty", "x", "--assume-empty-ttl", "0")
        stray = run_cli(tmp_path, "printer", "bed", "status", "u1", "--assume-empty", "x")
        helped = run_cli(tmp_path, "printer", "bed", "--help")

    assert blank.returncode == 2 and "needs the reason" in blank.stderr
    assert ttl_alone.returncode == 2 and "positive number of minutes" in ttl_alone.stderr
    assert bad_ttl.returncode == 2
    assert stray.returncode == 2 and "does not take --assume-empty" in stray.stderr
    assert "--assume-empty-ttl MIN" in helped.stdout
