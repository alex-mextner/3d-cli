"""dispatch.py — the thin typed dispatcher for the `3d` CLI.

Responsibilities (and nothing more — real work lives in the command modules):
  1. startup: export OPENSCADPATH from libs/, run the first-run bootstrap (non-fatal).
  2. build the registry by discovering `lib/commands/*.py`.
  3. route `3d <cmd> ...` to the resolved command (with alias support).
  4. build `3d help` / usage from the registry.
  5. catch ThreeDError -> print the rich message to stderr + exit with its code.

`bin/3d` is ~3 lines: resolve repo root through the symlink, put lib/ on sys.path,
call `main(sys.argv[1:])`.
"""
from __future__ import annotations

import sys

from cli.env import export_openscadpath, maybe_bootstrap
from cli.registry import Registry, discover
from errors import ThreeDError

VERSION = "0.1.0"


def _color() -> tuple[str, str, str, str]:
    if sys.stdout.isatty():
        return ("\033[1m", "\033[36m", "\033[31m", "\033[0m")
    return ("", "", "", "")


def usage(reg: Registry) -> str:
    bold, cyan, _red, z = _color()
    out: list[str] = []
    out.append(f"{bold}3d{z} — scriptable 3D / OpenSCAD pipeline CLI  (v{VERSION})")
    out.append("")
    out.append(f"{bold}USAGE{z}")
    out.append("  3d <command> [args]")
    out.append("  3d <command> --help        per-command help")
    out.append("  3d help                    this text")
    out.append("  3d version                 print version")
    out.append("")

    alias_by_canon: dict[str, list[str]] = {}
    for alias, canon in reg.alias_map().items():
        alias_by_canon.setdefault(canon, []).append(alias)

    last_group: str | None = None
    for cmd in reg.commands():
        if cmd.group != last_group:
            out.append(f"{bold}{cmd.group}{z}")
            last_group = cmd.group
        aliases = alias_by_canon.get(cmd.name)
        suffix = f"  (alias: {', '.join(sorted(aliases))})" if aliases else ""
        out.append(f"  {cyan}{cmd.name:<12}{z} {cmd.summary}{suffix}")
    out.append("")
    out.append("Run '3d <command> --help' for details and examples.")
    return "\n".join(out)


def _suggest(reg: Registry, cmd: str) -> list[str]:
    prefix = cmd[:3].lower()
    return [n for n in reg.names() if prefix and prefix in n.lower()]


def main(argv: list[str]) -> int:
    # 1. startup (must run before any subprocess so OpenSCAD/children inherit the path).
    export_openscadpath()
    maybe_bootstrap()

    # 2. registry.
    reg = discover()

    cmd = argv[0] if argv else "help"
    rest = argv[1:]

    if cmd in ("help", "-h", "--help"):
        print(usage(reg))
        return 0
    if cmd in ("version", "-v", "--version"):
        print(f"3d v{VERSION}")
        return 0

    resolved = reg.resolve(cmd)
    if resolved is None:
        bold, _cyan, red, z = _color()
        sys.stderr.write(f"{red}3d: unknown command '{cmd}'{z}\n")
        sys.stderr.write("Run '3d help' for the list of commands. Closest matches:\n")
        for s in _suggest(reg, cmd):
            sys.stderr.write(f"  {s}\n")
        return 2

    # 3. route + catch structured errors.
    try:
        return resolved.run(rest)
    except ThreeDError as e:
        if e.command is None:
            e.command = resolved.name
        msg = e.render()
        if msg:
            sys.stderr.write(msg + "\n")
        return e.exit_code
    except BrokenPipeError:
        # downstream closed the pipe (e.g. `| head`): exit quietly.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:
        return 130
