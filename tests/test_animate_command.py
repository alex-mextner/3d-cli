"""CLI tests for the animate command skeleton."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")
_CUBE = os.path.join(_REPO, "examples", "cube.scad")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO
    return subprocess.run(
        [sys.executable, _THREED, *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_animate_help() -> None:
    result = _run(["animate", "--help"])

    assert result.returncode == 0
    assert "3d animate <file.scad>" in result.stdout


def test_animate_plan_prints_deterministic_json_without_openscad(tmp_path: Path) -> None:
    outdir = os.path.join(str(tmp_path), "frames")

    result = _run(
        [
            "animate",
            _CUBE,
            "--plan",
            "--frames",
            "2",
            "--view",
            "left",
            "--outdir",
            outdir,
            "-D",
            "spin=0:180",
        ]
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["model"] == _CUBE
    assert [frame["defines"] for frame in data["frames"]] == [["spin=0"], ["spin=180"]]
    assert [os.path.basename(frame["output"]) for frame in data["frames"]] == [
        "frame_0000.png",
        "frame_0001.png",
    ]


def test_animate_missing_file_exits_2() -> None:
    result = _run(["animate", "/no/such/file.scad", "--plan"])

    assert result.returncode == 2
    assert "file not found" in result.stderr


def test_animate_plan_rejects_unknown_view() -> None:
    result = _run(["animate", _CUBE, "--plan", "--view", "bogus"])

    assert result.returncode == 2
    assert "accepted:" in result.stderr


def test_animate_render_rejects_outdir_that_is_file(tmp_path: Path) -> None:
    outdir = os.path.join(str(tmp_path), "not-a-dir")
    with open(outdir, "w") as fh:
        fh.write("file")

    result = _run(["animate", _CUBE, "--frames", "1", "--outdir", outdir])

    assert result.returncode == 2
    assert "cannot create output directory" in result.stderr
    assert "Traceback" not in result.stderr
