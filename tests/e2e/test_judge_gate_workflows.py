"""`3d judge --rubric / --min-dim` through bin/3d: usage errors, and a blind verdict that must not pass a gate."""
from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import run_cli

CANNED = json.dumps({"nostril_shape": 4, "nostril_colour": 4})
RUBRIC = [
    {"key": "nostril_shape", "anchor_0": "no nostrils", "anchor_2": "blobs", "anchor_4": "tight comma slits"},
    {"key": "nostril_colour", "anchor_0": "black", "anchor_2": "too dark", "anchor_4": "slightly darker than the nose"},
]


def _files(tmp_path: Path) -> None:
    for name in ("render.png", "reference.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "rubric.json").write_text(json.dumps(RUBRIC))


def test_a_text_only_judge_cannot_pass_min_dim_even_when_it_says_4(tmp_path: Path) -> None:
    """A user gates a judge run with --min-dim while only a text-only backend is available: the gate fails, a blind verdict passes nothing."""
    _files(tmp_path)
    result = run_cli(
        tmp_path, "judge", "render.png", "reference.png", "--rubric", "rubric.json", "--min-dim", "3", "--stability-n", "0",
        env_extra={"THREED_AI_MOCK_RESPONSE": CANNED},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "GATE=FAIL" in result.stdout and "RUNS_MEAN=NA" in result.stdout
    assert "blind verdict" in result.stderr


def test_rubric_flag_errors_exit_two(tmp_path: Path) -> None:
    """A user passes a broken rubric file, a missing one or a non-number gate: each is a usage error."""
    _files(tmp_path)
    (tmp_path / "bad.json").write_text("{}")
    bad = run_cli(tmp_path, "judge", "render.png", "reference.png", "--rubric", "bad.json")
    assert bad.returncode == 2 and "non-empty JSON list" in bad.stderr
    missing = run_cli(tmp_path, "judge", "render.png", "reference.png", "--rubric", "nope.json")
    assert missing.returncode == 2
    number = run_cli(tmp_path, "judge", "render.png", "reference.png", "--max-mean", "low")
    assert number.returncode == 2 and "--max-mean must be a number" in number.stderr
    assert "non-empty JSON list" in bad.stderr


def test_help_documents_the_gates_and_the_rubric_file(tmp_path: Path) -> None:
    """A user reads `3d judge --help` and the doc page: the rubric and gate options are in both."""
    result = run_cli(tmp_path, "judge", "--help")
    assert result.returncode == 0
    doc = (Path(__file__).resolve().parents[2] / "docs" / "commands" / "judge.md").read_text()
    for flag in ("--rubric FILE.json", "--min-dim", "--max-mean"):
        assert flag in result.stdout
        assert flag in doc
