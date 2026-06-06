"""3d workspaces - create/list/read workspace metadata for the web dashboard."""
from __future__ import annotations

import json
import os

from cli.registry import Command
from errors import UsageError

USAGE = """3d workspaces <subcommand> [options]
  Create, list, and read named web workspaces. A workspace is a scan root plus an
  optional list of known project directories for the future `3d web` project picker.

Subcommands:
  list [--json]                         show workspace names, roots, and project counts
  create <name> [--root DIR] [--project DIR ...] [--json]
                                        create a workspace rooted at DIR (default: cwd)
  show <name> [--json]                  show one workspace and its project metadata

Examples:
  3d workspaces create shop --root ~/models --project ~/models/bracket
  3d workspaces list --json
  3d workspaces show shop"""


def _print_usage() -> None:
    print(USAGE)


def _split_json_flag(argv: list[str]) -> tuple[list[str], bool]:
    return [arg for arg in argv if arg != "--json"], "--json" in argv


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=False))


def _require_name(argv: list[str], sub: str) -> str:
    if not argv:
        raise UsageError(
            f"'{sub}' needs a workspace name",
            command="workspaces",
            remediation=[f"Example:  3d workspaces {sub} shop"],
        )
    return argv[0]


def _list(*, as_json: bool) -> int:
    import workspaces

    items = workspaces.list_workspaces()
    if as_json:
        _print_json({"workspaces": items})
        return 0
    if not items:
        print("No workspaces configured yet.")
        print("Create one with:  3d workspaces create <name> --root <dir>")
        return 0
    name_w = max(max((len(str(item["name"])) for item in items), default=0), len("NAME"))
    count_w = len("PROJECTS")
    print(f"{'NAME':<{name_w}}  {'PROJECTS':>{count_w}}  ROOT")
    for item in items:
        print(f"{item['name']:<{name_w}}  {item['project_count']:>{count_w}}  {item['root']}")
    return 0


def _parse_create(argv: list[str]) -> tuple[str, str | None, list[str]]:
    name = _require_name(argv, "create")
    root: str | None = None
    projects: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError(
                    "--root needs a directory value",
                    command="workspaces",
                    remediation=["Example:  3d workspaces create shop --root ~/models"],
                )
            root = argv[i + 1]
            i += 2
        elif arg == "--project":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError(
                    "--project needs a directory value",
                    command="workspaces",
                    remediation=[
                        "Example:  3d workspaces create shop --project ~/models/bracket",
                    ],
                )
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                projects.append(argv[i])
                i += 1
        else:
            raise UsageError(
                f"unknown create option: {arg}",
                command="workspaces",
                remediation=["Run `3d workspaces --help` for accepted options."],
            )
    return name, root, projects


def _create(argv: list[str], *, as_json: bool) -> int:
    import workspaces

    name, root, projects = _parse_create(argv)
    entry = workspaces.create_workspace(name, root=root or os.getcwd(), projects=projects)
    if as_json:
        _print_json({"workspace": entry})
    else:
        print(f"Created workspace: {entry['name']}")
        print(f"  root: {entry['root']}")
        print(f"  projects: {entry['project_count']}")
    return 0


def _show(argv: list[str], *, as_json: bool) -> int:
    import workspaces

    name = _require_name(argv, "show")
    if len(argv) > 1:
        raise UsageError(
            f"unknown show option: {argv[1]}",
            command="workspaces",
            remediation=["Run `3d workspaces --help` for accepted options."],
        )
    entry = workspaces.get_workspace(name)
    if as_json:
        _print_json({"workspace": entry})
        return 0
    print(f"Workspace: {entry['name']}")
    print(f"  root: {entry['root']}")
    print(f"  projects: {entry['project_count']}")
    for project in entry["projects"]:
        print(f"    {project['name']}  {project['path']}")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    if argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0

    sub = argv[0]
    rest, as_json = _split_json_flag(argv[1:])
    if sub == "list":
        if rest:
            raise UsageError(
                f"unknown list option: {rest[0]}",
                command="workspaces",
                remediation=["Run `3d workspaces --help` for accepted options."],
            )
        return _list(as_json=as_json)
    if sub == "create":
        return _create(rest, as_json=as_json)
    if sub == "show":
        return _show(rest, as_json=as_json)

    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="workspaces",
        remediation=["Run `3d workspaces --help` for the available subcommands."],
    )


COMMAND = Command(
    name="workspaces",
    group="ENVIRONMENT",
    summary="create/list/read web workspace metadata",
    usage=USAGE,
    run=run,
)
