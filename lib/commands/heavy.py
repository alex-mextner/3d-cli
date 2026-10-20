"""3d heavy — run one command at a time under the machine-wide heavy lock and memory cap.

WHAT: `3d heavy -- CMD ...` runs CMD under `/tmp/3d-cli-heavy.lock` with a systemd memory/CPU
  cap and nice; `3d heavy status` names the current holder; `3d heavy prefix` prints the argv
  prefix for shell scripts that need to wrap something themselves.
WHY: AGENTS.md asks for the lock on every heavy run. Hand-typed, it deadlocked a 25-minute proof
  run when a script wrapped itself inside a wrapped shell. Here the wrapper marks its children
  (THREED_HEAVY_HELD=1) so nested uses run directly, and the wait is bounded.
INVARIANTS: stdlib-only at import time (registry contract); exit 75 when the wait ran out, else
  the wrapped command's own exit code.

Examples:
  3d heavy -- 3d prep model.stl --printer u1
  3d heavy --wait 600 --mem 12G -- python3 big_mesh_job.py
  3d heavy status
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from cli import heavy
from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d heavy [--wait SECONDS] [--mem SIZE] [--cpu PERCENT] [--nice N] -- CMD [ARG...]
3d heavy status
3d heavy prefix [--mem SIZE] [--cpu PERCENT] [--wait SECONDS]
  Run CMD alone on the machine under /tmp/3d-cli-heavy.lock with a memory cap
  (default 6G, 600% CPU, nice 10). Inside an already-wrapped process tree CMD runs directly,
  so a script may wrap itself without deadlocking. --wait is the longest queue time
  (default 1800 s); exit 75 when it ran out.

Examples:
  3d heavy -- 3d prep model.stl --printer u1
  3d heavy --wait 600 --mem 12G -- python3 big_mesh_job.py
  3d heavy --cpu 400% --nice 15 -- 3d slice part.3mf
  3d heavy status
  $(3d heavy prefix) python3 job.py        # shell scripts: wrap one command by hand"""

@dataclass
class _Opts:
    wait_s: int = heavy.DEFAULT_WAIT_S
    memory_max: str = heavy.DEFAULT_MEMORY_MAX
    cpu_quota: str = heavy.DEFAULT_CPU_QUOTA
    nice: int = heavy.DEFAULT_NICE


def _int(flag: str, raw: str) -> int:
    if not raw.lstrip("-").isdigit():
        raise InvalidArgument(flag, raw, ["an integer"], command="heavy")
    return int(raw)


def _parse(argv: list[str]) -> tuple[_Opts, list[str]]:
    opts = _Opts()
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return opts, argv[i + 1 :]
        if tok not in ("--wait", "--mem", "--cpu", "--nice"):
            raise UsageError(
                f"unexpected {tok!r} before `--`; the command to wrap goes after `--`", command="heavy"
            )
        if i + 1 >= len(argv):
            raise UsageError(f"{tok} needs a value", command="heavy")
        raw = argv[i + 1]
        if tok == "--wait":
            opts.wait_s = _int(tok, raw)
        elif tok == "--nice":
            opts.nice = _int(tok, raw)
        elif tok == "--mem":
            opts.memory_max = raw
        else:
            opts.cpu_quota = raw
        i += 2
    return opts, []


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] == "status":
        current = heavy.holder()
        print(f"held by pid {current.pid}: {current.command}" if current else "free")
        return 0
    if argv[0] == "prefix":
        o, _ = _parse(argv[1:])
        prefix = heavy.heavy_prefix(
            memory_max=o.memory_max, cpu_quota=o.cpu_quota, nice=o.nice, wait_s=o.wait_s
        )
        print(shlex.join(prefix))
        return 0
    o, cmd = _parse(argv)
    if not cmd:
        raise UsageError("nothing to run: put the command after `--`", command="heavy")
    return heavy.run_heavy(
        cmd, memory_max=o.memory_max, cpu_quota=o.cpu_quota, nice=o.nice, wait_s=o.wait_s
    )


COMMAND = Command(
    name="heavy",
    group="ENVIRONMENT",
    summary="run one command alone under the machine heavy lock + memory cap (re-entrant, bounded wait)",
    usage=USAGE,
    run=run,
)
