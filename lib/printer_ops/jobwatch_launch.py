"""What `3d printer start` does about watching the job it just started (issue #723).

`start --watch` launches `3d printer watch NAME --job --yes --gcode FILE` detached; without it
`start` prints the command, because an unwatched lost print runs on unseen. The watcher may
pause or CANCEL the job, so launching it is never implicit.

INVARIANTS: stdlib-only; nothing is launched unless the caller passes watch=True.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "bin" / "3d"
LONG_JOB_S = 30 * 60
_HEAD_TAIL = 64 * 1024
_ESTIMATE = re.compile(
    r"^;\s*estimated printing time \(normal mode\)\s*=\s*(.+?)\s*$", re.MULTILINE
)
_PART = re.compile(r"(\d+)\s*([dhms])")
_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def estimated_seconds(gcode: Path) -> int | None:
    """Slicer estimate from the G-code header/footer (first and last 64 KB), None when absent."""
    try:
        size = gcode.stat().st_size
        with gcode.open("rb") as fh:
            head = fh.read(_HEAD_TAIL)
            fh.seek(max(0, size - _HEAD_TAIL))
            tail = fh.read(_HEAD_TAIL)
    except OSError:
        return None
    text = (head + b"\n" + tail).decode("utf-8", errors="replace")
    found = _ESTIMATE.findall(text)
    if not found:
        return None
    parts = _PART.findall(found[-1])
    return sum(int(n) * _UNIT_S[u] for n, u in parts) if parts else None


def _human(seconds: int) -> str:
    h, rest = divmod(seconds, 3600)
    return f"{h}h {rest // 60:02d}m" if h else f"{rest // 60}m"


def watch_command(name: str, gcode: Path) -> list[str]:
    return [str(BIN), "printer", "watch", name, "--job", "--yes", "--gcode", str(gcode)]


def _launch(name: str, gcode: Path) -> None:
    from cli.paths import state_dir

    log = state_dir() / "jobwatch" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        proc = subprocess.Popen(
            watch_command(name, gcode),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"job watch started in the background: pid {proc.pid}, log {log}")
    print(
        f"  it may pause/cancel the job on a lost print; stop it with: kill {proc.pid}"
    )


def _not_watched(name: str) -> None:
    print(
        "NOT WATCHED: a lost print runs on unseen. Guard it (it may pause/cancel the job): "
        f"3d printer watch {name} --job --yes --gcode FILE"
    )


def after_start(name: str, gcode: Path, *, watch: bool) -> None:
    """Called once the printer acknowledged the start and its status was printed."""
    if watch:
        _launch(name, gcode)
        return
    _not_watched(name)
    seconds = estimated_seconds(gcode)
    if seconds is None:
        return
    note = f"  estimated print time {_human(seconds)}"
    if seconds >= LONG_JOB_S:
        note += ": jobs of 30 min or more should be watched"
    print(note)
