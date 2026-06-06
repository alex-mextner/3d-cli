"""events.py — append-only JSONL event store for CLI and model workflow activity.

ACCESSED VIA: `3d events record|list|query|path` and future automation hooks that need
to leave durable breadcrumbs without pulling in the web tier or model dependencies.

INVARIANTS:
  - The store is JSON Lines at `cli.paths.data_dir()/events.jsonl`; paths are resolved at
    call time so tests can sandbox `XDG_DATA_HOME`.
  - Entries are append-only and shaped for tooling: `{id, ts, type, source, subject,
    status, message, data}`. `data` is always a JSON object.
  - Reads skip blank/corrupt lines rather than breaking the whole log, so a partial write
    does not make later `3d events list` unusable.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import uuid
from typing import Any, Mapping

from cli import paths
from errors import InvalidArgument, UsageError

EVENTS_FILENAME = "events.jsonl"


def events_path() -> pathlib.Path:
    """Absolute path to the append-only events JSONL store."""
    return paths.data_dir() / EVENTS_FILENAME


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str, *, label: str) -> datetime.datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidArgument(
            label,
            value,
            ["ISO-8601 timestamp, e.g. 2026-06-05T12:00:00+00:00"],
            command="events",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _require_text(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise InvalidArgument(
            field,
            value,
            [f"non-empty {field}"],
            command="events",
        )
    return cleaned


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def record_event(
    event_type: str,
    *,
    source: str = "cli",
    subject: str | None = None,
    status: str | None = None,
    message: str | None = None,
    data: Mapping[str, object] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Append one event and return the stored entry."""
    event_ts = timestamp or _now_iso()
    normalized_ts = _parse_ts(event_ts, label="timestamp").isoformat(timespec="seconds")
    event: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "ts": normalized_ts,
        "type": _require_text(event_type, field="event type"),
        "source": _require_text(source, field="source"),
        "subject": _clean_optional(subject),
        "status": _clean_optional(status),
        "message": _clean_optional(message),
        "data": dict(data or {}),
    }

    path = events_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        raise UsageError(
            f"could not write events log: {path}",
            command="events",
            remediation=["Check permissions for the 3d data directory."],
        ) from exc
    return event


def _normalize_event(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("id"), str):
        return None
    if not isinstance(raw.get("ts"), str):
        return None
    if not isinstance(raw.get("type"), str):
        return None
    try:
        _parse_ts(raw["ts"], label="event timestamp")
    except InvalidArgument:
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    return {
        "id": raw["id"],
        "ts": raw["ts"],
        "type": raw["type"],
        "source": raw.get("source") if isinstance(raw.get("source"), str) else "cli",
        "subject": raw.get("subject") if isinstance(raw.get("subject"), str) else None,
        "status": raw.get("status") if isinstance(raw.get("status"), str) else None,
        "message": raw.get("message") if isinstance(raw.get("message"), str) else None,
        "data": data,
    }


def _load_events() -> list[dict[str, Any]]:
    path = events_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UsageError(
            f"could not read events log: {path}",
            command="events",
            remediation=["Check permissions for the 3d data directory."],
        ) from exc

    result: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = _normalize_event(json.loads(line))
        except json.JSONDecodeError:
            continue
        if event is not None:
            result.append(event)
    return result


def _event_datetime(event: Mapping[str, Any]) -> datetime.datetime:
    return _parse_ts(event["ts"], label="event timestamp")


def query_events(
    *,
    event_type: str | None = None,
    source: str | None = None,
    subject: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return matching events newest-first."""
    if limit is not None and limit < 1:
        raise InvalidArgument(
            "--limit",
            str(limit),
            ["positive integer"],
            command="events",
        )
    since_dt = _parse_ts(since, label="--since") if since else None
    matches: list[dict[str, Any]] = []
    ordered = sorted(
        enumerate(_load_events()),
        key=lambda item: (_event_datetime(item[1]), item[0]),
        reverse=True,
    )
    for _index, event in ordered:
        if event_type and event["type"] != event_type:
            continue
        if source and event["source"] != source:
            continue
        if subject and event["subject"] != subject:
            continue
        if status and event["status"] != status:
            continue
        if since_dt is not None:
            event_dt = _event_datetime(event)
            if event_dt < since_dt:
                continue
        matches.append(event)
        if limit is not None and len(matches) >= limit:
            break
    return matches
