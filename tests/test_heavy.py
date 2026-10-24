"""`3d heavy`: the lock excludes, the wrapper is re-entrant (the 25-minute nested-flock deadlock), the wait is bounded."""
from __future__ import annotations

import fcntl
import os
import shutil
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


@pytest.mark.skipif(shutil.which("lslocks") is None, reason="lslocks not installed")
def test_status_names_the_holder(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        out = subprocess.run([*THREED, "heavy", "status"], env=_env(lock), capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "held by pid" in out.stdout


@pytest.fixture
def bare_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """PATH holding only env/nice/sh/echo: no flock(1), no lslocks, no systemd-run (macOS, minimal containers)."""
    bindir = tmp_path / "barebin"
    bindir.mkdir()
    for tool in ("env", "nice", "sh", "echo"):
        found = shutil.which(tool)
        assert found is not None
        (bindir / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv(heavy.ENV_HELD, raising=False)
    return str(bindir)


def test_prefix_without_flock_still_takes_the_lock_before_marking_children(
    tmp_path: Path, bare_path: str
) -> None:
    lock = tmp_path / "lock"
    prefix = heavy.heavy_prefix(lock=str(lock), wait_s=1)
    env = {**os.environ, "PATH": bare_path}
    env.pop(heavy.ENV_HELD, None)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # someone else holds it
        busy = subprocess.run([*prefix, "echo", "never"], env=env, capture_output=True, text=True, check=False, timeout=60)
    assert busy.returncode == heavy.EXIT_BUSY and "never" not in busy.stdout  # a lock-less prefix would run it
    free = subprocess.run([*prefix, "sh", "-c", "echo $" + heavy.ENV_HELD], env=env, capture_output=True, text=True, check=False, timeout=60)
    assert free.returncode == 0 and free.stdout.strip() == "1", free.stderr


def test_status_without_lslocks_does_not_report_a_held_lock_as_free(
    tmp_path: Path, bare_path: str
) -> None:
    lock = tmp_path / "lock"
    env = {**_env(lock), "PATH": bare_path}
    absent = subprocess.run([*THREED, "heavy", "status"], env=env, capture_output=True, text=True, check=False)
    assert absent.returncode == 0 and absent.stdout.strip() == "free"
    lock.touch()
    unheld = subprocess.run([*THREED, "heavy", "status"], env=env, capture_output=True, text=True, check=False)
    assert unheld.returncode == 0 and unheld.stdout.strip() == "free"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        held = subprocess.run([*THREED, "heavy", "status"], env=env, capture_output=True, text=True, check=False)
    assert held.returncode == 0 and held.stdout.startswith("held") and "free" not in held.stdout
