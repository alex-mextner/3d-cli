"""journal.py — every APPLIED doctor fix leaves a settings-change record.

WHAT: builds one `printops.settings_change/1` line (the format of lib/printops/history_buffer.py),
  appends it to the offline buffer `<data_dir>/printer-history/<printer>.jsonl`, and — when 3d-api
  is configured and reachable — imports it through `POST /v1/settings/import` (the route behind
  `3d settings import`, idempotent by uid).
WHY: a restart or a re-pointed active spool changes what a printer does; the journal answers
  "who changed it, when, from what to what" like every other setting change (plan §7.4).
INVARIANTS: stdlib only. Never raises: a journal that cannot be written or imported must not
  undo or fail the fix, so the outcome is a string for the report: `imported` (buffered and
  imported), `buffered` (written locally; 3d-api not configured / unreachable / answered garbage /
  refused the line),
  or `failed: <why>` (not even the local append worked). The line is validated with the same
  parser the importer uses before it is written, so a line the import would refuse is never kept.
"""

from __future__ import annotations

import datetime
import fcntl
import http.client
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from printer_doctor.model import JsonValue

if TYPE_CHECKING:
    from printops.client import ApiClient

ACTOR = "3d printers doctor"
KIND = "doctor_fix"
SOURCE = "doctor"


@dataclass(frozen=True)
class JournalEntry:
    printer: str
    check: str
    risk: str
    before: JsonValue
    after: JsonValue
    note: str = ""


def build_line(entry: JournalEntry, now: datetime.datetime) -> dict[str, JsonValue]:
    """The settings_change line of `entry`; `uid` is random, so a retry never collides."""
    at = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": "printops.settings_change/1",
        "uid": str(uuid.uuid4()),
        "printer": entry.printer,
        "at": at,
        "kind": KIND,
        "key": entry.check,
        "old": entry.before,
        "new": entry.after,
        "status": "applied",
        "source": SOURCE,
        "actor": ACTOR,
        "command": f"3d printers doctor {entry.printer} --fix",
        "evidence": [{"check": entry.check, "risk": entry.risk, "note": entry.note}],
    }


def _append(path: Path, line: str) -> None:
    """One whole line, O_APPEND under flock, then fsync (the buffer contract)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def record(
    entry: JournalEntry,
    api: ApiClient | None,
    now: datetime.datetime | None = None,
) -> str:
    """Write (and import) the journal line of `entry`; returns the outcome word (module doc)."""
    from printops import history_buffer as hb

    if not entry.printer or "/" in entry.printer or entry.printer.startswith("."):
        return f"failed: {entry.printer!r} is not a usable buffer file name"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    text = json.dumps(build_line(entry, now), ensure_ascii=False)
    try:
        hb.parse_lines([text])
        path = hb.buffer_dir() / f"{entry.printer}.jsonl"
        _append(path, text)
    except (OSError, hb.HistoryBufferError) as exc:
        return f"failed: {exc}"
    if api is None:
        return "buffered"
    from printops.client import ApiError

    try:
        report = api.post(
            "/v1/settings/import",
            {"file": str(path), "lines": [text], "spools": {}, "methods": {}},
        )
    except (ApiError, OSError, ValueError, http.client.HTTPException):
        return "buffered"  # unreachable, refused, or an answer that is not JSON: the line is safe locally
    counts = report.get("counts") if isinstance(report, dict) else None
    if isinstance(counts, dict) and not counts.get("refused"):
        return "imported"
    return "buffered"
