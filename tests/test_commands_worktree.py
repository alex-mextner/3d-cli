from __future__ import annotations

import subprocess
from typing import Any

import pytest

from commands import worktree
from errors import InvalidArgument, MissingDependency, UsageError


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["cmd"], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "failed") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["cmd"], 1, stdout="", stderr=stderr)


def test_worktree_help() -> None:
    assert worktree.run(["--help"]) == 0


def test_worktree_no_args_prints_usage() -> None:
    assert worktree.run([]) == 1


def test_worktree_create_bootstraps_dev_env(monkeypatch: Any, tmp_path: Any) -> None:
    calls: list[tuple[str, list[str], str]] = []
    target = tmp_path / "agent"

    def fake_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(("git", args, ""))
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            return _fail()
        return _ok()

    def fake_run_in(path: Any, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(("run", args, str(path)))
        (target / ".venv" / "bin").mkdir(parents=True)
        for tool in worktree.DEV_TOOLS:
            (target / ".venv" / "bin" / tool).write_text("")
        return _ok()

    monkeypatch.setattr(worktree, "_run_git", fake_git)
    monkeypatch.setattr(worktree, "_run_in", fake_run_in)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    assert worktree.run(["create", "roadmap/demo", "--path", str(target), "--base", "main"]) == 0

    assert ("git", ["worktree", "add", "-b", "roadmap/demo", str(target), "main"], "") in calls
    assert ("run", ["/usr/bin/uv", "sync", "--extra", "dev"], str(target)) in calls


def test_worktree_create_resolves_relative_path(monkeypatch: Any, tmp_path: Any) -> None:
    calls: list[tuple[str, list[str], str]] = []
    monkeypatch.chdir(tmp_path)
    target = (tmp_path / "relative-agent").resolve()

    def fake_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(("git", args, ""))
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            return _fail()
        return _ok()

    def fake_run_in(path: Any, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(("run", args, str(path)))
        (target / ".venv" / "bin").mkdir(parents=True)
        for tool in worktree.DEV_TOOLS:
            (target / ".venv" / "bin" / tool).write_text("")
        return _ok()

    monkeypatch.setattr(worktree, "_run_git", fake_git)
    monkeypatch.setattr(worktree, "_run_in", fake_run_in)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    assert worktree.run(["create", "roadmap/demo", "--path", "relative-agent"]) == 0

    assert ("git", ["worktree", "add", "-b", "roadmap/demo", str(target), "main"], "") in calls
    assert ("run", ["/usr/bin/uv", "sync", "--extra", "dev"], str(target)) in calls


def test_worktree_create_fails_when_sync_leaves_missing_tools(monkeypatch: Any, tmp_path: Any) -> None:
    target = tmp_path / "agent"
    monkeypatch.setattr(worktree, "_run_git", lambda args: _fail() if args[0] == "rev-parse" else _ok())
    monkeypatch.setattr(worktree, "_run_in", lambda path, args: _ok())
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    with pytest.raises(UsageError):
        worktree.run(["create", "roadmap/demo", "--path", str(target)])


def test_worktree_create_no_sync_allows_missing_tools(monkeypatch: Any, tmp_path: Any) -> None:
    target = tmp_path / "agent"
    monkeypatch.setattr(worktree, "_run_git", lambda args: _fail() if args[0] == "rev-parse" else _ok())

    assert worktree.run(["create", "roadmap/demo", "--path", str(target), "--no-sync"]) == 0


def test_worktree_create_existing_path_rejected(tmp_path: Any) -> None:
    with pytest.raises(InvalidArgument):
        worktree.run(["create", "roadmap/demo", "--path", str(tmp_path)])


def test_worktree_create_requires_uv(monkeypatch: Any, tmp_path: Any) -> None:
    calls: list[list[str]] = []

    def fake_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _ok()

    monkeypatch.setattr(worktree, "_run_git", fake_git)
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(MissingDependency):
        worktree.run(["create", "roadmap/demo", "--path", str(tmp_path / "agent")])

    assert calls == []


def test_worktree_doctor_reports_missing_tools(tmp_path: Any) -> None:
    assert worktree.run(["doctor", str(tmp_path)]) == 1


def test_worktree_doctor_accepts_ready_venv(tmp_path: Any) -> None:
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in worktree.DEV_TOOLS:
        (bin_dir / tool).write_text("")

    assert worktree.run(["doctor", str(tmp_path), "--json"]) == 0


def test_worktree_list_json(monkeypatch: Any) -> None:
    output = (
        "worktree /repo\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo-agent\n"
        "HEAD def\n"
        "branch refs/heads/roadmap/demo\n"
        "\n"
    )
    monkeypatch.setattr(worktree, "_run_git", lambda args: _ok(output))

    assert worktree.run(["list", "--json"]) == 0


def test_worktree_unknown_subcommand() -> None:
    with pytest.raises(UsageError):
        worktree.run(["bogus"])
