"""Pytest setup: put the repo lib/ on sys.path so command/cli modules import by name,
exactly as bin/3d arranges at runtime."""
from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("REPO_ROOT", _REPO)
_LIB = os.path.join(_REPO, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
