"""Regression checks for the local and CI test-gate dependency contract."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _pyproject_list(name: str) -> list[str]:
    tomllib = pytest.importorskip("tomllib", reason="pyproject parsing uses the Python 3.11 stdlib TOML parser")
    pyproject = cast(dict[str, Any], tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")))
    value: object = pyproject
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            raise AssertionError(f"pyproject.toml must define {name} as a list")
        value = value[part]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssertionError(f"pyproject.toml {name} must be a list of strings")
    return value


def _declared_dependency_names(requirements: list[str]) -> set[str]:
    names: set[str] = set()
    for requirement in requirements:
        name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def _rig_test_script() -> str:
    yaml = pytest.importorskip("yaml", reason="rig.yaml parsing uses PyYAML from the core dependency set")
    rig = cast(dict[str, Any], yaml.safe_load((ROOT / "rig.yaml").read_text(encoding="utf-8")))
    scripts = rig.get("scripts")
    if not isinstance(scripts, dict):
        raise AssertionError("rig.yaml must define top-level scripts")
    script = scripts.get("test")
    if not isinstance(script, str) or not script.strip():
        raise AssertionError("rig.yaml scripts.test must be a non-empty command string")
    return script


def _test_script_tool_dependencies() -> set[str]:
    script = _rig_test_script()
    dependencies = {match.lower().replace("_", "-") for match in re.findall(r"--with\s+([A-Za-z0-9_.-]+)", script)}
    if not dependencies:
        raise AssertionError("rig.yaml scripts.test must declare uv --with dependencies")
    return dependencies


def test_rig_yaml_declares_the_full_test_gate_script() -> None:
    script = _rig_test_script()
    assert "tests/run_gate.py" in script
    assert "ruff" in script
    assert "pytest" in script
    assert "mypy" in script


def test_dev_extra_covers_the_local_test_gate_runtime() -> None:
    runtime = _declared_dependency_names(_pyproject_list("project.dependencies"))
    dev = _declared_dependency_names(_pyproject_list("project.optional-dependencies.dev"))

    missing = sorted(_test_script_tool_dependencies() - runtime - dev)

    assert not missing, (
        "`dev run test` resolves its tools from the dev/runtime dependency set; missing from "
        f"pyproject.toml: {', '.join(missing)}"
    )


def test_rig_test_script_accepts_appended_pytest_args(tmp_path: Path) -> None:
    script = _rig_test_script()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv-argv.txt"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(log))}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = str(fake_bin)
    result = subprocess.run(
        ["/bin/sh", "-c", f"{script} -k registry"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines()[-4:] == [
        "python",
        "tests/run_gate.py",
        "-k",
        "registry",
    ]


def test_dev_extra_keeps_ci_coverage_dependencies_installable() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--cov=lib" in workflow
    assert "--cov-fail-under=80" in workflow

    dev = _declared_dependency_names(_pyproject_list("project.optional-dependencies.dev"))

    assert "pytest-cov" in dev


def test_agenttools_dev_runner_appends_extra_args_when_available(tmp_path: Path) -> None:
    pytest.importorskip("agenttools_dev.cli", reason="agent-tools dev CLI is supplied by the broader dev-cli work")

    env = dict(os.environ)
    recorder = tmp_path / "record_args.py"
    recorder.write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        "pathlib.Path('args.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (tmp_path / "rig.yaml").write_text(
        f"scripts:\n  test: {shlex.quote(sys.executable)} {shlex.quote(str(recorder))}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenttools_dev",
            "run",
            "test",
            "--",
            "-k",
            "registry",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "args.json").read_text(encoding="utf-8")) == ["-k", "registry"]
