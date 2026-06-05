#!/usr/bin/env python3
"""scan.py — discover 3D-modeling projects under a root directory.

A "project" is a directory that contains at least one of: a `*.scad` file, a `SPEC.md`,
or a `3d.yaml`. Scanning is shallow-ish: we walk the tree but prune heavy/uninteresting
dirs (.git, node_modules, previews, libs, __pycache__, venvs) and cap depth so a huge
workspace doesn't take forever.

For each project we record a primary model (`.scad`), whether a SPEC / 3d.yaml /
constants.scad / previews / animations exist, so the UI can degrade gracefully when a
feature's source is missing.
"""
from __future__ import annotations

import dataclasses
import pathlib

_MARKERS = ("SPEC.md", "3d.yaml", "3d.yml")
_PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv", "libs", ".serena",
          ".claude", "ref", ".mypy_cache", ".pytest_cache"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".gif"}
_MAX_DEPTH = 5


@dataclasses.dataclass(slots=True)
class Project:
    name: str
    path: str                       # absolute project dir
    rel: str                        # path relative to the scan root
    scad_files: list[str]           # absolute .scad paths (project-local)
    primary_scad: str | None        # best guess at the main model
    spec: str | None                # absolute SPEC.md path
    yaml: str | None                # absolute 3d.yaml path
    constants: str | None           # absolute constants.scad path
    previews_dir: str | None        # absolute previews/ dir
    animations: list[str]           # absolute video/anim file paths

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def _is_project(d: pathlib.Path) -> bool:
    if any((d / m).is_file() for m in _MARKERS):
        return True
    return any(d.glob("*.scad"))


def _primary_scad(d: pathlib.Path, scads: list[pathlib.Path]) -> pathlib.Path | None:
    if not scads:
        return None
    # never pick a non-renderable contract/helper file as the headline model
    candidates = [s for s in scads if s.stem not in ("constants", "constant")] or scads
    by_name = {s.stem: s for s in candidates}
    # prefer a file matching the dir name, then assembly/model/main, then the largest
    if d.name in by_name:
        return by_name[d.name]
    for cand in ("assembly", "model", "main"):
        if cand in by_name:
            return by_name[cand]
    return max(candidates, key=lambda s: s.stat().st_size if s.exists() else 0)


def scan_projects(root: str | pathlib.Path) -> list[Project]:
    rootp = pathlib.Path(root).expanduser().resolve()
    found: list[Project] = []
    if not rootp.is_dir():
        return found

    def walk(d: pathlib.Path, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        if _is_project(d) and d != rootp:
            found.append(_build(d, rootp))
            # treat a project as a leaf: do NOT descend into its subdirs (parts/, verify/
            # are part of THIS project, not separate projects). Avoids noisy duplicates.
            return
        for e in entries:
            if e.is_dir() and e.name not in _PRUNE and not e.name.startswith("."):
                walk(e, depth + 1)

    # the root itself may be a project; also descend into it
    if _is_project(rootp):
        found.append(_build(rootp, rootp))
    for e in (rootp.iterdir() if rootp.is_dir() else []):
        if e.is_dir() and e.name not in _PRUNE and not e.name.startswith("."):
            walk(e, 1)

    # dedupe by path, sort by name
    seen: set[str] = set()
    uniq: list[Project] = []
    for p in found:
        if p.path not in seen:
            seen.add(p.path)
            uniq.append(p)
    uniq.sort(key=lambda p: p.rel.lower())
    return uniq


def _project_scads(d: pathlib.Path) -> list[pathlib.Path]:
    """All .scad files in the project tree, pruning noise dirs."""
    out: list[pathlib.Path] = []
    for f in d.rglob("*.scad"):
        if any(part in _PRUNE for part in f.relative_to(d).parts[:-1]):
            continue
        out.append(f)
    return sorted(out)


def _build(d: pathlib.Path, root: pathlib.Path) -> Project:
    scads = _project_scads(d)
    primary = _primary_scad(d, [s for s in scads if s.parent == d] or scads)
    spec = d / "SPEC.md"
    yml = next((d / m for m in ("3d.yaml", "3d.yml") if (d / m).is_file()), None)
    constants = d / "constants.scad"
    previews = d / "previews"
    anims: list[str] = []
    for sub in (d, d / "previews", d / "animations", d / "anim"):
        if sub.is_dir():
            for f in sub.iterdir():
                if f.suffix.lower() in _VIDEO_EXT:
                    anims.append(str(f))
    try:
        rel = str(d.relative_to(root)) if d != root else "."
    except ValueError:
        rel = d.name
    return Project(
        name=d.name,
        path=str(d),
        rel=rel,
        scad_files=[str(s) for s in scads],
        primary_scad=str(primary) if primary else None,
        spec=str(spec) if spec.is_file() else None,
        yaml=str(yml) if yml else None,
        constants=str(constants) if constants.is_file() else None,
        previews_dir=str(previews) if previews.is_dir() else None,
        animations=sorted(set(anims)),
    )
