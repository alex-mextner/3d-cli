"""heavy.py — the machine-wide heavy-job lock, re-entrant, with a bounded wait.

WHAT: `heavy_prefix()` returns the argv prefix that runs a command one-at-a-time under the shared
  lock with a memory/CPU cap (`flock LOCK systemd-run --user --scope -p MemoryMax=.. nice -n 10 env
  THREED_HEAVY_HELD=1`); `run_heavy()` runs a command that way; `holder()` says who holds the lock.
WHY: the lock was typed by hand in every script (AGENTS.md "Machine resources"). A script that
  wrapped itself AND was started under a wrapper waited for its own lock: a nested `flock` on the
  same file deadlocked a 25-minute proof run (Chekhov session 2026-10-20, ledger #2). Here the
  wrapper marks its children with THREED_HEAVY_HELD=1, and every later `heavy_prefix()` in that
  process tree returns [] instead of locking again; `--wait` is bounded so a stuck lock reports its
  holder instead of hanging for ever.
INVARIANTS: stdlib only. The lock file is the same `/tmp/3d-cli-heavy.lock` the AGENTS.md one-liner
  uses, so hand-typed wrappers and `3d heavy` exclude each other. Without systemd (macOS, containers)
  the cap is skipped and only the lock + nice apply; without `flock(1)` Python's fcntl takes the lock
  (`run_heavy` directly, `heavy_prefix` by re-entering `3d heavy`).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from errors import ThreeDError

LOCK_ENV = "THREED_HEAVY_LOCK"
#: shared with the documented one-liner in AGENTS.md; the env override is for tests and other boxes
LOCK_PATH = os.environ.get(LOCK_ENV, "/tmp/3d-cli-heavy.lock")  # noqa: S108
ENV_HELD = "THREED_HEAVY_HELD"
DEFAULT_MEMORY_MAX = "6G"
DEFAULT_CPU_QUOTA = "600%"
DEFAULT_NICE = 10
DEFAULT_WAIT_S = 1800
EXIT_BUSY = 75  # flock -E: the bounded wait ran out (EX_TEMPFAIL, same code as the remote `heavy`)
_THREED = Path(__file__).resolve().parents[2] / "bin" / "3d"


class HeavyBusy(ThreeDError):
    """The bounded wait for the heavy lock ran out. Exit 75 (EX_TEMPFAIL), same as the remote `heavy`."""

    exit_code = 75


@dataclass(frozen=True)
class Holder:
    """Who currently holds the lock (best effort: read from the lock file the holder stamped)."""

    pid: int
    command: str


def held(env: Mapping[str, str] | None = None) -> bool:
    """True inside a process tree that already runs under the heavy lock."""
    return (os.environ if env is None else env).get(ENV_HELD) == "1"


def _scope_works() -> bool:
    exe = shutil.which("systemd-run")
    if exe is None:
        return False
    probe = subprocess.run(  # noqa: S603 — fixed argv
        [exe, "--user", "--scope", "-q", "-p", "MemoryMax=1G", "--", "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def _capped(*, memory_max: str, cpu_quota: str, nice: int) -> list[str]:
    """The part of the prefix after the lock: systemd cap when available, nice, and the held marker."""
    argv: list[str] = []
    if _scope_works():
        argv += [
            "systemd-run",
            "--user",
            "--scope",
            "-q",
            "-p",
            f"MemoryMax={memory_max}",
            "-p",
            "MemorySwapMax=0",
            "-p",
            f"CPUQuota={cpu_quota}",
        ]
    return [*argv, "nice", "-n", str(nice), "env", f"{ENV_HELD}=1"]


def heavy_prefix(
    *,
    memory_max: str = DEFAULT_MEMORY_MAX,
    cpu_quota: str = DEFAULT_CPU_QUOTA,
    nice: int = DEFAULT_NICE,
    wait_s: int = DEFAULT_WAIT_S,
    lock: str = LOCK_PATH,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """argv prefix for a heavy command; `[]` when the caller is already inside the lock.

    The command that follows runs with THREED_HEAVY_HELD=1 so anything it starts that asks for a
    prefix gets `[]` back. The wait is bounded by `wait_s` (`flock -w`). Without `flock(1)` a prefix
    cannot take the lock itself, so it re-enters `3d heavy` (which holds it through fcntl): the marker
    is never set by a prefix that did not lock.
    """
    if held(env):
        return []
    if shutil.which("flock"):
        return [
            "flock",
            "-w",
            str(wait_s),
            "-E",
            str(EXIT_BUSY),
            lock,
            *_capped(memory_max=memory_max, cpu_quota=cpu_quota, nice=nice),
        ]
    return [
        "env",
        f"{LOCK_ENV}={lock}",
        sys.executable,
        str(_THREED),
        "heavy",
        "--wait",
        str(wait_s),
        "--mem",
        memory_max,
        "--cpu",
        cpu_quota,
        "--nice",
        str(nice),
        "--",
    ]


def _acquire(lock: str, wait_s: float) -> int:
    """Take the lock with fcntl (used when flock(1) is absent); returns the open fd."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:  # noqa: PERF203 — the retry loop needs the exception
            if time.monotonic() >= deadline:
                os.close(fd)
                raise HeavyBusy(describe_holder(lock), command="heavy", remediation=["Wait for it, or raise --wait"]) from None
            time.sleep(0.5)


def describe_holder(lock: str = LOCK_PATH) -> str:
    """One line for the user when the lock stays busy."""
    h = holder(lock)
    if h is None:
        return f"{lock} is busy (holder unknown)"
    return f"{lock} is held by pid {h.pid}: {h.command}"


def holder(lock: str = LOCK_PATH) -> Holder | None:
    """The lock's current holder via `lslocks` (pid + command), or None when free / unknown."""
    if not Path(lock).exists():
        return None
    lslocks = shutil.which("lslocks")
    if lslocks is None:
        return None
    out = subprocess.run(  # noqa: S603 — fixed argv
        [lslocks, "--noheadings", "--output", "PID,COMMAND,PATH", "--notruncate"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].strip() == lock and parts[0].isdigit():
            return Holder(int(parts[0]), parts[1])
    return None


def lock_free(lock: str = LOCK_PATH) -> bool:
    """True when nobody holds the lock; needs no `lslocks` (a non-blocking fcntl probe, released at once)."""
    if not Path(lock).exists():
        return True
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def run_heavy(
    argv: Sequence[str],
    *,
    memory_max: str = DEFAULT_MEMORY_MAX,
    cpu_quota: str = DEFAULT_CPU_QUOTA,
    nice: int = DEFAULT_NICE,
    wait_s: int = DEFAULT_WAIT_S,
    lock: str = LOCK_PATH,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run `argv` under the lock and the cap; returns its exit code (`EXIT_BUSY` when the lock stayed busy)."""
    base = dict(os.environ if env is None else env)
    if held(base):
        return subprocess.run(list(argv), cwd=cwd, env=base, check=False).returncode  # noqa: S603
    if shutil.which("flock"):
        prefix = heavy_prefix(
            memory_max=memory_max, cpu_quota=cpu_quota, nice=nice, wait_s=wait_s, lock=lock, env=base
        )
        return subprocess.run([*prefix, *argv], cwd=cwd, env=base, check=False).returncode  # noqa: S603
    # no flock(1): hold the lock from this process (`heavy_prefix` re-enters here for shell callers)
    fd = _acquire(lock, wait_s)
    try:
        capped = _capped(memory_max=memory_max, cpu_quota=cpu_quota, nice=nice)
        return subprocess.run([*capped, *argv], cwd=cwd, env=base, check=False).returncode  # noqa: S603
    finally:
        os.close(fd)
