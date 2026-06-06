"""Unit tests for commands.doctor — read-only health report."""
from __future__ import annotations

import shutil
import subprocess
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
    monkeypatch.setattr("os.path.isdir", lambda p: p == "/repo/libs/BOSL2")
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


def test_doctor_warns_missing_python_modules_when_uv_can_resolve(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.doctor.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: False)
    monkeypatch.setattr("commands.doctor.repo_root", lambda: "/repo")
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", ["PIL"])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: f"/usr/bin/{x}" if x in ("python3", "uv", "pip3", "pip") else None,
    )
    monkeypatch.setattr("os.access", lambda p, mode: p == "/repo/.venv/bin/python")
    monkeypatch.setattr("os.path.isdir", lambda p: True)

    rc = run([])

    assert rc in (0, 1)
    captured = capsys.readouterr()
    assert "WARN    py:PIL" in captured.out
    assert "MISSING py:PIL" not in captured.out


def test_doctor_reports_system_python_fallback_when_venv_is_incomplete(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.doctor.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: "/repo/.venv/bin/python")
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: False)
    monkeypatch.setattr("commands.doctor.repo_root", lambda: "/repo")
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", ["PIL"])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: "/usr/bin/python3" if x in ("python3", "pip3", "pip") else None,
    )
    monkeypatch.setattr("os.access", lambda p, mode: p == "/repo/.venv/bin/python")
    monkeypatch.setattr("os.path.isdir", lambda p: p == "/repo/libs/BOSL2")

    def run_import(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0 if argv[0] == "/usr/bin/python3" else 1)

    monkeypatch.setattr("commands.doctor.subprocess.run", run_import)

    rc = run([])

    assert rc in (0, 1)
    captured = capsys.readouterr()
    assert "PASS    py:PIL" in captured.out
    assert "system fallback" in captured.out
    assert "MISSING py:PIL" not in captured.out


def test_doctor_treats_system_python_probe_errors_as_missing(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.doctor.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: "/repo/.venv/bin/python")
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: False)
    monkeypatch.setattr("commands.doctor.repo_root", lambda: "/repo")
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", ["PIL"])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: "/usr/bin/python3" if x in ("python3", "pip3", "pip") else None,
    )
    monkeypatch.setattr("os.access", lambda p, mode: p == "/repo/.venv/bin/python")
    monkeypatch.setattr("os.path.isdir", lambda p: p == "/repo/libs/BOSL2")

    def broken_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("probe failed")

    monkeypatch.setattr("commands.doctor.subprocess.run", broken_probe)

    rc = run([])

    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING py:PIL" in captured.out


def test_doctor_does_not_claim_uv_resolves_web_deps(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.doctor.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: True)
    monkeypatch.setattr("commands.doctor.repo_root", lambda: "/repo")
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", [])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: f"/usr/bin/{x}" if x in ("python3", "uv", "pip3", "pip") else None,
    )
    monkeypatch.setattr("os.access", lambda p, mode: p == "/repo/.venv/bin/python")
    monkeypatch.setattr("os.path.isdir", lambda p: p == "/repo/libs/BOSL2")

    def missing_web_import(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr("commands.doctor.subprocess.run", missing_web_import)

    rc = run([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "WARN    py:fastapi" in captured.out
    assert "dispatcher Python" in captured.out
    assert "uv resolves 'fastapi' per-call for 3d web" not in captured.out
    assert "3d web system fallback" not in captured.out


def test_doctor_reports_web_deps_in_dispatcher_python(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.doctor.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: sys.executable)
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: True)
    monkeypatch.setattr("commands.doctor.repo_root", lambda: "/repo")
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", [])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3", "pip") else None,
    )
    monkeypatch.setattr("os.access", lambda p, mode: p == "/repo/.venv/bin/python")
    monkeypatch.setattr("os.path.isdir", lambda p: p == "/repo/libs/BOSL2")

    def present_web_import(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("commands.doctor.subprocess.run", present_web_import)

    rc = run([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "PASS    py:fastapi" in captured.out
    assert "PASS    py:uvicorn" in captured.out
    assert "dispatcher Python" in captured.out


def test_doctor_no_python(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_slicer", lambda: None)
    monkeypatch.setattr("cli.env.resolve_python", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.path.isdir", lambda p: p.endswith("libs/BOSL2"))
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
