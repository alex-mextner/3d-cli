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

import importlib.metadata
import os
import re
import shlex
import sys

from cli.env import export_openscadpath, maybe_bootstrap, repo_root
from cli.registry import Registry, discover
from errors import ThreeDError, UsageError

# Distribution name as declared in pyproject `[project] name`.
_DIST_NAME = "3d-cli"
# Scoped to the `[project]` table so we never pick up a `version =` in some other
# table (e.g. a dependency pin). stdlib-only: no tomllib (kept 3.10-compatible).
# `^[ \t]*` on the header/keys tolerates TOML's optional leading indentation: the
# scan stops at the next (possibly indented) `[...]` table header and matches the
# first (possibly indented) `version =` inside `[project]`.
_PYPROJECT_VERSION_RE = re.compile(
    r"^[ \t]*\[project\](?:(?!^[ \t]*\[).)*?^[ \t]*version[ \t]*=[ \t]*[\"']([^\"']+)[\"']",
    re.MULTILINE | re.DOTALL,
)


def _version_from_pyproject() -> str | None:
    path = os.path.join(repo_root(), "pyproject.toml")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _PYPROJECT_VERSION_RE.search(text)
    return m.group(1) if m else None


def _resolve_version() -> str:
    """Read the declared version dynamically — pyproject is the single source of
    truth. `bin/3d` resolves `REPO_ROOT` through its (symlinked) launcher and runs
    THIS checkout's `lib/`, so the checkout's `pyproject.toml` is what is actually
    executing: read it FIRST. A stale `3d-cli` dist installed in the interpreter
    (e.g. a leftover 0.1.0 `.dist-info`) must NOT shadow the running source tree —
    that would reintroduce the very drift this resolver fixes. Fall back to the
    installed-distribution metadata only when no pyproject is on disk (a real
    installed-wheel run, where `lib/` ships inside the package).
    """
    from_pyproject = _version_from_pyproject()
    if from_pyproject is not None:
        return from_pyproject
    try:
        return importlib.metadata.version(_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


VERSION = _resolve_version()


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


def _test_command_migration_error(argv: list[str]) -> UsageError:
    remediation = [
        "Run 'dev run test' for the full repo gate.",
        "Pass pytest args after '--': 'dev run test -- <pytest args>'.",
        "The 'dev' command is provided by the agent-tools dev CLI; ensure it is installed on PATH.",
    ]
    if argv:
        args = " ".join(shlex.quote(arg) for arg in argv)
        remediation.append(f"For this invocation: 'dev run test -- {args}'.")
    return UsageError(
        "`3d test` moved to rig.yaml scripts and is no longer a product CLI command",
        command="test",
        remediation=remediation,
    )


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
    if cmd == "test":
        err = _test_command_migration_error(rest)
        sys.stderr.write(err.render() + "\n")
        return err.exit_code

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
