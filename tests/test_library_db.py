from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from library import db as librarydb
from printops import db as pdb
from printops import events

UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = librarydb.open_library(tmp_path / "nested" / "library.db")
    yield c
    c.close()


def test_fresh_path_opens_and_migrates_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "library.db"
    c1 = librarydb.open_library(path)
    applied = sorted(pdb.applied_versions(c1))
    assert applied == [1] and path.is_file()
    assert pdb.migrate(c1, librarydb.MIGRATIONS_DIR) == []  # second run: no-op
    c1.close()
    c2 = librarydb.open_library(path)  # reopening is a no-op too
    assert sorted(pdb.applied_versions(c2)) == applied
    c2.close()


def test_pragmas_foreign_keys_and_wal(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migration_numbering_is_separate_from_printops() -> None:
    mine = {m.version for m in pdb.discover_migrations(librarydb.MIGRATIONS_DIR)}
    assert mine == {1}
    assert librarydb.MIGRATIONS_DIR != pdb.MIGRATIONS_DIR


def test_edited_applied_migration_is_refused(tmp_path: Path) -> None:
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "0001_x.sql").write_text("CREATE TABLE x (a);", encoding="utf-8")
    c = pdb.open_db(tmp_path / "x.db")
    pdb.migrate(c, mdir)
    (mdir / "0001_x.sql").write_text("CREATE TABLE x (a, b);", encoding="utf-8")
    with pytest.raises(pdb.MigrationError, match="changed after it was applied"):
        pdb.migrate(c, mdir)


def test_search_result_is_unique_per_session_site_model(conn: sqlite3.Connection) -> None:
    s = librarydb.insert_row(conn, "search_session", {"query": "case", "kind": "wide"})
    row = {"session_id": s.id, "site": "printables", "model_id": "42", "url": "https://x/42", "title": "Case"}
    librarydb.insert_row(conn, "search_result", row)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        librarydb.insert_row(conn, "search_result", row)
    other = librarydb.insert_row(conn, "search_session", {"query": "case v2", "kind": "rewrite", "parent_id": s.id})
    librarydb.insert_row(conn, "search_result", {**row, "session_id": other.id})  # same model, other session: ok


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        librarydb.insert_row(conn, "make", {"design_id": 999, "status": "queued", "queued_at": pdb.utc_now()})
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        librarydb.insert_row(conn, "search_result", {
            "session_id": 999, "site": "s", "model_id": "1", "url": "u", "title": "t"})


def test_invalid_json_is_rejected_by_check_constraints(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO model_source (uid, site, model_id, url, files, created_at) "
            "VALUES ('u', 's', '1', 'x', '{not json', 't')")
    s = librarydb.insert_row(conn, "search_session", {"query": "q", "kind": "user"})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO search_result (uid, session_id, site, model_id, url, title, analytics, created_at) "
            "VALUES ('u2', ?, 's', '1', 'x', 't', 'nope', 't')", (s.id,))


def test_enum_checks_and_photo_target(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        librarydb.insert_row(conn, "search_session", {"query": "q", "kind": "bogus"})
    d = librarydb.insert_row(conn, "design", {"name": "case"})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        librarydb.insert_row(conn, "make", {"design_id": d.id, "status": "paused", "queued_at": pdb.utc_now()})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # a photo belongs to a make or a design
        librarydb.insert_row(conn, "photo", {"path": "/x.jpg", "kind": "table", "taken_at": pdb.utc_now()})
    librarydb.insert_row(conn, "photo", {"design_id": d.id, "path": "/x.jpg", "kind": "render",
                                         "taken_at": pdb.utc_now()})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        librarydb.insert_row(conn, "relation", {
            "from_kind": "design", "from_id": d.uid, "to_kind": "design", "to_id": d.uid, "rel": "remix_of"})


def test_uids_are_full_uuidv7_and_timestamps_iso_utc(conn: sqlite3.Connection) -> None:
    ins = librarydb.insert_row(conn, "design", {"name": "d"})
    assert UUID7.match(ins.uid)
    created = conn.execute("SELECT created_at FROM design").fetchone()[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", created)


def test_source_key_makes_inserts_idempotent(conn: sqlite3.Connection) -> None:
    a = librarydb.insert_row(conn, "model_source", {"site": "printables", "model_id": "1", "url": "u"},
                             source_key="import:printables:1")
    b = librarydb.insert_row(conn, "model_source", {"site": "printables", "model_id": "1", "url": "u"},
                             source_key="import:printables:1")
    assert (a.created, b.created) == (True, False) and a.uid == b.uid
    assert conn.execute("SELECT COUNT(*) FROM model_source").fetchone()[0] == 1


def test_inserts_emit_events_through_printops(conn: sqlite3.Connection) -> None:
    ins = librarydb.insert_row(conn, "design", {"name": "d"})
    last = events.last(conn, 1)[0]
    assert last.kind == "library.design.created" and last.subject == f"design:{ins.uid}"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_unknown_table_or_column_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError, match="unknown library table"):
        librarydb.insert_row(conn, "events", {})
    with pytest.raises(KeyError, match="unknown column"):
        librarydb.insert_row(conn, "design", {"name": "x", "oops": 1})


def test_relation_make_and_status_export(conn: sqlite3.Connection) -> None:
    src = librarydb.insert_row(conn, "model_source", {"site": "s", "model_id": "1", "url": "u",
                                                      "files": [{"name": "a.stl"}]})
    d = librarydb.insert_row(conn, "design", {"name": "d"})
    librarydb.insert_row(conn, "relation", {"from_kind": "design", "from_id": d.uid,
                                            "to_kind": "model_source", "to_id": src.uid, "rel": "remix_of"})
    st = librarydb.status(conn, "x.db")
    assert st["schema"] == "3d.library/1" and st["counts"]["relation"] == 1 and st["migrations_pending"] == []
    doc = json.loads(json.dumps(librarydb.export(conn)))
    assert doc["tables"]["model_source"][0]["files"] == [{"name": "a.stl"}]
    assert doc["tables"]["relation"][0]["rel"] == "remix_of"


class _WriterAfterFirstRead:
    """Connection stand-in: another writer commits a design right after export reads its first table."""

    def __init__(self, inner: sqlite3.Connection, other: sqlite3.Connection) -> None:
        self.inner, self.other, self.fired = inner, other, False

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        cur = self.inner.execute(sql, *args)
        if sql.startswith("SELECT * FROM") and not self.fired:
            self.fired = True
            librarydb.insert_row(self.other, "design", {"name": "late"})
        return cur


def test_export_is_one_snapshot_a_concurrent_commit_is_not_half_exported(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    reader, writer = librarydb.open_library(path), librarydb.open_library(path)
    try:
        librarydb.insert_row(reader, "design", {"name": "early"})
        doc = librarydb.export(_WriterAfterFirstRead(reader, writer))  # type: ignore[arg-type]
        assert [d["name"] for d in doc["tables"]["design"]] == ["early"]
        assert [d["name"] for d in librarydb.export(reader)["tables"]["design"]] == ["early", "late"]
    finally:
        reader.close()
        writer.close()


def test_db_path_honours_env_then_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(librarydb.ENV_DB, str(tmp_path / "x.db"))
    assert librarydb.library_db_path() == tmp_path / "x.db"
    monkeypatch.delenv(librarydb.ENV_DB)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert librarydb.library_db_path() == tmp_path / "data" / "3d-cli" / "library" / "library.db"
