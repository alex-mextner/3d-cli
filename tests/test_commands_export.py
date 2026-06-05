"""Unit tests for commands.export — STL/3MF export with validation."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from commands.export import run
from errors import InputNotFound, InvalidArgument, UsageError


def test_export_no_args() -> None:
    assert run([]) == 1


def test_export_help() -> None:
    assert run(["--help"]) == 0


def test_export_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad"])


def test_export_bad_extension(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(InvalidArgument):
        run(["model.scad", "-o", "out.bogus"])


def test_export_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "--bogus"])


def test_export_stl_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"

    def fake_run(args, **kw):
        # create the output file
        if "-o" in args:
            idx = args.index("-o")
            p = args[idx + 1]
            pathlib.Path(p).write_text("fake stl")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: ">>> MESH CHECK: PASS")
    assert run([str(scad), "-o", str(out)]) == 0


def test_export_stl_warn_geometry(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake stl")
        return subprocess.CompletedProcess(args, 0, stdout="non-manifold", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: "PASS")
    assert run([str(scad), "-o", str(out)]) == 1


def test_export_3mf(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.3mf"

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    assert run([str(scad), "-o", str(out)]) == 0


def test_export_no_output_produced(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    # keep real isfile so it sees the missing output
    assert run([str(scad), "-o", str(out)]) == 1


def test_export_default_output(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: "PASS")
    assert run([str(scad)]) == 0


def test_export_ascii_stl(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: "PASS")
    assert run([str(scad), "-o", str(out), "--ascii"]) == 0


def test_export_mesh_degraded(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: "ModuleNotFoundError")
    assert run([str(scad), "-o", str(out)]) == 0


def test_export_mesh_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.stl"

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: ">>> MESH CHECK: FAIL")
    assert run([str(scad), "-o", str(out)]) == 1


def test_export_out_option_needs_value() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "-o"])


def test_export_d_option_needs_value() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "-D"])
