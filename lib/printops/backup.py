"""backup.py — the low-level copy primitive of the backups: a consistent SQLite snapshot.

ACCESSED VIA: `printops.backup_store` (which builds the backup sets `3d printops backup run` writes;
the schedule is Home Assistant's, integrations/homeassistant/packages/printops_backup.yaml).

INVARIANTS:
  - SQLite online-backup API (the same mechanism as `sqlite3 .backup`): consistent while the
    services keep writing, WAL included.
  - Each snapshot is written to a temp file, `PRAGMA integrity_check`ed, then atomically renamed
    over the previous one — a failed run never replaces a good snapshot with a bad one.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    source: Path
    dest: Path
    size: int


def _refuse_overwriting_source(src: Path, dest: Path) -> None:
    # Renaming a snapshot over the live DB would swap the inode under open connections, and
    # their later WAL checkpoints would write to the orphaned file: data loss.
    if dest.resolve() == src.resolve() or (dest.exists() and dest.samefile(src)):
        raise BackupError(
            f"backup destination {dest} is the source itself; choose another --dest"
        )


def snapshot_db(src: Path, dest: Path) -> Snapshot:
    """Copy SQLite DB `src` to `dest` consistently (online backup + integrity check + rename).

    One backup step: the copy reads a single WAL snapshot, so Spoolman keeps writing meanwhile and a write
    never restarts the copy. (CPython's `Connection.backup(pages=, sleep=)` sleeps only on BUSY/LOCKED, never
    between steps, so there is no pacing to configure; the run's nice 19 + ionice idle keep the host usable.)"""
    if not src.is_file():
        raise BackupError(f"database {src} does not exist")
    _refuse_overwriting_source(src, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}-", suffix=".tmp", dir=str(dest.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        source = sqlite3.connect(
            f"{src.resolve().as_uri()}?mode=rw", uri=True, timeout=30
        )
        try:
            target = sqlite3.connect(str(tmp))
            try:
                source.backup(target)
                verdict = target.execute("PRAGMA integrity_check").fetchone()[0]
                # A snapshot must be self-contained: no -wal sidecar next to it.
                target.execute("PRAGMA journal_mode = DELETE")
            finally:
                target.close()
        finally:
            source.close()
        if verdict != "ok":
            raise BackupError(f"snapshot of {src} failed integrity_check: {verdict}")
        tmp.chmod(0o600)
        os.replace(tmp, dest)
    except sqlite3.Error as e:
        raise BackupError(f"snapshot of {src} failed: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    return Snapshot(src, dest, dest.stat().st_size)
