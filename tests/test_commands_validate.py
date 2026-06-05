"""Unit tests for commands.validate — fast parse-only syntax check."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from commands.validate import run
from errors import GateFailure, InputNotFound


def test_validate_no_args() -> None:
    assert run([]) == 1


def test_validate_help() -> None:
    assert run(["--help"]) == 0


def test_validate_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad"])


def test_validate_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert run([str(scad)]) == 0


def test_validate_with_error_in_stdout(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="ERROR: line", stderr=""))
    with pytest.raises(GateFailure):
        run([str(scad)])


def test_validate_openscad_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="bad", stderr="syntax error"))
    assert run([str(scad)]) == 1


def test_validate_echo_output(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    echo = tmp_path / "echo.echo"
    echo.write_text("hello\n")

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert run([str(scad)]) == 0
