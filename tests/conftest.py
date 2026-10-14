"""Pytest setup: put the repo lib/ on sys.path so command/cli modules import by name,
exactly as bin/3d arranges at runtime, and keep the suite inside the machine budget
(cli.resources): native thread pools capped, `-n auto` (pytest-xdist) = max_workers()."""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("REPO_ROOT", _REPO)
# The repo's fleet/printers.yaml names the real printers on the LAN: no test may reach them by
# accident. Tests that want a fleet pass THREED_FLEET=<file> (printops.fleet; `3d` children inherit this).
os.environ["THREED_FLEET"] = ""
_LIB = os.path.join(_REPO, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from cli.resources import cap_threads, max_workers  # noqa: E402  (lib/ is on sys.path only from here)

# before any test imports numpy/scipy (their thread pools are sized at first import) and
# before any subprocess, so `3d` children started by the e2e tests inherit the caps too
cap_threads()


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers() -> int:
    """`pytest -n auto` would start one worker per core; on a shared machine use max_workers()."""
    return max_workers()
