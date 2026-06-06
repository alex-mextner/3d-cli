"""Workspace metadata for the future `3d web` project picker.

A workspace is a named scan root plus an explicit list of known project directories. This
module is headless core: it reads/writes JSON under cli.paths.config_dir() and returns
plain dictionaries for command and web callers to format.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
from typing import Any, Sequence

from cli import paths
from errors import ThreeDError, UsageError

WORKSPACES_FILENAME = "workspaces.json"


def workspaces_path() -> pathlib.Path:
    """Absolute path to the workspace registry, resolved live for XDG test overrides."""
    return paths.config_dir() / WORKSPACES_FILENAME


def _resolve_dir(path: str | os.PathLike[str], *, what: str) -> str:
    raw = str(path)
    resolved = pathlib.Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise UsageError(
            f"{what} is not a directory: {raw}",
            command="workspaces",
            remediation=[
                "Pass an existing directory.",
                "Example:  3d workspaces create shop --root ~/models",
            ],
        )
    return str(resolved)


def _load_raw() -> list[dict[str, Any]]:
    p = workspaces_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(doc, dict):
        return []
    entries = doc.get("workspaces")
    if not isinstance(entries, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        root = entry.get("root")
        projects = entry.get("projects", [])
        if not isinstance(name, str) or not name.strip() or not isinstance(root, str):
            continue
        if not isinstance(projects, list):
            projects = []
        out.append(
            {
                "name": name,
                "root": root,
                "projects": [p for p in projects if isinstance(p, str)],
                "created": entry.get("created"),
            }
        )
    return out


def _save_raw(entries: list[dict[str, Any]]) -> None:
    p = workspaces_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"workspaces": entries}, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _project_name(project_dir: str) -> str:
    fallback = os.path.basename(project_dir.rstrip(os.sep)) or project_dir
    try:
        import project

        loaded = project.load_project(project_dir, command="workspaces", check_files=False)
        return loaded.name
    except (OSError, ThreeDError):
        return fallback


def _project_info(project_dir: str) -> dict[str, Any]:
    path = pathlib.Path(project_dir)
    return {
        "path": project_dir,
        "name": _project_name(project_dir),
        "exists": path.is_dir(),
        "has_yaml": (path / "3d.yaml").is_file(),
    }


def _expand(entry: dict[str, Any]) -> dict[str, Any]:
    project_paths = [str(pathlib.Path(p).expanduser().resolve()) for p in entry.get("projects", [])]
    projects = [_project_info(p) for p in project_paths]
    return {
        "name": entry["name"],
        "root": str(pathlib.Path(str(entry["root"])).expanduser().resolve()),
        "created": entry.get("created"),
        "project_count": len(projects),
        "projects": projects,
    }


def list_workspaces() -> list[dict[str, Any]]:
    """Return all known workspaces with live project summaries."""
    return [_expand(entry) for entry in _load_raw()]


def get_workspace(name: str) -> dict[str, Any]:
    """Return one workspace by name, or raise a structured error."""
    needle = name.strip()
    for entry in _load_raw():
        if entry["name"] == needle:
            return _expand(entry)
    raise UsageError(
        f"workspace not found: {name}",
        command="workspaces",
        remediation=["List known workspaces with:  3d workspaces list"],
    )


def create_workspace(
    name: str,
    *,
    root: str | os.PathLike[str] | None = None,
    projects: Sequence[str | os.PathLike[str]] | None = None,
) -> dict[str, Any]:
    """Create a workspace entry and persist it to workspaces.json.

    Names are unique. Root and project paths must be existing directories because this
    metadata is meant to feed discovery, not act as a stale bookmark store.
    """
    clean_name = name.strip()
    if not clean_name:
        raise UsageError(
            "workspace name is required",
            command="workspaces",
            remediation=["Example:  3d workspaces create shop --root ~/models"],
        )

    root_path = _resolve_dir(root or os.getcwd(), what="workspace root")
    project_paths: list[str] = []
    for project_path in projects or []:
        resolved = _resolve_dir(project_path, what="project path")
        if resolved not in project_paths:
            project_paths.append(resolved)

    entries = _load_raw()
    if any(entry["name"] == clean_name for entry in entries):
        raise UsageError(
            f"workspace already exists: {clean_name}",
            command="workspaces",
            remediation=[
                "Choose a different name, or show the existing entry with:",
                f"3d workspaces show {clean_name}",
            ],
        )

    created = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    raw = {"name": clean_name, "root": root_path, "projects": project_paths, "created": created}
    entries.append(raw)
    _save_raw(entries)
    return _expand(raw)
