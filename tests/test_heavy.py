"""`3d heavy`: the lock excludes, the wrapper is re-entrant (the 25-minute nested-flock deadlock), the wait is bounded."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import heavy

REPO = Path(__file__).resolve().parent.parent
THREED = [sys.executable, str(REPO / "bin" / "3d")]


def _env(lock: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(heavy.ENV_HELD, None)
    env[heavy.LOCK_ENV] = str(lock)
    env.update(extra)
    return env


def test_prefix_is_empty_inside_the_lock_and_marks_children_outside() -> None:
    assert heavy.heavy_prefix(env={heavy.ENV_HELD: "1"}) == []
    prefix = heavy.heavy_prefix(env={})
    assert prefix[-2:] == ["env", f"{heavy.ENV_HELD}=1"]
    assert "nice" in prefix


def test_command_runs_with_the_held_marker(tmp_path: Path) -> None:
    out = subprocess.run(
        [*THREED, "heavy", "--", "sh", "-c", "echo $" + heavy.ENV_HELD],
        env=_env(tmp_path / "lock"), capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0 and out.stdout.strip() == "1"


def test_nested_heavy_does_not_wait_for_its_own_lock(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    inner = f"{THREED[0]} {THREED[1]} heavy --wait 2 -- echo nested-ok"
    out = subprocess.run(
        [*THREED, "heavy", "--wait", "20", "--", "sh", "-c", inner],
        env=_env(lock), capture_output=True, text=True, check=False, timeout=60,
    )
    # without the marker the inner call queues on the lock its parent holds and exits 75 after 2 s
    assert out.returncode == 0, out.stderr
    assert "nested-ok" in out.stdout


def test_busy_lock_times_out_with_exit_75(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # someone else holds it
        out = subprocess.run(
            [*THREED, "heavy", "--wait", "1", "--", "echo", "never"],
            env=_env(lock), capture_output=True, text=True, check=False, timeout=60,
        )
    assert out.returncode == heavy.EXIT_BUSY
    assert "never" not in out.stdout


def test_exit_code_of_the_wrapped_command_is_passed_through(tmp_path: Path) -> None:
    out = subprocess.run(
        [*THREED, "heavy", "--", "sh", "-c", "exit 7"],
        env=_env(tmp_path / "lock"), capture_output=True, check=False,
    )
    assert out.returncode == 7


def test_usage_errors(tmp_path: Path) -> None:
    env = _env(tmp_path / "lock")
    none = subprocess.run([*THREED, "heavy", "--wait", "5"], env=env, capture_output=True, text=True, check=False)
    assert none.returncode == 2 and "after `--`" in none.stderr
    bad = subprocess.run([*THREED, "heavy", "--wait", "soon", "--", "true"], env=env, capture_output=True, text=True, check=False)
    assert bad.returncode == 2
    helped = subprocess.run([*THREED, "heavy", "--help"], env=env, capture_output=True, text=True, check=False)
    assert helped.returncode == 0 and "3d heavy status" in helped.stdout


@pytest.mark.skipif(not Path("/usr/bin/lslocks").exists(), reason="lslocks not installed")
def test_status_names_the_holder(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        out = subprocess.run([*THREED, "heavy", "status"], env=_env(lock), capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "held by pid" in out.stdout
