"""`3d prep`, `3d heavy` and `3d remote` as a user runs them: files in, verdicts and artifacts out."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .workflow_helper import run_cli

trimesh: Any | None = None


def _mushroom(path: Path) -> Path:
    tm = pytest.importorskip("trimesh")
    stem = tm.creation.box((10, 10, 20))
    stem.apply_translation([0, 0, 10])
    cap = tm.creation.box((30, 30, 5))
    cap.apply_translation([0, 0, 22.5])
    tm.boolean.union([stem, cap], engine="manifold").export(path)
    return path


PREP = ("prep",)
FLAGS = ("--no-lock", "--skip", "project,slice,gates,proof", "--pitch", "0.5", "--orient", "keep")


def test_prep_json_report_then_all_steps_cached_on_the_second_run(tmp_path: Path) -> None:
    """A user preps a model twice: the first run does the work, the second is cache hits only."""
    model = _mushroom(tmp_path / "mushroom.stl")
    out = tmp_path / "out"
    first = run_cli(tmp_path, *PREP, str(model), "--out", str(out), *FLAGS, "--json", timeout=300)
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout)
    steps = {s["name"]: s for s in data["steps"]}
    assert steps["adapt"]["verdict"] == "PASS" and steps["adapt"]["result"]["ops"]  # the cap got wedges
    assert steps["overhang"]["result"]["island_count"] == 0
    assert (out / "prep.json").is_file() and (out / "meshes" / "adapt.npz").is_file()

    second = run_cli(tmp_path, *PREP, str(model), "--out", str(out), *FLAGS, "--json", timeout=300)
    again = json.loads(second.stdout)["steps"]
    ran = [s for s in again if s["verdict"] != "SKIP"]
    assert ran and all(s["cached"] for s in ran)


def test_prep_status_and_steps_read_the_last_run(tmp_path: Path) -> None:
    """After a run the user lists the steps and asks what the last run decided."""
    model = _mushroom(tmp_path / "m.stl")
    out = tmp_path / "out"
    assert run_cli(tmp_path, *PREP, str(model), "--out", str(out), *FLAGS, "--quiet", timeout=300).returncode == 0
    status = run_cli(tmp_path, *PREP, "status", str(out))
    assert status.returncode == 0
    assert "adapt" in status.stdout and "overall" in status.stdout
    listing = run_cli(tmp_path, *PREP, "steps")
    assert listing.returncode == 0
    assert "diagnose" in listing.stdout and "gates" in listing.stdout


def test_prep_table_is_printed_and_failure_exits_one(tmp_path: Path) -> None:
    """An unrepairable sheet FAILs `repair`: the table says so and the exit code is 1."""
    tm = pytest.importorskip("trimesh")
    sheet = tm.creation.box((20, 20, 0.001))
    sheet = tm.Trimesh(sheet.vertices, sheet.faces[:2], process=False)
    model = tmp_path / "sheet.stl"
    sheet.export(model)
    res = run_cli(tmp_path, *PREP, str(model), "--out", str(tmp_path / "out"), *FLAGS, timeout=300)
    assert res.returncode == 1
    assert "repair" in res.stdout and "FAIL" in res.stdout and "blocked" in res.stdout


def test_prep_usage_errors_exit_two(tmp_path: Path) -> None:
    """Typos and missing files are refused with a message that names the accepted values, never a traceback."""
    model = _mushroom(tmp_path / "m.stl")
    assert run_cli(tmp_path, *PREP).returncode == 1  # no args: usage
    helped = run_cli(tmp_path, *PREP, "--help")
    assert helped.returncode == 0
    assert "3d prep MODEL" in helped.stdout
    for argv in (
        (str(tmp_path / "missing.stl"), "--no-lock"),
        (str(model), "--printer", "x1", "--no-lock"),
        (str(model), "--only", "nope", "--no-lock"),
        (str(model), "--bogus", "--no-lock"),
    ):
        res = run_cli(tmp_path, *PREP, *argv, "--out", str(tmp_path / "o"))
        assert res.returncode == 2, (argv, res.stdout, res.stderr)
        assert res.stderr.strip(), argv  # a rendered message, not a silent exit
    bad_printer = run_cli(tmp_path, *PREP, str(model), "--printer", "x1", "--no-lock", "--out", str(tmp_path / "o"))
    assert "u1" in bad_printer.stderr  # the accepted values are named


def test_prep_spec_file_sets_options_and_flags_win(tmp_path: Path) -> None:
    """A settings file carries the per-object knobs; a flag on the command line beats it; a typo in it is refused."""
    model = _mushroom(tmp_path / "m.stl")
    spec = tmp_path / "spec.yaml"
    spec.write_text("printer: u1\nangle: 50\nprocess:\n  walls: 3\n")
    out = tmp_path / "out"
    res = run_cli(tmp_path, *PREP, str(model), "--out", str(out), "--spec", str(spec), "--angle", "40", *FLAGS, "--json", timeout=300)
    assert res.returncode == 0, res.stderr
    overhang = next(s for s in json.loads(res.stdout)["steps"] if s["name"] == "overhang")
    assert overhang["result"]["angle_deg"] == 40.0  # the flag beat the spec's 50
    bad = tmp_path / "bad.yaml"
    bad.write_text("colour: x\n")
    assert run_cli(tmp_path, *PREP, str(model), "--spec", str(bad), "--no-lock").returncode == 2


def test_heavy_runs_a_command_and_reports_status(tmp_path: Path) -> None:
    """A user wraps a job in the machine lock; inside it the marker is set, and the lock reads free afterwards."""
    lock = tmp_path / "lock"
    env = {"THREED_HEAVY_LOCK": str(lock)}
    res = run_cli(tmp_path, "heavy", "--", "sh", "-c", "echo held=$THREED_HEAVY_HELD", env_extra=env)
    assert res.returncode == 0 and "held=1" in res.stdout
    assert run_cli(tmp_path, "heavy", "status", env_extra=env).stdout.strip() == "free"


def test_remote_without_a_config_asks_for_a_host(tmp_path: Path) -> None:
    """On a machine with no remote.yaml `3d remote` says how to name a host instead of guessing one."""
    env = {"THREED_REMOTE_CONFIG": str(tmp_path / "none.yaml")}
    res = run_cli(tmp_path, "remote", "exec", "--", "ls", env_extra=env)
    assert res.returncode == 2
    assert "no remote host" in res.stderr
    hosts = run_cli(tmp_path, "remote", "hosts", env_extra=env)
    assert hosts.returncode == 0
    assert "no hosts configured" in hosts.stdout
