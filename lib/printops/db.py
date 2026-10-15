"""db.py — the PrintOps SQLite store: connection setup and ordered, checksummed migrations.

ACCESSED VIA: `printops.api` (one connection per request), `3d printops backup`, and every
slice's domain module. Slices never edit this file: each adds its own
`lib/printops/migrations/NNNN_<slice>.sql` from its reserved number range (see the PrintOps
contract: 0001–0009 core, 0010 locations, 0020 spools/ledger, …).

INVARIANTS:
  - Connections run in autocommit mode (`isolation_level=None`): a statement outside
    `transaction()` commits on its own, and multi-statement writes use `transaction()`, which
    takes the write lock up front (`BEGIN IMMEDIATE`) so two writers never deadlock on upgrade.
  - WAL journal, `foreign_keys=ON`, `sqlite3.Row` rows, 5 s busy timeout.
  - `schema_migrations(version, name, checksum, applied_at)` records every applied file. A
    migration is applied atomically (its statements plus its bookkeeping row in one
    transaction). An applied file whose content later changes is refused — a migration that
    already ran in production is history; the fix is a new migration.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
BUSY_TIMEOUT_MS = 5000

# Called after a transaction() commits; printops.events wires its bus wake-up here so
# listeners learn about rows that only became visible at COMMIT.
_after_commit_hooks: list[Callable[[], None]] = []


class MigrationError(RuntimeError):
    """A migration file is malformed, duplicated, edited after it ran, or failed to apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating parent dirs) a PrintOps database with the standard pragmas."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def on_commit(hook: Callable[[], None]) -> None:
    """Register a callback run after every successful `transaction()` commit."""
    if hook not in _after_commit_hooks:
        _after_commit_hooks.append(hook)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` … `COMMIT` (or `ROLLBACK` on error). Not re-entrant."""
    if conn.in_transaction:
        raise RuntimeError("printops.db.transaction() is not re-entrant: a transaction is already open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")  # inside the try: a commit-time failure (deferred FK) must roll back too
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    for hook in _after_commit_hooks:
        hook()


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All `NNNN_<name>.sql` files in version order; rejects strays and duplicate versions."""
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if m is None:
            raise MigrationError(
                f"migration file {path.name!r} does not match NNNN_<slice>.sql (lower-case, digits, _)"
            )
        version = int(m.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: {found[version].path.name} and {path.name}"
            )
        found[version] = Migration(version, m.group(2), path, path.read_text(encoding="utf-8"))
    return [found[v] for v in sorted(found)]


def split_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements (trigger bodies stay intact)."""
    statements: list[str] = []
    buf = ""
    for piece in sql.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt.strip(";").strip() and not _only_comments(stmt):
                statements.append(stmt)
            buf = ""
    if buf.rstrip(";").strip() and not _only_comments(buf):
        raise MigrationError(f"incomplete SQL statement at end of migration: {buf.strip()[:80]!r}")
    return statements


def _only_comments(stmt: str) -> bool:
    lines = [ln.strip() for ln in stmt.rstrip(";").splitlines()]
    return all(not ln or ln.startswith("--") for ln in lines)


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " checksum TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    """version -> checksum of every applied migration."""
    _ensure_bookkeeping(conn)
    return {int(r["version"]): str(r["checksum"]) for r in conn.execute("SELECT version, checksum FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration in order; return the versions applied by this call."""
    migrations = discover_migrations(directory)
    applied_now: list[int] = []
    for mig in migrations:
        with transaction(conn):
            done = applied_versions(conn)
            if mig.version in done:
                if done[mig.version] != mig.checksum:
                    raise MigrationError(
                        f"migration {mig.path.name} changed after it was applied "
                        f"(recorded sha256 {done[mig.version][:12]}…, file {mig.checksum[:12]}…); "
                        "add a new migration instead of editing an applied one"
                    )
                continue
            try:
                for stmt in split_statements(mig.sql):
                    conn.execute(stmt)
            except sqlite3.Error as e:
                raise MigrationError(f"migration {mig.path.name} failed: {e}") from e
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (mig.version, mig.name, mig.checksum, utc_now()),
            )
            applied_now.append(mig.version)
    return applied_now


def utc_now() -> str:
    """ISO-8601 UTC timestamp with milliseconds and a `Z` suffix (sortable as text)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
