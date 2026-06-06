"""Unit tests for cli.pyrun — python tool runner."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from cli import pyrun
from errors import MissingDependency, UsageError


def test_tool_argv_venv(monkeypatch: Any, tmp_path: Any) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(pyrun, "_venv_has_deps", lambda py, deps: True)
    argv = pyrun.tool_argv("", "script.py", ["a"])
    assert argv[0] == str(venv_py)
    assert any("script.py" in str(arg) for arg in argv)


def test_tool_argv_prefers_venv_for_empty_deps_even_if_nonempty_deps_would_fail(
    monkeypatch: Any, tmp_path: Any
) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(pyrun, "_venv_has_deps", lambda py, deps: deps == "")

    argv = pyrun.tool_argv("", "render.py", [])

    assert argv[0] == str(venv_py)


def test_import_name_maps_distribution_names_to_modules() -> None:
    assert pyrun._import_name("pillow") == "PIL"
    assert pyrun._import_name("beautifulsoup4") == "bs4"
    assert pyrun._import_name("msgpack-python") == "msgpack"
    assert pyrun._import_name("Pillow") == "PIL"
    assert pyrun._import_name("PyYAML") == "yaml"
    assert pyrun._import_name("opencv-python") == "cv2"
    assert pyrun._import_name("opencv-contrib-python") == "cv2"
    assert pyrun._import_name("opencv-python-headless") == "cv2"
    assert pyrun._import_name("python-dotenv") == "dotenv"
    assert pyrun._import_name("python-dateutil") == "dateutil"
    assert pyrun._import_name("scikit-image") == "skimage"
    assert pyrun._import_name("scikit-learn") == "sklearn"
    assert pyrun._import_name("usd-core") == "pxr"
    assert pyrun._import_name("pytest-timeout") == "pytest_timeout"
    assert pyrun._import_name("requests") == "requests"


def test_dep_names_ignores_empty_entries() -> None:
    assert pyrun._dep_names("") == []
    assert pyrun._dep_names(" , , ") == []
    assert pyrun._dep_names("numpy, pillow,") == ["numpy", "pillow"]


def test_dep_names_rejects_version_specs_and_extras() -> None:
    with pytest.raises(UsageError):
        pyrun._dep_names("numpy>=1.20")
    with pytest.raises(UsageError):
        pyrun._dep_names("rembg[cpu]")
    with pytest.raises(UsageError):
        pyrun._dep_names("numpy pillow")
    with pytest.raises(UsageError):
        pyrun._dep_names("requests@https://example.invalid/pkg.whl")
    with pytest.raises(UsageError):
        pyrun._dep_names("requests#fragment")


def test_venv_has_deps_probes_importable_modules() -> None:
    assert pyrun._venv_has_deps(sys.executable, "") is True
    assert pyrun._venv_has_deps(sys.executable, "json") is True
    assert pyrun._venv_has_deps(sys.executable, "definitely-missing-3d-cli-module") is False


def test_venv_has_deps_probes_installed_test_runner_package() -> None:
    assert pyrun._venv_has_deps(sys.executable, "pytest") is True


def test_venv_has_deps_fails_when_any_requested_dep_is_missing() -> None:
    assert pyrun._venv_has_deps(sys.executable, "json,definitely-missing-3d-cli-module") is False


def test_venv_has_deps_probe_uses_real_imports(monkeypatch: Any) -> None:
    pyrun._venv_has_deps.cache_clear()
    seen: dict[str, str] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["code"] = argv[2]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert pyrun._venv_has_deps("/tmp/python", "json") is True
    assert "importlib.import_module" in seen["code"]


def test_venv_has_deps_treats_probe_errors_as_missing(monkeypatch: Any) -> None:
    pyrun._venv_has_deps.cache_clear()

    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    monkeypatch.setattr(subprocess, "run", timeout)

    assert pyrun._venv_has_deps(sys.executable, "json") is False


def test_venv_has_deps_treats_oserror_as_missing(monkeypatch: Any) -> None:
    pyrun._venv_has_deps.cache_clear()

    def oserror(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("not executable")

    monkeypatch.setattr(subprocess, "run", oserror)

    assert pyrun._venv_has_deps(sys.executable, "json") is False


def test_tool_argv_uv(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/uv" if x == "uv" else None)
    monkeypatch.setenv("PY3D_NO_UV", "")
    argv = pyrun.tool_argv("trimesh", "script.py", ["a"])
    assert argv[0] == "uv"
    assert "--with" in argv
    assert "trimesh" in argv


def test_tool_argv_uses_complete_venv_for_required_deps(monkeypatch: Any, tmp_path: Any) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(pyrun, "_venv_has_deps", lambda py, deps: deps == "numpy,pillow")

    argv = pyrun.tool_argv("numpy,pillow", "fit_camera.py", ["--help"])

    assert argv[0] == str(venv_py)
    assert argv[-1] == "--help"


def test_tool_argv_skips_incomplete_venv_for_required_deps(monkeypatch: Any, tmp_path: Any) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(pyrun, "_venv_has_deps", lambda py, deps: False)
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/uv" if x == "uv" else None)
    monkeypatch.delenv("PY3D_NO_UV", raising=False)

    argv = pyrun.tool_argv("numpy,pillow", "fit_camera.py", [])

    assert argv[:2] == ["uv", "run"]
    assert argv.count("--with") == 2
    assert "numpy" in argv
    assert "pillow" in argv


def test_tool_argv_system_python(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/python3" if x == "python3" else None)
    monkeypatch.setattr(pyrun, "_venv_has_deps", lambda py, deps: True)
    monkeypatch.setenv("PY3D_NO_UV", "1")
    argv = pyrun.tool_argv("", "script.py", ["a"])
    assert argv[0] == "/usr/bin/python3"


def test_tool_argv_uses_system_python_for_empty_deps_without_venv_or_uv(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/python3" if x == "python3" else None)
    monkeypatch.setenv("PY3D_NO_UV", "1")

    argv = pyrun.tool_argv("", "render.py", [])

    assert argv[0] == "/usr/bin/python3"


def test_tool_argv_uses_system_python_when_venv_incomplete_and_uv_disabled(
    monkeypatch: Any, tmp_path: Any
) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/python3" if x == "python3" else None)
    monkeypatch.setenv("PY3D_NO_UV", "1")

    def has_deps(py: str, deps: str) -> bool:
        return py == "/usr/bin/python3"

    monkeypatch.setattr(pyrun, "_venv_has_deps", has_deps)

    argv = pyrun.tool_argv("numpy", "script.py", ["a"])

    assert argv[0] == "/usr/bin/python3"


def test_tool_argv_rejects_system_python_missing_required_deps(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(pyrun, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/python3" if x == "python3" else None)
    monkeypatch.setattr(pyrun, "_venv_has_deps", lambda py, deps: False)
    monkeypatch.setenv("PY3D_NO_UV", "1")

    with pytest.raises(MissingDependency):
        pyrun.tool_argv("numpy", "script.py", ["a"])


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
