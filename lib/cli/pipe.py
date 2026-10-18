"""pipe.py — stdin/stdout plumbing for the commands that chain in a shell pipe.

WHAT: `3d 3mf pick … | 3d 3mf orient … | 3d slice - | 3d print-preflight - | 3d printer start
  NAME - --yes` passes a 3MF, then a G-code, from one command to the next. `-` names stdin;
  binary output goes to stdout only when stdout is not a terminal (or `-o -`), and every human
  line of a piping command goes to stderr so the stream stays clean.

WHY a stash: a G-code arriving on stdin has no name, yet print-preflight looks for
  NAME.slice-warnings.json and NAME.3mf next to NAME.gcode (the slicer's warnings and the
  project the G-code was sliced from). `stash_gcode` keeps a piped G-code in a directory named
  after its own SHA-256 under the cache dir, and `3d slice -` writes those two siblings into the
  same directory, so the next command that receives the same bytes finds them again — the
  stream stays a plain G-code (`> part.gcode` still works) and no gate loses its input.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

from errors import UsageError

STDIN = "-"
STASH_MAX_AGE_S = (
    7 * 24 * 3600
)  # stash dirs older than a week are removed on the next stash


def read_input(path: str, *, command: str, what: str) -> bytes:
    """The bytes of PATH, or of stdin for `-`. An empty stdin is a usage error: the command
    before this one in the pipe failed and passed nothing on."""
    if path != STDIN:
        from errors import InputNotFound

        if not os.path.isfile(path):
            raise InputNotFound(path, command=command)
        return Path(path).read_bytes()
    if sys.stdin.isatty():
        raise UsageError(
            f"`-` reads a {what} from stdin, but stdin is a terminal",
            command=command,
            remediation=[
                f"Pipe a {what} in, e.g. `3d 3mf pick proj.3mf 'A*' | 3d {command} -`, "
                "or pass a file path."
            ],
        )
    data = sys.stdin.buffer.read()
    if not data:
        raise UsageError(
            f"no {what} on stdin (the command before this one in the pipe passed nothing on)",
            command=command,
            remediation=[
                "Read the error of the previous command above; it stopped the chain."
            ],
        )
    return data


def stdout_is_stream(out: str | None) -> bool:
    """True when the result goes to stdout: `-o -`, or no -o and stdout is not a terminal."""
    return out == STDIN or (out is None and not sys.stdout.isatty())


def write_output(data: bytes, out: str | None, *, command: str, what: str) -> str:
    """Write DATA to OUT (a path), or to stdout per `stdout_is_stream`. Never writes binary to a
    terminal. Returns where it went, for the report line."""
    if out is not None and out != STDIN:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(data)
        return out
    if not stdout_is_stream(out):
        raise UsageError(
            f"refusing to write a binary {what} to the terminal",
            command=command,
            remediation=["Pass -o FILE, or pipe it into the next command (`| 3d …`)."],
        )
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return "stdout"


def stash_root() -> Path:
    from cli.paths import cache_dir

    return cache_dir() / "pipe"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stash_dir(gcode: bytes) -> Path:
    """The directory a G-code with these bytes lives in: <cache>/pipe/<sha256[:16]>/."""
    return stash_root() / sha256_hex(gcode)[:16]


def stash_gcode(gcode: bytes, stem: str = "pipe") -> Path:
    """The stashed path of a piped G-code. The same bytes find the same file (and its
    NAME.slice-warnings.json / NAME.3mf siblings written by `3d slice -`); new bytes are
    written under STEM.gcode."""
    d = stash_dir(gcode)
    for existing in sorted(d.glob("*.gcode")) if d.is_dir() else []:
        if existing.read_bytes() == gcode:
            return existing
    _prune()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_stem(stem)}.gcode"
    tmp = path.with_suffix(".gcode.tmp")
    tmp.write_bytes(gcode)
    tmp.replace(path)
    return path


def _safe_stem(stem: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_+." else "_" for c in stem).strip("._")
    return keep[:60] or "pipe"


def _prune() -> None:
    root = stash_root()
    if not root.is_dir():
        return
    cutoff = time.time() - STASH_MAX_AGE_S
    for d in root.iterdir():
        with contextlib.suppress(OSError):  # raced with another pipe removing it
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
