"""3d events — record and inspect CLI/model workflow events."""
from __future__ import annotations

import json
from typing import Any, cast

import events
from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d events <subcommand> [options]
  Record and inspect append-only CLI/model workflow events.

Subcommands:
  record --type <name> [options]     append one event
  list [filters]                     show matching events as a table
  query [filters]                    print matching events as JSON Lines
  path                               print the events log path

Record options:
  --type <name>                      event type, e.g. cli.render or model.match
  --source <name>                    event source (default: cli)
  --subject <value>                  file, model, project, or workflow subject
  --status <value>                   pass, fail, start, stop, note, or any workflow label
  --message <text>                   human-readable detail
  --data key=value                   structured data field (repeatable)
  --ts <iso>                         explicit ISO-8601 timestamp

Filters:
  --type <name>                      match event type
  --source <name>                    match event source
  --subject <value>                  match subject exactly
  --status <value>                   match status exactly
  --since <iso>                      only events at/after this ISO-8601 timestamp
  --limit <n>                        maximum number of events (default: 20)

Examples:
  3d events record --type cli.render --subject examples/cube.scad --status pass
  3d events record --type model.match --source agent --message "round accepted" --data round=2 --ts 2026-06-05T12:00:00+00:00
  3d events list --type cli.render --source cli --status pass --since 2026-06-05T00:00:00+00:00 --limit 10
  3d events query --subject examples/cube.scad
  3d events path"""


def _print_usage() -> None:
    print(USAGE)


def _take_value(argv: list[str], index: int, flag: str) -> tuple[str, int]:
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise UsageError(
            f"option {flag} needs a value",
            command="events",
            remediation=["Run `3d events --help` for examples."],
        )
    return argv[index + 1], index + 2


def _parse_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise InvalidArgument(
            "--limit",
            value,
            ["positive integer"],
            command="events",
        ) from exc
    if limit < 1:
        raise InvalidArgument(
            "--limit",
            value,
            ["positive integer"],
            command="events",
        )
    return limit


def _parse_data(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise InvalidArgument(
            "--data",
            value,
            ["key=value"],
            command="events",
        )
    key, val = value.split("=", 1)
    key = key.strip()
    if not key:
        raise InvalidArgument(
            "--data",
            value,
            ["key=value with a non-empty key"],
            command="events",
        )
    return key, val


def _parse_record(argv: list[str]) -> dict[str, object]:
    opts: dict[str, object] = {"source": "cli", "data": {}}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help", "help"):
            opts["help"] = True
            i += 1
        elif arg == "--type":
            opts["type"], i = _take_value(argv, i, arg)
        elif arg == "--source":
            opts["source"], i = _take_value(argv, i, arg)
        elif arg == "--subject":
            opts["subject"], i = _take_value(argv, i, arg)
        elif arg == "--status":
            opts["status"], i = _take_value(argv, i, arg)
        elif arg == "--message":
            opts["message"], i = _take_value(argv, i, arg)
        elif arg == "--ts":
            opts["ts"], i = _take_value(argv, i, arg)
        elif arg == "--data":
            raw, i = _take_value(argv, i, arg)
            key, val = _parse_data(raw)
            data = cast(dict[str, str], opts["data"])
            data[key] = val
        else:
            raise UsageError(
                f"unknown option: {arg}",
                command="events",
                remediation=["Run `3d events --help` for available options."],
            )
    return opts


def _parse_filters(argv: list[str], *, default_limit: int | None) -> dict[str, object]:
    filters: dict[str, object] = {"limit": default_limit}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help", "help"):
            filters["help"] = True
            i += 1
        elif arg == "--type":
            filters["type"], i = _take_value(argv, i, arg)
        elif arg == "--source":
            filters["source"], i = _take_value(argv, i, arg)
        elif arg == "--subject":
            filters["subject"], i = _take_value(argv, i, arg)
        elif arg == "--status":
            filters["status"], i = _take_value(argv, i, arg)
        elif arg == "--since":
            filters["since"], i = _take_value(argv, i, arg)
        elif arg == "--limit":
            raw, i = _take_value(argv, i, arg)
            filters["limit"] = _parse_limit(raw)
        else:
            raise UsageError(
                f"unknown option: {arg}",
                command="events",
                remediation=["Run `3d events --help` for available filters."],
            )
    return filters


def _as_str(opts: dict[str, object], key: str) -> str | None:
    value = opts.get(key)
    return value if isinstance(value, str) else None


def _as_limit(opts: dict[str, object]) -> int | None:
    value = opts.get("limit")
    return value if isinstance(value, int) else None


def _as_data(opts: dict[str, object]) -> dict[str, str]:
    value = opts["data"]
    return cast(dict[str, str], value)


def _record(argv: list[str]) -> int:
    opts = _parse_record(argv)
    if opts.get("help"):
        _print_usage()
        return 0
    event_type = _as_str(opts, "type")
    if event_type is None:
        raise UsageError(
            "record needs --type",
            command="events",
            remediation=["Example:  3d events record --type cli.render --status pass"],
        )
    event = events.record_event(
        event_type,
        source=_as_str(opts, "source") or "cli",
        subject=_as_str(opts, "subject"),
        status=_as_str(opts, "status"),
        message=_as_str(opts, "message"),
        data=_as_data(opts),
        timestamp=_as_str(opts, "ts"),
    )
    print(f"Recorded event {event['id']}")
    return 0


def _query(opts: dict[str, object]) -> list[dict[str, Any]]:
    return events.query_events(
        event_type=_as_str(opts, "type"),
        source=_as_str(opts, "source"),
        subject=_as_str(opts, "subject"),
        status=_as_str(opts, "status"),
        since=_as_str(opts, "since"),
        limit=_as_limit(opts),
    )


def _list(argv: list[str]) -> int:
    opts = _parse_filters(argv, default_limit=20)
    if opts.get("help"):
        _print_usage()
        return 0
    rows = _query(opts)
    if not rows:
        print("No events found.")
        return 0
    print(f"{'TIME':<20}  {'TYPE':<18}  {'STATUS':<8}  {'SUBJECT':<24}  MESSAGE")
    for row in rows:
        ts = str(row["ts"])[:20]
        status = row["status"] or "-"
        subject = row["subject"] or "-"
        message = row["message"] or "-"
        print(f"{ts:<20}  {row['type']:<18}  {status:<8}  {subject:<24}  {message}")
    return 0


def _query_jsonl(argv: list[str]) -> int:
    opts = _parse_filters(argv, default_limit=None)
    if opts.get("help"):
        _print_usage()
        return 0
    for row in _query(opts):
        print(json.dumps(row, sort_keys=False, separators=(",", ":")))
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        _print_usage()
        return 0
    rest = argv[1:]
    if sub == "record":
        return _record(rest)
    if sub == "list":
        return _list(rest)
    if sub == "query":
        return _query_jsonl(rest)
    if sub == "path":
        print(events.events_path())
        return 0
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="events",
        remediation=["Run `3d events --help` for available subcommands."],
    )


COMMAND = Command(
    name="events",
    group="META",
    summary="record and inspect CLI/model workflow events",
    usage=USAGE,
    run=run,
)
