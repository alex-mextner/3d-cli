"""library/db.py — the local library database: path, open+migrate, typed inserts, status and export.

WHAT: one SQLite file (`data_dir()/library/library.db`, or `$THREED_LIBRARY_DB`) holding search sessions and
results, downloaded model sources, designs, makes, photos, publications and relations between them. It reuses
`printops.db` for the connection (WAL, foreign_keys=ON, `BEGIN IMMEDIATE` transactions) and migrations
(checksummed `NNNN_<name>.sql`), with its OWN migration directory `lib/library/migrations/` numbered from 0001
(engine decision: docs/library-db.md).

ACCESSED VIA: `3d library` (`lib/commands/library.py`), `library.actions` and the slices S5/S7/S8 which write
rows with `insert_row` and read with plain SQL on the `sqlite3.Row` connection.

INVARIANTS:
  - `open_library()` always returns a migrated connection; running it twice is a no-op.
  - Every row has a full UUIDv7 `uid` (never a prefix) and an ISO-8601 UTC `created_at`.
  - `insert_row(..., source_key=K)` is idempotent: the second call returns the first row and `created=False`.
  - Every insert emits `library.<table>.created` through `printops.events.emit` (inside the same transaction).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli import paths
from printops import db, events
from printops.printer_task_buffer import uuid7

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
ENV_DB = "THREED_LIBRARY_DB"
SCHEMA = "3d.library/1"
ACTOR = "cli:library"

# table -> (insertable columns, JSON columns). `id`, `uid`, `created_at` are generated.
TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "search_session": (("query", "parent_id", "kind"), ()),
    "search_result": (
        (
            "session_id",
            "site",
            "model_id",
            "url",
            "title",
            "rank",
            "score",
            "verdict",
            "rationale",
            "analytics",
            "image_url",
        ),
        ("analytics",),
    ),
    "model_source": (
        ("site", "model_id", "url", "license", "author", "dir", "files", "sha256"),
        ("files",),
    ),
    "design": (("name", "project_dir"), ()),
    "make": (
        (
            "design_id",
            "source_id",
            "printer",
            "filament",
            "status",
            "queued_at",
            "finished_at",
            "gcode",
        ),
        (),
    ),
    "photo": (("make_id", "design_id", "path", "kind", "z_mm", "taken_at"), ()),
    "publication": (("design_id", "platform", "url", "state"), ()),
    "relation": (("from_kind", "from_id", "to_kind", "to_id", "rel"), ()),
}


@dataclass(frozen=True)
class Inserted:
    table: str
    id: int
    uid: str
    created: bool  # False when `source_key` matched an existing row


def library_db_path() -> Path:
    """`$THREED_LIBRARY_DB`, else `<data_dir>/library/library.db` (not created here)."""
    raw = os.environ.get(ENV_DB)
    return (
        Path(raw).expanduser() if raw else paths.data_dir() / "library" / "library.db"
    )


def open_library(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating dirs and file) and migrate the library database."""
    conn = db.open_db(path if path is not None else library_db_path())
    db.migrate(conn, MIGRATIONS_DIR)
    return conn


def new_uid() -> str:
    """A full UUIDv7 string."""
    return uuid7()


def insert_row(
    conn: sqlite3.Connection,
    table: str,
    values: dict[str, Any],
    *,
    source_key: str | None = None,
    actor: str = ACTOR,
) -> Inserted:
    """Insert one row (own transaction) with generated uid/created_at; idempotent under `source_key`.

    JSON columns accept any JSON-able Python value and are stored compact; unknown columns raise `KeyError`.
    """
    if table not in TABLES:
        raise KeyError(f"unknown library table {table!r}; known: {sorted(TABLES)}")
    columns, json_columns = TABLES[table]
    unknown = set(values) - set(columns)
    if unknown:
        raise KeyError(
            f"{table}: unknown column(s) {sorted(unknown)}; insertable: {list(columns)}"
        )
    row = {
        k: (
            json.dumps(v, sort_keys=True, separators=(",", ":"))
            if k in json_columns
            else v
        )
        for k, v in values.items()
    }
    with db.transaction(conn):
        if source_key is not None:
            hit = conn.execute(
                f"SELECT id, uid FROM {table} WHERE source_key = ?", (source_key,)
            ).fetchone()
            if hit is not None:
                return Inserted(table, int(hit["id"]), str(hit["uid"]), False)
        uid = new_uid()
        row.update({"uid": uid, "created_at": db.utc_now(), "source_key": source_key})
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(row.values())
        )
        events.emit(
            conn,
            f"library.{table}.created",
            f"{table}:{uid}",
            {"id": cur.lastrowid},
            actor,
        )
        return Inserted(table, int(cur.lastrowid or 0), uid, True)


def status(conn: sqlite3.Connection, path: str | Path | None = None) -> dict[str, Any]:
    """Stable `3d.library/1` status document: path, applied migrations, row counts."""
    applied = db.applied_versions(conn)
    counts = {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in TABLES
    }
    return {
        "schema": SCHEMA,
        "path": str(path if path is not None else library_db_path()),
        "migrations": sorted(applied),
        "migrations_pending": [
            m.version
            for m in db.discover_migrations(MIGRATIONS_DIR)
            if m.version not in applied
        ],
        "counts": counts,
        "events": int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
    }


def export(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every library table as decoded JSON rows (JSON columns parsed), for backup and agent hand-off."""
    out: dict[str, Any] = {"schema": SCHEMA, "exported_at": db.utc_now(), "tables": {}}
    for table, (_, json_columns) in TABLES.items():
        rows = []
        for r in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
            d = dict(r)
            for col in json_columns:
                d[col] = json.loads(d[col])
            rows.append(d)
        out["tables"][table] = rows
    return out
