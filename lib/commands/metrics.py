"""3d metrics — inspect the longitudinal metrics JSONL store (ROADMAP §13.4)."""
from __future__ import annotations

import json

from cli.registry import Command
from errors import UsageError

USAGE = """3d metrics <subcommand>
  Inspect the longitudinal metrics store under ~/.local/share/3d-cli/metrics/
  (or $XDG_DATA_HOME/3d-cli/metrics/).

Subcommands:
  list                         show command JSONL files, record counts, and latest timestamp
  show [--limit N]             print metrics records as deterministic JSON lines
       [--command NAME]        restrict records to one command JSONL file

Examples:
  3d metrics list
  3d metrics show --limit 20
  3d metrics show --command render --limit 5"""


def _print_usage() -> None:
    print(USAGE)


def _list() -> int:
    from metrics import list_metric_files  # lazy: command discovery stays import-light

    items = list_metric_files()
    if not items:
        print("No metrics recorded yet.")
        return 0
    command_w = max(max(len(i["command"]) for i in items), len("COMMAND"))
    count_w = max(max(len(str(i["records"])) for i in items), len("RECORDS"))
    latest_w = max(max(len(i["latest"]) for i in items), len("LATEST"))
    print(f"{'COMMAND':<{command_w}}  {'RECORDS':>{count_w}}  {'LATEST':<{latest_w}}  PATH")
    for item in items:
        print(
            f"{item['command']:<{command_w}}  "
            f"{item['records']:>{count_w}}  "
            f"{item['latest']:<{latest_w}}  "
            f"{item['path']}"
        )
    return 0


def _parse_show_args(argv: list[str]) -> tuple[int | None, str | None]:
    limit: int | None = None
    command: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--limit":
            if i + 1 >= len(argv):
                raise UsageError(
                    "--limit needs a value",
                    command="metrics",
                    remediation=["Example:  3d metrics show --limit 20"],
                )
            raw = argv[i + 1]
            try:
                limit = int(raw)
            except ValueError as exc:
                raise UsageError(
                    f"invalid --limit value: {raw!r}",
                    command="metrics",
                    remediation=["Use a non-negative integer, e.g. `3d metrics show --limit 20`."],
                ) from exc
            if limit < 0:
                raise UsageError(
                    f"invalid --limit value: {raw!r}",
                    command="metrics",
                    remediation=["Use a non-negative integer, e.g. `3d metrics show --limit 20`."],
                )
            i += 2
            continue
        if arg == "--command":
            if i + 1 >= len(argv):
                raise UsageError(
                    "--command needs a name",
                    command="metrics",
                    remediation=["Example:  3d metrics show --command render"],
                )
            command = argv[i + 1]
            i += 2
            continue
        raise UsageError(
            f"unknown option '{arg}'",
            command="metrics",
            remediation=["Run `3d metrics --help` for accepted options."],
        )
    return limit, command


def _show(argv: list[str]) -> int:
    from metrics import read_records  # lazy: command discovery stays import-light

    limit, command = _parse_show_args(argv)
    for record in read_records(command=command, limit=limit):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        _print_usage()
        return 0
    if sub == "list":
        if len(argv) > 1:
            raise UsageError(
                "`list` does not accept options",
                command="metrics",
                remediation=["Use `3d metrics list`."],
            )
        return _list()
    if sub == "show":
        return _show(argv[1:])
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="metrics",
        remediation=["Use `list` or `show` — see `3d metrics --help`."],
    )


COMMAND = Command(
    name="metrics",
    group="ENVIRONMENT",
    summary="inspect persisted metrics history: list / show",
    usage=USAGE,
    run=run,
)
