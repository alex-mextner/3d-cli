"""Unit tests for commands.printability — FDM printability gate."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from commands.printability import run
from errors import UsageError


def test_printability_no_args() -> None:
    assert run([]) == 1


def test_printability_help() -> None:
    assert run(["--help"]) == 0


def test_printability_no_parts() -> None:
    with pytest.raises(UsageError):
        run(["-D", "x=1"])


def test_printability_missing_file(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "missing.stl"]) == 1


def test_printability_scad_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "model.stl"
    stl.write_text("fake")

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 0)
    assert run([str(scad)]) == 0


def test_printability_scad_export_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 0)
    assert run([str(scad)]) == 1


def test_printability_scad_geometry_warning(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "model.stl"
    stl.write_text("fake")

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="non-manifold", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 0)
    assert run([str(scad)]) == 0


def test_printability_stl_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    stl = tmp_path / "model.stl"
    stl.write_text("fake")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 0)
    assert run([str(stl)]) == 0


def test_printability_stl_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    stl = tmp_path / "model.stl"
    stl.write_text("fake")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 1)
    assert run([str(stl)]) == 1


def test_printability_d_option(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "model.stl"
    stl.write_text("fake")

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.printability.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "-D", "x=1"]) == 0


def test_printability_d_option_needs_value() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "-D"])
