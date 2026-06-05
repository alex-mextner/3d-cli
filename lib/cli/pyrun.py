"""pyrun.py — run a bundled python tool with its deps, degrading gracefully.

Python port of the `lib/pyrun` bash shim. Resolution order (first that works):
  1. <repo>/.venv/bin/python              — if the user bootstrapped a venv.
  2. uv run --with <dep>... python3        — uv resolves deps on the fly (no global installs).
  3. system python3                        — only if deps already importable.

`PY3D_NO_UV=1` forces skipping uv (venv or system only). DEPS may be empty (the caller
wants no extra deps, e.g. render's optional mesh stack).

`tool_argv()` returns the full argv to exec/run; `run_tool()` runs it and returns the
exit code. The tool path is resolved against the repo lib/ dir.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from cli.env import repo_root
from errors import MissingDependency


def tool_argv(deps: str, script: str, args: list[str]) -> list[str]:
    """Build the argv that runs lib/<script> with its deps via the resolved runtime.

    `deps` is a comma-separated list (may be empty). `script` is a filename under lib/
    (e.g. "render.py") or an absolute path. Raises MissingDependency if no runtime exists.
    """
    root = repo_root()
    script_path = script if os.path.isabs(script) else os.path.join(root, "lib", script)

    venv_py = os.path.join(root, ".venv", "bin", "python")
    if os.access(venv_py, os.X_OK):
        return [venv_py, script_path, *args]

    if not os.environ.get("PY3D_NO_UV") and shutil.which("uv"):
        with_flags: list[str] = []
        for d in (p.strip() for p in deps.split(",")):
            if d:
                with_flags += ["--with", d]
        return ["uv", "run", *with_flags, "python3", script_path, *args]

    py = shutil.which("python3")
    if py:
        # last resort: deps must already be importable; the tool reports its own
        # missing-import errors (and where written, degrades internally).
        return [py, script_path, *args]

    raise MissingDependency(
        "a python runtime (.venv, uv, or python3)",
        install=(
            f"cd {root} && python3 -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt"
        ),
        degrades="all python-backed commands (mesh / collision / printability / preprocess) cannot run",
    )


def run_tool(deps: str, script: str, args: list[str]) -> int:
    """Run a bundled tool and return its exit code."""
    argv = tool_argv(deps, script, args)
    return subprocess.run(argv).returncode


def exec_tool(deps: str, script: str, args: list[str]) -> int:
    """Replace the current process with the tool (mirrors `exec pyrun ...`).

    On platforms without os.execvp this falls back to run_tool + sys.exit.
    """
    argv = tool_argv(deps, script, args)
    try:
        os.execvp(argv[0], argv)
    except OSError:
        return subprocess.run(argv).returncode
    sys.exit(0)  # unreachable; for type-checkers
