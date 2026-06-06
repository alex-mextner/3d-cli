"""Unit tests for commands.export — STL/3MF export with validation."""
from __future__ import annotations

import pathlib
import subprocess
import sys
import types
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


def test_export_extensionless_output_needs_explicit_format(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    with pytest.raises(InvalidArgument):
        run(["model.scad", "-o", "out"])


def test_export_list_formats(capsys: Any) -> None:
    assert run(["--list-formats"]) == 0
    out = capsys.readouterr().out
    assert "3d export formats" in out
    assert "usdz" in out
    assert "planned" in out
    assert "--glb" in out


def test_export_plan_for_planned_format_does_not_require_openscad(
    monkeypatch: Any,
    tmp_path: pathlib.Path,
    capsys: Any,
) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr(
        "commands.export.require_openscad",
        lambda command: (_ for _ in ()).throw(AssertionError(command)),
    )

    assert run([str(scad), "--plan", "--format", "glb"]) == 0

    out = capsys.readouterr().out
    assert "3d export plan" in out
    assert "format: glb (planned)" in out
    assert "GLB export is planned" in out


def test_export_planned_format_without_plan_is_usage_error(tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")

    with pytest.raises(UsageError):
        run([str(scad), "--format", "glb"])


def test_export_conflicting_format_selectors(tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")

    with pytest.raises(UsageError):
        run([str(scad), "--stl", "--3mf"])


def test_export_format_output_extension_mismatch(tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")

    with pytest.raises(InvalidArgument):
        run([str(scad), "--format", "stl", "-o", str(tmp_path / "model.glb")])


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


def test_export_extensionless_off_passes_explicit_openscad_format(
    monkeypatch: Any,
    tmp_path: pathlib.Path,
) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model"
    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(list(args))
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake off")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)

    assert run([str(scad), "--format", "off", "-o", str(out)]) == 0
    assert calls[0][:3] == ["/usr/bin/openscad", "--export-format", "off"]


def test_export_usdz_uses_integrated_converter_without_recursive_command(
    monkeypatch: Any,
    tmp_path: pathlib.Path,
) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    out = tmp_path / "model.usdz"
    fake_usdz = types.ModuleType("commands.usdz")
    fake_usdz.run = lambda argv: (_ for _ in ()).throw(AssertionError(argv))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "commands.usdz", fake_usdz)

    def fake_run(args, **kw):
        if "-o" in args:
            idx = args.index("-o")
            pathlib.Path(args[idx + 1]).write_text("fake intermediate stl")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    calls: list[tuple[str, list[str]]] = []

    def fake_run_tool(deps: str, script: str, args: list[str]) -> int:
        assert deps == "trimesh,usd-core"
        calls.append((script, args))
        return 0

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 100)
    monkeypatch.setattr("commands.export._mesh_check_capture", lambda p: ">>> MESH CHECK: PASS")
    monkeypatch.setattr("commands.export.run_tool", fake_run_tool)

    assert run([str(scad), "--usdz", "-o", str(out), "--color", "0.3,0.55,0.85"]) == 0
    assert len(calls) == 1
    script, args = calls[0]
    assert script == "usdz.py"
    assert pathlib.Path(args[0]).name == "model.stl"
    assert args[1:] == [str(out), "0.3", "0.55", "0.85", "model"]


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
