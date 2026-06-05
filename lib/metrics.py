from __future__ import annotations

import datetime as _dt
import json
import math
import pathlib
import re
from collections.abc import Mapping, Sequence
from typing import TypeAlias, TypedDict, cast

from cli import paths
from errors import InvalidArgument

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


class MetricRecord(TypedDict, total=False):
    timestamp: str
    command: str
    tool: str
    inputs: dict[str, JSONValue]
    metrics: dict[str, JSONValue]
    wall_time: float
    backend: str
    model: str
    tokens: int
    cost: float


class MetricFileSummary(TypedDict):
    command: str
    records: int
    latest: str
    path: str


_COMMAND_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def metrics_dir(data_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    """Return the metrics JSONL directory, without creating it."""
    root = pathlib.Path(data_dir) if data_dir is not None else paths.data_dir()
    return root / "metrics"


def append_record(
    *,
    command: str,
    inputs: Mapping[str, object],
    metrics: Mapping[str, object],
    tool: str | None = None,
    wall_time: float | int | None = None,
    backend: str | None = None,
    model: str | None = None,
    tokens: int | None = None,
    cost: float | int | None = None,
    timestamp: str | None = None,
    data_dir: str | pathlib.Path | None = None,
) -> MetricRecord:
    """Validate and append one timestamped metrics record to `<data_dir>/metrics/<command>.jsonl`."""
    clean_command = _validate_name("command", command)
    record: MetricRecord = {
        "timestamp": _validate_timestamp(timestamp) if timestamp is not None else _utc_timestamp(),
        "command": clean_command,
        "inputs": _validate_mapping("inputs", inputs),
        "metrics": _validate_mapping("metrics", metrics),
    }
    if tool is not None:
        record["tool"] = _validate_name("tool", tool)
    if wall_time is not None:
        record["wall_time"] = _validate_nonnegative_number("wall_time", wall_time)
    if backend is not None:
        record["backend"] = _validate_text("backend", backend)
    if model is not None:
        record["model"] = _validate_text("model", model)
    if tokens is not None:
        record["tokens"] = _validate_nonnegative_int("tokens", tokens)
    if cost is not None:
        record["cost"] = _validate_nonnegative_number("cost", cost)

    directory = metrics_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{clean_command}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return record


def read_records(
    *,
    command: str | None = None,
    limit: int | None = None,
    data_dir: str | pathlib.Path | None = None,
) -> list[MetricRecord]:
    """Read records sorted by timestamp, then command. `limit` returns the newest N records."""
    if limit is not None and limit < 0:
        raise InvalidArgument(
            "limit",
            str(limit),
            ["a non-negative integer"],
            command="metrics",
        )
    directory = metrics_dir(data_dir)
    if not directory.is_dir():
        return []

    files = [_record_path(command, directory)] if command is not None else sorted(directory.glob("*.jsonl"))
    records: list[MetricRecord] = []
    for path in files:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = _parse_json_line(line, path)
            records.append(_record_from_json(raw, path))

    records.sort(key=lambda r: (r.get("timestamp", ""), r.get("command", "")))
    if limit is not None:
        return records[-limit:] if limit else []
    return records


def list_metric_files(*, data_dir: str | pathlib.Path | None = None) -> list[MetricFileSummary]:
    """Summarize each command JSONL file in deterministic command-name order."""
    directory = metrics_dir(data_dir)
    if not directory.is_dir():
        return []

    summaries: list[MetricFileSummary] = []
    for path in sorted(directory.glob("*.jsonl")):
        count = 0
        latest = "-"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = _parse_json_line(line, path)
            record = _record_from_json(raw, path)
            count += 1
            ts = record["timestamp"]
            if ts > latest:
                latest = ts
        summaries.append(
            {
                "command": path.stem,
                "records": count,
                "latest": latest,
                "path": str(path),
            }
        )
    return summaries


def _record_path(command: str, directory: pathlib.Path) -> pathlib.Path:
    return directory / f"{_validate_name('command', command)}.jsonl"


def _parse_json_line(line: str, path: pathlib.Path) -> object:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise InvalidArgument(
            "record",
            f"{path.name}:{exc.lineno}:{exc.colno}",
            ["valid JSON object lines"],
            command="metrics",
            extra=f"Repair or remove the corrupt metrics line in {path}.",
        ) from exc


def _utc_timestamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("timestamp", repr(value), ["a non-empty ISO-8601 string"], command="metrics")
    return value


def _validate_name(flag: str, value: str) -> str:
    if not isinstance(value, str) or not value or not _COMMAND_RE.match(value):
        raise InvalidArgument(
            flag,
            str(value),
            ["letters, numbers, dot, underscore, or dash"],
            command="metrics",
            extra="Use a stable command/tool name such as render, check, openscad, or slicer.",
        )
    return value


def _validate_text(flag: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(flag, str(value), ["a non-empty string"], command="metrics")
    return value


def _validate_nonnegative_int(flag: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidArgument(flag, str(value), ["a non-negative integer"], command="metrics")
    return value


def _validate_nonnegative_number(flag: str, value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise InvalidArgument(flag, str(value), ["a non-negative finite number"], command="metrics")
    return float(value)


def _validate_mapping(flag: str, value: Mapping[str, object]) -> dict[str, JSONValue]:
    if not isinstance(value, Mapping):
        raise InvalidArgument(flag, type(value).__name__, ["a JSON object"], command="metrics")
    out: dict[str, JSONValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise InvalidArgument(flag, repr(key), ["string keys"], command="metrics")
        out[key] = _validate_json_value(flag, item)
    return out


def _validate_json_value(flag: str, value: object) -> JSONValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise InvalidArgument(flag, str(value), ["finite JSON values"], command="metrics")
    if isinstance(value, Mapping):
        return _validate_mapping(flag, value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_validate_json_value(flag, item) for item in value]
    raise InvalidArgument(flag, type(value).__name__, ["JSON scalar, array, or object"], command="metrics")


def _record_from_json(raw: object, path: pathlib.Path) -> MetricRecord:
    if not isinstance(raw, dict):
        raise InvalidArgument("record", path.name, ["JSON object records"], command="metrics")
    command = raw.get("command")
    inputs = raw.get("inputs")
    values = raw.get("metrics")
    timestamp = raw.get("timestamp")
    if not isinstance(command, str) or not isinstance(inputs, dict) or not isinstance(values, dict):
        raise InvalidArgument("record", path.name, ["timestamp, command, inputs, and metrics fields"], command="metrics")
    if not isinstance(timestamp, str):
        raise InvalidArgument("record", path.name, ["timestamp, command, inputs, and metrics fields"], command="metrics")
    return cast(MetricRecord, raw)
