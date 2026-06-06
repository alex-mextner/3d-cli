"""Regression checks for the local and CI test-gate dependency contract."""
from __future__ import annotations

import ast
import re
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


def _test_command_tool_dependencies() -> set[str]:
    tree = ast.parse((ROOT / "lib" / "commands" / "test.py").read_text(encoding="utf-8"))
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "exec_tool":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if not isinstance(node.args[0].value, str):
            continue
        dependencies.update(dep.strip().lower().replace("_", "-") for dep in node.args[0].value.split(","))

    if not dependencies:
        raise AssertionError("lib/commands/test.py must call exec_tool with literal dependencies")
    return dependencies


def test_dev_extra_covers_the_local_test_gate_runtime() -> None:
    runtime = _declared_dependency_names(_pyproject_list("project.dependencies"))
    dev = _declared_dependency_names(_pyproject_list("project.optional-dependencies.dev"))

    missing = sorted(_test_command_tool_dependencies() - runtime - dev)

    assert not missing, (
        "`3d test` resolves its tools from the dev/runtime dependency set; missing from "
        f"pyproject.toml: {', '.join(missing)}"
    )


def test_dev_extra_keeps_ci_coverage_dependencies_installable() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--cov=lib" in workflow
    assert "--cov-fail-under=80" in workflow

    dev = _declared_dependency_names(_pyproject_list("project.optional-dependencies.dev"))

    assert "pytest-cov" in dev
