"""pyrun.py — run a bundled python tool with its deps, degrading gracefully.

Python port of the `lib/pyrun` bash shim. Resolution order (first that works):
  1. <repo>/.venv/bin/python              — if it imports the requested deps.
  2. uv run --with <dep>... python3        — uv resolves deps on the fly (no global installs).
  3. system python3                        — if it imports the requested deps.

`PY3D_NO_UV=1` forces skipping uv (venv or system only). DEPS may be empty (the caller
wants no extra deps, e.g. render's optional mesh stack).

`tool_argv()` returns the full argv to exec/run; `run_tool()` runs it and returns the
exit code. The tool path is resolved against the repo lib/ dir. DEPS entries are bare
distribution names, not version specifiers or extras.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache

from cli.env import repo_root
from errors import MissingDependency, UsageError

# Keep this table in sync when a new bare `deps` package imports under a non-obvious
# module name; Python package names and import names are not the same namespace.
_IMPORT_NAMES = {
    "beautifulsoup4": "bs4",
    "msgpack-python": "msgpack",
    "opencv-python": "cv2",
    "opencv-contrib-python": "cv2",
    "opencv-python-headless": "cv2",
    "pillow": "PIL",
    "python-dotenv": "dotenv",
    "python-dateutil": "dateutil",
    "pyyaml": "yaml",
    "scikit-image": "skimage",
    "scikit-learn": "sklearn",
    "usd-core": "pxr",
}


def _dep_names(deps: str) -> list[str]:
    names = [d for d in (p.strip() for p in deps.split(",")) if d]
    invalid = [d for d in names if any(ch in d for ch in "<>=!~[]; @#")]
    if invalid:
        raise UsageError(
            f"pyrun deps must be bare PyPI distribution names, got: {', '.join(invalid)}",
            command="pyrun",
            remediation=["Use comma-separated bare names such as 'numpy,pillow', not version specs or extras."],
        )
    return names


def _import_name(dep: str) -> str:
    normalized = dep.strip().lower().replace("_", "-")
    return _IMPORT_NAMES.get(normalized, normalized.replace("-", "_"))


@lru_cache(maxsize=128)
def _venv_has_deps(venv_py: str, deps: str) -> bool:
    names = [_import_name(dep) for dep in _dep_names(deps)]
    if not names:
        return True
    code = (
        "import importlib, json, sys; "
        "names=json.loads(sys.argv[1]); "
        "missing=[]; "
        "\nfor n in names:\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "    except Exception:\n"
        "        missing.append(n)\n"
        "sys.exit(1 if missing else 0)"
    )
    try:
        probe = subprocess.run(
            [venv_py, "-c", code, json.dumps(names)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def tool_argv(deps: str, script: str, args: list[str]) -> list[str]:
    """Build the argv that runs lib/<script> with its deps via the resolved runtime.

    `deps` is a comma-separated list (may be empty). `script` is a filename under lib/
    (e.g. "render.py") or an absolute path. Raises MissingDependency if no runtime exists.
    """
    root = repo_root()
    script_path = script if os.path.isabs(script) else os.path.join(root, "lib", script)

    venv_py = os.path.join(root, ".venv", "bin", "python")
    if os.access(venv_py, os.X_OK) and _venv_has_deps(venv_py, deps):
        return [venv_py, script_path, *args]

    if not os.environ.get("PY3D_NO_UV") and shutil.which("uv"):
        with_flags: list[str] = []
        for d in _dep_names(deps):
            with_flags += ["--with", d]
        return ["uv", "run", *with_flags, "python3", script_path, *args]

    py = shutil.which("python3")
    if py and _venv_has_deps(py, deps):
        return [py, script_path, *args]

    raise MissingDependency(
        "a python runtime (.venv, uv, or python3)",
        install=(
            f"cd {root} && uv sync --all-extras"
            "  (creates .venv from pyproject.toml + uv.lock; or install uv from https://docs.astral.sh/uv/)"
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
