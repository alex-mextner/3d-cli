"""Unit tests for commands.init — project scaffolding."""
from __future__ import annotations

import pathlib
from typing import Any

import pytest
from commands.init import run
from errors import InvalidArgument


def test_init_no_args(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    assert run([]) == 0


def test_init_help() -> None:
    assert run(["--help"]) == 0


def test_init_project(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    assert run(["myproject"]) == 0


def test_init_project_exists(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    (tmp_path / "myproject").mkdir()
    assert run(["myproject"]) == 0


def test_init_scaffold(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    proj = tmp_path / "myproject"
    proj.mkdir()
    monkeypatch.setattr("os.getcwd", lambda: str(proj))
    assert run(["scaffold"]) == 0


def test_init_scaffold_already_exists(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / "model.scad").write_text("cube(1);\n")
    monkeypatch.setattr("os.getcwd", lambda: str(proj))
    assert run(["scaffold"]) == 0


def test_init_unknown_template() -> None:
    with pytest.raises(InvalidArgument):
        run(["myproject", "--template", "nope"])
