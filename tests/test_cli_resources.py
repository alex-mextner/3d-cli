"""cli.resources: the machine budget of one `3d` run (worker pools, native thread caps)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from cli import resources
from cli.resources import THREAD_VARS, cap_threads, max_workers, pool_size

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize(("cpus", "want"), [(1, 1), (2, 1), (4, 2), (8, 4), (12, 6), (16, 6), (64, 6)])
def test_default_is_half_the_cores_at_most_six(cpus: int, want: int) -> None:
    assert max_workers({}, cpu_count=cpus) == want


def test_env_override_wins_over_the_default() -> None:
    assert max_workers({"THREED_MAX_WORKERS": "12"}, cpu_count=16) == 12
    assert max_workers({"THREED_MAX_WORKERS": " 1 "}, cpu_count=16) == 1


@pytest.mark.parametrize("bad", ["0", "-3", "many", ""])
def test_unusable_override_falls_back_to_the_default(bad: str) -> None:
    assert max_workers({"THREED_MAX_WORKERS": bad}, cpu_count=16) == 6


def test_pool_size_never_exceeds_tasks_and_honours_explicit_jobs() -> None:
    env = {"THREED_MAX_WORKERS": "4"}
    assert pool_size(10, env=env) == 4
    assert pool_size(2, env=env) == 2
    assert pool_size(0, env=env) == 1
    assert pool_size(10, requested=8, env=env) == 8


def test_cap_threads_sets_every_pool_but_keeps_explicit_values() -> None:
    env = {"THREED_MAX_WORKERS": "3", "OMP_NUM_THREADS": "1"}
    cap_threads(env)
    assert env["OMP_NUM_THREADS"] == "1"
    assert all(env[v] == "3" for v in THREAD_VARS if v != "OMP_NUM_THREADS")


def test_dispatcher_caps_threads_for_its_children() -> None:
    """`bin/3d` applies the caps before any subprocess: a child of the dispatcher sees them."""
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARS}
    env["THREED_MAX_WORKERS"] = "2"
    code = ("import os, sys; sys.path.insert(0, 'lib'); from cli import dispatch; "
            "dispatch.main(['version']); print(os.environ['OMP_NUM_THREADS'], os.environ['NUMBA_NUM_THREADS'])")
    out = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env, capture_output=True, text=True,
                         check=True).stdout.split()
    assert out[-2:] == ["2", "2"]


def test_pools_take_their_size_from_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    import video

    monkeypatch.setenv("THREED_MAX_WORKERS", "3")
    assert video._parallelism(100, None) == 3
    assert video._parallelism(100, 7) == 7
    assert resources.max_workers() == 3
