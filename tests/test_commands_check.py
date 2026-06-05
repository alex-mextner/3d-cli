"""Unit tests for commands.check — the acceptance master gate."""
from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Any

import pytest

from commands.check import run, _lastmatch, _lastvalue, _read, _run_capture
from errors import GateFailure, InputNotFound, UsageError


def test_check_no_args() -> None:
    assert run([]) == 1


def test_check_help() -> None:
    assert run(["--help"]) == 0


def test_check_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad"])


def test_check_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["file.scad", "--bogus"])


def test_check_skip_needs_value() -> None:
    with pytest.raises(UsageError):
        run(["file.scad", "--skip"])


def test_check_manifold_pass(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "a.stl"
    stl.write_text("")

    def fake_run(args, **kw):
        cmd = " ".join(args)
        if "--render" in cmd:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "mesh_check.py" in cmd:
            return subprocess.CompletedProcess(args, 0, stdout=">>> MESH CHECK: PASS", stderr="")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 1)
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    assert run([str(scad), "--mesh"]) == 0


def test_check_manifold_fail_openscad_warning(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "a.stl"
    stl.write_text("")

    def fake_run(args, **kw):
        if "--render" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout="WARNING: something", stderr="")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 1)
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    with pytest.raises(GateFailure):
        run([str(scad), "--mesh"])


def test_check_consistency_no_asserts(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    assert run([str(scad), "--consistency"]) == 0


def test_check_consistency_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("assert(false);\n")

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="ERROR: Assertion failed", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    with pytest.raises(GateFailure):
        run([str(scad), "--consistency"])


def test_check_printability_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout=">>> PRINTABILITY: FAIL", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    with pytest.raises(GateFailure):
        run([str(scad), "--printability"])


def test_check_collision_skip_when_no_config(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    with pytest.raises(GateFailure):
        run([str(scad), "--collision", "nope.json", "--skip", "manifold", "--skip", "consistency", "--skip", "printability"])


def test_check_collision_config_not_found(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("os.path.isfile", lambda p: str(p).endswith(".scad") or str(p) == str(scad))
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    with pytest.raises(GateFailure):
        run([str(scad), "--collision", "nope.json"])


def test_check_silhouette_skip_no_ref(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    assert run([str(scad), "--silhouette", "--skip", "manifold", "--skip", "consistency", "--skip", "printability"]) == 0


def test_check_silhouette_skip_no_magick(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    monkeypatch.setattr("cli.env.find_magick", lambda: None)
    assert run([str(scad), "--silhouette", "--ref", "ref.jpg", "--skip", "manifold", "--skip", "consistency", "--skip", "printability"]) == 0


def test_check_silhouette_with_ref(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    ref = tmp_path / "ref.jpg"
    ref.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: str(p).endswith(".scad") or str(p) == str(ref))
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="IoU=0.8\nAE=12", stderr=""))
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    assert run([str(scad), "--silhouette", "--ref", str(ref), "--skip", "manifold", "--skip", "consistency", "--skip", "printability"]) == 0


def test_check_all_gates_pass(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);\n")
    stl = tmp_path / "a.stl"
    stl.write_text("")

    def fake_run(args, **kw):
        cmd = " ".join(args)
        if "--render" in cmd:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "mesh_check.py" in cmd:
            return subprocess.CompletedProcess(args, 0, stdout=">>> MESH CHECK: PASS", stderr="")
        if "printability" in cmd:
            return subprocess.CompletedProcess(args, 0, stdout=">>> PRINTABILITY: PASS", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 1)
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    assert run([str(scad)]) == 0


def test_check_skip_gate(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "a.stl"
    stl.write_text("")

    def fake_run(args, **kw):
        if "--render" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "mesh_check.py" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout=">>> MESH CHECK: PASS", stderr="")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 1)
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    assert run([str(scad), "--skip", "printability"]) == 0


def test_check_selected_gate_only(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "a.stl"
    stl.write_text("")

    def fake_run(args, **kw):
        if "--render" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "mesh_check.py" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout=">>> MESH CHECK: PASS", stderr="")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 1)
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    assert run([str(scad), "--mesh"]) == 0


def test_check_part_files(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "a.scad"
    scad.write_text("cube(1);")
    stl = tmp_path / "a.stl"
    stl.write_text("")

    def fake_run(args, **kw):
        if "--render" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "mesh_check.py" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout=">>> MESH CHECK: PASS", stderr="")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.path.getsize", lambda p: 1)
    monkeypatch.setattr("commands.check._threed", lambda: "bin/3d")
    assert run([str(scad), "--part", str(scad)]) == 0


# --- helpers ---
def test_lastmatch() -> None:
    assert _lastmatch("a\nb\nc", r"b") == "b"
    assert _lastmatch("a", r"z") == ""


def test_lastvalue() -> None:
    assert _lastvalue("x=1\nx=2", "x") == "2"
    assert _lastvalue("a", "z") == ""


def test_read_missing() -> None:
    assert _read("/no/such/file.txt") == ""


def test_run_capture() -> None:
    # just smoke that it builds a list and runs subprocess
    # we monkeypatch subprocess.run in other tests
    assert isinstance(_run_capture([sys.executable, "-c", "pass"]), str)
