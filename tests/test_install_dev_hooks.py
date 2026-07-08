"""Tests for scripts/install-dev-hooks.sh — the repo-dev pre-commit gate installer.

The installer logic lives in bash (dispatcher-block splicing, idempotent re-install,
.bak preservation, --git-common-dir resolution for linked worktrees, exec-bit repair),
so these tests drive the REAL script against a throwaway git repo via subprocess rather
than mocking it away — mocking the shell would exercise none of the regression-prone
logic the issue (#17) asks to cover.

SAFETY (critical): a git-hook installer that inherits the host's `core.hooksPath` /
`GIT_CONFIG_GLOBAL` can read or clobber the developer's real global hook
(~/.config/git/hooks/pre-commit — a secret-scan + review gate). Every git invocation here
runs through `_git_env()`, which points HOME / GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM at
throwaway temp files and unsets core.hooksPath, so neither the script nor our own setup
ever touches the host config or the host hooks dir. The script operates solely inside the
per-test temp repo created under pytest's tmp_path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALLER = _REPO_ROOT / "scripts" / "install-dev-hooks.sh"
_TRACKED_HOOK = _REPO_ROOT / "scripts" / "hooks" / "pre-commit"

# The dispatcher block exactly as the global composer (agent-tools
# git-hooks/global-dispatcher/install-local-hooks.sh, inject_raw) prepends it into a raw
# .git/hooks/pre-commit: a marker comment line + a guarded call line ending in
# `|| exit $?`. It is a flat two-line block (NOT a for/done loop), so the spliced result
# is valid, executable bash — which the splice tests below assert by RUNNING it.
_DISPATCHER_BLOCK = (
    "# global-git-hooks-dispatcher — runs every global hook for this event\n"
    '"${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks" pre-commit "$@" || exit $?\n'
)
# Same marker, but truncated before the `|| exit $?` terminator. The extractor must emit
# NOTHING for this (no terminator -> no block), so the installer does not splice a partial
# stranger's prefix on top of the tracked source.
_DISPATCHER_NO_TERMINATOR = (
    "# global-git-hooks-dispatcher — runs every global hook for this event\n"
    '"${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks" pre-commit "$@"\n'
)


def _git_env(home: Path) -> dict[str, str]:
    """A git environment fully isolated from the host.

    Pins HOME and the global/system config files at throwaway paths, so neither git nor
    the installer can read or write the developer's real ~/.gitconfig or
    ~/.config/git/hooks. Without this, the installer's `git config --get core.hooksPath`
    would see the host value and could mis-resolve, and a stray git write could land in
    the host hooks dir. (core.hooksPath itself is unset per-repo in `_make_repo`.)

    XDG_CONFIG_HOME is pinned under the throwaway HOME too: the spliced dispatcher line
    resolves run-global-hooks via `${XDG_CONFIG_HOME:-$HOME/.config}`, so leaving the host
    value would make the end-to-end splice test either miss its stub (exit 127) or — worse
    — execute the host's REAL run-global-hooks, defeating the isolation this docstring
    promises.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_CONFIG_GLOBAL"] = str(home / "gitconfig")
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    # Belt-and-suspenders: some git builds ignore GIT_CONFIG_SYSTEM; NOSYSTEM is honored
    # universally, so the host /etc/gitconfig can never leak a core.hooksPath in either.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Stop repo discovery (`git rev-parse --show-toplevel`/`--git-common-dir`) from walking
    # ABOVE the throwaway tree: if TMPDIR happens to sit inside someone's working tree (CI,
    # custom $TMPDIR), discovery would otherwise climb out and pick up a foreign repo —
    # breaking the outside-a-repo test and risking host-config bleed.
    env["GIT_CEILING_DIRECTORIES"] = str(home.parent)
    # Scrub inherited GIT_* that would (a) pin git to a foreign repo regardless of cwd, or
    # (b) inject arbitrary config — including a core.hooksPath — past NOSYSTEM and the
    # pinned global file. The docstring promises no host core.hooksPath can leak; these are
    # the remaining channels through which it could.
    for var in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        # GIT_TEMPLATE_DIR seeds every `git init` with template files — including a
        # hooks/pre-commit. Left set, the first install would find a foreign pre-commit,
        # back it up (breaking the "no .bak" idempotency assertion), and a failing/hanging
        # template hook could even run during the linked-worktree seed commit. (We also
        # pass `--template=` to git init below so a global init.templateDir can't seed.)
        "GIT_TEMPLATE_DIR",
    ):
        env.pop(var, None)
    # GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> pairs (consumed via GIT_CONFIG_COUNT) can
    # also smuggle config; drop every one rather than guess the count.
    for key in [k for k in env if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
        env.pop(key, None)
    return env


def _run_git(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True,
    )


def _make_repo(root: Path) -> tuple[Path, dict[str, str]]:
    """Create an isolated temp git repo with the installer + tracked hook copied in.

    Returns (repo_path, git_env). The repo gets its own config and a copy of
    scripts/install-dev-hooks.sh + scripts/hooks/pre-commit so the script under test runs
    against a real `git rev-parse` without depending on the host checkout's layout.
    """
    home = root / "home"
    home.mkdir()
    repo = root / "repo"
    repo.mkdir()
    env = _git_env(home)

    _run_git(repo, env, "init", "-q", "--template=")
    _run_git(repo, env, "config", "user.email", "test@example.com")
    _run_git(repo, env, "config", "user.name", "Test")
    # Belt-and-suspenders: ensure no hooksPath leaks into the repo config.
    subprocess.run(
        ["git", "config", "--unset-all", "core.hooksPath"],
        cwd=repo, env=env, capture_output=True, text=True, check=False,
    )

    (repo / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy2(_INSTALLER, repo / "scripts" / "install-dev-hooks.sh")
    shutil.copy2(_TRACKED_HOOK, repo / "scripts" / "hooks" / "pre-commit")
    return repo, env


def _install(repo: Path, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the installer from `cwd` (defaults to the repo root)."""
    return subprocess.run(
        ["bash", "scripts/install-dev-hooks.sh"],
        cwd=cwd or repo, env=env, capture_output=True, text=True,
    )


def _common_hooks_dir(repo: Path, env: dict[str, str]) -> Path:
    out = _run_git(repo, env, "rev-parse", "--git-common-dir").stdout.strip()
    common = Path(out)
    if not common.is_absolute():
        common = repo / common
    return common / "hooks"


def _assert_valid_bash(hook: Path) -> None:
    """Parse-check the hook with `bash -n` — a splice that drops a `done`/`fi` or leaves a
    half-block would syntax-error here, which a text-only assertion would miss."""
    res = subprocess.run(
        ["bash", "-n", str(hook)], capture_output=True, text=True,
    )
    assert res.returncode == 0, f"spliced hook is not valid bash:\n{res.stderr}"


def _install_fake_dev(
    root: Path,
    env: dict[str, str],
    *,
    exit_code: int = 0,
    marker: Path | None = None,
    run_log: Path | None = None,
    stderr: str = "",
) -> None:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    dev = fake_bin / "dev"
    dev.write_text(
        "#!/bin/sh\n"
        'if [ -n "${DEV_RUN_LOG:-}" ]; then printf "%s\\n" "$*" >> "$DEV_RUN_LOG"; fi\n'
        'if [ -n "${DEV_MARKER:-}" ]; then touch "$DEV_MARKER"; fi\n'
        'if [ -n "${DEV_STDERR:-}" ]; then printf "%s\\n" "$DEV_STDERR" >&2; fi\n'
        'exit "${DEV_EXIT_CODE:-0}"\n'
    )
    dev.chmod(0o755)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["DEV_EXIT_CODE"] = str(exit_code)
    if marker is not None:
        env["DEV_MARKER"] = str(marker)
    else:
        env.pop("DEV_MARKER", None)
    if run_log is not None:
        env["DEV_RUN_LOG"] = str(run_log)
    else:
        env.pop("DEV_RUN_LOG", None)
    if stderr:
        env["DEV_STDERR"] = stderr
    else:
        env.pop("DEV_STDERR", None)


def _restrict_path_to_tools(root: Path, env: dict[str, str], tools: tuple[str, ...]) -> None:
    path_dir = root / "path-bin"
    path_dir.mkdir(exist_ok=True)
    for tool in tools:
        resolved = shutil.which(tool)
        if resolved is None:
            raise AssertionError(f"{tool} must be available to run this hook test")
        link = path_dir / tool
        if not link.exists():
            link.symlink_to(resolved)
    env["PATH"] = str(path_dir)


@pytest.fixture()
def repo_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    return _make_repo(tmp_path)


# --- fresh install -----------------------------------------------------------------

def test_fresh_install_lands_hook(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    res = _install(repo, env)
    assert res.returncode == 0, res.stderr

    dest = _common_hooks_dir(repo, env) / "pre-commit"
    assert dest.exists()
    # With no pre-existing hook, the installed file is byte-identical to the tracked source.
    assert dest.read_text() == _TRACKED_HOOK.read_text()
    assert os.access(dest, os.X_OK)
    assert "installed" in res.stdout


def test_installer_aborts_when_tracked_source_missing(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    (repo / "scripts" / "hooks" / "pre-commit").unlink()
    res = _install(repo, env)
    assert res.returncode == 1
    assert "tracked source missing" in res.stderr


def test_installer_aborts_outside_a_git_repo(tmp_path: Path) -> None:
    # The script's first action is `git rev-parse --show-toplevel`; outside any repo that
    # fails under `set -e`, so the installer must abort non-zero rather than silently
    # writing a hook into the wrong place.
    home = tmp_path / "home"
    home.mkdir()
    env = _git_env(home)
    not_a_repo = tmp_path / "loose"
    (not_a_repo / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy2(_INSTALLER, not_a_repo / "scripts" / "install-dev-hooks.sh")
    shutil.copy2(_TRACKED_HOOK, not_a_repo / "scripts" / "hooks" / "pre-commit")

    res = _install(not_a_repo, env)
    assert res.returncode != 0
    assert "not a git repository" in res.stderr


# --- idempotency -------------------------------------------------------------------

def test_reinstall_is_idempotent(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    assert _install(repo, env).returncode == 0
    dest = _common_hooks_dir(repo, env) / "pre-commit"
    first = dest.read_bytes()

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    assert dest.read_bytes() == first
    assert "already up to date" in res.stdout
    # A clean re-run must not manufacture a backup of an identical file.
    assert not (dest.parent / "pre-commit.bak").exists()


def test_reinstall_repairs_exec_bit_on_up_to_date_branch(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    assert _install(repo, env).returncode == 0
    dest = _common_hooks_dir(repo, env) / "pre-commit"
    # Strip the exec bit; content is already up to date, so this exercises the branch
    # that reports "already up to date" yet still must chmod +x.
    dest.chmod(0o644)
    assert not os.access(dest, os.X_OK)

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    assert "already up to date" in res.stdout
    assert os.access(dest, os.X_OK)


# --- dispatcher-block extraction ---------------------------------------------------

def test_dispatcher_block_is_preserved_on_splice(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    hooks_dir = _common_hooks_dir(repo, env)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-commit"
    tracked_lines = _TRACKED_HOOK.read_text().splitlines(keepends=True)
    # An existing hook = shebang + dispatcher block + a STALE body (an old gate command).
    # The stale body forces the installer down the write branch (rather than "already up
    # to date"), so we can assert the spliced result both keeps the dispatcher prefix AND
    # replaces the body with the current tracked source.
    # Use a DIFFERENT shebang in the existing hook (#!/bin/sh) than the tracked source
    # (#!/bin/bash) so the shebang assertion below actually proves the spliced result takes
    # the TRACKED source's shebang, not whatever the existing hook had.
    stale_body = '#!/bin/sh\nset -e\necho "old gate"\n./bin/3d lint\n'
    existing = stale_body.splitlines(keepends=True)[0] + _DISPATCHER_BLOCK + "\n" + "".join(
        stale_body.splitlines(keepends=True)[1:]
    )
    assert tracked_lines[0] != stale_body.splitlines(keepends=True)[0]  # shebangs differ
    dest.write_text(existing)
    dest.chmod(0o755)

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    out = dest.read_text()
    assert "global-git-hooks-dispatcher" in out  # dispatcher prefix preserved
    assert out.count("global-git-hooks-dispatcher") == 1  # preserved once, not duplicated
    assert "|| exit $?" in out
    assert "dev run test" in out  # body replaced by the current tracked gate
    assert "old gate" not in out  # the stale body is gone
    # Expected layout: shebang from the tracked source, then the dispatcher block, then
    # the tracked body.
    assert out.splitlines()[0] == tracked_lines[0].rstrip("\n")
    assert "preserved the existing global-hooks-dispatcher prefix" in res.stdout

    # The spliced result must be valid, EXECUTABLE bash — not just the right text. A
    # splice that mangled the block (dropped a token, left a half-statement) would parse-
    # fail here even while the substring asserts above passed.
    _assert_valid_bash(dest)
    # End-to-end: run the spliced hook. Provide a no-op run-global-hooks so the dispatcher
    # line's `|| exit $?` succeeds, then the tracked body invokes dev run test.
    # The stub lands at the SAME path the dispatcher line resolves —
    # ${XDG_CONFIG_HOME:-$HOME/.config}/git — which _git_env pins under the throwaway HOME.
    gdir = Path(env["XDG_CONFIG_HOME"]) / "git"
    gdir.mkdir(parents=True, exist_ok=True)
    rgh = gdir / "run-global-hooks"
    rgh.write_text("#!/bin/sh\nexit 0\n")
    rgh.chmod(0o755)
    _install_fake_dev(repo, env)
    run = subprocess.run(["bash", str(dest)], cwd=repo, env=env, capture_output=True, text=True)
    assert run.returncode == 0, f"spliced hook failed to run:\n{run.stdout}\n{run.stderr}"

    # The original (dispatcher prefix + stale body) differed, so it must be preserved in a
    # .bak verbatim — the user's body edits are never silently lost on a splice-rewrite.
    bak = dest.parent / "pre-commit.bak"
    assert bak.exists()
    assert bak.read_text() == existing
    assert "backed up to" in res.stdout


def test_dispatcher_splice_is_idempotent(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    hooks_dir = _common_hooks_dir(repo, env)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-commit"
    tracked_lines = _TRACKED_HOOK.read_text().splitlines(keepends=True)
    existing = tracked_lines[0] + _DISPATCHER_BLOCK + "\n" + "".join(tracked_lines[1:])
    dest.write_text(existing)
    dest.chmod(0o755)

    assert _install(repo, env).returncode == 0
    first = dest.read_bytes()
    _assert_valid_bash(dest)
    # The dispatcher marker must appear EXACTLY once after the first install — a single
    # install that double-spliced would already show two, and a byte-equal second run over
    # that doubled state would still look "idempotent". Pin the count to catch it here.
    assert dest.read_text().count("global-git-hooks-dispatcher") == 1
    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    # Re-running over an already-spliced hook leaves it byte-identical (no double-splice).
    assert dest.read_bytes() == first
    assert dest.read_text().count("global-git-hooks-dispatcher") == 1
    assert "already up to date" in res.stdout


def test_dispatcher_marker_without_terminator_emits_no_block(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    hooks_dir = _common_hooks_dir(repo, env)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-commit"
    # Marker present but the `|| exit $?` terminator is missing: the extractor must emit
    # nothing, so the installer overwrites with the plain tracked source — it must NOT
    # splice a partial stranger's prefix nor a stray tail.
    dest.write_text("#!/bin/bash\n" + _DISPATCHER_NO_TERMINATOR)
    dest.chmod(0o755)

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    out = dest.read_text()
    # Whole file equals the tracked source verbatim: no dispatcher fragment carried over.
    assert out == _TRACKED_HOOK.read_text()
    assert "preserved the existing global-hooks-dispatcher prefix" not in res.stdout
    # The differing original (marker-but-no-terminator) was overwritten, so it must have
    # been preserved in a .bak — the user's file is never silently lost.
    bak = dest.parent / "pre-commit.bak"
    assert bak.exists()
    assert bak.read_text() == "#!/bin/bash\n" + _DISPATCHER_NO_TERMINATOR


# --- .bak preservation -------------------------------------------------------------

def test_existing_differing_hook_is_backed_up(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    hooks_dir = _common_hooks_dir(repo, env)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-commit"
    dest.write_text("#!/bin/sh\necho custom user hook\n")
    dest.chmod(0o755)

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    bak = dest.parent / "pre-commit.bak"
    assert bak.exists()
    assert bak.read_text() == "#!/bin/sh\necho custom user hook\n"
    assert dest.read_text() == _TRACKED_HOOK.read_text()
    assert "backed up to" in res.stdout


def test_existing_bak_is_left_untouched(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    hooks_dir = _common_hooks_dir(repo, env)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-commit"
    dest.write_text("#!/bin/sh\necho second custom hook\n")
    dest.chmod(0o755)
    bak = dest.parent / "pre-commit.bak"
    bak.write_text("#!/bin/sh\necho ORIGINAL precious hook\n")

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    # The pre-existing .bak (the user's true original) must NOT be overwritten.
    assert bak.read_text() == "#!/bin/sh\necho ORIGINAL precious hook\n"
    assert dest.read_text() == _TRACKED_HOOK.read_text()
    assert ".bak already exists" in res.stdout


# --- linked worktree: --git-common-dir resolution ----------------------------------

def test_linked_worktree_installs_into_common_hooks_dir(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    # A linked worktree needs at least one commit on the main repo first.
    (repo / "README.md").write_text("seed\n")
    _run_git(repo, env, "add", "README.md")
    _run_git(repo, env, "commit", "-q", "-m", "seed")

    linked = repo.parent / "linked-wt"
    _run_git(repo, env, "worktree", "add", "-q", str(linked), "-b", "wt-branch")
    # Copy the installer + tracked hook into the linked worktree's tree.
    (linked / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(_INSTALLER, linked / "scripts" / "install-dev-hooks.sh")
    shutil.copy2(_TRACKED_HOOK, linked / "scripts" / "hooks" / "pre-commit")

    res = _install(linked, env, cwd=linked)
    assert res.returncode == 0, res.stderr

    common_hooks = _common_hooks_dir(linked, env)
    dest = common_hooks / "pre-commit"
    assert dest.exists(), f"hook should land in the common hooks dir {common_hooks}"
    # It must NOT have been written into the per-worktree private hooks dir.
    private = repo / ".git" / "worktrees" / "linked-wt" / "hooks" / "pre-commit"
    assert not private.exists()
    assert dest.read_text() == _TRACKED_HOOK.read_text()


# --- core.hooksPath resolution ------------------------------------------------------

def test_custom_repo_hooks_path_still_lands_in_common_dir_and_warns(
    repo_env: tuple[Path, dict[str, str]],
) -> None:
    repo, env = repo_env
    # A repo-level core.hooksPath that redirects git to a custom dir. The installer
    # resolves DEST from --git-common-dir (not from core.hooksPath), so the hook still
    # lands in .git/hooks — but it must warn that the custom path will shadow it.
    (repo / ".githooks").mkdir()
    _run_git(repo, env, "config", "core.hooksPath", ".githooks")

    res = _install(repo, env)
    assert res.returncode == 0, res.stderr
    common_dest = _common_hooks_dir(repo, env) / "pre-commit"
    assert common_dest.exists()
    assert common_dest.read_text() == _TRACKED_HOOK.read_text()
    # The hook was NOT written into the custom dir; git would run from there, so warn.
    assert not (repo / ".githooks" / "pre-commit").exists()
    assert "WARNING: core.hooksPath=.githooks is set" in res.stderr


# --- fail-closed pre-commit hook ---------------------------------------------------

def test_installed_hook_fails_closed_without_dev(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    assert _install(repo, env).returncode == 0
    dest = _common_hooks_dir(repo, env) / "pre-commit"

    # Run the installed hook in a clean checkout that has no dev CLI: it must refuse
    # (fail closed) rather than silently pass.
    bare = repo.parent / "no-dev"
    bare.mkdir()
    _run_git(bare, env, "init", "-q", "--template=")
    _restrict_path_to_tools(bare, env, ("bash", "git"))
    res = subprocess.run(
        ["bash", str(dest)], cwd=bare, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 1
    assert "dev CLI not found" in res.stderr
    assert "git hooks can find it on PATH" in res.stderr


def test_installed_hook_runs_dev_run_test_when_present(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    assert _install(repo, env).returncode == 0
    dest = _common_hooks_dir(repo, env) / "pre-commit"

    # A repo whose dev CLI records that it was invoked: the hook must call it.
    runner = repo.parent / "with-dev"
    runner.mkdir()
    _run_git(runner, env, "init", "-q", "--template=")
    marker = runner / "ran"
    run_log = runner / "dev.log"
    _install_fake_dev(runner, env, marker=marker, run_log=run_log)
    # Stage a change so the run mirrors a real `git commit` (the hook fires with an index
    # populated), even though this hook gates on `dev run test`, not on staged content.
    (runner / "file.txt").write_text("change\n")
    _run_git(runner, env, "add", "file.txt")

    res = subprocess.run(
        ["bash", str(dest)], cwd=runner, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert marker.exists(), "the installed hook must invoke dev (the dev gate)"
    assert run_log.read_text().strip() == "run test"


def test_installed_hook_blocks_commit_when_gate_fails(repo_env: tuple[Path, dict[str, str]]) -> None:
    repo, env = repo_env
    assert _install(repo, env).returncode == 0
    dest = _common_hooks_dir(repo, env) / "pre-commit"

    # The central gate behavior: dev is present but the dev gate FAILS (non-zero).
    # The hook must propagate that failure so the commit is blocked — a hook that swallowed
    # a failing gate and exited 0 would let broken code land. A stub that always exit 0
    # cannot prove this; this one returns 1.
    runner = repo.parent / "failing-gate"
    runner.mkdir()
    _run_git(runner, env, "init", "-q", "--template=")
    _install_fake_dev(runner, env, exit_code=1, stderr="GATE-FAILED-MARKER")

    res = subprocess.run(
        ["bash", str(dest)], cwd=runner, env=env, capture_output=True, text=True,
    )
    # Pin the exact code AND prove it was the GATE that failed (not a missing-bin / syntax
    # error producing some other non-zero) — the stub's marker must reach stderr.
    assert res.returncode == 1, "a failing dev gate must block the commit (exit 1)"
    assert "GATE-FAILED-MARKER" in res.stderr, "the gate's own failure must have propagated"
