#!/usr/bin/env python3
"""Install the repository's warning-only pre-push hook without losing its gate.

Accessed via: ``python scripts/install_pre_push_hook.py`` from this checkout.
The installer targets only the effective local/worktree hook directory. On first
installation it records the executable predecessor as ``pre-push.previous`` and
writes adjacent metadata containing both SHA-256 values.

Invariants: no global Git configuration is read or changed; repeat installation
only replaces the recorded shim; unknown or altered layouts fail before mutation.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_METADATA_NAME = "pre-push.3d-antislop.json"
_PREDECESSOR_NAME = "pre-push.previous"


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=_git_env(), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git command failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _git_optional(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=_git_env(), capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _effective_hooks_dir(root: Path) -> Path:
    configured = _git_optional(root, "config", "--worktree", "--get", "core.hooksPath")
    if configured is None:
        configured = _git_optional(root, "config", "--local", "--get", "core.hooksPath")
    if configured is None and _git_optional(root, "config", "--global", "--get", "core.hooksPath") is not None:
        raise RuntimeError("unsupported global core.hooksPath without local or worktree override")
    if configured is not None:
        path = Path(configured).expanduser()
        return (root / path).resolve() if not path.is_absolute() else path.resolve()
    return Path(_git(root, "rev-parse", "--git-path", "hooks")).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata_path(destination: Path) -> Path:
    return destination.with_name(_METADATA_NAME)


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unsupported pre-push installation metadata at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"unsupported pre-push installation metadata at {path}")
    return value


def _validate_recorded_installation(destination: Path, metadata_path: Path) -> None:
    metadata = _read_metadata(metadata_path)
    required = {"version", "shim_sha256", "predecessor", "predecessor_sha256"}
    if set(metadata) != required or metadata["version"] != 1:
        raise RuntimeError(f"unsupported pre-push installation metadata at {metadata_path}")
    if not isinstance(metadata["shim_sha256"], str) or _sha256(destination) != metadata["shim_sha256"]:
        raise RuntimeError("unsupported pre-push layout: installed shim differs from recorded metadata")
    predecessor = metadata["predecessor"]
    predecessor_hash = metadata["predecessor_sha256"]
    if predecessor is None and predecessor_hash is None:
        if destination.with_name(_PREDECESSOR_NAME).exists():
            raise RuntimeError("unsupported pre-push layout: unexpected predecessor file")
        return
    if predecessor != _PREDECESSOR_NAME or not isinstance(predecessor_hash, str):
        raise RuntimeError("unsupported pre-push installation metadata")
    predecessor_path = destination.with_name(_PREDECESSOR_NAME)
    if not predecessor_path.is_file() or _sha256(predecessor_path) != predecessor_hash:
        raise RuntimeError("unsupported pre-push layout: recorded predecessor differs from metadata")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(content)
    temporary.chmod(mode)
    os.replace(temporary, path)


def _install(destination: Path, source: Path) -> None:
    source_bytes = source.read_bytes()
    metadata_path = _metadata_path(destination)
    predecessor_path = destination.with_name(_PREDECESSOR_NAME)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if metadata_path.exists():
        if not destination.is_file():
            raise RuntimeError("unsupported pre-push layout: metadata exists without an installed shim")
        _validate_recorded_installation(destination, metadata_path)
    else:
        if predecessor_path.exists():
            raise RuntimeError("unsupported pre-push layout: predecessor exists without installation metadata")
        if destination.exists():
            if not destination.is_file():
                raise RuntimeError("unsupported pre-push layout: existing hook is not a regular file")
            if not os.access(destination, os.X_OK):
                raise RuntimeError("unsupported pre-push layout: existing hook is not executable")
            if destination.read_bytes() == source_bytes:
                raise RuntimeError("unsupported pre-push layout: unrecorded anti-slop shim")
            shutil.copy2(destination, predecessor_path)
            print(f"[install-pre-push-hook] preserved existing hook at {predecessor_path}")
    _atomic_write(destination, source_bytes, (source.stat().st_mode & 0o777) | 0o755)
    predecessor_hash = _sha256(predecessor_path) if predecessor_path.exists() else None
    metadata = {
        "version": 1,
        "shim_sha256": _sha256(destination),
        "predecessor": _PREDECESSOR_NAME if predecessor_hash is not None else None,
        "predecessor_sha256": predecessor_hash,
    }
    _atomic_write(metadata_path, (json.dumps(metadata, sort_keys=True) + "\n").encode(), 0o600)
    print(f"[install-pre-push-hook] installed {destination}")


def install(repo: Path) -> Path:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    source = root / "scripts" / "hooks" / "pre-push"
    if not source.is_file():
        raise RuntimeError(f"tracked hook source missing: {source}")
    destination = _effective_hooks_dir(root) / "pre-push"
    _install(destination, source)
    return destination


def main() -> int:
    try:
        install(Path.cwd())
    except (OSError, RuntimeError) as exc:
        print(f"[install-pre-push-hook] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
