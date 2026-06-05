"""paths.py — the SINGLE source of truth for the `3d` tool's on-disk directories.

ACCESSED VIA: cli.env (bootstrap marker), web.webconfig (web.json + web-state),
commands/{web,libs,doctor,init,projects,materials,printers}.py (help text + registries),
and lib.metrics (the longitudinal store, ROADMAP §13.4).

INVARIANT (ROADMAP §23): there is exactly ONE config dir for the whole tool —
`~/.config/3d-cli/` (honoring `XDG_CONFIG_HOME`) — and ONE data dir —
`~/.local/share/3d-cli/` (honoring `XDG_DATA_HOME`). Every caller routes through the
helpers here; nobody re-derives `.config` / `.local/share` on their own. Before §23 the
path was `~/.config/3d/` and it was computed independently in env.py and webconfig.py;
those two sites drifted, which is exactly what this module exists to prevent.

PAST BUG / migration: the dir was renamed `3d` -> `3d-cli`. `migrate_legacy_config()` moves
a pre-existing `~/.config/3d/` into place once, so an existing user does not silently
re-trigger the OpenSCAD-library bootstrap (env.py gates on a marker FILE inside this dir).
"""
from __future__ import annotations

import os
import pathlib

# The one app directory name, used under both the config and data roots.
APP_DIRNAME = "3d-cli"
_LEGACY_DIRNAME = "3d"  # pre-§23 name; migrated away by migrate_legacy_config()


def _xdg_root(env_var: str, default_rel: tuple[str, ...]) -> pathlib.Path:
    base = os.environ.get(env_var)
    if base:
        return pathlib.Path(base)
    return pathlib.Path.home().joinpath(*default_rel)


def config_dir() -> pathlib.Path:
    """`~/.config/3d-cli/` (or `$XDG_CONFIG_HOME/3d-cli`). Holds web.json, the bootstrap
    marker, projects registry, and user/project registry overrides. Not auto-created."""
    return _xdg_root("XDG_CONFIG_HOME", (".config",)) / APP_DIRNAME


def data_dir() -> pathlib.Path:
    """`~/.local/share/3d-cli/` (or `$XDG_DATA_HOME/3d-cli`). Holds the longitudinal
    metrics store (ROADMAP §13.4) and other generated state. Not auto-created."""
    return _xdg_root("XDG_DATA_HOME", (".local", "share")) / APP_DIRNAME


def legacy_config_dir() -> pathlib.Path:
    """The pre-§23 `~/.config/3d/` location, kept only so migration can find it."""
    return _xdg_root("XDG_CONFIG_HOME", (".config",)) / _LEGACY_DIRNAME


def migrate_legacy_config() -> None:
    """One-time move of `~/.config/3d/` -> `~/.config/3d-cli/` if the old dir exists and the
    new one does not. Best-effort and non-fatal: a failure just means the new dir starts
    empty (bootstrap re-runs, which is itself idempotent). Safe to call on every invocation."""
    new = config_dir()
    if new.exists():
        return
    old = legacy_config_dir()
    if not old.is_dir():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    except OSError:
        # Cross-device or permission issue — leave the old dir; callers degrade gracefully.
        pass
