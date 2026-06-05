"""Unit tests for commands.om — object model inspection."""
from __future__ import annotations

import pathlib
from typing import Any

import pytest
from commands.om import run
from errors import InvalidArgument


def test_om_no_args() -> None:
    assert run([]) == 1


def test_om_help() -> None:
    assert run(["--help"]) == 0


def test_om_inspect(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("// @id cube1\ncube(1);\n")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert run([str(scad), "#cube1"]) == 0


def test_om_validate(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("// @class part\ncube(1);\n")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert run([str(scad), ".part"]) == 0


def test_om_unknown(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);\n")
    with pytest.raises(InvalidArgument):
        run([str(scad), "nope"])
