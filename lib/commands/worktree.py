"""3d worktree - create agent git worktrees with a ready dev .venv."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from cli.env import install_cmd, repo_root
from cli.registry import Command
from errors import InvalidArgument, MissingDependency, UsageError

USAGE = """3d worktree <subcommand> [options]
  Create agent git worktrees with the project dev environment bootstrapped.
  Agents MUST use this command instead of raw `git worktree add`.

Subcommands:
  create <branch> [--path DIR] [--base REF] [--json] [--no-sync]
      create a git worktree and run `uv sync --extra dev` in it
  doctor [DIR] [--json]
      verify DIR (default cwd) has .venv/bin/{ruff,pytest,mypy}
  list [--json]
      list git worktrees known to this repository

Examples:
  3d worktree create roadmap/e2e-expansion --base main
  3d worktree create roadmap/fit-camera-video --path /tmp/3d-cli-fit-video
  3d worktree doctor /tmp/3d-cli-fit-video --json
  3d worktree list --json"""

DEFAULT_ROOT = Path.home() / ".config" / "superpowers" / "worktrees" / "3d-cli"
DEV_TOOLS = ("ruff", "pytest", "mypy")


def _print_usage() -> None:
    print(USAGE)


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root(),
        text=True,
        capture_output=True,
    )


def _run_in(path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=path, text=True, capture_output=True)


def _require_uv() -> str:
    uv = shutil.which("uv")
    if uv:
        return uv
    raise MissingDependency(
        "uv",
        command="worktree",
        install=install_cmd("uv"),
        degrades="agent worktree bootstrap cannot install ruff, pytest, and mypy",
    )


def _branch_exists(branch: str) -> bool:
    return _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def _safe_name(branch: str) -> str:
    return branch.strip("/").replace("/", "-").replace(" ", "-")


def _default_path(branch: str) -> Path:
    return DEFAULT_ROOT / _safe_name(branch)


def _parse_common_json(argv: list[str]) -> tuple[list[str], bool]:
    return [arg for arg in argv if arg != "--json"], "--json" in argv


def _parse_create(argv: list[str]) -> tuple[str, Path, str, bool, bool]:
    if not argv:
        raise UsageError(
            "create needs a branch name",
            command="worktree",
            remediation=["Example:  3d worktree create roadmap/e2e-expansion --base main"],
        )
    branch = argv[0]
    path: Path | None = None
    base = "main"
    sync = True
    as_json = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            as_json = True
            i += 1
        elif arg == "--no-sync":
            sync = False
            i += 1
        elif arg == "--base":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError("--base needs a git ref", command="worktree")
            base = argv[i + 1]
            i += 2
        elif arg == "--path":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError("--path needs a directory", command="worktree")
            path = Path(argv[i + 1]).expanduser()
            i += 2
        else:
            raise UsageError(
                f"unknown create option: {arg}",
                command="worktree",
                remediation=["Run `3d worktree --help` for accepted options."],
            )
    resolved_path = (path or _default_path(branch)).resolve()
    return branch, resolved_path, base, sync, as_json


def _dev_tool_status(path: Path) -> dict[str, bool]:
    return {tool: (path / ".venv" / "bin" / tool).exists() for tool in DEV_TOOLS}


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=False))


def _sync_dev(path: Path) -> None:
    uv = _require_uv()
    result = _run_in(path, [uv, "sync", "--extra", "dev"])
    if result.returncode != 0:
        raise UsageError(
            "`uv sync --extra dev` failed",
            command="worktree",
            remediation=[
                (result.stderr or result.stdout)[-800:],
                f"Inspect the worktree:  cd {path}",
                "Then retry:  uv sync --extra dev",
            ],
        )


def _raise_missing_dev_tools(path: Path, missing: list[str]) -> None:
    raise UsageError(
        "agent worktree dev environment is incomplete",
        command="worktree",
        remediation=[
            f"Missing tools in {path / '.venv' / 'bin'}: {', '.join(missing)}",
            f"Inspect the worktree:  cd {path}",
            "Retry bootstrap:  uv sync --extra dev",
            "Then verify:  3d worktree doctor .",
        ],
    )


def _create(argv: list[str]) -> int:
    branch, path, base, sync, as_json = _parse_create(argv)
    if sync:
        _require_uv()
    if path.exists():
        raise InvalidArgument(
            "--path",
            str(path),
            ["a directory that does not already exist"],
            command="worktree",
            extra="Choose another --path, or remove the existing worktree with `git worktree remove`.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", str(path), branch] if _branch_exists(branch) else [
        "worktree",
        "add",
        "-b",
        branch,
        str(path),
        base,
    ]
    result = _run_git(args)
    if result.returncode != 0:
        raise UsageError(
            "git worktree add failed",
            command="worktree",
            remediation=[
                (result.stderr or result.stdout)[-800:],
                "Check the branch/base ref and path, then retry.",
            ],
        )
    if sync:
        _sync_dev(path)
    tools = _dev_tool_status(path)
    missing = [tool for tool, ok in tools.items() if not ok]
    if sync and missing:
        _raise_missing_dev_tools(path, missing)
    payload = {
        "branch": branch,
        "path": str(path),
        "base": base,
        "synced": sync,
        "dev_tools": tools,
    }
    if as_json:
        _print_json(payload)
    else:
        print(f"Created worktree: {path}")
        print(f"  branch: {branch}")
        print(f"  base: {base}")
        print(f"  dev sync: {'yes' if sync else 'no'}")
        if missing:
            print(f"  missing dev tools: {', '.join(missing)}")
            print("  run: uv sync --extra dev")
        else:
            print("  dev tools: ruff, pytest, mypy")
    return 0


def _doctor(argv: list[str]) -> int:
    rest, as_json = _parse_common_json(argv)
    if len(rest) > 1:
        raise UsageError("doctor accepts at most one DIR", command="worktree")
    path = Path(rest[0]).expanduser() if rest else Path.cwd()
    tools = _dev_tool_status(path)
    ok = all(tools.values())
    payload = {"path": str(path), "ok": ok, "dev_tools": tools}
    if as_json:
        _print_json(payload)
    else:
        print(f"Worktree: {path}")
        for tool, present in tools.items():
            print(f"  {tool}: {'ok' if present else 'missing'}")
        if not ok:
            print("Remediation: uv sync --extra dev")
    return 0 if ok else 1


def _list(argv: list[str]) -> int:
    rest, as_json = _parse_common_json(argv)
    if rest:
        raise UsageError("list accepts only --json", command="worktree")
    result = _run_git(["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        raise UsageError(
            "git worktree list failed",
            command="worktree",
            remediation=[result.stderr or "Run `git worktree list --porcelain` for details."],
        )
    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                items.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        items.append(current)
    if as_json:
        _print_json({"worktrees": items})
    else:
        for item in items:
            print(f"{item.get('branch', '(detached)')}  {item.get('worktree', '')}")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    if argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0
    sub = argv[0]
    rest = argv[1:]
    if sub == "create":
        return _create(rest)
    if sub == "doctor":
        return _doctor(rest)
    if sub == "list":
        return _list(rest)
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="worktree",
        remediation=["Run `3d worktree --help` for available subcommands."],
    )


COMMAND = Command(
    name="worktree",
    group="ENVIRONMENT",
    summary="create agent git worktrees with dev .venv bootstrap",
    usage=USAGE,
    run=run,
)
