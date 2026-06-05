"""The load-bearing contract test: every command module must import under a STDLIB-ONLY
interpreter (no trimesh/numpy/cv2 at module top level). Discovery imports them all on
every `3d` invocation, so a heavy top-level import would break offline `3d help`/`render`
and the per-call uv dep resolution.

We enforce it by importing each command module in a SUBPROCESS whose import path is
poisoned: any attempt to import a heavy dep raises ImportError even if it is installed.
"""
from __future__ import annotations

import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB = os.path.join(_REPO, "lib")
_COMMANDS_DIR = os.path.join(_LIB, "commands")

# deps that must never be imported at module top level of a command/cli module.
HEAVY = ["trimesh", "manifold3d", "numpy", "scipy", "cv2", "rtree", "pyvista", "PIL", "open3d", "torch"]


def _command_module_names() -> list[str]:
    names = []
    for fn in sorted(os.listdir(_COMMANDS_DIR)):
        if fn.endswith(".py") and not fn.startswith("_"):
            names.append("commands." + fn[:-3])
    return names


def test_command_modules_import_stdlib_only() -> None:
    names = _command_module_names()
    assert names, "no command modules found"
    blocker = (
        "import sys, importlib.abc, importlib.machinery\n"
        f"HEAVY = {HEAVY!r}\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        top = name.split('.')[0]\n"
        "        if top in HEAVY:\n"
        "            raise ImportError('blocked heavy dep at import time: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import importlib\n"
        "for n in " + repr(names) + ":\n"
        "    importlib.import_module(n)\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _LIB + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-c", blocker], capture_output=True, text=True, env=env)
    assert r.returncode == 0, (
        "a command module imports a heavy dep at module top level:\n"
        + r.stdout + r.stderr
    )
    assert "OK" in r.stdout


def test_cli_core_imports_stdlib_only() -> None:
    """The dispatcher + its support modules must also be stdlib-only."""
    mods = ["cli.dispatch", "cli.registry", "cli.env", "cli.pyrun", "cli.imaging", "errors"]
    blocker = (
        "import sys, importlib.abc\n"
        f"HEAVY = {HEAVY!r}\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name.split('.')[0] in HEAVY:\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import importlib\n"
        "for n in " + repr(mods) + ":\n"
        "    importlib.import_module(n)\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _LIB + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-c", blocker], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
