"""history_buffer.py — the offline settings-history buffer: where it lives and how its lines parse (#264).

ACCESSED VIA: `3d settings import` (commands/settings.py) finds the buffer files here;
`printops.settings_history.import_lines` (3d-api, `POST /v1/settings/import`) parses the lines.

FORMAT: `<data_dir>/printer-history/<printer>.jsonl` (`cli.paths.data_dir()`, i.e.
`~/.local/share/3d-cli/printer-history/`), one change per line, schema
`printops.settings_change/1` (plan §7.4): `uid` (the import idempotency key), `printer`, `at`,
`kind`, `key`, `old`, `new`, `status`, `source`, `actor`, binding fields, evidence. Writers append
one whole line with O_APPEND under `flock` on the file, then `fsync`.

PAST LOCATION: until #264 the buffer lived in `~/.local/share/3d/printer-history/` (the pre-§23
app dir name). `buffer_dir()` moves it once: when only the legacy dir has buffer files it is
renamed into place while holding a migration lock and `flock` on every legacy file (an append in
flight finishes first and lands in the moved file — same inode). When both dirs have files the
move is refused with the import commands for both: `3d settings import` is idempotent by uid, so
importing both and archiving the legacy dir loses nothing. Writers of the legacy path do not take
the migration lock, so a line written there after the move (or after a check that found nothing)
recreates the legacy dir; the next `import` reports it as the two-buffer case — never lost.

PARSING: a line that is not a JSON object of this schema with a `uid` stops the whole import with
its line number — a torn write is never skipped silently. Blank lines are ignored.

INVARIANTS: stdlib only; nothing here talks to 3d-api or the DB; buffer files are only read.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli import paths

SCHEMA = "printops.settings_change/1"
DIRNAME = "printer-history"


class HistoryBufferError(Exception):
    """A buffer that cannot be used; `line` is 1-based (0 = not about one line)."""

    def __init__(
        self, message: str, *, line: int = 0, remediation: list[str] | None = None
    ) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.message = message
        self.line = line
        self.remediation = remediation or []


@dataclass(frozen=True)
class Line:
    """One parsed buffer line: `n` is its 1-based number, `raw` the text as read."""

    n: int
    raw: str
    data: dict[str, Any]

    @property
    def uid(self) -> str:
        return str(self.data["uid"])


def legacy_buffer_dir() -> Path:
    """The pre-#264 `~/.local/share/3d/printer-history` (honouring XDG_DATA_HOME)."""
    return paths.legacy_data_dir() / DIRNAME


def _files(d: Path) -> list[Path]:
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def buffer_dir() -> Path:
    """`<data_dir>/printer-history`, after the one-time move of the legacy dir (module doc)."""
    new = paths.data_dir() / DIRNAME
    old = legacy_buffer_dir()
    if not _files(old):
        return new
    new.parent.mkdir(parents=True, exist_ok=True)
    with open(new.parent / f".{DIRNAME}.migrate.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        legacy = _files(
            old
        )  # re-read under the lock: another process may have moved it
        if not legacy:
            return new
        current = _files(new)
        if current:
            raise HistoryBufferError(
                f"two settings-history buffers: {old} (legacy) and {new}",
                remediation=[
                    "Import both (idempotent by uid), then archive the legacy dir:",
                    f"  3d settings import {' '.join(str(f) for f in legacy)}",
                    f"  3d settings import {' '.join(str(f) for f in current)}",
                    f"  mv {old} {old}.imported",
                ],
            )
        with contextlib.ExitStack() as held:
            for f in legacy:
                fh = held.enter_context(open(f, "rb"))
                fcntl.flock(fh, fcntl.LOCK_EX)  # wait for an append in flight
            try:
                old.rename(
                    new
                )  # replaces an empty `new` dir; the parents share one filesystem
            except OSError as e:
                raise HistoryBufferError(
                    f"cannot move the settings-history buffer {old} → {new}: {e.strerror}",
                    remediation=[f"Move it by hand: mv {old} {new}"],
                ) from e
    return new


def buffer_files() -> tuple[Path, list[Path]]:
    """The buffer dir (after the legacy move) and every `<printer>.jsonl` in it."""
    d = buffer_dir()
    return d, _files(d)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _int64(text: str) -> int:
    value = int(text)
    if not -(2**63) <= value < 2**63:  # SQLite INTEGER; a float() of it could overflow too
        raise ValueError(f"{text[:20]}… is out of the integer range")
    return value


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(
        value
    ):  # 1e999: valid JSON, but an infinity nothing can serialise back
        raise ValueError(f"{text} is out of the float range")
    return value


def parse_lines(raw_lines: Iterable[str]) -> list[Line]:
    """Parse buffer text lines; the first malformed one raises with its line number."""
    out: list[Line] = []
    for n, raw in enumerate(raw_lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            data = json.loads(text, parse_constant=_no_constant, parse_float=_finite, parse_int=_int64)
        except ValueError as e:  # JSONDecodeError, or NaN/Infinity/1e999/huge ints (unstorable)
            if not isinstance(e, json.JSONDecodeError):
                raise HistoryBufferError(str(e), line=n) from e
            raise HistoryBufferError(
                f"not JSON ({e.msg}): a torn write? fix or remove the line", line=n
            ) from e
        if not isinstance(data, dict):
            raise HistoryBufferError("not a JSON object", line=n)
        if data.get("schema") != SCHEMA:
            raise HistoryBufferError(
                f"schema must be {SCHEMA!r}, got {data.get('schema')!r}", line=n
            )
        uid = data.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            raise HistoryBufferError(
                "uid must be a non-empty string (the import idempotency key)", line=n
            )
        out.append(Line(n, text, data))
    return out
