"""Shared helpers for readable real-CLI workflow e2e tests."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter
from pathlib import Path
from typing import TypedDict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
THREED = REPO_ROOT / "bin" / "3d"
CUBE = REPO_ROOT / "examples" / "cube.scad"
# The binaries the temporary printer agent spawns (lib/printer_ops/printer_agent.py). Every CLI
# run finds recording fakes of them first on PATH, so no test can ever start a real omp agent on
# the production tmux server (#399: one ran ~21 h as 3d-printer-agent-u1).
AGENT_SPAWN_BINARIES = ("tmux", "omp", "systemd-run")
_AGENT_FAKE = """#!{python}
import json, os, sys
name = os.path.basename(sys.argv[0])
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": [name, *sys.argv[1:]], "TMUX_TMPDIR": os.environ.get("TMUX_TMPDIR")}}) + "\\n")
args = sys.argv[1:]
if name == "systemd-run":  # run the wrapped command (the fake tmux) so it is recorded too
    while args and args[0].startswith("-"):
        args = args[1:]
    os.execvp(args[0], args)
sys.exit(1 if name == "tmux" and "has-session" in args else 0)
"""


def agent_fakebin(tmp_path: Path) -> Path:
    """The dir of recording fake tmux/omp/systemd-run; each call appends to agent_spawns()."""
    bindir = tmp_path / "agent-fakebin"
    if not bindir.is_dir():
        bindir.mkdir(parents=True)
        for name in AGENT_SPAWN_BINARIES:
            fake = bindir / name
            fake.write_text(_AGENT_FAKE.format(python=sys.executable, log=str(tmp_path / "agent-spawns.jsonl")),
                            encoding="utf-8")
            fake.chmod(0o755)
    return bindir


class AgentSpawn(TypedDict):
    argv: list[str]           # [binary name, *args]
    TMUX_TMPDIR: str | None   # where a real tmux would have put its socket


def agent_spawns(tmp_path: Path) -> list[AgentSpawn]:
    """Every tmux/omp/systemd-run invocation the CLI runs of `tmp_path` made, in order."""
    log = tmp_path / "agent-spawns.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _agent_fakes_first(env: dict[str, str], tmp_path: Path) -> None:
    bindir = str(agent_fakebin(tmp_path))
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != bindir]
    env["PATH"] = os.pathsep.join([bindir, *parts])


def isolated_env(tmp_path: Path) -> dict[str, str]:
    """Return an environment whose user config/data/cache/state cannot leak from the host, with
    the printer agent's spawn binaries faked and tmux sockets in tmp_path."""
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
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
            # any tmux server a test reaches (even a real one) lives here, never next to the user's
            "TMUX_TMPDIR": str(tmp_path / "tmux"),
        }
    )
    env.pop("TMUX", None)  # a test run inside tmux must not hand the CLI the user's server
    env.pop("THREED_TEST_REAL_PRINTER_AGENT", None)  # only a test that fakes tmux/omp opts in
    _agent_fakes_first(env, tmp_path)
    env.pop("PYTHONPATH", None)
    env.pop("HF_TOKEN", None)
    env.pop("OPENSCADPATH", None)
    env.pop("PRINTOPS_API", None)  # a host 3d-api config must not reach a test's CLI
    env.pop("PRINTOPS_TOKEN", None)
    # A git hook (the pre-commit gate) exports GIT_INDEX_FILE/GIT_DIR; left set, a test's
    # `git init/add/commit` in a temp repo would write into the host repo's index instead.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_PREFIX"):
        env.pop(var, None)
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
        _agent_fakes_first(env, tmp_path)  # an env_extra PATH (a test's own fakes) keeps the agent fakes first
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


def require_imagemagick() -> str:
    path = shutil.which("magick") or shutil.which("convert")
    if path is None:
        pytest.skip("ImageMagick is not installed")
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


def _python_imports(python: str, imports: list[str], *, cwd: Path, env: dict[str, str]) -> bool:
    if not imports:
        return True
    code = "import " + ", ".join(imports)
    probe = subprocess.run(
        [python, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return probe.returncode == 0


def require_cli_python_deps(tmp_path: Path, deps: list[str], imports: list[str]) -> None:
    """Skip unless the CLI can resolve Python deps through .venv, uv, or system python."""
    env = isolated_env(tmp_path)
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if (
        venv_python.exists()
        and os.access(venv_python, os.X_OK)
        and _python_imports(str(venv_python), imports, cwd=tmp_path, env=env)
    ):
        return

    uv = shutil.which("uv")
    if uv is not None:
        with_flags = [flag for dep in deps for flag in ("--with", dep)]
        code = "import " + ", ".join(imports)
        probe = subprocess.run(
            [uv, "run", *with_flags, "python3", "-c", code],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if probe.returncode == 0:
            return

    python3 = shutil.which("python3")
    if python3 is not None and _python_imports(python3, imports, cwd=tmp_path, env=env):
        return

    wanted = ", ".join(deps)
    pytest.skip(f"CLI Python runtime cannot import {wanted} via .venv, uv, or system python")


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


def png_colour_counts(path: Path) -> Counter[bytes]:
    """Pixel count per colour of an 8-bit, non-interlaced RGB/RGBA PNG (what OpenSCAD writes).

    Stdlib only: the gate has no Pillow. Used to tell a real render (model pixels on the
    background) from a blank frame — file size can't: a flat-shaded side view of a box
    compresses to well under 1 KB."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    width = height = channels = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and colour_type in (2, 6) and interlace == 0, "only 8-bit RGB/RGBA, non-interlaced"
            channels = 3 if colour_type == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels
    prev = bytearray(stride)
    counts: Counter[bytes] = Counter()
    for row in range(height):
        start = row * (stride + 1)
        kind_byte, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            if kind_byte == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind_byte == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind_byte == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind_byte == 4:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                line[i] = (line[i] + pred) & 0xFF
        counts.update(bytes(line[i:i + channels]) for i in range(0, stride, channels))
        prev = line
    return counts
