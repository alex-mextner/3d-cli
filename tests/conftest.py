"""Pytest setup: put the repo lib/ on sys.path so command/cli modules import by name,
exactly as bin/3d arranges at runtime, and keep the suite inside the machine budget
(cli.resources): native thread pools capped, `-n auto` (pytest-xdist) = max_workers(),
and tmp_path dirs on disk under the user cache instead of the /tmp RAM tmpfs; strip the
repo-locating GIT_* variables a git hook exports so no test's git call can touch a real checkout."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("REPO_ROOT", _REPO)
_LIB = os.path.join(_REPO, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from cli.resources import cap_threads, max_workers  # noqa: E402  (lib/ is on sys.path only from here)

# before any test imports numpy/scipy (their thread pools are sized at first import) and
# before any subprocess, so `3d` children started by the e2e tests inherit the caps too
cap_threads()

# PrintOps shared fixtures (tmp DB, FakeSpoolman, 3d-api TestClient, api_client, live_api).
pytest_plugins = ["printops_fixtures"]


def cache_temproot(environ: Mapping[str, str]) -> str:
    """`<XDG cache>/3d-cli/pytest`: $XDG_CACHE_HOME when absolute (the XDG spec ignores a
    relative one), else ~/.cache — so CI runners and other users get their own home's cache."""
    cache = environ.get("XDG_CACHE_HOME", "")
    if not os.path.isabs(cache):
        cache = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "3d-cli", "pytest")


def use_cache_temproot(environ: MutableMapping[str, str]) -> None:
    """Point PYTEST_DEBUG_TEMPROOT at cache_temproot() unless already set; an unusable cache dir
    leaves it unset (pytest falls back to the system temp dir) rather than breaking the suite."""
    if "PYTEST_DEBUG_TEMPROOT" in environ:
        return
    temproot = cache_temproot(environ)
    try:
        os.makedirs(temproot, exist_ok=True)
    except OSError:
        return
    environ["PYTEST_DEBUG_TEMPROOT"] = temproot


# /tmp is a RAM tmpfs on the shared workstation and parallel worktree suites fill it with
# meshes and renders. pytest reads PYTEST_DEBUG_TEMPROOT lazily (at the first tmp_path), so
# setting it here moves its numbered, concurrency-safe pytest-of-<user>/pytest-N dirs; an
# explicit --basetemp or an exported PYTEST_DEBUG_TEMPROOT still wins.
use_cache_temproot(os.environ)

# The variables git exports to hooks that aim git at a repository (`git rev-parse
# --local-env-vars`). Under the pre-commit hook they point at the worktree being committed, so a
# test running `git init`/`git add` in tmp_path rewrote THAT repo: a linked worktree's GIT_DIR
# made `git init` set core.bare=true in the shared .git/config, and GIT_INDEX_FILE let
# `git add` replace the commit's index (2026-10-14, #130).
GIT_REPO_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)

# at import, so collection-time git calls are covered too
for _name in GIT_REPO_ENV:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _isolate_git_repo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's git calls find their repo from their cwd, never from an inherited (or
    test-leaked) GIT_DIR/GIT_INDEX_FILE aimed at a real checkout."""
    for name in GIT_REPO_ENV:
        monkeypatch.delenv(name, raising=False)



@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers() -> int:
    """`pytest -n auto` would start one worker per core; on a shared machine use max_workers()."""
    return max_workers()
