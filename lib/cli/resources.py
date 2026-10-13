"""resources.py — the ONE place that decides how much of the machine a `3d` run may use.

`3d` runs on shared workstations next to other agents, slicers and the owner's desktop.
Pools sized by `os.cpu_count()` (a 16-core box: 16 openscad renders at once) and numeric
libraries that start a thread per core (OpenMP/BLAS/numba inside numpy, scipy, trimesh,
open3d) together drove the load average past 20. Every worker pool, every heavy tool
subprocess and the test runner take their size from here.

- `max_workers()`: `THREED_MAX_WORKERS` when set (an integer >= 1), else
  `min(6, cpu_count // 2)` but at least 1.
- `cap_threads(env)`: sets the native thread-pool variables (`OMP_NUM_THREADS`, BLAS,
  numba, numexpr) to `max_workers()` in `env` unless the caller already set them, so an
  explicit `OMP_NUM_THREADS=1 3d …` still wins. The dispatcher applies it to `os.environ`
  before any subprocess, so every `lib/*.py` tool inherits the cap.

Stdlib-only: imported by the dispatcher on every invocation.
"""
from __future__ import annotations

import os
from collections.abc import MutableMapping

ENV_MAX_WORKERS = "THREED_MAX_WORKERS"
DEFAULT_CAP = 6

# native thread pools started by numpy/scipy/BLAS/numba/open3d; each defaults to one thread per core
THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)


def max_workers(env: MutableMapping[str, str] | None = None, cpu_count: int | None = None) -> int:
    """Upper bound for parallel workers/threads of one `3d` run."""
    env = os.environ if env is None else env
    raw = env.get(ENV_MAX_WORKERS, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return value
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    return max(1, min(DEFAULT_CAP, cpus // 2))


def pool_size(tasks: int, requested: int | None = None, env: MutableMapping[str, str] | None = None) -> int:
    """Workers for `tasks` jobs: `requested` (an explicit `--jobs N`) else `max_workers()`,
    never more than there are tasks, at least 1."""
    want = requested if requested is not None else max_workers(env)
    return max(1, min(tasks, want))


def cap_threads(env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Cap native thread pools in `env` (default `os.environ`) at `max_workers()`; keeps values already set."""
    env = os.environ if env is None else env
    n = str(max_workers(env))
    for var in THREAD_VARS:
        env.setdefault(var, n)
    return env
