"""`python -m threed_vision <command> ...`: self-registering stage commands.

A command is one module `threed_vision/commands/<name>.py` defining NAME, SUMMARY,
`add_arguments(parser)` and `run(args) -> int`. Discovery globs the package, so adding a command
edits no shared file. `--json` commands print exactly one JSON object on stdout; progress and
diagnostics go to stderr. Exit codes: 0 ok, 2 bad input, 3 low confidence, 127 missing dependency/model.
"""
from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from types import ModuleType

from . import commands as _commands_pkg
from .errors import VisionError


def discover() -> dict[str, ModuleType]:
    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(_commands_pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{_commands_pkg.__name__}.{info.name}")
        found[mod.NAME] = mod
    return found


def build_parser(cmds: dict[str, ModuleType]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threed_vision", description="photo -> 3D vision stages")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name in sorted(cmds):
        sp = sub.add_parser(name, help=cmds[name].SUMMARY, description=cmds[name].SUMMARY)
        cmds[name].add_arguments(sp)
    return parser


def main(argv: list[str] | None = None) -> int:
    cmds = discover()
    parser = build_parser(cmds)
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        return int(cmds[args.command].run(args))
    except VisionError as exc:
        print(f"threed_vision {args.command}: {exc.render()}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
