"""Shared helpers for readable real-CLI workflow e2e tests."""
from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
THREED = REPO_ROOT / "bin" / "3d"
CUBE = REPO_ROOT / "examples" / "cube.scad"


def isolated_env(tmp_path: Path) -> dict[str, str]:
    """Return an environment whose user config/data/cache cannot leak from the host."""
    config_home = tmp_path / "xdg-config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True, exist_ok=True)
    (app_config / ".bootstrapped").write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "CUBE": str(CUBE),
            "PYTHON": sys.executable,
            "PYTHONWARNINGS": "error",
            "REPO_ROOT": str(REPO_ROOT),
            "THREED": str(THREED),
            "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
    )
    env.pop("PYTHONPATH", None)
    return env


def run_cli(
    tmp_path: Path,
    *argv: str,
    cwd: Path | None = None,
    timeout: int = 120,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = isolated_env(tmp_path)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(THREED), *argv],
        cwd=str(cwd or tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_shell(script: str, tmp_path: Path, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not installed")
    assert path is not None
    return path


def require_working_binary(name: str, *version_args: str) -> str:
    path = require_binary(name)
    probe = subprocess.run(
        [path, *(version_args or ("--version",))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(f"{name} is installed but not runnable: {(probe.stderr or probe.stdout).strip()}")
    return path


def require_working_openscad() -> str:
    path = require_working_binary("openscad", "--version")
    with tempfile.TemporaryDirectory(prefix="3d-openscad-probe-") as tmp:
        root = Path(tmp)
        model = root / "probe.scad"
        stl = root / "probe.stl"
        png = root / "probe.png"
        model.write_text("cube([1, 1, 1]);\n", encoding="utf-8")
        stl_probe = subprocess.run(
            [path, "--render", "--export-format", "binstl", "-o", str(stl), str(model)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if stl_probe.returncode != 0 or not stl.exists() or stl.stat().st_size == 0:
            pytest.skip(
                f"openscad is installed but cannot export STL: {(stl_probe.stderr or stl_probe.stdout).strip()}"
            )
        png_probe = subprocess.run(
            [path, "--render", "-o", str(png), "--imgsize=32,32", str(model)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if png_probe.returncode != 0 or not png.exists() or png.stat().st_size == 0:
            pytest.skip(
                f"openscad is installed but cannot render PNG: {(png_probe.stderr or png_probe.stdout).strip()}"
            )
    return path


def require_python_module(name: str) -> None:
    if importlib.util.find_spec(name) is None:
        pytest.skip(f"python module {name!r} is not installed")


def write_pgm(path: Path, rows: list[str]) -> None:
    """Write a tiny ASCII PGM image from rows of 0/1 characters."""
    height = len(rows)
    width = len(rows[0]) if rows else 0
    pixels = ["255" if char == "1" else "0" for row in rows for char in row]
    path.write_text(
        f"P2\n{width} {height}\n255\n" + " ".join(pixels) + "\n",
        encoding="ascii",
    )


def key_value_lines(stdout: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
    return pairs


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height
