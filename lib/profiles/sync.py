"""sync.py — three-way sync of user presets between the repo `profiles/<slicer>/` and a slicer's
user preset dir.

An Entry is one preset in its repo form (preset.py): `<kind>/[base/]<name>.json` plus its
portable `.info`. The base snapshot (`~/.config/3d-cli/profiles-sync.json`) records each entry's
digest at the last sync, per (repo dir, slicer dir) pair. Against it: changed only in the slicer
-> pull, changed only in the repo -> push, changed on both sides (or new on both sides with
different content) -> conflict, never resolved here. A deletion is a change like any other.

Every write into a slicer dir first copies the files it replaces or removes to
`~/.cache/3d-cli/profiles-backup/<UTC time>/<slicer>/`, and is refused while that slicer runs
(it rewrites presets from memory on save) unless forced.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cli.paths import cache_dir, config_dir
from errors import GateFailure, ThreeDError
from profiles import preset, slicers

STATE_VERSION = 1
DIRECTIONS = ("pull", "push")


@dataclass(frozen=True)
class Entry:
    rel: str
    json: bytes
    info: bytes | None

    @property
    def digest(self) -> str:
        h = hashlib.sha256(self.json)
        h.update(b"\0info\0" + (self.info or b""))
        return h.hexdigest()


@dataclass(frozen=True)
class Item:
    rel: str
    action: str  # same | pull | push | conflict
    reason: str
    repo: Entry | None
    slicer: Entry | None

    @property
    def name(self) -> str:
        return Path(self.rel).stem


@dataclass(frozen=True)
class Pair:
    slicer: slicers.Slicer
    repo_dir: Path
    user_dir: Path

    @property
    def key(self) -> str:
        return f"{self.repo_dir.resolve()}::{self.user_dir.resolve()}"


def find_pair(slicer: slicers.Slicer) -> Pair | None:
    data = slicers.find_data_dir(slicer)
    if data is None:
        return None
    return Pair(
        slicer,
        slicers.profiles_root() / slicer.id,
        slicers.user_presets_dir(data, slicer),
    )


def _preset_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for kind in slicers.KINDS:
        for sub in (root / kind, root / kind / "base"):
            if sub.is_dir():
                out.extend(p for p in sub.glob("*.json") if p.is_file())
    return sorted(out)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def repo_entry(root: Path, rel: str) -> Entry | None:
    path = root / rel
    if not path.is_file():
        return None
    raw = path.read_bytes()
    local = preset.local_keys_in(raw, where=str(path))
    if local:
        raise ThreeDError(
            f"{path} holds slicer-local keys: {', '.join(local)}",
            command="profiles",
            remediation=[
                "Remove them from the repo file: connection settings stay in the slicer, "
                "and a push keeps the slicer's values."
            ],
        )
    return Entry(rel, raw, _portable_info_beside(path))


def slicer_entry(user_dir: Path, rel: str) -> Entry | None:
    path = user_dir / rel
    if not path.is_file():
        return None
    return Entry(
        rel,
        preset.portable_json(path.read_bytes(), where=str(path)),
        _portable_info_beside(path),
    )


def _portable_info_beside(preset_path: Path) -> bytes | None:
    info = _read_text(preset_path.with_suffix(".info"))
    return preset.portable_info(info) if info is not None else None


def _scan(root: Path, read: Callable[[Path, str], Entry | None]) -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for path in _preset_files(root):
        entry = read(root, path.relative_to(root).as_posix())
        if entry is not None:
            out[entry.rel] = entry
    return out


def scan_repo(root: Path) -> dict[str, Entry]:
    return _scan(root, repo_entry)


def scan_slicer(user_dir: Path) -> dict[str, Entry]:
    return _scan(user_dir, slicer_entry)


def _digest(entry: Entry | None) -> str | None:
    return entry.digest if entry is not None else None


def plan(
    repo: dict[str, Entry], slicer: dict[str, Entry], base: dict[str, str]
) -> list[Item]:
    items: list[Item] = []
    for rel in sorted(repo.keys() | slicer.keys() | base.keys()):
        r, s, b = repo.get(rel), slicer.get(rel), base.get(rel)
        rd, sd = _digest(r), _digest(s)
        if rd == sd:
            items.append(Item(rel, "same", "in sync", r, s))
        elif rd == b:
            items.append(
                Item(
                    rel,
                    "pull",
                    "deleted in slicer"
                    if s is None
                    else ("new in slicer" if r is None else "changed in slicer"),
                    r,
                    s,
                )
            )
        elif sd == b:
            items.append(
                Item(
                    rel,
                    "push",
                    "deleted in repo"
                    if r is None
                    else ("new in repo" if s is None else "changed in repo"),
                    r,
                    s,
                )
            )
        else:
            why = "changed in both" if b is not None else "differs, no common base"
            if r is None or s is None:
                why = (
                    "deleted in repo, changed in slicer"
                    if r is None
                    else "deleted in slicer, changed in repo"
                )
            items.append(Item(rel, "conflict", why, r, s))
    return items


# ---- base snapshot --------------------------------------------------------------------------


def state_path() -> Path:
    return config_dir() / "profiles-sync.json"


@contextmanager
def exclusive() -> Iterator[None]:
    """Hold the sync lock: two syncs at once would each load the base snapshot and the
    later save would drop the other's recorded bases."""
    lock_path = config_dir() / "profiles-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateFailure(
                f"another `3d profiles sync` is running (lock {lock_path})",
                command="profiles",
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class State:
    pairs: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> State:
        try:
            data = json.loads(state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except ValueError as exc:
            raise ThreeDError(
                f"sync state {state_path()} is unreadable: {exc}",
                command="profiles",
                remediation=[
                    "Move it aside and rerun; without a base, presets that differ show as conflicts."
                ],
            ) from exc
        pairs = data.get("pairs", {}) if isinstance(data, dict) else {}
        return cls(pairs if isinstance(pairs, dict) else {})

    def base(self, pair: Pair) -> dict[str, str]:
        entries = self.pairs.get(pair.key, {}).get("entries", {})
        return dict(entries) if isinstance(entries, dict) else {}

    def set_base(self, pair: Pair, entries: dict[str, str]) -> None:
        self.pairs[pair.key] = {
            "slicer": pair.slicer.id,
            "repo_dir": str(pair.repo_dir.resolve()),
            "user_dir": str(pair.user_dir.resolve()),
            "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entries": dict(sorted(entries.items())),
        }

    def save(self) -> None:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            path,
            (
                json.dumps({"version": STATE_VERSION, "pairs": self.pairs}, indent=2)
                + "\n"
            ).encode(),
        )


def status(pair: Pair, state: State) -> list[Item]:
    return plan(scan_repo(pair.repo_dir), scan_slicer(pair.user_dir), state.base(pair))


# ---- diffs ----------------------------------------------------------------------------------


def _lines(entry: Entry | None) -> list[str]:
    if entry is None:
        return []
    text = entry.json.decode("utf-8")
    if entry.info is not None:
        text += "// .info\n" + entry.info.decode("utf-8")
    return text.splitlines(keepends=True)


def item_diff(item: Item, slicer_id: str) -> str:
    """Unified diff repo -> slicer of an entry's repo form (`.info` appended after a marker)."""
    a = f"repo/{slicer_id}/{item.rel}" if item.repo is not None else "/dev/null"
    b = f"slicer/{slicer_id}/{item.rel}" if item.slicer is not None else "/dev/null"
    return "".join(difflib.unified_diff(_lines(item.repo), _lines(item.slicer), a, b))


# ---- applying -------------------------------------------------------------------------------


@dataclass
class Applied:
    done: list[Item] = field(default_factory=list)
    # Changed on disk after the plan was made; left for the next sync.
    stale: list[Item] = field(default_factory=list)
    backup_dir: Path | None = None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".3d-tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def _backup_root() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return cache_dir() / "profiles-backup" / stamp


def ensure_not_running(pair: Pair) -> None:
    found = slicers.running_processes(pair.slicer)
    if found == []:
        return
    what = (
        f"{pair.slicer.label} is running ({', '.join(found)})"
        if found
        else f"cannot tell whether {pair.slicer.label} is running (`ps` unavailable)"
    )
    err = GateFailure(f"{what}; refusing to write its presets", command="profiles")
    err.remediation = [
        f"Close {pair.slicer.label} (it rewrites presets from memory on save), then rerun.",
        "Or pass --force to write anyway.",
    ]
    raise err


def _pull(pair: Pair, item: Item) -> None:
    target = pair.repo_dir / item.rel
    if item.slicer is None:
        _remove(target)
        _remove(target.with_suffix(".info"))
        return
    _write_atomic(target, item.slicer.json)
    if item.slicer.info is None:
        _remove(target.with_suffix(".info"))
    else:
        _write_atomic(target.with_suffix(".info"), item.slicer.info)


def _push(pair: Pair, item: Item, backup: Path, now: int) -> None:
    target = pair.user_dir / item.rel
    info_path = target.with_suffix(".info")
    for path in (target, info_path):
        if path.is_file():
            dest = backup / pair.slicer.id / path.relative_to(pair.user_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    old_info = _read_text(info_path)
    if item.repo is None:
        _remove(target)
        kept = preset.deleted_info(old_info) if old_info is not None else None
        if kept is None:
            _remove(info_path)
        else:
            _write_atomic(info_path, kept.encode())
        return
    old_json = target.read_bytes() if target.is_file() else None
    _write_atomic(
        target, preset.merge_local(item.repo.json, old_json, where=str(target))
    )
    _write_atomic(info_path, preset.pushed_info(old_info, item.repo.info, now).encode())


def apply(
    pair: Pair,
    items: Iterable[Item],
    state: State,
    *,
    directions: Iterable[str] = DIRECTIONS,
    force: bool = False,
    now: int | None = None,
) -> Applied:
    """Apply the pull/push items in `directions`; conflicts are skipped, and so is an item
    whose files changed on disk after the plan (`stale`). Records the new base for every entry
    now equal on both sides; the caller saves `state`."""
    wanted = set(directions)
    items = list(items)
    result = Applied()
    if any(i.action == "push" for i in items if i.action in wanted):
        if not force:
            ensure_not_running(pair)
        result.backup_dir = _backup_root()
    stamp = int(time.time()) if now is None else now
    base = state.base(pair)
    for item in items:
        if item.action == "same":
            _record(base, item.rel, item.repo)
        if item.action not in wanted:
            continue
        if _digest(repo_entry(pair.repo_dir, item.rel)) != _digest(
            item.repo
        ) or _digest(slicer_entry(pair.user_dir, item.rel)) != _digest(item.slicer):
            result.stale.append(item)
            continue
        if item.action == "pull":
            _pull(pair, item)
            _record(base, item.rel, item.slicer)
        else:
            assert result.backup_dir is not None
            _push(pair, item, result.backup_dir, stamp)
            pushed = slicer_entry(pair.user_dir, item.rel)
            if (
                item.repo is not None
                and pushed is not None
                and pushed.digest != item.repo.digest
            ):
                # The repo file was not in the slicer's own format: adopt the slicer's bytes.
                _pull(pair, Item(item.rel, "pull", "normalized", item.repo, pushed))
            _record(base, item.rel, pushed)
        result.done.append(item)
    state.set_base(pair, base)
    return result


def _record(base: dict[str, str], rel: str, entry: Entry | None) -> None:
    if entry is None:
        base.pop(rel, None)
    else:
        base[rel] = entry.digest
