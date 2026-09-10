"""Regression checks for the CI-integrity contract in tests/run_gate.py.

These guard the exact footgun the gate is meant to prevent: a stale/leaked REPO_ROOT or a
sibling-worktree venv making `dev run test` test the WRONG tree and report a false green.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_run_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_gate_under_test", ROOT / "tests" / "run_gate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_gate = _load_run_gate()


def test_resolve_root_is_the_tree_this_file_lives_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPO_ROOT", raising=False)
    assert run_gate._resolve_root() == os.path.realpath(str(ROOT))


def test_resolve_root_ignores_a_stale_leaked_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = tmp_path / "sibling-worktree"
    foreign.mkdir()
    monkeypatch.setenv("REPO_ROOT", str(foreign))

    resolved = run_gate._resolve_root()

    assert resolved == os.path.realpath(str(ROOT))
    assert resolved != os.path.realpath(str(foreign))
    assert "ignoring REPO_ROOT" in capsys.readouterr().err


def test_warn_on_foreign_venv_flags_a_sibling_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_gate.sys, "prefix", str(tmp_path / "sibling-worktree" / ".venv"))
    run_gate._warn_on_foreign_venv(os.path.realpath(str(ROOT)))
    assert "OUTSIDE this tree" in capsys.readouterr().err


def test_warn_on_foreign_venv_is_quiet_for_a_local_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_gate.sys, "prefix", str(ROOT / ".venv"))
    run_gate._warn_on_foreign_venv(os.path.realpath(str(ROOT)))
    assert capsys.readouterr().err == ""


def test_warn_on_foreign_venv_ignores_a_uv_overlay_cache_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `uv run --with` may report a prefix in its cache (basename is a hash, not `.venv`).
    # That is the normal gate path, not a sibling-worktree borrow — it must stay quiet.
    monkeypatch.setattr(run_gate.sys, "prefix", str(tmp_path / "environments-v2" / "a1b2c3"))
    run_gate._warn_on_foreign_venv(os.path.realpath(str(ROOT)))
    assert capsys.readouterr().err == ""


def test_gate_targets_the_local_tree_and_propagates_corrected_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A leaked REPO_ROOT must not reach ruff/pytest/mypy: every child runs against the local
    tree (paths under ROOT, cwd == ROOT) with REPO_ROOT rewritten to the local root."""
    monkeypatch.setenv("REPO_ROOT", str(tmp_path / "elsewhere"))
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_gate.subprocess, "run", fake_run)

    rc = run_gate.main([])
    assert rc == 0

    real_root = os.path.realpath(str(ROOT))
    assert len(calls) == 3  # ruff, pytest, mypy
    for call in calls:
        assert call["cwd"] == real_root
        assert call["env"]["REPO_ROOT"] == real_root
        # Every filesystem target handed to a tool lives under the local tree.
        for token in call["cmd"]:
            if isinstance(token, str) and token.startswith("/"):
                assert token == real_root or token.startswith(real_root + os.sep) or token == sys.executable


def test_rig_test_script_pins_the_project_environment() -> None:
    yaml = pytest.importorskip("yaml")
    rig = yaml.safe_load((ROOT / "rig.yaml").read_text(encoding="utf-8"))
    script = rig["scripts"]["test"]
    # Pins uv to the local tree's own environment so a leaked absolute UV_PROJECT_ENVIRONMENT
    # can't redirect it to a sibling worktree's .venv.
    assert "UV_PROJECT_ENVIRONMENT=.venv" in script
