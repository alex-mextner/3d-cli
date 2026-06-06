"""projects.py — the on-disk registry of known 3d projects (ROADMAP §28/§9).

ACCESSED VIA: `3d projects` (list/add/remove), `3d init` (appends the new project),
and eventually `3d web` (lists every registered project instead of a single root).
This is headless core: it does NO printing — callers (the command) format and warn.

INVARIANTS:
  - The registry lives at `cli.paths.config_dir()/projects.json` and is the SINGLE source
    of truth for "which directories are 3d projects". The path is resolved at call time
    (never cached at import), so an `XDG_CONFIG_HOME` override / test sandbox is honored.
  - Entries store ONLY `{path, added}` — `path` is an absolute, resolved directory. The
    human `name` is read LIVE from each project's `3d.yaml` at list time (falling back to
    the directory basename), so a renamed project never goes stale in the registry.
  - Dedup and removal both match on the RESOLVED absolute path, so `./foo` and `/abs/foo`
    refer to the same entry regardless of how the user spelled it.
  - Robust to a missing/corrupt registry file: it is treated as empty rather than crashing.
    config_dir() is created on write only (paths.py says it is not auto-created).
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

from cli import paths
from errors import ThreeDError

REGISTRY_FILENAME = "projects.json"


class ProjectRegistryError(ThreeDError):
    """The registry got a bad argument (missing dir / unregistered path). Exit 2."""

    exit_code = 2


def registry_path() -> pathlib.Path:
    """Absolute path to projects.json (resolved live so XDG overrides are honored)."""
    return paths.config_dir() / REGISTRY_FILENAME


def _load_raw() -> list[dict[str, Any]]:
    """Parse projects.json into a list of entry dicts; empty list if missing/corrupt."""
    p = registry_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(doc, dict):
        return []
    entries = doc.get("projects")
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("path"), str):
            out.append({"path": e["path"], "added": e.get("added")})
    return out


def _save_raw(entries: list[dict[str, Any]]) -> None:
    """Write entries back to projects.json, creating config_dir() if needed."""
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"projects": entries}, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _resolve(path: str | os.PathLike[str]) -> str:
    """Resolve a user-supplied path to an absolute string (the registry's match key)."""
    return str(pathlib.Path(path).expanduser().resolve())


def _read_name(project_dir: str) -> str:
    """Read the project name from `<dir>/3d.yaml`, falling back to the dir basename.

    Lazy-imports the project loader (heavy/yaml) so this module stays import-light.
    Any error (missing/corrupt 3d.yaml) degrades to the basename — one broken project
    must never break listing the rest."""
    fallback = os.path.basename(project_dir.rstrip(os.sep)) or project_dir
    try:
        import project  # lazy: pulls in yaml; only needed when actually listing

        proj = project.load_project(project_dir, command="projects", check_files=False)
        return proj.name
    except Exception:
        return fallback


def list_projects() -> list[dict[str, Any]]:
    """Return registered projects as `[{path, name, added}, ...]`.

    `name` is read live from each `3d.yaml` (basename fallback). Order is the order they
    were added. A removed-on-disk directory is still listed (so the user can prune it);
    callers may flag it."""
    result: list[dict[str, Any]] = []
    for e in _load_raw():
        path = e["path"]
        result.append({"path": path, "name": _read_name(path), "added": e.get("added")})
    return result


def is_registered(path: str | os.PathLike[str]) -> bool:
    """True if the resolved path is already in the registry."""
    target = _resolve(path)
    return any(e["path"] == target for e in _load_raw())


def add(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Register a project directory (idempotent; dedup on the resolved absolute path).

    Validates that the directory exists. A `3d.yaml` is PREFERRED but not required: the
    returned dict carries `had_yaml` so the command can warn when it is absent (this core
    module never prints). Returns `{path, added, had_yaml}`.

    Raises ProjectRegistryError (exit 2) if the path is not an existing directory."""
    raw_in = str(path)
    target = _resolve(path)
    if not os.path.isdir(target):
        raise ProjectRegistryError(
            f"not a directory: {raw_in}",
            command="projects",
            remediation=[
                "Pass the path to a project directory (the folder that holds 3d.yaml).",
                "Example:  3d projects add ./my-bracket",
            ],
        )
    had_yaml = os.path.isfile(os.path.join(target, "3d.yaml"))

    entries = _load_raw()
    for e in entries:
        if e["path"] == target:
            # Already registered — idempotent; report the existing entry.
            return {"path": target, "added": e.get("added"), "had_yaml": had_yaml}

    import datetime

    added = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    entries.append({"path": target, "added": added})
    _save_raw(entries)
    return {"path": target, "added": added, "had_yaml": had_yaml}


def remove(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Unregister a project directory (matches on the resolved absolute path).

    Returns the removed `{path, added}` entry. Raises ProjectRegistryError (exit 2) if the
    path was not registered, so the user gets feedback instead of a silent no-op."""
    target = _resolve(path)
    entries = _load_raw()
    kept: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for e in entries:
        if e["path"] == target:
            removed = e
        else:
            kept.append(e)
    if removed is None:
        raise ProjectRegistryError(
            f"not registered: {target}",
            command="projects",
            remediation=[
                "List the registered projects to see exact paths:  3d projects list",
            ],
        )
    _save_raw(kept)
    return {"path": removed["path"], "added": removed.get("added")}
