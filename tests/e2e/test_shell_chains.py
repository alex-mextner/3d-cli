from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_THREED = _REPO / "bin" / "3d"
_CUBE = _REPO / "examples" / "cube.scad"


def _run_shell(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    config_home = tmp_path / "xdg-config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True, exist_ok=True)
    (app_config / ".bootstrapped").write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "CUBE": str(_CUBE),
        "PYTHON": sys.executable,
        "REPO_ROOT": str(_REPO),
        "THREED": str(_THREED),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    })
    return subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_params_json_redirects_to_file(tmp_path: Path) -> None:
    result = _run_shell('"$THREED" params "$CUBE" --json > params.json', tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    payload = json.loads((tmp_path / "params.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in payload] == ["width", "depth", "height", "wall"]


def test_params_json_pipe_feeds_python_stdin(tmp_path: Path) -> None:
    result = _run_shell(
        '"$THREED" params "$CUBE" --json | '
        "\"$PYTHON\" -c 'import json, sys; "
        "print(json.load(sys.stdin)[-1][\"name\"])'",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "wall\n"


def test_help_output_survives_pipeline(tmp_path: Path) -> None:
    result = _run_shell(
        '"$THREED" help | '
        "\"$PYTHON\" -c 'import sys; "
        "print(\"USAGE\" in sys.stdin.read())'",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "True\n"


def test_unknown_command_with_redirection_preserves_exit_code(tmp_path: Path) -> None:
    result = _run_shell(
        '"$THREED" definitely-not-a-command > stdout.txt 2> stderr.txt',
        tmp_path,
    )

    assert result.returncode == 2
    assert (tmp_path / "stdout.txt").read_text(encoding="utf-8") == ""
    assert "unknown command" in (tmp_path / "stderr.txt").read_text(encoding="utf-8")
