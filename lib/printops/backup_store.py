"""backup_store.py — the on-disk format of the Spoolman/PrintOps backups: write, verify, list, prune, restore.

ACCESSED VIA: `printops.backup_run` (the nightly run), `3d printops backup verify|ls|prune|restore`.

LAYOUT (one backup SET per run, written under `<disk>/<subdir>/`):
    3d-backup-20261101T040003Z/
        spoolman.db.gz   printops.db.gz   tokens.json      the items (SQLite files gzipped at level 1)
        MANIFEST.json    what, from where, sizes, row counts per table, sha256 of every stored file, and
                         `absent`: items whose source did not exist (a missing tokens file is a valid
                         deny-all state; restoring must not resurrect a stale one)
        SHA256SUMS       `sha256sum -c` compatible
    <disk>/.3d-backup-disk    the DISK MARKER: JSON {"id": ...}; a run refuses a disk whose marker
                              is missing or has another id (an unmounted disk must not fill the root FS)

INVARIANTS:
  - A set appears atomically: it is built in `.3d-backup-tmp-*`, verified there (every stored file is
    decompressed again and `PRAGMA integrity_check`ed), then `rename`d to its final name.
  - Prune matches ONLY `backup_policy.SET_RE` directories that carry a MANIFEST; anything else in the
    directory (other files, other dirs, a set without a manifest) is left alone.
  - Nothing here touches a live database except the SQLite online-backup read (`printops.backup`).
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from printops import backup as low_level
from printops.backup_policy import (
    SET_RE,
    TMP_RE,
    Retention,
    parse_stamp,
    plan_prune,
    set_name,
    stamp_now,
)

MARKER = ".3d-backup-disk"
STATE_FILE = ".3d-backup-state.json"
MANIFEST = "MANIFEST.json"
SUMS = "SHA256SUMS"
FORMAT = 1
CHUNK = 1 << 20


class BackupRefused(RuntimeError):
    """The run must not start (wrong/missing disk, not enough space). Nothing was written."""


class BackupBroken(RuntimeError):
    """A backup set (or a run) is not good: failed integrity, checksum or write."""


@dataclass(frozen=True)
class Source:
    name: str  # `spoolman`, `printops`, `tokens`, ...: the item name inside the set
    path: Path
    sqlite: bool


@dataclass(frozen=True)
class SetInfo:
    name: str
    path: Path
    created: datetime.datetime
    manifest: dict[str, Any] | None

    @property
    def stored_bytes(self) -> int:
        items = (self.manifest or {}).get("items", {})
        return int(sum(i.get("stored_bytes", 0) for i in items.values()))


# ------------------------------------------------------------------------------------- the disk


def read_marker(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("id"), str) else None


def write_marker(root: Path, note: str = "") -> str:
    """Identify `root` as a backup disk. Refuses to replace an existing marker (its id is in configs)."""
    if read_marker(root) is not None:
        raise BackupRefused(
            f"{root / MARKER} already exists; its id is what the configured targets expect"
        )
    ident = uuid.uuid4().hex
    body = {
        "id": ident,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "note": note,
        "writer": "3d printops backup",
    }
    tmp = root / f".{MARKER}.tmp"
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, root / MARKER)
    return ident


def check_disk(root: Path, expect_id: str | None) -> str:
    """The marker id of `root`, or BackupRefused: missing dir/marker, or not the expected disk."""
    if not root.is_dir():
        raise BackupRefused(
            f"backup disk {root} is not there (not mounted?); nothing was written"
        )
    marker = read_marker(root)
    if marker is None:
        raise BackupRefused(
            f"{root / MARKER} is missing: this is not the backup disk (or it is not mounted); "
            "nothing was written"
        )
    if expect_id and marker["id"] != expect_id:
        raise BackupRefused(
            f"{root} carries disk id {marker['id']}, the target expects {expect_id}; "
            "another disk is mounted there; nothing was written"
        )
    return str(marker["id"])


def check_space(dest: Path, sources: list[Source], min_free_bytes: int) -> int:
    """Free bytes on `dest`'s filesystem, or BackupRefused when it cannot hold the run + the reserve."""
    need = sum(s.path.stat().st_size for s in sources if s.path.is_file())
    free = shutil.disk_usage(dest).free
    if free < need + min_free_bytes:
        raise BackupRefused(
            f"{dest}: {free} bytes free, the run needs up to {need} + {min_free_bytes} reserve; "
            "nothing was written"
        )
    return free


# ------------------------------------------------------------------------------------ writing


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def table_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            n: int(conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0])
            for n in names
        }
    finally:
        conn.close()


def _gzip(src: Path, dst: Path, level: int) -> None:
    with (
        src.open("rb") as fin,
        gzip.GzipFile(dst, "wb", compresslevel=level, mtime=0) as fout,
    ):
        shutil.copyfileobj(fin, fout, CHUNK)


def _gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fin, dst.open("wb") as fout:
        shutil.copyfileobj(fin, fout, CHUNK)


def _integrity(db: Path) -> str:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _write_item(src: Source, tmp: Path, level: int) -> dict[str, Any]:
    if not src.sqlite:
        dst = tmp / src.path.name
        shutil.copyfile(src.path, dst)
        dst.chmod(0o600)
        return {
            "source": str(src.path),
            "file": dst.name,
            "kind": "file",
            "raw_bytes": dst.stat().st_size,
            "stored_bytes": dst.stat().st_size,
            "sha256": sha256_file(dst),
        }
    raw = tmp / f".{src.name}.raw"
    try:  # online backup + integrity_check
        low_level.snapshot_db(src.path, raw, pages=256, pause=0.02)
    except low_level.BackupError as e:
        raise BackupBroken(f"{src.name}: {e}") from e
    stored = tmp / f"{src.name}.db.gz"
    _gzip(raw, stored, level)
    item = {
        "source": str(src.path),
        "file": stored.name,
        "kind": "sqlite",
        "raw_bytes": raw.stat().st_size,
        "stored_bytes": stored.stat().st_size,
        "sha256": sha256_file(stored),
        "integrity": "ok",
        "tables": table_counts(raw),
    }
    raw.unlink()
    stored.chmod(0o600)
    return item


def _seal(
    tmp: Path,
    items: dict[str, dict[str, Any]],
    created: str,
    target: str,
    absent: dict[str, str],
) -> None:
    manifest = {
        "format": FORMAT,
        "created": created,
        "host": socket.gethostname(),
        "target": target,
        "tool": "3d printops backup",
        "items": items,
        "absent": absent,
    }
    (tmp / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    names = sorted(p.name for p in tmp.iterdir() if p.is_file() and p.name != SUMS)
    (tmp / SUMS).write_text(
        "".join(f"{sha256_file(tmp / n)}  {n}\n" for n in names), encoding="utf-8"
    )
    for p in tmp.iterdir():
        p.chmod(0o600)
        fd = os.open(p, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_set(
    dest: Path,
    sources: list[Source],
    *,
    target: str = "",
    level: int = 1,
    now: datetime.datetime | None = None,
    absent: dict[str, str] | None = None,
) -> SetInfo:
    """Write, verify and atomically publish one set under `dest` (which must exist).

    `absent` maps an item name to the file name it would have been restored as, for a source that
    did not exist at backup time (`{"tokens": "tokens.json"}`): restoring this set removes that file."""
    when = now or datetime.datetime.now(datetime.timezone.utc)
    final = dest / set_name(stamp_now(when))
    if final.exists():
        raise BackupBroken(f"{final} already exists (two runs in the same second)")
    tmp = Path(tempfile.mkdtemp(prefix=".3d-backup-tmp-", dir=dest))
    try:
        tmp.chmod(0o700)
        items = {s.name: _write_item(s, tmp, level) for s in sources}
        _seal(tmp, items, when.isoformat(timespec="seconds"), target, absent or {})
        _manifest, problems = verify_dir(tmp)
        if problems:
            raise BackupBroken(
                "the new set failed its own verification: " + "; ".join(problems)
            )
        os.rename(tmp, final)
        _fsync_dir(dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return SetInfo(
        final.name,
        final,
        when,
        json.loads((final / MANIFEST).read_text(encoding="utf-8")),
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------------- verifying


def _sum_problems(root: Path) -> list[str]:
    sums = root / SUMS
    if not sums.is_file():
        return [f"{SUMS} is missing"]
    out: list[str] = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        target = root / name
        if not name or "/" in name or not target.is_file():
            out.append(f"{name or line!r}: listed in {SUMS} but not a file of the set")
        elif sha256_file(target) != digest:
            out.append(f"{name}: sha256 differs from {SUMS}")
    return out


def _plain_name(value: object) -> bool:
    """A single path component: nothing a manifest says may name a place outside its own directory."""
    return (
        isinstance(value, str)
        and value not in ("", ".", "..", MANIFEST, SUMS)
        and not any(c in value for c in "/\\\0")
    )


def _item_problems(root: Path, name: str, item: dict[str, Any]) -> list[str]:
    if (
        not isinstance(item, dict)
        or not _plain_name(name)
        or not _plain_name(item.get("file"))
    ):
        return [
            f"{name!r}: the manifest names a path outside the set (item or file is not a plain name)"
        ]
    file = root / item["file"]
    if file.is_symlink() or file.resolve().parent != root.resolve():
        return [f"{name}: file {item['file']!r} is not a regular file inside the set"]
    if not file.is_file():
        return [f"{name}: file {item.get('file')!r} is missing"]
    if sha256_file(file) != item.get("sha256"):
        return [f"{name}: sha256 differs from the manifest"]
    if item.get("kind") != "sqlite":
        return []
    with tempfile.TemporaryDirectory(prefix="3d-backup-verify-") as scratch:
        raw = Path(scratch) / "db"
        _gunzip(file, raw)
        verdict = _integrity(raw)
        counts = table_counts(raw) if verdict == "ok" else {}
    if verdict != "ok":
        return [f"{name}: PRAGMA integrity_check says {verdict}"]
    return (
        []
        if counts == item.get("tables")
        else [f"{name}: row counts differ from the manifest"]
    )


def _absent_problems(manifest: dict[str, Any]) -> list[str]:
    absent = manifest.get("absent", {})
    if isinstance(absent, dict) and all(
        _plain_name(k) and _plain_name(v) for k, v in absent.items()
    ):
        return []
    return [f"{MANIFEST}: `absent` must map plain item names to plain file names"]


def verify_dir(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """(the manifest parsed ONCE, every problem of the set at `root`; [] = good): checksums, manifest,
    integrity, row counts. Callers that go on to use the set must use this manifest, never re-read it."""
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, [f"{MANIFEST}: {e}"]
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, dict) or not items:
        return None, [f"{MANIFEST} lists no items"]
    problems = _sum_problems(root) + _absent_problems(manifest)
    for name, item in items.items():
        problems += _item_problems(root, name, item)
    return manifest, problems


# ------------------------------------------------------------------------------ list / prune


def list_sets(dest: Path) -> list[SetInfo]:
    """Sets of `dest` (names matching SET_RE, real directories), newest first."""
    out: list[SetInfo] = []
    if not dest.is_dir():
        return out
    for p in dest.iterdir():
        created = parse_stamp(p.name)
        if created is None or p.is_symlink() or not p.is_dir():
            continue
        try:
            manifest = json.loads((p / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        out.append(
            SetInfo(
                p.name, p, created, manifest if isinstance(manifest, dict) else None
            )
        )
    return sorted(out, key=lambda s: s.created, reverse=True)


def stale_tmp_dirs(dest: Path, older_than_s: float = 86400.0) -> Iterator[Path]:
    """Leftovers of a crashed run: `.3d-backup-tmp-*` directories older than a day."""
    if not dest.is_dir():
        return
    cutoff = datetime.datetime.now().timestamp() - older_than_s
    for p in dest.iterdir():
        if (
            TMP_RE.fullmatch(p.name)
            and p.is_dir()
            and not p.is_symlink()
            and p.stat().st_mtime < cutoff
        ):
            yield p


def prune(
    dest: Path, retention: Retention, *, dry_run: bool = False
) -> tuple[list[str], list[str]]:
    """(kept, removed) set names. Only manifest-carrying directories named like a set are candidates."""
    sets = {s.name: s for s in list_sets(dest) if s.manifest is not None}
    keep, drop = plan_prune(sets, retention)
    if not dry_run:
        for name in drop:
            victim = sets[name].path
            if (
                victim.parent != dest
                or not SET_RE.fullmatch(victim.name)
                or victim.is_symlink()
            ):
                raise BackupBroken(
                    f"refusing to remove {victim}: not a backup set of {dest}"
                )
            shutil.rmtree(victim)
        for leftover in stale_tmp_dirs(dest):
            shutil.rmtree(leftover, ignore_errors=True)
    return keep, drop


# ------------------------------------------------------------------------------------ state


def write_state(dest: Path, retention: Retention) -> None:
    """Remember the last policy a run used, so a hand-run `prune` never prunes harder than the schedule."""
    tmp = dest / f"{STATE_FILE}.tmp"
    tmp.write_text(
        json.dumps({"retention": retention.to_dict()}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, dest / STATE_FILE)


def read_state(dest: Path) -> Retention | None:
    try:
        raw = json.loads((dest / STATE_FILE).read_text(encoding="utf-8"))["retention"]
        return Retention(int(raw["daily"]), int(raw["weekly"]), int(raw["monthly"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------------- restore


@dataclass(frozen=True)
class Restored:
    """What a restore did, taken from the manifest that was verified (not from the listing)."""

    written: list[Path]
    absent: dict[
        str, str
    ]  # items the set records as absent -> the file name moved aside
    items: dict[str, dict[str, Any]]


def _stage_set(root: Path, scratch: Path) -> tuple[dict[str, Any], list[str]]:
    """Copy the set's files into the private `scratch` dir, then verify THAT copy: a set on a NAS can
    change between two reads, so everything restore uses is parsed and hashed from one stable copy."""
    problems: list[str] = []
    for entry in root.iterdir():
        if entry.is_symlink():
            problems.append(f"{entry.name}: not a regular file inside the set (a link)")
        elif entry.is_file():
            shutil.copyfile(entry, scratch / entry.name)
    manifest, found = verify_dir(scratch)
    return manifest or {}, problems + found


def restore_set(info: SetInfo, into: Path, *, layout: str = "flat") -> Restored:
    """Unpack a VERIFIED set into `into`.

    flat: `<into>/<item>.db` / `<into>/tokens.json`.  data: the compose data-dir layout —
    `<into>/spoolman/spoolman.db`, `<into>/api/printops.db`, `<into>/api/tokens.json`. An existing
    target file is moved to `<name>.pre-restore` first, together with its -wal/-shm sidecars, and an
    existing `.pre-restore` is never overwritten (`.pre-restore.1`, ...). An item the set records as
    absent (no tokens file when it was taken) is moved aside the same way, so the restored state is
    the backed-up one: nothing there, deny-all.

    Every new file is unpacked next to its target BEFORE any live file is moved, so a full disk
    leaves the live databases where they are."""
    with tempfile.TemporaryDirectory(prefix="3d-backup-stage-") as scratch_dir:
        scratch = Path(scratch_dir)
        manifest, problems = _stage_set(info.path, scratch)
        if problems:
            raise BackupBroken(f"{info.name} is not restorable: " + "; ".join(problems))
        items: dict[str, dict[str, Any]] = manifest["items"]
        absent: dict[str, str] = dict(manifest.get("absent", {}))
        pending: list[tuple[Path, Path]] = []
        try:
            for name, item in items.items():
                out = _restore_path(into, name, item, layout)
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(f".{out.name}.restoring")
                tmp.unlink(missing_ok=True)  # never write through a leftover link
                pending.append((tmp, out))
                if item.get("kind") == "sqlite":
                    _gunzip(scratch / item["file"], tmp)
                else:
                    shutil.copyfile(scratch / item["file"], tmp)
                tmp.chmod(0o600)
        except BaseException:
            for tmp, _out in pending:
                tmp.unlink(missing_ok=True)
            raise
        for tmp, out in pending:
            _move_aside(out)
            os.replace(tmp, out)
        for name, filename in absent.items():
            _move_aside(
                _restore_path(into, name, {"kind": "file", "file": filename}, layout)
            )
    return Restored([out for _tmp, out in pending], absent, items)


def _restore_path(into: Path, name: str, item: dict[str, Any], layout: str) -> Path:
    filename = f"{name}.db" if item.get("kind") == "sqlite" else str(item["file"])
    if layout != "data":
        return into / filename
    return into / ("spoolman" if name == "spoolman" else "api") / filename


SIDECARS = ("", "-wal", "-shm")


def _move_aside(out: Path) -> None:
    """Rename `out` and its -wal/-shm to `<out>.pre-restore[...]`: the WAL of a database that was not shut
    down cleanly holds committed rows. A `.pre-restore` that is already there stays; the next free
    `.pre-restore.N` is used."""
    if not any(out.with_name(out.name + s).exists() for s in SIDECARS):
        return
    base = out.name + ".pre-restore"
    n = 0
    while any(out.with_name(base + s).exists() for s in SIDECARS):
        n += 1
        base = f"{out.name}.pre-restore.{n}"
    for s in SIDECARS:
        src = out.with_name(out.name + s)
        if src.exists():
            os.replace(src, out.with_name(base + s))


def restore_check(info: SetInfo) -> list[str]:
    """Restore the set into a scratch directory and prove it: every SQLite item opens, passes
    integrity_check and has the manifest's row counts. Returns report lines; BackupBroken on a mismatch."""
    report: list[str] = []
    with tempfile.TemporaryDirectory(prefix="3d-backup-restore-") as scratch:
        restored = restore_set(info, Path(scratch))
        written = {p.name: p for p in restored.written}
        for name, item in restored.items.items():
            if item.get("kind") != "sqlite":
                report.append(
                    f"{name}: restored {item['file']} ({item['raw_bytes']} bytes)"
                )
                continue
            db = written[f"{name}.db"]
            verdict, counts = _integrity(db), table_counts(db)
            if verdict != "ok" or counts != item.get("tables"):
                raise BackupBroken(
                    f"restore check of {name} failed: integrity {verdict}, tables {counts}"
                )
            rows = ", ".join(f"{t}={n}" for t, n in counts.items() if n)
            report.append(
                f"{name}: restored, integrity ok, {sum(counts.values())} rows ({rows or 'empty'})"
            )
    return report
