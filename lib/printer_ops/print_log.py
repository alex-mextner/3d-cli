"""print_log.py — 3d-cli's own log of the prints it started: one JSON line per acknowledged `3d printer start`.

WHY (issue https://github.com/alex-mextner/3d-cli/issues/762): the glue reminder counts prints since the glue
  from the printer's job history (Moonraker); when the printer cannot be asked, this log is the fallback count.
  It only knows the prints 3d started (not Orca's or the touchscreen's), so a count from it is a lower bound.

FILE: ~/.local/state/3d-cli/prints/NAME.jsonl (XDG_STATE_HOME honoured), append-only:
  {"at": <unix seconds>, "remote": "<stored file name>"}

INVARIANTS: stdlib only; appending never fails a start (the caller ignores OSError); unreadable lines are skipped.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def log_path(printer: str) -> Path:
    from cli.paths import state_dir

    return state_dir() / "prints" / f"{printer}.jsonl"


def record_start(printer: str, remote: str, *, at: float | None = None) -> None:
    """Append the acknowledged start of `remote` on `printer`."""
    path = log_path(printer)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"at": time.time() if at is None else at, "remote": remote}, ensure_ascii=False
    )
    with path.open("ab+") as fh:
        fh.seek(0, 2)
        if fh.tell() > 0:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                line = (
                    "\n" + line
                )  # an earlier failed write left a partial line: end it first
        fh.write((line + "\n").encode("utf-8"))


def starts_since(printer: str, since: float) -> int:
    """How many starts the log holds for `printer` at or after `since` (unix seconds); 0 without a log."""
    try:
        lines = log_path(printer).read_bytes().splitlines()
    except FileNotFoundError:
        return 0
    # Decoding per line (issue #768): one corrupted line must not discard the starts around it.
    return sum(1 for line in lines if (at := _at(line)) is not None and at >= since)


def _at(line: bytes) -> float | None:
    import math

    try:
        value = json.loads(line.decode("utf-8")).get("at")
        at = (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )
    except (ValueError, AttributeError, OverflowError):  # UnicodeDecodeError is a ValueError
        return None
    return at if at is not None and math.isfinite(at) else None
