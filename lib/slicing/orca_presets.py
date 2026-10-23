"""orca_presets.py — find an Orca-family preset by name and flatten its `inherits` chain.

WHAT: looks up a machine / process / filament preset (the `.json` files OrcaSlicer, Snapmaker
  Orca and Bambu Studio keep, and the copies in the repo `profiles/` dir) and returns it with
  every inherited key filled in, so a caller gets the printer's real bed, start G-code, limits.

WHY: a user preset such as "Creality K1 SE 0.4 nozzle - Copy" only stores the keys the user
  changed; the bed size and the start G-code live in its system parent. `3d retarget` needs the
  full set to rewrite a project 3MF for another printer.

Lookup roots, best first: explicit `extra` dirs, the repo `profiles/` dir, each slicer's user
presets, each slicer's `system/` vendor presets. A root may hold `<kind>/*.json` directly or one
or two directories above that (`profiles/snapmaker-orca/machine`, `system/Creality/machine`).
Vendor folders reuse base names (`fdm_machine_common` exists in several), so a parent is looked
up next to its child first, then in the child's root, then anywhere.

Examples:
  catalog = PresetCatalog()                      # scans lazily, once per kind
  ref = catalog.find("machine", "k1se")          # fuzzy: ignores case, spaces, punctuation
  settings = catalog.flatten(ref)                # {"printable_area": [...], ...}
"""
from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import InvalidArgument, ThreeDError

KINDS = ("machine", "process", "filament")

# Keys that identify a preset rather than configure a print.
_NON_SETTINGS = frozenset(
    {
        "type",
        "name",
        "inherits",
        "from",
        "setting_id",
        "instantiation",
        "is_custom_defined",
        "version",
        "description",
        "compatible_printers",
        "compatible_printers_condition",
        "compatible_prints",
        "compatible_prints_condition",
        "renamed_from",
        "print_host",
        "print_host_webui",
        "printhost_apikey",
        "printhost_cafile",
        "printhost_password",
        "printhost_port",
        "printhost_user",
        "printhost_authorization_type",
        "printhost_ssl_ignore_revoke",
    }
)

_ORIGIN_RANK = {"extra": 0, "repo": 1, "user": 2, "system": 3}
_MAX_DEPTH = 12


@dataclass(frozen=True)
class PresetRef:
    name: str
    kind: str
    path: Path
    origin: str
    root: Path
    selectable: bool = True


def _kind_dirs(root: Path, kind: str) -> list[Path]:
    """`root/kind`, `root/*/kind` and `root/*/*/kind` that exist."""
    found: list[Path] = []
    if (root / kind).is_dir():
        found.append(root / kind)
    try:
        level1 = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for sub in level1:
        if (sub / kind).is_dir():
            found.append(sub / kind)
        try:
            found.extend(p / kind for p in sorted(sub.iterdir()) if p.is_dir() and (p / kind).is_dir())
        except OSError:
            continue
    return found


def preset_roots(extra: Sequence[Path] = ()) -> list[tuple[str, Path]]:
    """`(origin, root)` pairs to search, best first."""
    from profiles.slicers import SLICERS, find_data_dir, profiles_root, user_presets_dir

    roots: list[tuple[str, Path]] = [("extra", Path(p)) for p in extra]
    roots.append(("repo", profiles_root()))
    for slicer in SLICERS.values():
        data = find_data_dir(slicer)
        if data is None:
            continue
        with contextlib.suppress(ThreeDError):  # a broken slicer config must not hide the other roots
            roots.append(("user", user_presets_dir(data, slicer)))
        roots.append(("system", data / "system"))
    return sorted(roots, key=lambda r: _ORIGIN_RANK[r[0]])


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "", text.lower())


_STOCK_NOZZLE = re.compile(r"(?<![\d.])0\.4(?![\d])")


def _preference(ref: PresetRef) -> tuple[int, bool, int, str]:
    """Best origin first; then the stock 0.4 mm nozzle; then the shortest, then alphabetical."""
    return (_ORIGIN_RANK[ref.origin], not _STOCK_NOZZLE.search(ref.name), len(ref.name), ref.name)


class PresetCatalog:
    """The presets visible from this machine, scanned lazily once per kind."""

    def __init__(self, extra: Sequence[Path] = ()) -> None:
        self._roots = preset_roots(extra)
        self._index: dict[str, dict[str, list[PresetRef]]] = {}

    def _scan(self, kind: str) -> dict[str, list[PresetRef]]:
        """Name -> refs (best origin first). A preset is registered under its JSON `name` and,
        when that differs, under its file stem too: system files are named `... K1 SE 0.4.json`
        while the preset is `... K1 SE`."""
        if kind in self._index:
            return self._index[kind]
        index: dict[str, list[PresetRef]] = {}
        for origin, root in self._roots:
            for kind_dir in _kind_dirs(root, kind):
                for path in sorted(kind_dir.glob("*.json")):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue  # a broken file in a searched dir must not hide the others
                    if not isinstance(data, dict):
                        continue
                    name = str(data.get("name") or path.stem)
                    selectable = (
                        data.get("type") != f"{kind}_model"
                        and str(data.get("instantiation", "true")).lower() != "false"
                    )
                    ref = PresetRef(name, kind, path, origin, root, selectable)
                    for alias in {name, path.stem}:
                        index.setdefault(alias, []).append(ref)
        self._index[kind] = index
        return index

    def matches(self, kind: str, query: str) -> list[PresetRef]:
        """Every selectable preset `query` names, best first: those with exactly this name, else
        those whose name equals it ignoring case/punctuation, else those containing it. Best =
        origin (extra > repo > user > system), then a 0.4 mm nozzle, then the shortest name.
        Empty when nothing matches."""
        index = self._scan(kind)
        wanted = _normal(query)
        groups = (
            list(index.get(query, [])),
            [r for n, refs in index.items() if _normal(n) == wanted for r in refs],
            [r for n, refs in index.items() if wanted and wanted in _normal(n) for r in refs],
        )
        for group in groups:
            found = sorted({r.path: r for r in group if r.selectable}.values(), key=_preference)
            if found:
                return found
        return []

    def find(self, kind: str, query: str) -> PresetRef:
        """The best preset `matches` finds; raises InvalidArgument listing close names on a miss."""
        found = self.matches(kind, query)
        if found:
            return found[0]
        index = self._scan(kind)
        wanted = _normal(query)
        near = sorted(index)
        hints = [n for n in near if wanted[:4] and wanted[:4] in _normal(n)][:12] or near[:12]
        raise InvalidArgument(
            f"{kind} preset",
            query,
            hints or ["(no presets found)"],
            command="retarget",
            extra=(
                "Searched --preset-dir dirs, the repo profiles/ dir and the Orca-family slicer data "
                f"dirs; found {len(index)} {kind} preset name(s). Install/open the slicer once so its "
                "system presets exist, or pass --preset-dir DIR."
            ),
        )

    def flatten(self, ref: PresetRef) -> dict[str, Any]:
        """The preset's settings with every inherited key filled in (child wins), minus the
        identity/bookkeeping keys (`name`, `inherits`, `from`, `setting_id`, ...)."""
        chain: list[dict[str, Any]] = []
        seen: set[Path] = set()
        current: PresetRef | None = ref
        while current is not None:
            if current.path in seen or len(chain) > _MAX_DEPTH:
                raise InvalidArgument(
                    f"{ref.kind} preset", ref.name, ["a preset without an inherits cycle"], command="retarget"
                )
            seen.add(current.path)
            data = _read(current)
            chain.append(data)
            parent = data.get("inherits")
            current = self._parent(current, str(parent)) if parent else None
        merged: dict[str, Any] = {}
        for data in reversed(chain):
            merged.update(data)
        return {k: v for k, v in merged.items() if k not in _NON_SETTINGS}

    def _parent(self, child: PresetRef, name: str) -> PresetRef:
        """The parent named `name`: beside the child, else in the child's root, else best origin."""
        candidates = [r for r in self._scan(child.kind).get(name, []) if r.path != child.path]
        if not candidates:
            raise InvalidArgument(
                f"{child.kind} preset",
                child.name,
                [f"a preset whose parent {name!r} is installed"],
                command="retarget",
                extra=f"{child.path} inherits {name!r}, which is not in any searched preset dir.",
            )
        beside = [r for r in candidates if r.path.parent == child.path.parent]
        same_root = [r for r in candidates if r.root == child.root]
        return (beside or same_root or candidates)[0]


def _read(ref: PresetRef) -> dict[str, Any]:
    try:
        data = json.loads(ref.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidArgument(
            f"{ref.kind} preset",
            str(ref.path),
            ["a readable Orca-family preset .json"],
            command="retarget",
            extra=f"{ref.path}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise InvalidArgument(
            f"{ref.kind} preset", str(ref.path), ["a JSON object"], command="retarget"
        )
    return data
