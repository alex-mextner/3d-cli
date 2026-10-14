"""tests/conftest.py: pytest tmp dirs live under the user cache, not the /tmp RAM tmpfs, and
no test's git call can reach a real checkout through a git hook's GIT_* environment."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import GIT_REPO_ENV, cache_temproot, use_cache_temproot

_TESTS_DIR = Path(__file__).resolve().parent

# What a test innocently does under the pre-commit hook: set up a scratch repo in tmp_path.
_SCRATCH_REPO_TEST = (
    "import subprocess\n"
    "def test_scratch_repo(tmp_path):\n"
    "    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)\n"
)


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _checkout_with_linked_worktree(root: Path) -> tuple[Path, Path]:
    """A throwaway repo plus a linked worktree, like ~/xp/3d-cli and its .worktrees/<name>;
    returns (main checkout, the worktree's gitdir that the hook exports as GIT_DIR)."""
    main = root / "main"
    main.mkdir(parents=True)
    _git(main, "init", "-q")
    (main / "f.txt").write_text("x\n", encoding="utf-8")
    _git(main, "add", "f.txt")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    _git(main, "worktree", "add", "-q", "-b", "wt", str(root / "wt"))
    return main, main / ".git" / "worktrees" / "wt"


def _run_scratch_test_under_hook_env(root: Path, gitdir: Path, *pytest_args: str) -> None:
    inner = root / "inner"
    inner.mkdir()
    (inner / "test_scratch.py").write_text(_SCRATCH_REPO_TEST, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_DIR": str(gitdir), "GIT_INDEX_FILE": str(gitdir / "index"),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(_TESTS_DIR), os.environ.get("PYTHONPATH")])),
    }
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *pytest_args, str(inner)],
        cwd=inner, env=env, capture_output=True, text=True, check=False,
    )
    # the scratch test (and so its `git init`) must really have run, or the checks prove nothing
    assert done.returncode == 0 and "1 passed" in done.stdout, done.stdout + done.stderr


def test_without_the_scrub_a_hook_git_dir_turns_the_real_checkout_bare(tmp_path: Path) -> None:
    # control: proves the scenario below is real, so its passing assertion means something
    main, gitdir = _checkout_with_linked_worktree(tmp_path)
    _run_scratch_test_under_hook_env(tmp_path, gitdir)
    assert _git(main, "config", "core.bare") == "true"


def test_conftest_keeps_a_hook_git_dir_away_from_the_real_checkout(tmp_path: Path) -> None:
    main, gitdir = _checkout_with_linked_worktree(tmp_path)
    _run_scratch_test_under_hook_env(tmp_path, gitdir, "-p", "conftest")
    assert _git(main, "config", "core.bare") == "false"
    assert _git(tmp_path / "wt", "ls-files") == "f.txt"  # the worktree's index is untouched


def test_no_repo_locating_git_variable_reaches_a_test() -> None:
    assert not [name for name in GIT_REPO_ENV if name in os.environ]


def test_temproot_honours_an_absolute_xdg_cache_home(tmp_path: Path) -> None:
    assert cache_temproot({"XDG_CACHE_HOME": str(tmp_path)}) == str(tmp_path / "3d-cli" / "pytest")


@pytest.mark.parametrize("xdg", [None, "", "relative/cache"])
def test_temproot_falls_back_to_home_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, xdg: str | None) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    environ = {} if xdg is None else {"XDG_CACHE_HOME": xdg}
    assert cache_temproot(environ) == str(tmp_path / ".cache" / "3d-cli" / "pytest")


def test_usable_cache_dir_is_created_and_exported(tmp_path: Path) -> None:
    environ = {"XDG_CACHE_HOME": str(tmp_path)}
    use_cache_temproot(environ)
    assert environ["PYTEST_DEBUG_TEMPROOT"] == str(tmp_path / "3d-cli" / "pytest")
    assert (tmp_path / "3d-cli" / "pytest").is_dir()


def test_unusable_cache_dir_leaves_pytest_on_the_system_temp_dir(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    environ = {"XDG_CACHE_HOME": str(blocker)}
    use_cache_temproot(environ)  # makedirs under a regular file fails; must not raise
    assert "PYTEST_DEBUG_TEMPROOT" not in environ


def test_an_exported_temproot_wins(tmp_path: Path) -> None:
    environ = {"PYTEST_DEBUG_TEMPROOT": "/elsewhere", "XDG_CACHE_HOME": str(tmp_path)}
    use_cache_temproot(environ)
    assert environ["PYTEST_DEBUG_TEMPROOT"] == "/elsewhere"
    assert not (tmp_path / "3d-cli").exists()


def test_tmp_path_is_created_under_the_session_temproot(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    if request.config.option.basetemp:
        pytest.skip("an explicit --basetemp overrides the temproot")
    temproot = Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).resolve()
    assert temproot in tmp_path.resolve().parents
