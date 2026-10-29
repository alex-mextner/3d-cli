"""Platform-specific paths in a standard Python virtual environment."""
from __future__ import annotations
import os
from pathlib import Path
import sys

def executable(project: str | os.PathLike[str], name: str, *, platform: str | None = None) -> Path:
    windows = (sys.platform if platform is None else platform) == "win32"
    return Path(project) / ".venv" / ("Scripts" if windows else "bin") / (name + ".exe" if windows else name)
