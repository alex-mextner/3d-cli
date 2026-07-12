"""ai — pluggable, backend-agnostic AI text/vision completion layer.

This package is the FOUNDATION for the generative-modeling pipeline: it lets callers
(the match-loop critic, future `3d ai` executors) request an AI completion without
hard-wiring a single vendor. See `ai.backends` for the Backend abstraction and the
concrete claude/codex/opencode/ollama/mock backends.

STDLIB-ONLY: nothing here imports a heavy dep, so importing `ai` never breaks the
offline `3d help`/`render` guarantee (tests/test_imports.py).

`load_backend_config()` reads the shared `~/.config/3d-cli/ai.json` and returns the raw
mapping (or `{}`), so `resolve_backend(config=...)` can honor a configured `backend`
WITHOUT the eager `claude` default that `ai_tools.load_config` applies.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

from .backends import (
    BACKEND_ORDER,
    VALID_BACKENDS,
    Backend,
    BackendError,
    ClaudeBackend,
    CodexBackend,
    MockBackend,
    OllamaBackend,
    OpencodeBackend,
    resolve_backend,
)

__all__ = [
    "BACKEND_ORDER",
    "VALID_BACKENDS",
    "Backend",
    "BackendError",
    "ClaudeBackend",
    "CodexBackend",
    "MockBackend",
    "OllamaBackend",
    "OpencodeBackend",
    "resolve_backend",
    "load_backend_config",
]


def _config_path(path: str | os.PathLike[str] | None = None) -> pathlib.Path:
    if path is not None:
        return pathlib.Path(path).expanduser()
    env = os.environ.get("THREED_AI_CONFIG")
    if env:
        return pathlib.Path(env).expanduser()
    from cli import paths  # local import keeps the module import graph shallow

    return paths.config_dir() / "ai.json"


def load_backend_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return the raw `ai.json` mapping (or `{}` if the file is simply absent).

    Unlike `ai_tools.load_config`, this does NOT inject a default `backend`: an absent
    `backend` key stays absent so `resolve_backend` falls through to first-available.

    FAIL-CLOSED on a present-but-broken file: a config that EXISTS but is unreadable,
    invalid JSON, or not a JSON object raises a structured error rather than silently
    degrading to `{}` (which would route the overlay/prompt to an unintended
    auto-picked backend on a typo). This mirrors `ai_tools.load_config`'s behavior so
    the shared `ai.json` has one parse contract.
    """
    from ai_tools import AIConfigError  # shared parse contract for ~/.config/3d-cli/ai.json

    cfg_path = _config_path(path)
    if not cfg_path.exists():
        return {}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIConfigError(
            f"could not parse AI config {cfg_path}: {exc}",
            command="ai",
            remediation=["Fix the JSON, or delete the file to fall back to auto-pick."],
        ) from exc
    if not isinstance(raw, dict):
        raise AIConfigError(
            f"AI config {cfg_path} must be a JSON object",
            command="ai",
            remediation=['Use keys like {"backend": "codex", "model": "..."}.'],
        )
    return raw
