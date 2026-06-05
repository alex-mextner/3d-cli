"""Unit tests for cli.pyrun — python tool runner."""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from cli import pyrun
from errors import MissingDependency


def test_tool_argv_venv(monkeypatch: Any, tmp_path: Any) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    argv = pyrun.tool_argv("", "script.py", ["a"])
    assert argv[0] == str(venv_py)
    assert any("script.py" in str(arg) for arg in argv)


def test_tool_argv_uv(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/uv" if x == "uv" else None)
    monkeypatch.setenv("PY3D_NO_UV", "")
    argv = pyrun.tool_argv("trimesh", "script.py", ["a"])
    assert argv[0] == "uv"
    assert "--with" in argv
    assert "trimesh" in argv


def test_tool_argv_system_python(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/python3" if x == "python3" else None)
    monkeypatch.setenv("PY3D_NO_UV", "1")
    argv = pyrun.tool_argv("", "script.py", ["a"])
    assert argv[0] == "/usr/bin/python3"


def test_tool_argv_raises_when_no_runtime(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setenv("PY3D_NO_UV", "1")
    with pytest.raises(MissingDependency):
        pyrun.tool_argv("", "script.py", ["a"])


def test_run_tool(monkeypatch: Any) -> None:
    monkeypatch.setattr(pyrun, "tool_argv", lambda d, s, a: [sys.executable, "-c", "pass"])
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda argv: subprocess.CompletedProcess(argv, 0))
    assert pyrun.run_tool("", "s.py", []) == 0


def test_exec_tool_oserror_falls_back(monkeypatch: Any) -> None:
    monkeypatch.setattr(pyrun, "tool_argv", lambda d, s, a: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(os, "execvp", lambda *a: (_ for _ in ()).throw(OSError("no exec")))
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda argv: subprocess.CompletedProcess(argv, 0))
    assert pyrun.exec_tool("", "s.py", []) == 0
