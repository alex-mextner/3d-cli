"""Unit tests for commands.libs — OpenSCAD library helpers."""
from __future__ import annotations

from typing import Any

import pytest
from commands.libs import run
from errors import UsageError


def test_libs_no_args() -> None:
    assert run([]) == 1


def test_libs_help() -> None:
    assert run(["help"]) == 0


def test_libs_path(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("os.environ.get", lambda k, d=None: "/tmp/libs" if k == "OPENSCADPATH" else d)
    rc = run(["path"])
    assert rc == 0
    assert "OPENSCADPATH" in capsys.readouterr().out


def test_libs_list(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.libs.repo_root", lambda: str(tmp_path))
    libs = tmp_path / "libs"
    libs.mkdir()
    (libs / "BOSL2").mkdir()
    rc = run(["list"])
    assert rc == 0
    assert "BOSL2" in capsys.readouterr().out


def test_libs_list_empty(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.libs.repo_root", lambda: str(tmp_path))
    libs = tmp_path / "libs"
    libs.mkdir()
    rc = run(["list"])
    assert rc == 0


def test_libs_install_removed() -> None:
    with pytest.raises(UsageError):
        run(["install"])


def test_libs_unknown() -> None:
    with pytest.raises(UsageError):
        run(["nope"])
