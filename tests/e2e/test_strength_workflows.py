from __future__ import annotations

import json
from pathlib import Path

import pytest
from structural_fixtures import bar

from .workflow_helper import run_cli


def _bar(tmp_path: Path, name: str = "bar.stl") -> Path:
    path = tmp_path / name
    bar(100.0, 10.0, 10.0).export(path)
    return path


def test_strength_screens_a_bracket_then_checks_it_with_fea(tmp_path: Path) -> None:
    """A user screens a bar with the closed-form beam method, then asks for the FEA label and JSON."""
    _bar(tmp_path)
    screen = run_cli(
        tmp_path,
        "strength",
        "bar.stl",
        "--material",
        "PLA",
        "--load",
        "10",
        "--method",
        "beam",
    )
    assert screen.returncode == 0, screen.stdout + screen.stderr
    assert "method: beam  [ANALYTICAL_SCREEN]" in screen.stdout
    assert ">>> STRENGTH: PASS  (ANALYTICAL_SCREEN" in screen.stdout
    assert "not verified:" in screen.stdout and "creep" in screen.stdout
    assert "NOT_EVALUATED" not in screen.stdout

    solved = run_cli(
        tmp_path,
        "strength",
        "bar.stl",
        "--material",
        "PLA",
        "--load",
        "10",
        "--method",
        "fea",
        "--max-elements",
        "2500",
        "--json",
        "--viz",
        "bar.png",
    )
    assert solved.returncode == 0, solved.stdout + solved.stderr
    report = json.loads(solved.stdout)
    assert report["schema"] == "3d.strength/2"
    assert (
        report["method"] == "fea"
        and report["label"] == "FEA_LINEAR"
        and report["verdict"] == "PASS"
    )
    assert report["mesh"]["total_tets"] > 500 and report["cases"][0]["hotspots"]
    assert report["cases"][0]["max_displacement_mm"] == pytest.approx(1.21, rel=0.05)
    assert (tmp_path / "bar.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert report["artifacts"]["stress_png"].endswith("bar.png")


def test_strength_exits_1_below_the_required_safety_factor(tmp_path: Path) -> None:
    _bar(tmp_path)
    weak = run_cli(
        tmp_path,
        "strength",
        "bar.stl",
        "--material",
        "PLA",
        "--load",
        "10",
        "--method",
        "beam",
        "--safety-factor",
        "9",
    )
    assert weak.returncode == 1
    assert ">>> STRENGTH: WEAK" in weak.stdout
    fail = run_cli(
        tmp_path,
        "strength",
        "bar.stl",
        "--material",
        "PLA",
        "--load",
        "90",
        "--method",
        "beam",
        "--json",
    )
    assert fail.returncode == 1
    assert json.loads(fail.stdout)["verdict"] == "FAIL"


def test_strength_rejects_bad_input_with_exit_2(tmp_path: Path) -> None:
    _bar(tmp_path)
    for argv in (
        ["bar.stl", "--material", "PLA", "--load", "nan"],
        ["bar.stl", "--material", "PLA", "--load", "-3"],
        ["bar.stl", "--material", "NOPE", "--load", "5", "--method", "beam"],
        ["missing.stl", "--material", "PLA", "--load", "5"],
        [
            "bar.stl",
            "--material",
            "PLA",
            "--load",
            "5",
            "--orientation",
            "sideways",
            "--method",
            "beam",
        ],
    ):
        result = run_cli(tmp_path, "strength", *argv)
        assert result.returncode == 2, (argv, result.stdout, result.stderr)
        assert result.stderr.strip()


def test_strength_orientation_changes_the_verdict_and_records_the_source(
    tmp_path: Path,
) -> None:
    _bar(tmp_path)
    base = [
        "strength",
        "bar.stl",
        "--material",
        "PLA",
        "--load",
        "26",
        "--method",
        "beam",
        "--json",
    ]
    flat = run_cli(tmp_path, *base, "--orientation", "z-up")
    standing = run_cli(tmp_path, *base, "--orientation", "x-up")
    flat_report, standing_report = json.loads(flat.stdout), json.loads(standing.stdout)
    assert flat_report["verdict"] == "PASS" and flat.returncode == 0
    assert standing_report["verdict"] == "WEAK" and standing.returncode == 1
    assert standing_report["input"]["bodies"][0]["orientation"]["source"] == "flag x-up"
    assert standing_report["cases"][0]["allowable_mpa"] == pytest.approx(
        0.45 * flat_report["cases"][0]["allowable_mpa"]
    )


def test_strength_hook_check_runs_without_a_file(tmp_path: Path) -> None:
    ok = run_cli(
        tmp_path,
        "strength",
        "--hook",
        "t=1.0,L=7,w=6,y=0.2",
        "--material",
        "PETG",
        "--arm-axis",
        "Z",
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ">>> STRENGTH: PASS" in ok.stdout and "root strain" in ok.stdout
    bad = run_cli(
        tmp_path,
        "strength",
        "--hook",
        "t=1.0,L=7,w=6,y=0.6",
        "--material",
        "PLA",
        "--arm-axis",
        "Z",
        "--json",
    )
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["verdict"] == "FAIL"


def test_strength_project_assembly_uses_mates_and_loads(tmp_path: Path) -> None:
    pytest.importorskip("skfem")
    bar(60.0, 10.0, 6.0).export(tmp_path / "base.stl")
    plate = bar(60.0, 10.0, 3.0)
    plate.apply_translation([0.0, 0.0, 4.5])
    plate.export(tmp_path / "plate.stl")
    (tmp_path / "3d.yaml").write_text(
        "project: {name: bracket, material: PLA}\n"
        "parts:\n  base: {file: base.stl}\n  plate: {file: plate.stl}\n"
        "anchors:\n  wall: {pos: [0, 0, 0], area: 400}\n  tip: {pos: [60, 0, 0], area: 200}\n"
        "loads:\n  clamp: {anchor: wall, kind: fixed}\n  push: {anchor: tip, vector: [0, 0, -4]}\n"
        "mates:\n  - {a: base, b: plate, fit_type: glue}\n",
        encoding="utf-8",
    )
    dry = run_cli(tmp_path, "strength", "3d.yaml", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert json.loads(dry.stdout)["status"] == "PLAN"
    solved = run_cli(tmp_path, "strength", ".", "--max-elements", "3000", "--json")
    assert solved.returncode == 0, solved.stdout + solved.stderr
    report = json.loads(solved.stdout)
    assert report["label"] == "FEA_LINEAR" and [
        c["kind"] for c in report["contacts"]
    ] == ["tied"]
    assert [b["name"] for b in report["input"]["bodies"]] == ["base", "plate"]
    text = run_cli(tmp_path, "strength", "3d.yaml", "--max-elements", "3000")
    assert "contact base <-> plate: tied (3d.yaml mate)" in text.stdout
    assert "hotspot 1:" in text.stdout


def test_strength_help_lists_the_surface(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "strength", "--help")
    assert result.returncode == 0
    for flag in (
        "--method",
        "--orientation",
        "--hook",
        "--loads",
        "--viz",
        "--contact",
        "--crop",
        "--infill",
    ):
        assert flag in result.stdout
