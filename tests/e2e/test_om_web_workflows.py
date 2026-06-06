from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from .workflow_helper import isolated_env, require_python_module, run_cli, run_shell


def _annotated_project(root: Path) -> Path:
    project = root / "gearbox"
    project.mkdir()
    (project / "3d.yaml").write_text("name: gearbox\n", encoding="utf-8")
    model = project / "gearbox.scad"
    model.write_text(
        """// @id base
// @class printed structural
// @anchor motor pos=[10,0,4] dir=[1,0,0]
// @anchor output pos=[40,0,4] dir=[1,0,0]
// @color steelblue
cube([48, 24, 8]);
""",
        encoding="utf-8",
    )
    return model


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_object_model_exports_named_features_for_automation(tmp_path: Path) -> None:
    """A user extracts anchors/classes/styles from SCAD comments as structured JSON."""
    model = _annotated_project(tmp_path)

    result = run_cli(tmp_path, "om", str(model), ".printed")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["nodes"][0]["id"] == "base"
    assert payload["nodes"][0]["classes"] == ["printed", "structural"]
    assert payload["nodes"][0]["style"] == {"color": "steelblue"}
    assert [anchor["name"] for anchor in payload["anchors"]] == ["motor", "output"]
    assert payload["styles"] == [{"node": "base", "style": {"color": "steelblue"}}]


def test_object_model_json_can_feed_a_project_anchor_inventory(tmp_path: Path) -> None:
    """A user redirects object-model data and turns anchors into a review checklist."""
    model = _annotated_project(tmp_path)

    result = run_shell(
        f'"$PYTHON" "$THREED" om "{model}" .printed > om.json && '
        "\"$PYTHON\" -c 'import json, pathlib; "
        "doc=json.loads(pathlib.Path(\"om.json\").read_text()); "
        "print(\"\\n\".join(a[\"node\"] + \":\" + a[\"name\"] for a in doc[\"anchors\"]))' "
        "> anchors.txt",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "anchors.txt").read_text(encoding="utf-8").splitlines() == [
        "base:motor",
        "base:output",
    ]


def test_web_bad_port_is_a_structured_cli_error_before_server_start(tmp_path: Path) -> None:
    """A user sees a precise web-dashboard argument error without starting uvicorn."""
    result = run_cli(tmp_path, "web", "--root", str(tmp_path), "--port", "not-a-port")

    assert result.returncode == 2
    assert "got --port='not-a-port'" in result.stderr
    assert "an integer (e.g. 8733)" in result.stderr
    assert "Traceback" not in result.stderr


def test_web_dashboard_serves_project_json_when_optional_tier_is_installed(tmp_path: Path) -> None:
    """A user starts the dashboard and reads the scanned project list through HTTP JSON."""
    require_python_module("fastapi")
    require_python_module("uvicorn")
    _annotated_project(tmp_path)
    port = _free_port()
    env = isolated_env(tmp_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(env["THREED"])),
            "web",
            "--root",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 20
        payload: dict[str, object] | None = None
        last_error = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"web exited early with {proc.returncode}: {proc.stdout.read() if proc.stdout else ''}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/projects", timeout=1) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    break
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.25)
        assert payload is not None, last_error
        projects = payload["projects"]
        assert isinstance(projects, list)
        assert projects[0]["name"] == "gearbox"
        assert str(projects[0]["path"]).endswith("/gearbox")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
