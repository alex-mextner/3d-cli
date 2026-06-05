"""CLI smoke harness: run `3d <cmd> --help` for every registered command, and run the
SAFE commands against examples/cube.scad. Commands needing a tool that is absent are
SKIPPED (not failed) so the suite is green on a bare machine."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from cli.registry import discover

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")
_CUBE = os.path.join(_REPO, "examples", "cube.scad")


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO
    return subprocess.run(
        [sys.executable, _THREED, *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _have_openscad() -> bool:
    if shutil.which("openscad"):
        return True
    return any(
        os.access(p, os.X_OK)
        for p in (
            "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
            "/opt/homebrew/bin/openscad",
            "/usr/local/bin/openscad",
        )
    )


def _have_magick() -> bool:
    return bool(shutil.which("magick") or shutil.which("convert"))


ALL_COMMANDS = sorted(c.name for c in discover().commands())


def test_help_top_level() -> None:
    r = _run(["help"])
    assert r.returncode == 0
    assert "USAGE" in r.stdout
    r2 = _run([])
    assert r2.returncode == 0  # bare `3d` => help


def test_version() -> None:
    r = _run(["version"])
    assert r.returncode == 0
    assert r.stdout.strip().startswith("3d v")


def test_unknown_command_exits_2() -> None:
    r = _run(["definitely-not-a-command"])
    assert r.returncode == 2
    assert "unknown command" in r.stderr


@pytest.mark.parametrize("cmd", ALL_COMMANDS)
def test_each_command_help(cmd: str) -> None:
    """`3d <cmd> --help` must exit 0 and print its own usage (no traceback)."""
    r = _run([cmd, "--help"])
    assert r.returncode == 0, f"{cmd} --help exited {r.returncode}: {r.stderr}"
    assert "Traceback" not in r.stderr
    assert r.stdout.strip(), f"{cmd} --help produced no usage text"


@pytest.mark.skipif(not _have_openscad(), reason="OpenSCAD not installed")
def test_validate_cube() -> None:
    r = _run(["validate", _CUBE])
    assert r.returncode == 0
    assert "syntax OK" in r.stdout


def test_params_cube() -> None:
    # params is pure-python; no external tool needed.
    r = _run(["params", _CUBE, "--json"])
    assert r.returncode == 0
    assert '"width"' in r.stdout


@pytest.mark.skipif(not _have_openscad(), reason="OpenSCAD not installed")
def test_render_view_cube(tmp_path: object) -> None:
    out = os.path.join(str(tmp_path), "v.png")  # type: ignore[arg-type]
    r = _run(["render", _CUBE, "--view", "left", "-o", out])
    assert r.returncode == 0, r.stderr
    assert os.path.isfile(out) and os.path.getsize(out) > 0


@pytest.mark.skipif(not _have_openscad(), reason="OpenSCAD not installed")
def test_export_cube(tmp_path: object) -> None:
    out = os.path.join(str(tmp_path), "c.stl")  # type: ignore[arg-type]
    r = _run(["export", _CUBE, "-o", out])
    assert r.returncode == 0, r.stderr
    assert "STATUS: PASS" in r.stdout
    assert os.path.isfile(out)


@pytest.mark.skipif(not _have_openscad(), reason="OpenSCAD not installed")
def test_export_bad_extension_exits_2(tmp_path: object) -> None:
    out = os.path.join(str(tmp_path), "c.bogus")  # type: ignore[arg-type]
    r = _run(["export", _CUBE, "-o", out])
    assert r.returncode == 2
    assert "accepted: .stl" in r.stdout + r.stderr


@pytest.mark.skipif(not _have_openscad(), reason="OpenSCAD not installed")
def test_render_missing_file_exits_2() -> None:
    r = _run(["render", "/no/such/file.scad"])
    assert r.returncode == 2
    assert "file not found" in r.stderr


def test_doctor_runs() -> None:
    # doctor never crashes; exit 0 (all present) or 1 (something missing).
    r = _run(["doctor"])
    assert r.returncode in (0, 1)
    assert "3d doctor" in r.stdout
