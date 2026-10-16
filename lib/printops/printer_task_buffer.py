"""printer_task_buffer.py — the printer to-do JSONL buffer used while 3d-api is unreachable (plan §7.5; #281).

ACCESSED VIA: `3d printer todo` (printops.todo_cli) appends here when 3d-api cannot be reached and
reads it back for an offline `list`; `printops.printer_tasks.import_lines` (3d-api,
`POST /v1/tasks/import`) parses the same lines to replay them into the DB.

FORMAT: `~/.local/share/3d/printer-todo/<printer>.jsonl` (`$XDG_DATA_HOME/3d/printer-todo`), one
operation per line. Schema `printops.printer_task/1`: `uid` (UUIDv7, the import idempotency key),
`op` (open | update | done | dismiss | snooze), `task_uid` (= `uid` for `open`), `printer` and the
task fields of an open/update. Schema `printops.idle_notice/1`: one state of an idle period
(#283). A line that is not JSON or names another schema stops the parse with its line number —
a torn write is never skipped silently.

WRITES: one `write` of the whole line with O_APPEND under `flock` on the file, then `fsync`, so
two writers never interleave and a crash loses at most the line being written.

INVARIANTS: stdlib only; nothing here talks to 3d-api or the DB.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_SCHEMA = "printops.printer_task/1"
NOTICE_SCHEMA = "printops.idle_notice/1"
OPS = ("open", "update", "done", "dismiss", "snooze")
TYPES = ("calibration", "inspect", "maintenance", "repair", "setup", "other")
BLOCKS = ("none", "printer", "head", "slot")
WHEN = ("idle", "before_print", "any")
NOTICE_STATES = ("pending", "sent", "failed")
CLOSED = ("done", "dismissed")


class BufferError(Exception):
    """A buffer line that cannot be replayed; `line` is 1-based (0 = not about one line)."""

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.message = message
        self.line = line


def uuid7() -> str:
    """A UUIDv7 (RFC 9562): 48-bit Unix ms timestamp, version 7, variant 10, random rest."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")  # 80 random bits, 74 used
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a: 12 bits
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)  # rand_b: 62 bits
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def buffer_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "3d" / "printer-todo"


def buffer_path(printer: str) -> Path:
    if not printer or "/" in printer or printer.startswith("."):
        raise BufferError(f"printer {printer!r} is not a fleet printer name")
    return buffer_dir() / f"{printer}.jsonl"


@dataclass(frozen=True)
class Line:
    """One parsed buffer line: `n` is its 1-based number, `raw` the text as read."""

    n: int
    raw: str
    data: dict[str, Any]

    @property
    def schema(self) -> str:
        return str(self.data["schema"])


def _str(
    d: Mapping[str, Any], key: str, n: int, *, required: bool = True
) -> str | None:
    v = d.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise BufferError(f"{key}: must be a non-empty string, got {v!r}", n)
    return v.strip()


def _strings(d: Mapping[str, Any], key: str, n: int) -> list[str] | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise BufferError(f"{key}: must be a list of non-empty strings, got {v!r}", n)
    return [x.strip() for x in v]


def blocks_of(
    d: Mapping[str, Any], n: int
) -> tuple[str, str | None, str | None] | None:
    """`blocks` as (scope, head, slot): a {scope, head, slot} object, a scope string, or
    false/"none" (written by older tools for "blocks nothing"); None = not given."""
    raw = d.get("blocks")
    if raw is None:
        return None
    if raw is False:
        return ("none", None, None)
    if isinstance(raw, str):
        raw = {"scope": raw}
    if not isinstance(raw, dict) or raw.get("scope") not in BLOCKS:
        raise BufferError(
            f"blocks: must be {{scope: {'|'.join(BLOCKS)}, head?, slot?}}, got {raw!r}",
            n,
        )
    head = raw.get("head") or d.get("head")
    slot = raw.get("slot") or d.get("slot")
    scope = str(raw["scope"])
    if scope == "head" and not head:
        raise BufferError("blocks: scope head needs the head", n)
    if scope == "slot" and not slot:
        raise BufferError("blocks: scope slot needs the slot", n)
    return (scope, head, slot)


def _check_task(d: dict[str, Any], n: int) -> None:
    _str(d, "uid", n)
    _str(d, "task_uid", n)
    _str(d, "printer", n)
    op = d.get("op")
    if op not in OPS:
        raise BufferError(f"op: {op!r} is not one of {', '.join(OPS)}", n)
    if op == "open":
        if d["task_uid"] != d["uid"]:
            raise BufferError("op open: task_uid must equal uid", n)
        _str(d, "title", n)
        if d.get("type") not in TYPES:
            raise BufferError(
                f"type: {d.get('type')!r} is not one of {', '.join(TYPES)}", n
            )
    elif d.get("type") is not None and d["type"] not in TYPES:
        raise BufferError(f"type: {d['type']!r} is not one of {', '.join(TYPES)}", n)
    if d.get("when") is not None and d["when"] not in WHEN:
        raise BufferError(f"when: {d['when']!r} is not one of {', '.join(WHEN)}", n)
    dur = d.get("duration_min")
    if dur is not None and (
        isinstance(dur, bool) or not isinstance(dur, int) or dur <= 0
    ):
        raise BufferError(
            f"duration_min: must be a positive whole number of minutes, got {dur!r}", n
        )
    if d.get("needs_human") is not None and not isinstance(d["needs_human"], bool):
        raise BufferError(
            f"needs_human: must be true or false, got {d['needs_human']!r}", n
        )
    blocks_of(d, n)
    until = d.get("snoozed_until")
    if until is not None:
        try:
            datetime.datetime.fromisoformat(str(until).strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise BufferError(
                f"snoozed_until: {until!r} is not an ISO-8601 date/time, e.g. 2026-10-25",
                n,
            ) from e
    for key in ("depends_on", "refs", "calibration_kinds"):
        _strings(d, key, n)
    spools = d.get("spools")
    if spools is not None and (
        not isinstance(spools, list)
        or not all(
            isinstance(s, dict) and (s.get("name") or s.get("spool_id") is not None)
            for s in spools
        )
    ):
        raise BufferError(
            f"spools: must be a list of {{name, spool_id}}, got {spools!r}", n
        )
    for key in (
        "head",
        "slot",
        "component",
        "when_note",
        "why",
        "note",
        "reason",
        "source",
        "source_key",
        "snoozed_until",
        "actor",
        "at",
    ):
        v = d.get(key)
        if v is not None and not isinstance(v, str):
            raise BufferError(f"{key}: must be a string, got {v!r}", n)


def _check_notice(d: dict[str, Any], n: int) -> None:
    for key in ("uid", "printer", "idle_since", "claimed_at"):
        _str(d, key, n)
    if d.get("state") not in NOTICE_STATES:
        raise BufferError(
            f"state: {d.get('state')!r} is not one of {', '.join(NOTICE_STATES)}", n
        )
    if _strings(d, "task_uids", n) is None:
        raise BufferError("task_uids: required (list of task uids)", n)
    tg = d.get("tg_message_id")
    if tg is not None and (isinstance(tg, bool) or not isinstance(tg, int)):
        raise BufferError(f"tg_message_id: must be a number, got {tg!r}", n)


def parse_lines(lines: Iterable[str]) -> list[Line]:
    """Every non-blank line, validated; the first bad one raises BufferError with its number."""
    out: list[Line] = []
    for n, raw in enumerate(lines, start=1):
        text = raw.rstrip("\r\n")
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BufferError(
                f"not a JSON object ({e.msg}); a torn write? fix or remove the line", n
            ) from e
        if not isinstance(data, dict):
            raise BufferError("not a JSON object", n)
        schema = data.get("schema")
        if schema == TASK_SCHEMA:
            _check_task(data, n)
        elif schema == NOTICE_SCHEMA:
            _check_notice(data, n)
        else:
            raise BufferError(
                f"unknown schema {schema!r} (known: {TASK_SCHEMA}, {NOTICE_SCHEMA})", n
            )
        out.append(Line(n, text, data))
    return out


def read(path: Path) -> list[Line]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse_lines(text.splitlines())


def append(printer: str, record: Mapping[str, Any]) -> Path:
    """Append one record as a line: flock, a single O_APPEND write, fsync."""
    import fcntl

    path = buffer_path(printer)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        json.dumps(dict(record), ensure_ascii=False, separators=(", ", ": ")) + "\n"
    ).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(line)
            while view:  # a short write (ENOSPC, signal) must not leave a torn line behind silently
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return path


def replay(lines: Iterable[Line]) -> dict[str, dict[str, Any]]:
    """The tasks a buffer describes, by task uid, after its ops in order (the offline view).
    Notices are not tasks and are skipped; an op on an unknown task raises BufferError."""
    tasks: dict[str, dict[str, Any]] = {}
    for ln in lines:
        d = ln.data
        if ln.schema != TASK_SCHEMA:
            continue
        op, tu = d["op"], d["task_uid"]
        if op == "open":
            if tu in tasks:
                continue  # the same open twice (re-appended): the first one wins
            b = blocks_of(d, ln.n) or ("none", None, None)
            tasks[tu] = {
                **{
                    k: v
                    for k, v in d.items()
                    if k not in ("schema", "op", "task_uid", "blocks")
                },
                "uid": tu,
                "blocks": b[0],
                "status": "open",
                "snoozed_until": None,
                "closed_at": None,
                "created_at": d.get("recorded_at") or d.get("at"),
            }
            continue
        t = tasks.get(tu)
        if t is None:
            raise BufferError(f"op {op} for task {tu} that no earlier line opens", ln.n)
        if op == "update":
            for key in (
                "head",
                "slot",
                "component",
                "type",
                "title",
                "why",
                "when",
                "when_note",
                "duration_min",
                "needs_human",
                "calibration_kinds",
                "spools",
                "depends_on",
                "refs",
                "source_key",
            ):
                if d.get(key) is not None:
                    t[key] = d[key]
            blocks = blocks_of(d, ln.n)
            if blocks is not None:
                t["blocks"] = blocks[0]
        elif op in ("done", "dismiss"):
            t["status"] = "done" if op == "done" else "dismissed"
            t["closed_at"] = d.get("at") or d.get("recorded_at")
            t["note"] = d.get("note") or d.get("reason") or t.get("note")
        else:  # snooze
            t["status"] = "snoozed"
            t["snoozed_until"] = d.get("snoozed_until")
            t["note"] = d.get("note") or d.get("reason") or t.get("note")
    return tasks
