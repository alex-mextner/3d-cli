"""Unit tests for commands.doctor — read-only health report."""
from __future__ import annotations

import shutil
import sys
from typing import Any


from commands.doctor import run


def test_doctor_help() -> None:
    assert run(["--help"]) == 0


def test_doctor_runs(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("cli.env.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("cli.env.py_has_module", lambda m: True)
    monkeypatch.setattr(shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "uv", "pip3", "pip") else None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    rc = run([])
    assert rc in (0, 1)


def test_doctor_missing_openscad(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: None)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: None)
    monkeypatch.setattr("cli.env.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("cli.env.py_has_module", lambda m: True)
    monkeypatch.setattr(shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3") else None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    rc = run([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING" in captured.out or "openscad" in captured.out


def test_doctor_missing_uv_warns(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: None)
    monkeypatch.setattr("cli.env.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("cli.env.py_has_module", lambda m: True)
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    monkeypatch.setenv("PY3D_NO_UV", "")
    rc = run([])
    assert rc in (0, 1)
    captured = capsys.readouterr()
    assert "uv" in captured.out or "WARN" in captured.out


def test_doctor_pyvista_warn(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: None)
    monkeypatch.setattr("cli.env.resolve_python", lambda: sys.executable)
    def has_mod(m):
        return m != "pyvista"
    monkeypatch.setattr("cli.env.py_has_module", has_mod)
    monkeypatch.setattr(shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3") else None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    rc = run([])
    assert rc in (0, 1)


def test_doctor_no_python(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: None)
    monkeypatch.setattr("cli.env.resolve_python", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    rc = run([])
    assert rc == 1


def test_doctor_no_slicer(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: None)
    monkeypatch.setattr("cli.env.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("cli.env.py_has_module", lambda m: True)
    monkeypatch.setattr(shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3") else None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    rc = run([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "slicer" in captured.out


def test_doctor_missing_libs(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("cli.env.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("cli.env.py_has_module", lambda m: True)
    monkeypatch.setattr(shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3") else None)
    monkeypatch.setattr("os.path.isdir", lambda p: False)
    rc = run([])
    assert rc in (0, 1)
    captured = capsys.readouterr()
    assert "BOSL2" in captured.out or "libs" in captured.out
