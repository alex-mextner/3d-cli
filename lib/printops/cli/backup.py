"""`3d printops backup` — snapshot the PrintOps DB, tokens file and Spoolman DB into a backup dir."""
from __future__ import annotations

import os
from pathlib import Path

from errors import GateFailure, UsageError
from printops.backup import BackupError, Snapshot, copy_file, snapshot_db
from printops.cli import Sub
from printops.cli._common import parse
from printops.settings import Settings

USAGE = """3d printops backup [--dest DIR] [--spoolman-db PATH]
  Write consistent snapshots (SQLite online backup + integrity_check + atomic rename) of:
    DIR/printops.db   the PrintOps DB ($PRINTOPS_DB)
    DIR/tokens.json   the 3d-api tokens file ($PRINTOPS_TOKENS_FILE; sha256 only), if present
    DIR/spoolman.db   Spoolman's SQLite DB, if --spoolman-db / $SPOOLMAN_DB is set
  On the Odroid the printops-backup.timer runs this daily at 04:15 inside the 3d-api container,
  into /home/ultra/homeassistant/printops-backup/, which Home Assistant's automatic daily backup
  then includes (the host's existing backup contour).

Options:
  --dest DIR          backup directory (default: $PRINTOPS_BACKUP_DIR)
  --spoolman-db PATH  Spoolman's spoolman.db (default: $SPOOLMAN_DB; skipped when unset)

Examples:
  3d printops backup --dest ~/printops-backup
  ssh ultra@192.168.0.39 docker exec printops-api 3d printops backup
  3d printops backup --dest /backup --spoolman-db /spoolman-data/spoolman.db"""


def _one(label: str, src: Path, target: Path, is_db: bool) -> Snapshot | str:
    """The snapshot, or a failure message (every item is attempted even if one fails)."""
    try:
        return snapshot_db(src, target) if is_db else copy_file(src, target)
    except BackupError as e:
        return f"{label}: {e}"


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(argv, "backup", values={"--dest", "--spoolman-db"})
    dest_raw = p.opts.get("--dest") or os.environ.get("PRINTOPS_BACKUP_DIR")
    if not dest_raw:
        raise UsageError("no backup directory: pass --dest DIR or set PRINTOPS_BACKUP_DIR", command="printops backup")
    dest = Path(dest_raw).expanduser()
    settings = Settings.from_env()
    spoolman_raw = p.opts.get("--spoolman-db") or os.environ.get("SPOOLMAN_DB")

    jobs: list[tuple[str, Path, Path, bool]] = [("printops", settings.db_path, dest / "printops.db", True)]
    if settings.tokens_file.is_file():
        jobs.append(("tokens", settings.tokens_file, dest / "tokens.json", False))
    if spoolman_raw:
        jobs.append(("spoolman", Path(spoolman_raw).expanduser(), dest / "spoolman.db", True))
    sources = {src.resolve() for _, src, _, _ in jobs if src.exists()}
    clashes = [str(target) for _, _, target, _ in jobs if target.resolve() in sources]
    if clashes:
        raise UsageError(f"backup destination would overwrite a live source: {', '.join(clashes)}",
                         command="printops backup", remediation=["Choose a --dest outside the data dirs."])
    if not settings.tokens_file.is_file():
        # No tokens file = every token revoked; a stale copy must not survive to be restored.
        (dest / "tokens.json").unlink(missing_ok=True)
    results = [_one(*job) for job in jobs]
    done = [r for r in results if isinstance(r, Snapshot)]
    failed = [r for r in results if isinstance(r, str)]
    for snap in done:
        print(f"ok    {snap.source} -> {snap.dest}  ({snap.size} bytes)")
    for msg in failed:
        print(f"FAIL  {msg}")
    if failed:
        raise GateFailure(f"{len(failed)} of {len(jobs)} backup item(s) failed", command="printops backup")
    return 0


SUB = Sub(name="backup", summary="snapshot PrintOps + Spoolman DBs (daily on the Odroid)", usage=USAGE, run=run)
