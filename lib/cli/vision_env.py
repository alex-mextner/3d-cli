"""vision_env.py — run a `threed_vision` stage (packages/threed-vision) from a `3d` command.

The algorithms live in the `threed_vision` package (single source of truth, also imported by the
hsw-editor vision service); a `3d <stage>` command is only a thin launcher around
`python -m threed_vision <stage> ...`. Interpreter resolution (first that exists):
  1. $THREED_VISION_PYTHON                      — any python with the package's deps
  2. ~/.cache/3d-cli/vision/venv/bin/python     — the vision venv (`uv venv` + `uv pip install -e 'packages/threed-vision[gpu,seg,fit]'`)
  3. (light stages only) `uv run --with numpy,opencv-python-headless,pillow,pillow-heif,scipy`
Heavy stages (torch / onnxruntime / sam2) never fall back to ad-hoc uv: they need the venv and say how to create it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from cli.env import repo_root
from errors import MissingDependency

LIGHT_DEPS = ("numpy", "opencv-python-headless", "pillow", "pillow-heif", "scipy")


def package_src() -> str:
    return os.path.join(repo_root(), "packages", "threed-vision", "src")


def vision_venv_python() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "3d-cli", "vision", "venv", "bin", "python")


def stage_argv(stage: str, args: list[str], *, heavy: bool) -> list[str]:
    """argv that runs one stage; raises MissingDependency when no suitable interpreter exists."""
    explicit = os.environ.get("THREED_VISION_PYTHON")
    venv_py = vision_venv_python()
    if explicit:
        resolved = shutil.which(explicit)
        if resolved is None:
            raise MissingDependency(
                f"an executable python at THREED_VISION_PYTHON ({explicit})",
                install="point THREED_VISION_PYTHON at a python that has the package's deps, or unset it",
                degrades=f"`3d {stage}`",
                command=stage,
            )
        return [resolved, "-m", "threed_vision", stage, *args]
    if os.access(venv_py, os.X_OK):
        return [venv_py, "-m", "threed_vision", stage, *args]
    if not heavy and not os.environ.get("PY3D_NO_UV") and shutil.which("uv"):
        flags = [f for dep in LIGHT_DEPS for f in ("--with", dep)]
        return ["uv", "run", "--no-project", *flags, "python", "-m", "threed_vision", stage, *args]
    raise MissingDependency(
        "the threed-vision python environment",
        install=(
            f"uv venv {os.path.dirname(os.path.dirname(venv_py))} --python 3.12 && "
            f"uv pip install --python {venv_py} torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128"
            f" --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match && "
            f"uv pip install --python {venv_py} -e '{os.path.join(repo_root(), 'packages', 'threed-vision')}[gpu,seg,fit]'"
            "  (or set THREED_VISION_PYTHON to a python that has the package's deps)"
        ),
        degrades=f"`3d {stage}` (model-backed vision stage)",
        command=stage,
    )


def _stage_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = package_src() + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_stage(stage: str, args: list[str], *, heavy: bool = False) -> int:
    """Run `python -m threed_vision <stage> ...` with PYTHONPATH pointing at the package; returns its exit code."""
    argv = stage_argv(stage, args, heavy=heavy)
    try:
        return subprocess.run(argv, env=_stage_env()).returncode
    except FileNotFoundError as exc:  # interpreter/uv vanished between resolution and exec
        raise MissingDependency(
            f"the launcher {argv[0]}",
            install="recreate the vision venv (see packages/threed-vision/README.md) or reinstall uv",
            degrades=f"`3d {stage}`", command=stage,
        ) from exc


def stage_json(stage: str, args: list[str], *, timeout: float = 60) -> dict[str, Any] | None:
    """Run a heavy-tier stage that prints one JSON object and parse it; None when the venv, the stage or the JSON is unavailable."""
    try:
        argv = stage_argv(stage, args, heavy=True)
        r = subprocess.run(argv, env=_stage_env(), capture_output=True, text=True, timeout=timeout)
        data = json.loads(r.stdout)
    except (MissingDependency, OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, dict) else None
