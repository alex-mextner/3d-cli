"""Unit tests for cli.env — environment + dependency helpers."""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
from typing import Any

import pytest

import cli.env as env
from errors import MissingDependency


# --- repo_root ---
def test_repo_root_uses_env(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "_REPO_ROOT", None)
    monkeypatch.setenv("REPO_ROOT", "/tmp/repo")
    assert env.repo_root() == "/tmp/repo"


# --- export_openscadpath ---
def test_export_openscadpath_skips_when_no_libs(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "_REPO_ROOT", None)
    monkeypatch.setenv("REPO_ROOT", "/tmp/repo")
    monkeypatch.delenv("OPENSCADPATH", raising=False)
    monkeypatch.setattr("os.path.isdir", lambda p: False)
    env.export_openscadpath()
    assert "OPENSCADPATH" not in os.environ


def test_export_openscadpath_prepends_once(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setattr(env, "_REPO_ROOT", None)
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    os.environ.pop("OPENSCADPATH", None)
    env.export_openscadpath()
    assert os.environ["OPENSCADPATH"].startswith(str(libs))
    env.export_openscadpath()
    parts = os.environ["OPENSCADPATH"].split(os.pathsep)
    assert parts.count(str(libs)) == 1


# --- find_openscad ---
def test_find_openscad_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSCAD", "openscad")
    monkeypatch.setattr(shutil, "which", lambda x: x)
    assert env.find_openscad() == "openscad"


def test_find_openscad_none(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSCAD", "")
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert env.find_openscad() is None


# --- require_openscad ---
def test_require_openscad_raises_when_missing(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "find_openscad", lambda: None)
    monkeypatch.setattr(env, "install_cmd", lambda t: "brew install openscad")
    with pytest.raises(MissingDependency):
        env.require_openscad("test")


def test_require_openscad_sets_env(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "find_openscad", lambda: "/opt/openscad")
    env.require_openscad()
    assert os.environ["OPENSCAD"] == "/opt/openscad"


# --- find_magick ---
def test_find_magick_prefers_magick(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/magick" if x == "magick" else None)
    assert env.find_magick() == "magick"


def test_find_magick_falls_back_to_convert(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/convert" if x == "convert" else None)
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert env.find_magick() == "convert"


def test_find_magick_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert env.find_magick() is None


# --- magick_compare ---
def test_magick_compare_im7() -> None:
    assert env.magick_compare("magick") == ["magick", "compare"]


def test_magick_compare_im6() -> None:
    assert env.magick_compare("convert") == ["compare"]


# --- require_magick ---
def test_require_magick_raises(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "find_magick", lambda: None)
    with pytest.raises(MissingDependency):
        env.require_magick()


def test_require_magick_ok(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "find_magick", lambda: "magick")
    assert env.require_magick() == "magick"


# --- find_slicer ---
def test_find_slicer_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("SLICER", "/app/orca")
    monkeypatch.setattr("os.access", lambda p, m: True)
    assert env.find_slicer() == ("custom", "/app/orca")


def test_find_slicer_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert env.find_slicer() is None


# --- detect_os ---
def test_detect_os_macos(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert env.detect_os() == "macos"


def test_detect_os_linux_apt(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/apt-get" if x == "apt-get" else None)
    assert env.detect_os() == "linux-apt"


def test_detect_os_linux_unknown(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda x: None)
    assert env.detect_os() == "linux-unknown"


def test_detect_os_other(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert env.detect_os() == "other"


# --- sudo_prefix ---
def test_sudo_prefix_root(monkeypatch: Any) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert env.sudo_prefix() == ""


def test_sudo_prefix_non_root(monkeypatch: Any) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/sudo" if x == "sudo" else None)
    assert env.sudo_prefix() == "sudo "


def test_sudo_prefix_no_sudo(monkeypatch: Any) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(shutil, "which", lambda x: None)
    assert env.sudo_prefix() == ""


# --- install_cmd ---
def test_install_cmd_known(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "detect_os", lambda: "macos")
    assert "brew install" in env.install_cmd("openscad")


def test_install_cmd_unknown_os(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "detect_os", lambda: "other")
    assert "no package map" in env.install_cmd("openscad")


# --- pypkg_for ---
def test_pypkg_for_mapping() -> None:
    assert env.pypkg_for("PIL") == "pillow"
    assert env.pypkg_for("cv2") == "opencv-python-headless"
    assert env.pypkg_for("trimesh") == "trimesh"


# --- resolve_python ---
def test_resolve_python_prefers_venv(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    venv_py.chmod(0o755)
    monkeypatch.setattr(env, "repo_root", lambda: str(tmp_path))
    assert env.resolve_python() == str(venv_py)


def test_resolve_python_fallback_to_python3(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "repo_root", lambda: "/no_venv")
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/python3" if x == "python3" else None)
    assert env.resolve_python() == "/usr/bin/python3"


def test_resolve_python_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "repo_root", lambda: "/no_venv")
    monkeypatch.setattr(shutil, "which", lambda x: None)
    assert env.resolve_python() is None


# --- py_has_module ---
def test_py_has_module_true(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "resolve_python", lambda: sys.executable)
    assert env.py_has_module("os") is True


def test_py_has_module_false(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "resolve_python", lambda: sys.executable)
    assert env.py_has_module("not_a_real_module_xyz") is False


def test_py_has_module_no_python(monkeypatch: Any) -> None:
    monkeypatch.setattr(env, "resolve_python", lambda: None)
    assert env.py_has_module("os") is False


# --- bootstrap_marker ---
def test_bootstrap_marker(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert env.bootstrap_marker() == str(tmp_path / "3d-cli" / ".bootstrapped")


# --- maybe_bootstrap ---
def test_maybe_bootstrap_fast_path(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    cfg = tmp_path / "3d-cli"
    cfg.mkdir()
    marker = cfg / ".bootstrapped"
    marker.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # should return immediately without side effects
    env.maybe_bootstrap()
    assert marker.is_file()


def test_maybe_bootstrap_no_git(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    cfg = tmp_path / "3d-cli"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda x: None)
    env.maybe_bootstrap()
    # marker is still absent because no git to clone
    assert not (cfg / ".bootstrapped").exists()


# --- find_slicer preference ---
def test_find_slicer_prefers_orca(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/orca-slicer" if x == "orca-slicer" else None)
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert env.find_slicer() == ("orca", "/usr/bin/orca-slicer")


def test_find_slicer_bambu_fallback(monkeypatch: Any) -> None:
    calls: list[str] = []
    def which_impl(x: str) -> str | None:
        calls.append(x)
        return "/usr/bin/bambu-studio" if x == "bambu-studio" else None
    monkeypatch.setattr(shutil, "which", which_impl)
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert env.find_slicer() == ("bambu", "/usr/bin/bambu-studio")


def test_find_slicer_bundle_path(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda x: None)
    monkeypatch.setattr("os.access", lambda p, m: p == "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
    assert env.find_slicer() == ("orca", "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
