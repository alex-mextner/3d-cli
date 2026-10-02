from __future__ import annotations

import os
import subprocess
import shutil
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_ANALYZER = _ROOT / "scripts" / "outgoing_antislop.py"


def _git(cwd: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _repo_env(home: Path) -> dict[str, str]:
    runner = home / ".config" / "git" / "run-global-hooks"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(
        "#!/bin/sh\n"
        "[ -z \"$GLOBAL_HOOKS_DISPATCH_MARKER\" ] || : > \"$GLOBAL_HOOKS_DISPATCH_MARKER\"\n"
        ": >/dev/null\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.test",
        }
    )
    return env


def _commit(repo: Path, message: str, env: dict[str, str]) -> None:
    assert _git(repo, "add", "-A", env=env).returncode == 0
    assert _git(repo, "commit", "-m", message, env=env).returncode == 0


def test_real_push_warns_on_committed_blob_and_ignores_unstaged_edit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0

    target = repo / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    _commit(repo, "initial", env)

    # The real pre-push hook invokes the future analyzer with Git's ref-update stdin.
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} {str(_ANALYZER)!r} \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _git(repo, "config", "core.hooksPath", str(hooks), env=env).returncode == 0

    first = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert first.returncode == 0, first.stderr

    target.write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "committed anti-slop candidate", env)
    target.write_text("value = 2\n", encoding="utf-8")

    result = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert result.returncode == 0, result.stderr
    assert "WARN anti-slop" in result.stderr
    assert "sample.py" in result.stderr
    assert "unstaged" not in result.stderr.lower()


def test_new_branch_uses_remote_tracking_baseline_without_scanning_history(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    legacy = repo / "legacy.py"
    legacy.write_text("def legacy() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "main baseline", env)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} {str(_ANALYZER)!r} \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _git(repo, "config", "core.hooksPath", str(hooks), env=env).returncode == 0
    assert _git(repo, "push", "origin", "HEAD:refs/heads/main", env=env).returncode == 0
    assert _git(repo, "fetch", "origin", "main", env=env).returncode == 0
    assert _git(
        repo,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
        env=env,
    ).returncode == 0
    assert _git(repo, "switch", "-c", "topic", env=env).returncode == 0
    candidate = repo / "candidate.py"
    candidate.write_text("def candidate() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "topic candidate", env)
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "candidate.py" in pushed.stderr
    assert "legacy.py" not in pushed.stderr

    already_remote = _git(repo, "push", "origin", "HEAD:refs/heads/already-remote", env=env)
    assert already_remote.returncode == 0, already_remote.stderr
    assert "candidate.py" not in already_remote.stderr

    assert _git(repo, "switch", "--orphan", "orphan", env=env).returncode == 0
    orphan = repo / "orphan.py"
    orphan.write_text("def orphan() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "orphan candidate", env)
    orphan_push = _git(repo, "push", "origin", "HEAD:refs/heads/orphan", env=env)
    assert orphan_push.returncode == 0, orphan_push.stderr
    assert "orphan.py" in orphan_push.stderr


def test_body_only_placeholder_change_is_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    target = repo / "sample.py"
    target.write_text("def pending() -> None:\n    return None\n", encoding="utf-8")
    _commit(repo, "initial", env)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} {str(_ANALYZER)!r} \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _git(repo, "config", "core.hooksPath", str(hooks), env=env).returncode == 0
    assert _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env).returncode == 0
    target.write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "body-only placeholder", env)
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "no-placeholder-pass" in pushed.stderr


def test_sha256_repository_ref_updates_are_supported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", "--object-format=sha256", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", "--object-format=sha256", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    (repo / "sample.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "sha256 candidate", env)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} {str(_ANALYZER)!r} \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _git(repo, "config", "core.hooksPath", str(hooks), env=env).returncode == 0
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "no-placeholder-pass" in pushed.stderr


def test_installer_wires_local_core_hooks_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    local_hooks = repo / "hooks"
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    (repo / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    assert _git(repo, "config", "core.hooksPath", "hooks", env=env).returncode == 0
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    assert (local_hooks / "pre-push").is_file()
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "candidate.py" in pushed.stderr



def test_installer_refuses_ambiguous_existing_predecessor_chain(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    destination = repo / ".git" / "hooks" / "pre-push"
    previous = repo / ".git" / "hooks" / "pre-push.previous"
    destination.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    previous.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    destination.chmod(0o755)
    previous.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "unsupported" in result.stderr
    assert destination.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    assert previous.read_text(encoding="utf-8") == "#!/bin/sh\nexit 23\n"


def test_installer_refuses_nonexecutable_predecessor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    destination = repo / ".git" / "hooks" / "pre-push"
    destination.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    before = destination.read_bytes()
    result = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "not executable" in result.stderr
    assert destination.read_bytes() == before


def test_installer_refuses_unrecorded_global_hooks_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[core]\n\thooksPath = global-hooks\n", encoding="utf-8")
    env = _repo_env(home)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    result = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "global core.hooksPath" in result.stderr
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_linked_worktree_hooks_path_is_wired(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    (repo / "seed.py").write_text("value = 1\n", encoding="utf-8")
    _commit(repo, "initial", env)
    linked = tmp_path / "linked"
    assert _git(repo, "worktree", "add", "-b", "linked", str(linked), env=env).returncode == 0
    scripts = linked / "scripts"
    (scripts / "hooks").mkdir(parents=True)
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", scripts / "hooks" / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    assert _git(linked, "config", "extensions.worktreeConfig", "true", env=env).returncode == 0
    assert _git(linked, "config", "--worktree", "core.hooksPath", "worktree-hooks", env=env).returncode == 0
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=linked,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    assert (linked / "worktree-hooks" / "pre-push").is_file()
    (linked / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    assert _git(linked, "add", "candidate.py", env=env).returncode == 0
    assert _git(linked, "commit", "-m", "candidate", env=env).returncode == 0
    pushed = _git(linked, "push", "origin", "HEAD:refs/heads/linked", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "candidate.py" in pushed.stderr


def test_real_push_handles_spaces_renames_deletes_multiref_and_ref_deletion(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    (repo / "old.py").write_text(
        "def old() -> None:\n"
        "    value = 1\n"
        "    value = 2\n"
        "    value = 3\n",
        encoding="utf-8",
    )
    (repo / "deleted.py").write_text("def deleted() -> None:\n    pass\n", encoding="utf-8")
    (repo / "legacy.py").write_text("value = 1  # type: ignore\n", encoding="utf-8")
    (repo / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    _commit(repo, "initial", env)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} {str(_ANALYZER)!r} \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _git(repo, "config", "core.hooksPath", str(hooks), env=env).returncode == 0
    initial = _git(
        repo,
        "push",
        "origin",
        "HEAD:refs/heads/topic",
        "HEAD:refs/heads/other",
        env=env,
    )
    assert initial.returncode == 0, initial.stderr

    (repo / "old.py").rename(repo / "renamed file.py")
    (repo / "legacy.py").rename(repo / "renamed legacy.py")
    (repo / "broken.py").rename(repo / "renamed broken.py")
    (repo / "renamed file.py").write_text(
        "def changed() -> None:\n    pass\n",
        encoding="utf-8",
    )
    (repo / "deleted.py").unlink()
    _commit(repo, "rename and delete", env)
    assert _git(repo, "tag", "-a", "v1", "-m", "annotated", env=env).returncode == 0
    updated = _git(
        repo,
        "push",
        "origin",
        "HEAD:refs/heads/topic",
        "HEAD:refs/heads/other",
        "refs/tags/v1",
        env=env,
    )
    assert updated.returncode == 0, updated.stderr
    assert "renamed file.py" in updated.stderr
    assert "renamed legacy.py" not in updated.stderr
    assert "renamed broken.py" not in updated.stderr
    assert "deleted.py" not in updated.stderr
    assert "not a commit" in updated.stderr

    deleted = _git(repo, "push", "origin", ":refs/heads/other", env=env)
    assert deleted.returncode == 0, deleted.stderr
    assert "ref deletion has no outgoing blob" in deleted.stderr

def test_multiref_analysis_unions_changed_lines_for_one_blob(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    sample = repo / "sample.py"
    sample.write_text("value = 1\n", encoding="utf-8")
    _commit(repo, "initial", env)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nexec {sys.executable} {str(_ANALYZER)!r} \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    assert _git(repo, "config", "core.hooksPath", str(hooks), env=env).returncode == 0
    assert _git(
        repo,
        "push",
        "origin",
        "HEAD:refs/heads/topic",
        "HEAD:refs/heads/other",
        env=env,
    ).returncode == 0

    sample.write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "intermediate", env)
    intermediate = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert intermediate.returncode == 0, intermediate.stderr
    assert "no-placeholder-pass" in intermediate.stderr

    sample.write_text("def pending() -> None:\n    pass\nvalue = 2\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    final = _git(
        repo,
        "push",
        "origin",
        "HEAD:refs/heads/topic",
        "HEAD:refs/heads/other",
        env=env,
    )
    assert final.returncode == 0, final.stderr
    assert "no-placeholder-pass" in final.stderr



def test_unlaunchable_preserved_gate_blocks_push(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    stale_marker = tmp_path / "stale-dispatch.marker"
    stale_marker.write_text("old", encoding="utf-8")
    os.utime(stale_marker, (0, 0))
    env["GLOBAL_HOOKS_DISPATCH_MARKER"] = str(stale_marker)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    existing = repo / ".git" / "hooks" / "pre-push"
    existing.write_text("#!/definitely/missing/interpreter\n", encoding="utf-8")
    existing.chmod(0o755)
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    gate = subprocess.run(
        [str(repo / ".git" / "hooks" / "pre-push")],
        cwd=repo,
        env=env,
        input=b"",
        capture_output=True,
        check=False,
    )
    assert gate.returncode == 127
    assert b"ERROR pre-push predecessor unavailable" in gate.stderr

def test_recorded_predecessor_missing_or_dangling_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    existing = repo / ".git" / "hooks" / "pre-push"
    existing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    existing.chmod(0o755)
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    previous = repo / ".git" / "hooks" / "pre-push.previous"
    assert previous.is_file()
    metadata_path = repo / ".git" / "hooks" / "pre-push.3d-antislop.json"
    assert '"predecessor": "pre-push.previous"' in metadata_path.read_text(encoding="utf-8")
    before = _git(tmp_path, "ls-remote", str(remote), env=env).stdout
    previous.unlink()
    (repo / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode != 0
    assert "candidate.py" not in pushed.stderr
    assert "ERROR" in pushed.stderr
    after = _git(tmp_path, "ls-remote", str(remote), env=env).stdout
    assert before == after
    assert "refs/heads/topic" not in after

    previous.symlink_to(repo / ".git" / "hooks" / "nonexistent-target")
    dangling = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert dangling.returncode != 0
    after_dangling = _git(tmp_path, "ls-remote", str(remote), env=env).stdout
    assert before == after_dangling


def test_missing_or_malformed_installation_metadata_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    metadata_path = repo / ".git" / "hooks" / "pre-push.3d-antislop.json"
    assert metadata_path.is_file()
    before = _git(tmp_path, "ls-remote", str(remote), env=env).stdout
    metadata_path.unlink()
    (repo / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode != 0
    assert "candidate.py" not in pushed.stderr
    assert "ERROR" in pushed.stderr
    after = _git(tmp_path, "ls-remote", str(remote), env=env).stdout
    assert before == after

    metadata_path.write_text("{not json", encoding="utf-8")
    malformed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert malformed.returncode != 0
    after_malformed = _git(tmp_path, "ls-remote", str(remote), env=env).stdout
    assert before == after_malformed


def test_recorded_no_predecessor_allows_normal_push_and_advisory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    metadata_path = repo / ".git" / "hooks" / "pre-push.3d-antislop.json"
    metadata = metadata_path.read_text(encoding="utf-8")
    assert '"predecessor": null' in metadata
    assert '"predecessor_sha256": null' in metadata
    assert not (repo / ".git" / "hooks" / "pre-push.previous").exists()
    (repo / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert "candidate.py" in pushed.stderr



def test_preserved_dispatcher_is_not_run_twice(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    xdg = home / ".config"
    runner_dir = xdg / "git"
    runner_dir.mkdir(parents=True, exist_ok=True)
    runner = runner_dir / "run-global-hooks"
    runner_log = tmp_path / "runner.log"
    dispatch_gate = tmp_path / "dispatch-gate.marker"
    runner.write_text(
        "#!/bin/sh\n"
        "[ -z \"$GLOBAL_HOOKS_DISPATCH_MARKER\" ] || : > \"$GLOBAL_HOOKS_DISPATCH_MARKER\"\n"
        f"printf '%s\\n' \"$1\" >> {str(runner_log)!r}\n"
        ": >/dev/null\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    env["XDG_CONFIG_HOME"] = str(xdg)
    env["DISPATCH_GATE"] = str(dispatch_gate)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    existing = repo / ".git" / "hooks" / "pre-push"
    existing.write_text(
        '#!/bin/sh\n'
        '# global-git-hooks-dispatcher\n'
        '"${XDG_CONFIG_HOME}/git/run-global-hooks" pre-push "$@" && '
        'printf gate > "$DISPATCH_GATE" && exit 23\n',
        encoding="utf-8",
    )
    existing.chmod(0o755)
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    dispatch_marker = tmp_path / f"global-hooks-dispatched.pre-push.{os.getpid()}"
    env["GLOBAL_HOOKS_DISPATCH_MARKER"] = str(dispatch_marker)
    invoked = subprocess.run(
        [str(repo / ".git" / "hooks" / "pre-push")],
        cwd=repo,
        env=env,
        input=b"",
        capture_output=True,
        check=False,
    )
    assert invoked.returncode == 23
    assert dispatch_gate.read_text(encoding="utf-8") == "gate"
    assert dispatch_marker.is_file()
    assert runner_log.read_text(encoding="utf-8").splitlines() == ["pre-push"]




def test_installer_preserves_existing_hook_and_installed_shim_runs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    existing = repo / ".git" / "hooks" / "pre-push"
    gate_marker = tmp_path / "gate.marker"
    env["GATE_MARKER"] = str(gate_marker)
    existing.write_text(
        '#!/bin/sh\nprintf gate > "$GATE_MARKER"\nexit 23\n',
        encoding="utf-8",
    )
    existing.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert installed == (hooks / "pre-push").read_text(encoding="utf-8")
    previous = repo / ".git" / "hooks" / "pre-push.previous"
    assert previous.read_text(encoding="utf-8") == '#!/bin/sh\nprintf gate > "$GATE_MARKER"\nexit 23\n'
    metadata = (repo / ".git" / "hooks" / "pre-push.3d-antislop.json").read_text(encoding="utf-8")
    assert '"predecessor": "pre-push.previous"' in metadata
    remote = tmp_path / "remote.git"
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    (repo / "candidate.py").write_text("value = 1\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode != 0
    assert gate_marker.read_text(encoding="utf-8") == "gate"
    second = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert not (repo / ".git" / "hooks" / "pre-push.bak").exists()



def test_installer_ignores_inherited_git_overrides(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(tmp_path, "init", str(other), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    other_config = (other / ".git" / "config").read_bytes()
    poisoned = env | {
        "GIT_DIR": str(other / ".git"),
        "GIT_WORK_TREE": str(other),
        "GIT_INDEX_FILE": str(other / ".git" / "index"),
        "GIT_CONFIG_GLOBAL": str(other / ".git" / "config"),
    }
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=poisoned,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    assert (repo / ".git" / "hooks" / "pre-push").is_file()
    assert (other / ".git" / "config").read_bytes() == other_config


def test_installed_shim_runs_global_dispatcher_and_advisory_stage(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    xdg = home / ".config"
    runner_dir = xdg / "git"
    runner_dir.mkdir(parents=True, exist_ok=True)
    runner_log = tmp_path / "runner.log"
    runner = runner_dir / "run-global-hooks"
    runner.write_text(
        "#!/bin/sh\n"
        "[ -z \"$GLOBAL_HOOKS_DISPATCH_MARKER\" ] || : > \"$GLOBAL_HOOKS_DISPATCH_MARKER\"\n"
        f"printf '%s\\n' \"$1\" >> {str(runner_log)!r}\n"
        ": >/dev/null\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    env["XDG_CONFIG_HOME"] = str(xdg)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "hooks"
    scripts.mkdir(parents=True)
    hooks.mkdir()
    assert _git(tmp_path, "init", "--bare", str(remote), env=env).returncode == 0
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    shutil.copy2(_ROOT / "scripts" / "outgoing_antislop.py", scripts / "outgoing_antislop.py")
    shutil.copy2(_ROOT / "scripts" / "hooks" / "pre-push", hooks / "pre-push")
    shutil.copy2(_ROOT / "scripts" / "install_pre_push_hook.py", scripts / "install_pre_push_hook.py")
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(remote), env=env).returncode == 0
    (repo / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    predecessor = repo / ".git" / "hooks" / "pre-push"
    predecessor.write_text(
        '#!/bin/sh\n"${XDG_CONFIG_HOME}/git/run-global-hooks" pre-push "$@" || exit $?\nexit 0\n',
        encoding="utf-8",
    )
    predecessor.chmod(0o755)
    installed = subprocess.run(
        [sys.executable, str(scripts / "install_pre_push_hook.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/topic", env=env)
    assert pushed.returncode == 0, pushed.stderr
    assert runner_log.read_text(encoding="utf-8").splitlines() == ["pre-push"]
    assert "WARN anti-slop" in pushed.stderr
    assert "candidate.py" in pushed.stderr


def test_missing_ruff_is_an_explicit_nonblocking_warning(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _repo_env(home)
    git_path = shutil.which("git")
    assert git_path is not None
    path_bin = tmp_path / "bin"
    path_bin.mkdir()
    (path_bin / "git").symlink_to(git_path)
    env["PATH"] = str(path_bin)
    repo = tmp_path / "repo"
    assert _git(tmp_path, "init", str(repo), env=env).returncode == 0
    assert _git(repo, "config", "user.name", "Test Author", env=env).returncode == 0
    assert _git(repo, "config", "user.email", "author@example.test", env=env).returncode == 0
    (repo / "candidate.py").write_text("def pending() -> None:\n    pass\n", encoding="utf-8")
    _commit(repo, "candidate", env)
    local_sha = _git(repo, "rev-parse", "HEAD", env=env).stdout.strip()
    payload = (
        f"refs/heads/topic {local_sha} refs/heads/topic {'0' * 40}\n"
    ).encode()
    result = subprocess.run(
        [sys.executable, str(_ANALYZER)],
        cwd=repo,
        env=env,
        input=payload,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    stderr = result.stderr.decode("utf-8")
    assert "analyzer dependency 'ruff' is missing" in stderr
    assert "WARN anti-slop" in stderr
