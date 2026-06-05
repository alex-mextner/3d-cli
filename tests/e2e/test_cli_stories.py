"""Human-readable e2e stories for practical 3d CLI workflows."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .cli_helper import CUBE, REPO_ROOT, THREED


def _story_env(tmp_path: Path) -> dict[str, str]:
    config_home = tmp_path / "xdg-config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True, exist_ok=True)
    (app_config / ".bootstrapped").write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "REPO_ROOT": str(REPO_ROOT),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "PYTHONWARNINGS": "error",
        }
    )
    env.pop("PYTHONPATH", None)
    return env


def _run_3d(
    tmp_path: Path,
    *args: str,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(THREED), *args],
        cwd=REPO_ROOT,
        env=_story_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _json_rows(stdout: str) -> list[dict[str, Any]]:
    payload = json.loads(stdout)
    assert isinstance(payload, list)
    assert all(isinstance(row, dict) for row in payload)
    return payload


def test_user_discovers_the_command_surface_before_installing_heavy_tools(
    tmp_path: Path,
) -> None:
    """A new user can read top-level and command help without OpenSCAD work."""
    help_result = _run_3d(tmp_path, "help")

    assert help_result.returncode == 0, help_result.stderr
    assert "USAGE" in help_result.stdout
    assert "RENDER & VIEW" in help_result.stdout
    assert "QA & GATES" in help_result.stdout
    assert "params" in help_result.stdout
    assert "check" in help_result.stdout
    assert "render" in help_result.stdout

    for command, expected_phrase in (
        ("params", "Customizer-style parameters"),
        ("check", "acceptance master gate"),
        ("render", "single view"),
    ):
        command_help = _run_3d(tmp_path, command, "--help")
        assert command_help.returncode == 0, command_help.stderr
        assert f"3d {command}" in command_help.stdout
        assert expected_phrase in command_help.stdout


def test_user_inspects_a_parametric_model_as_structured_json(tmp_path: Path) -> None:
    """The params command turns OpenSCAD Customizer comments into data."""
    result = _run_3d(tmp_path, "params", str(CUBE), "--json")

    assert result.returncode == 0, result.stderr
    rows = _json_rows(result.stdout)
    by_name = {row["name"]: row for row in rows}
    assert list(by_name) == ["width", "depth", "height", "wall"]
    assert by_name["width"] == {
        "name": "width",
        "value": "20",
        "type": "integer",
        "range": "10:40",
        "description": "outer width (mm)",
    }
    assert by_name["wall"]["value"] == "2"
    assert by_name["wall"]["description"] == "wall thickness (mm)"


def test_user_saves_params_json_for_the_next_shell_step(tmp_path: Path) -> None:
    """A shell workflow can redirect CLI JSON, then load it in another tool."""
    output_path = tmp_path / "cube-params.json"
    with output_path.open("w", encoding="utf-8") as stdout_file:
        result = subprocess.run(
            [sys.executable, str(THREED), "params", str(CUBE), "--json"],
            cwd=REPO_ROOT,
            env=_story_env(tmp_path),
            stdout=stdout_file,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )

    assert result.returncode == 0, result.stderr
    rows = _json_rows(output_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["width", "depth", "height", "wall"]
    assert {row["name"]: row["value"] for row in rows}["height"] == "16"


def test_user_gets_actionable_errors_for_wrong_command_or_file(
    tmp_path: Path,
) -> None:
    """Bad invocations fail with stable exit codes and user-facing text."""
    unknown = _run_3d(tmp_path, "definitely-not-a-command")

    assert unknown.returncode == 2
    assert unknown.stdout == ""
    assert "unknown command" in unknown.stderr
    assert "Traceback" not in unknown.stderr

    missing_file = _run_3d(tmp_path, "params", "examples/does-not-exist.scad")
    assert missing_file.returncode == 2
    assert "file not found" in missing_file.stderr
    assert "examples/does-not-exist.scad" in missing_file.stderr
    assert "Traceback" not in missing_file.stderr


def test_user_checks_the_local_environment_with_doctor(tmp_path: Path) -> None:
    """Doctor is a readable environment report, even when dependencies are absent."""
    result = _run_3d(tmp_path, "doctor", timeout=30)

    assert result.returncode in (0, 1)
    assert "3d doctor" in result.stdout
    assert "Core" in result.stdout
    assert "Python runtime" in result.stdout
    assert "OpenSCAD libraries" in result.stdout
    assert "install:" in result.stdout or "DOCTOR: PASS" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_user_exports_machine_capabilities_as_json(tmp_path: Path) -> None:
    """Hardware list gives scripts a stable machine/toolchain capability report."""
    result = _run_3d(tmp_path, "hardware", "list", "--json", timeout=30)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["os"]
    assert payload["machine"]
    assert payload["cpu_count"] >= 1
    assert isinstance(payload["valid"], bool)
    names = {item["name"] for item in payload["items"]}
    assert {"openscad", "imagemagick", "slicer", "python3", "python mesh stack"} <= names
    assert "Traceback" not in result.stderr
