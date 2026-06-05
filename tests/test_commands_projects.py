"""Unit tests for commands.projects — project registry."""
from __future__ import annotations

from typing import Any

from commands.projects import run


def test_projects_no_args() -> None:
    assert run([]) == 1


def test_projects_help() -> None:
    assert run(["--help"]) == 0


def test_projects_list(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setattr("projects_registry.list_projects", lambda: [{"name": "p1", "path": str(tmp_path), "added": "2026-01-01T00:00:00"}])
    rc = run(["list"])
    assert rc == 0


def test_projects_add(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("projects_registry.add", lambda path: {"path": path, "had_yaml": True})
    rc = run(["add", "/tmp/proj"])
    assert rc == 0


def test_projects_remove(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("projects_registry.remove", lambda path: {"path": path})
    rc = run(["remove", "/tmp/proj"])
    assert rc == 0
