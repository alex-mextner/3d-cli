"""E2e: `3d worktree create` never mistakes a flag for the branch name (#234)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from commands import worktree

from .cli_helper import REPO_ROOT, CliCase, run_3d


def _flag_mutations(flag: str, home: Path) -> list[str]:
    """The branch and default worktree dir (under HOME) the old bug would create for FLAG.

    Only these exact names are checked: other agents share this checkout and
    may add or remove unrelated branches/worktrees concurrently.
    """
    found = []
    branch = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "branch", "--list", "--", flag],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if branch.strip():
        found.append(f"branch {branch.strip()}")
    default_dir = home / worktree.DEFAULT_ROOT.relative_to(Path.home()) / worktree._safe_name(flag)
    if default_dir.exists():
        found.append(f"dir {default_dir}")
    return found


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_worktree_create_help_prints_usage_and_leaves_git_alone(flag: str, tmp_path: Path) -> None:
    """An agent discovering the flags runs `3d worktree create --help` and gets the usage, not a git error."""
    result = run_3d(
        CliCase(
            id=f"worktree-create-{flag}",
            argv=["worktree", "create", flag],
            expected_returncodes=(0,),
            env_extra={"HOME": str(tmp_path)},
        )
    )
    assert result.returncode == 0, result.stderr
    assert "create <branch>" in result.stdout
    assert "invalid reference" not in result.stderr
    assert _flag_mutations(flag, tmp_path) == []


def test_worktree_create_refuses_option_as_branch(tmp_path: Path) -> None:
    """An agent puts an option where the branch goes and gets a usage error before git is touched."""
    result = run_3d(
        CliCase(
            id="worktree-create-dash",
            argv=["worktree", "create", "--base", "main"],
            expected_returncodes=(2,),
            env_extra={"HOME": str(tmp_path)},
        )
    )
    assert result.returncode == 2
    assert "branch name before options" in result.stderr
    assert _flag_mutations("--base", tmp_path) == []
