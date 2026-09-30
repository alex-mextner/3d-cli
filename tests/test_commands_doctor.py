"""Unit tests for commands.doctor — read-only health report."""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from commands.doctor import run


@pytest.fixture
def healthy_host(monkeypatch: Any) -> None:
    """Pin every host probe `3d doctor` makes to a fully healthy machine.

    doctor binds cli.env's finders by name (`from cli.env import find_openscad, ...`),
    so patching `cli.env.find_openscad` never reaches it — the probes must be patched
    on `commands.doctor`. Tests override one probe on top of this baseline, so an
    assertion about a single missing dependency is decided by that override alone,
    not by whatever the real host (or its .venv) happens to lack.
    """
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.doctor.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: "/repo/.venv/bin/python")
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: True)
    monkeypatch.setattr("commands.doctor.repo_root", lambda: "/repo")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: f"/usr/bin/{x}" if x in ("python3", "uv", "pip3", "pip") else None,
    )
    monkeypatch.setattr("os.access", lambda p, mode: p == "/repo/.venv/bin/python")
    monkeypatch.setattr("os.path.isdir", lambda p: p == "/repo/libs/BOSL2")
    monkeypatch.setattr(
        "commands.doctor.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )
    monkeypatch.delenv("PY3D_NO_UV", raising=False)


def test_doctor_help() -> None:
    assert run(["--help"]) == 0


def test_doctor_passes_on_healthy_host(healthy_host: None, capsys: Any) -> None:
    rc = run([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "MISSING" not in captured.out
    assert "DOCTOR: PASS" in captured.out


def test_doctor_missing_openscad(healthy_host: None, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.doctor.find_openscad", lambda: None)
    rc = run([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING openscad" in captured.out
    assert "DOCTOR: 1 MISSING" in captured.out


def test_doctor_missing_uv_only_warns(healthy_host: None, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3", "pip") else None
    )
    rc = run([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARN    uv" in captured.out


def test_doctor_missing_pyvista_only_warns(
    healthy_host: None, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: m != "pyvista")
    rc = run([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARN    py:pyvista" in captured.out


def test_doctor_warns_missing_python_modules_when_uv_can_resolve(
    healthy_host: None, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: False)
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", ["PIL"])

    rc = run([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "WARN    py:PIL" in captured.out
    assert "MISSING py:PIL" not in captured.out


def test_doctor_reports_system_python_fallback_when_venv_is_incomplete(
    healthy_host: None, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: False)
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", ["PIL"])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: "/usr/bin/python3" if x in ("python3", "pip3", "pip") else None,
    )

    def run_import(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0 if argv[0] == "/usr/bin/python3" else 1)

    monkeypatch.setattr("commands.doctor.subprocess.run", run_import)

    rc = run([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "PASS    py:PIL" in captured.out
    assert "system fallback" in captured.out
    assert "MISSING py:PIL" not in captured.out


def test_doctor_treats_system_python_probe_errors_as_missing(
    healthy_host: None, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.py_has_module", lambda m: False)
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", ["PIL"])
    monkeypatch.setattr(
        shutil,
        "which",
        lambda x: "/usr/bin/python3" if x in ("python3", "pip3", "pip") else None,
    )

    def broken_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("probe failed")

    monkeypatch.setattr("commands.doctor.subprocess.run", broken_probe)

    rc = run([])

    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING py:PIL" in captured.out


def test_doctor_does_not_claim_uv_resolves_web_deps(
    healthy_host: None, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", [])

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
    healthy_host: None, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr("commands.doctor.PY_MESH_MODULES", [])
    monkeypatch.setattr(
        shutil, "which", lambda x: f"/usr/bin/{x}" if x in ("python3", "pip3", "pip") else None
    )

    rc = run([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "PASS    py:fastapi" in captured.out
    assert "PASS    py:uvicorn" in captured.out
    assert "dispatcher Python" in captured.out


def test_doctor_no_python(healthy_host: None, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.doctor.resolve_python", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.access", lambda p, mode: False)
    rc = run([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING python3" in captured.out
    assert "MISSING python mesh stack" in captured.out


def test_doctor_no_slicer(healthy_host: None, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("commands.doctor.find_slicer", lambda: None)
    rc = run([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "MISSING slicer" in captured.out
    assert "DOCTOR: 1 MISSING" in captured.out


def test_doctor_missing_libs_only_warns(healthy_host: None, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("os.path.isdir", lambda p: False)
    rc = run([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARN    libs/BOSL2" in captured.out
