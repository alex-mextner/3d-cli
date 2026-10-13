"""cli.resources: the machine budget of one `3d` run (worker pools, native thread caps)."""
from __future__ import annotations

import asyncio
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
    assert pool_size(10, requested=0, env=env) == 1      # `--jobs 0` still runs


def test_cap_threads_sets_every_pool_but_keeps_explicit_values() -> None:
    env = {"THREED_MAX_WORKERS": "3", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": " "}
    cap_threads(env)
    assert env["OMP_NUM_THREADS"] == "1"
    # an empty value is not a choice: OpenBLAS would read it as one thread per core
    assert all(env[v] == "3" for v in THREAD_VARS if v != "OMP_NUM_THREADS")


def test_unusable_override_is_reported_once_by_cap_threads(capsys: pytest.CaptureFixture[str]) -> None:
    env = {"THREED_MAX_WORKERS": "1O"}
    cap_threads(env)
    err = capsys.readouterr().err
    assert "ignoring THREED_MAX_WORKERS='1O'" in err
    assert env["OMP_NUM_THREADS"] == str(max_workers({}))


def test_dispatcher_caps_threads_for_its_children() -> None:
    """`bin/3d` applies the caps before any subprocess: a child of the dispatcher sees them."""
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARS}
    env["THREED_MAX_WORKERS"] = "2"
    code = ("import os, sys; sys.path.insert(0, 'lib'); from cli import dispatch; dispatch.main(['version']); "
            "print('CAPS', os.environ['OMP_NUM_THREADS'], os.environ['NUMBA_NUM_THREADS'])")
    out = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env, capture_output=True, text=True,
                         check=True).stdout
    assert "CAPS 2 2" in out.splitlines()


def test_render_multi_runs_at_most_max_workers_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import render

    live = peak = 0

    async def fake_render(argv: list[str], sem: asyncio.Semaphore, label: str) -> tuple[str, int, str]:
        nonlocal live, peak
        async with sem:
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
        return label, 0, ""

    monkeypatch.setenv("THREED_MAX_WORKERS", "3")
    monkeypatch.setattr(render, "_render_one_async", fake_render)
    cmds = [(f"v{i}", f"v{i}.png", ["openscad"]) for i in range(10)]
    res = asyncio.run(render._render_multi_async(cmds))
    assert [r[0] for r in res] == [c[0] for c in cmds]
    assert peak == 3
    assert resources.max_workers() == 3
