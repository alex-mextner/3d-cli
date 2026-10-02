#!/usr/bin/env python3
"""Install the tracked warning-only pre-push shim without replacing gate logic.

Accessed via: ``python scripts/install_pre_push_hook.py`` from a checkout before
its first push. The shim is copied to the current worktree's Git hooks directory
and, when distinct, the common Git hooks directory. This supports both the
installed global composer variants.

Assumptions: Git can resolve the worktree's and common Git directories; an
existing hook may be a dispatcher or repository gate and is copied to the stable
``pre-push.previous`` chain target before replacement.

Past bugs: linked worktrees and local/global hook composers can select different
Git hook directories. This installer provisions both locations without replacing
either existing chain.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or "git command failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _configured_hooks_path(root: Path, scope: str) -> Path | None:
    result = subprocess.run(
        ["git", "config", scope, "--get", "core.hooksPath"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    configured = Path(result.stdout.strip()).expanduser()
    return (root / configured).resolve() if not configured.is_absolute() else configured.resolve()


def _local_hooks_path(root: Path) -> Path | None:
    return _configured_hooks_path(root, "--worktree") or _configured_hooks_path(root, "--local")


def _report_global_hooks_path(root: Path, hook_dirs: list[Path]) -> None:
    configured_hooks = _configured_hooks_path(root, "--global")
    if configured_hooks is None or configured_hooks in hook_dirs:
        return
    destination = configured_hooks / "pre-push"
    if _looks_like_global_composer(destination):
        print(f"[install-pre-push-hook] retained global composer at {destination}")
    else:
        print(f"[install-pre-push-hook] did not modify global core.hooksPath at {configured_hooks}")



def _looks_like_global_composer(destination: Path) -> bool:
    if not destination.is_file():
        return False
    try:
        content = destination.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "rev-parse --absolute-git-dir" in content and "hooks/pre-push" in content
def _looks_like_antislop_shim(destination: Path) -> bool:
    if not destination.is_file():
        return False
    try:
        content = destination.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "outgoing_antislop.py" in content and "WARN anti-slop" in content





def _numbered_backup_path(destination: Path) -> Path:
    candidate = destination.with_name(f"{destination.name}.bak")
    index = 1
    while candidate.exists():
        candidate = destination.with_name(f"{destination.name}.bak.{index}")
        index += 1
    return candidate


def _install_destination(destination: Path, source: Path, source_bytes: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (
        destination.exists()
        and destination.read_bytes() != source_bytes
        and not _looks_like_antislop_shim(destination)
    ):
        previous = destination.with_name("pre-push.previous")
        if not previous.exists():
            shutil.copy2(destination, previous)
            print(f"[install-pre-push-hook] preserved existing hook at {previous}")
        else:
            backup = _numbered_backup_path(destination)
            shutil.copy2(destination, backup)
            print(f"[install-pre-push-hook] preserved additional hook at {backup}")
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_bytes(source_bytes)
    temporary.chmod((source.stat().st_mode & 0o777) | 0o755)
    os.replace(temporary, destination)
    print(f"[install-pre-push-hook] installed {destination}")


def install(repo: Path) -> Path:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    source = root / "scripts" / "hooks" / "pre-push"
    if not source.is_file():
        raise RuntimeError(f"tracked hook source missing: {source}")
    worktree_hooks = (Path(_git(root, "rev-parse", "--absolute-git-dir")) / "hooks").resolve()
    common = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (root / common).resolve()
    hook_dirs = [worktree_hooks]
    common_hooks = (common / "hooks").resolve()
    if common_hooks != worktree_hooks:
        hook_dirs.append(common_hooks)
    configured_hooks = _local_hooks_path(root)
    if configured_hooks is not None and configured_hooks not in hook_dirs:
        hook_dirs.append(configured_hooks)
    source_bytes = source.read_bytes()
    _report_global_hooks_path(root, hook_dirs)
    for hook_dir in hook_dirs:
        _install_destination(hook_dir / "pre-push", source, source_bytes)
    return worktree_hooks / "pre-push"


def main() -> int:
    try:
        install(Path.cwd())
    except (OSError, RuntimeError) as exc:
        print(f"[install-pre-push-hook] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
