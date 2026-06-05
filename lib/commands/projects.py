"""3d projects — register / list / unregister 3d projects (ROADMAP §28/§9).

WHY: the CLI works on the nearest 3d.yaml walking up from cwd, but the dashboard
(`3d web`) and cross-project tooling need to know ALL your projects, not just the one
you happen to be standing in. This command maintains that list so you can jump between a
dozen prints without remembering where each lives.

ACCESSED VIA: `3d projects list|add|remove`. The on-disk store and all logic live in the
headless core `projects_registry`; this module only parses argv, prints, and raises
structured errors (it does NO disk logic itself).

INVARIANTS:
  - Thin frontend: every read/write goes through `projects_registry`; no path resolution
    or JSON handling here.
  - Warnings (e.g. registering a dir with no 3d.yaml) print to STDERR; the table prints to
    STDOUT, so `3d projects list` is pipe-clean.
"""
from __future__ import annotations

import sys

import projects_registry
from cli.registry import Command
from projects_registry import ProjectRegistryError

USAGE = """3d projects <subcommand>
  Track every directory that is a 3d project, so `3d web` and cross-project tools can
  list them all (not just the project under your current directory).

Subcommands:
  list                 show registered projects (name, path, when added)
  add <path>           register a project directory (the folder that holds 3d.yaml)
  remove <path>        unregister a project directory

Why: you usually have many prints in flight; the CLI only sees the nearest 3d.yaml. The
registry remembers the rest so you do not have to `cd` around or keep paths in your head.

Examples:
  3d projects add ./my-bracket        # register the project in ./my-bracket
  3d projects list                    # see everything you have registered
  3d projects remove ~/old-print      # forget a project you are done with"""


def _print_usage() -> None:
    print(USAGE)


def _require_path(argv: list[str], sub: str) -> str:
    if not argv:
        raise ProjectRegistryError(
            f"'{sub}' needs a path argument",
            command="projects",
            remediation=[f"Example:  3d projects {sub} ./my-project"],
        )
    return argv[0]


def _added_date(added: object) -> str:
    """The date portion of a stored ISO timestamp ('-' for legacy entries with none)."""
    if isinstance(added, str) and added:
        return added.split("T", 1)[0]
    return "-"


def _list() -> int:
    items = projects_registry.list_projects()
    if not items:
        print("No projects registered yet.")
        print("Register one with:  3d projects add <path>")
        return 0
    name_w = max(max((len(i["name"]) for i in items), default=0), len("NAME"))
    date_w = len("ADDED")
    print(f"{'NAME':<{name_w}}  {'ADDED':<{date_w}}  PATH")
    for i in items:
        print(f"{i['name']:<{name_w}}  {_added_date(i['added']):<{date_w}}  {i['path']}")
    return 0


def _add(path: str) -> int:
    entry = projects_registry.add(path)
    print(f"Registered: {entry['path']}")
    if not entry["had_yaml"]:
        print(
            f"  warning: no 3d.yaml in {entry['path']} — "
            "run `3d init` there to scaffold one.",
            file=sys.stderr,
        )
    return 0


def _remove(path: str) -> int:
    entry = projects_registry.remove(path)
    print(f"Unregistered: {entry['path']}")
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

    if sub == "list":
        return _list()
    if sub == "add":
        return _add(_require_path(rest, "add"))
    if sub == "remove":
        return _remove(_require_path(rest, "remove"))

    raise ProjectRegistryError(
        f"unknown subcommand '{sub}'",
        command="projects",
        remediation=["Run `3d projects --help` for the available subcommands."],
    )


COMMAND = Command(
    name="projects",
    group="ENVIRONMENT",
    summary="register / list / unregister 3d projects (used by `3d web`)",
    usage=USAGE,
    run=run,
)
