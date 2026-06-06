"""3d report — deterministic summaries from existing gate/metric artifacts."""
from __future__ import annotations

import os

from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d report <artifact.log|metrics.jsonl|report.json> [...] [options]
  Compose existing gate/metric artifacts into deterministic text or JSON summaries.
  This command reads artifacts only; it does not run render, mesh, collision, or score tools.

Options:
  --format text|json    output format (default: text)
  --json                shortcut for --format json
  --title TITLE         report title (default: 3d report)
  -o, --out FILE        write summary to FILE instead of stdout

Examples:
  3d report check.log score.log
  3d report --json metrics.jsonl -o report.json"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    fmt = "text"
    title = "3d report"
    out = ""
    artifacts: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            return 0
        if arg == "--json":
            fmt = "json"
            i += 1
        elif arg == "--format":
            if i + 1 >= n:
                raise UsageError("option --format needs a value", command="report")
            fmt = argv[i + 1]
            i += 2
        elif arg == "--title":
            if i + 1 >= n:
                raise UsageError("option --title needs a value", command="report")
            title = argv[i + 1]
            i += 2
        elif arg in ("-o", "--out"):
            if i + 1 >= n:
                raise UsageError(f"option {arg} needs a value", command="report")
            out = argv[i + 1]
            i += 2
        elif arg.startswith("-"):
            raise UsageError(f"unknown option '{arg}'", command="report")
        else:
            artifacts.append(arg)
            i += 1

    if fmt not in {"text", "json"}:
        raise InvalidArgument("--format", fmt, ["text", "json"], command="report")
    if not artifacts:
        raise UsageError("no artifacts given", command="report")
    for path in artifacts:
        if not os.path.isfile(path):
            raise InputNotFound(path, command="report")

    import reporting

    report = reporting.build_report(artifacts, title=title)
    rendered = reporting.render_json(report) if fmt == "json" else reporting.render_text(report)
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(rendered)
        except OSError as exc:
            raise UsageError(f"cannot write output file {out}: {exc}", command="report") from exc
    else:
        print(rendered, end="")
    return 0


COMMAND = Command(
    name="report",
    group="QA & GATES",
    summary="deterministic text/json summaries from existing gate and metric artifacts",
    usage=USAGE,
    run=run,
)
