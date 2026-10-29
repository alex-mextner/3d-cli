"""Fake executables for the model browser tests (#578): a program that records its argv and cwd, so a test proves
which slicer / browser / agent a key started and with what, without starting anything real."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

RECORDER = """#!{python}
import json, os, sys
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": [os.path.basename(sys.argv[0]), *sys.argv[1:]], "cwd": os.getcwd()}}) + "\\n")
sys.exit({code})
"""


def fake_program(directory: Path, name: str, *, code: int = 0) -> tuple[Path, Path]:
    """`(binary, log)`: an executable that appends its argv and cwd as one JSON line to `log`, then exits `code`."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / f"{name}.jsonl"
    binary = directory / name
    binary.write_text(
        RECORDER.format(python=sys.executable, log=str(log), code=code),
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, log


def calls(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
