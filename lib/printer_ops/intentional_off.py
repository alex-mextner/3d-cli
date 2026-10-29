"""intentional_off.py — the `NAME.intentional-off` marker: "this printer is switched off on purpose".

WHAT: a small JSON file `<state dir>/NAME.intentional-off` (`{"at": ISO time, "reason": text}`; any
  content, even an empty file, counts as set). It means exactly ONE thing: while the printer is
  unreachable, `3d printer watch` / `3d printer vision` stay quiet (no "cannot connect" /
  "connection lost" event, no tg line). That is all.

NOT A GATE (#640): the marker never decides whether a supervision unit runs. The units are always
  up and wait for the printer with bounded polling (<= 30 s); the moment the printer answers, the
  watcher supervises it, marker or not. The first successful answer clears the marker (`clear_marker`
  is called by the watcher), so "switched off once" never outlives the power cycle. Before #640 the
  owner's hand-made `intentional-off.conf` systemd drop-ins carried `ConditionPathExists=!<marker>`,
  which kept both units stopped while the marker existed — a printer switched on again and printed
  from its touchscreen was then unwatched. `3d printer supervise install` reports such drop-ins.

READERS: printer_ops/watch_run.py (silences the unreachable event, clears the marker on the first
  answer), vision_detect.py/vision_cycle.py (same), printer_ops/supervise.py (status/doctor show it).
WRITERS: `3d printer supervise quiet NAME [--reason TEXT]` (this module's `set_marker`); the owner by
  hand (`echo '{}' > ~/.local/state/3d-cli/k1se.intentional-off`).

INVARIANTS: stdlib only; a missing or unreadable marker is "not set"; `clear_marker` never raises.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUFFIX = ".intentional-off"
_NAME = re.compile(r"[A-Za-z0-9_.+-]+")


def marker_path(printer: str) -> Path:
    """`<state dir>/NAME.intentional-off`; a printer name that is not a plain file name is refused."""
    from cli.paths import state_dir

    if not _NAME.fullmatch(printer) or printer in (".", ".."):
        raise ValueError(f"not a printer name: {printer!r}")
    return state_dir() / f"{printer}{SUFFIX}"


def is_set(printer: str) -> bool:
    try:
        return marker_path(printer).exists()
    except (OSError, ValueError):
        return False


def read_marker(printer: str) -> dict[str, Any] | None:
    """The marker's content (`{}` for an empty or non-JSON file), None when it is not set."""
    try:
        text = marker_path(printer).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def set_marker(printer: str, reason: str = "", now: float | None = None) -> Path:
    """Write the marker atomically (temp file + rename); -> its path."""
    path = marker_path(printer)
    stamp = datetime.fromtimestamp(
        time.time() if now is None else now, timezone.utc
    ).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps({"at": stamp, "reason": reason}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def clear_marker(printer: str) -> bool:
    """Remove the marker; -> whether one was removed. Never raises (a read-only state dir is not a reason
    to stop supervising)."""
    try:
        marker_path(printer).unlink()
    except (OSError, ValueError):
        return False
    return True


__all__ = [
    "SUFFIX",
    "clear_marker",
    "is_set",
    "marker_path",
    "read_marker",
    "set_marker",
]
