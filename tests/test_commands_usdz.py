"""Unit tests for commands.usdz — USDZ export."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest
from commands.usdz import run, _parse_color
from errors import InvalidArgument, InputNotFound, UsageError


def test_usdz_no_args() -> None:
    assert run([]) == 1


def test_usdz_help() -> None:
    assert run(["--help"]) == 0


def test_usdz_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad"])


def test_usdz_bad_ext(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        run(["model.bogus"])


def test_usdz_bad_color(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        run(["model.scad", "--color", "red"])


def test_usdz_bad_color_range(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        run(["model.scad", "--color", "1.5,0,0"])


def test_usdz_scad(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.usdz._bin3d", lambda: "bin/3d")

    def fake_run(args, **kw):
        if "export" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("commands.usdz.run_tool", lambda d, s, a: 0)
    assert run([str(scad)]) == 0


def test_usdz_stl(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    stl = tmp_path / "model.stl"
    stl.write_text("fake")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.usdz.run_tool", lambda d, s, a: 0)
    assert run([str(stl)]) == 0


def test_usdz_export_fails(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.usdz._bin3d", lambda: "bin/3d")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="err", stderr=""))
    rc = run([str(scad)])
    assert rc == 1


def test_parse_color_ok() -> None:
    assert _parse_color("0.1,0.2,0.3") == (0.1, 0.2, 0.3)


def test_parse_color_bad_count() -> None:
    with pytest.raises(InvalidArgument):
        _parse_color("0.1,0.2")


def test_parse_color_bad_value() -> None:
    with pytest.raises(InvalidArgument):
        _parse_color("a,b,c")


def test_usdz_unknown_option(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(UsageError):
        run(["model.scad", "--bogus"])


def test_usdz_out_needs_value(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(UsageError):
        run(["model.scad", "-o"])
