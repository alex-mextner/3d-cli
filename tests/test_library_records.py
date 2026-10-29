"""library/records.py: the writes of makes, photos, publications, relations and the listings/graph on top."""

from __future__ import annotations

import sqlite3
from typing import Any
from collections.abc import Iterator
from pathlib import Path

import pytest

from errors import InvalidArgument
from library import db as librarydb
from library import records


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = librarydb.open_library(tmp_path / "library.db")
    yield c
    c.close()


def source(conn: sqlite3.Connection) -> sqlite3.Row:
    row, created = records.ensure_source(
        conn,
        site="printables",
        model_id="311253",
        url="https://www.printables.com/model/311253-spoon",
        license="CC BY 4.0",
        author="ann",
    )
    assert created
    return row


def test_a_make_links_to_its_design_and_the_source_it_was_printed_from(
    conn: sqlite3.Connection,
) -> None:
    src = source(conn)
    make, created = records.add_make(
        conn,
        "mole",
        printer="k1se",
        filament="PLA",
        status="printing",
        source_uid=str(src["uid"]),
        project_dir="/p/mole",
    )
    assert created and make["status"] == "printing" and make["finished_at"] is None
    design = records.resolve(conn, "design", "mole")
    assert design["project_dir"] == "/p/mole"
    rels = {
        (r["rel"], r["from"]["kind"], r["to"]["kind"])
        for r in records.list_relations(conn)
    }
    assert rels == {
        ("make_of", "make", "design"),
        ("derived_from", "design", "model_source"),
    }


def test_make_status_moves_forward_and_finishing_sets_the_time(
    conn: sqlite3.Connection,
) -> None:
    make, _ = records.add_make(conn, "mole")
    assert make["status"] == "queued"
    done = records.set_make_status(conn, str(make["uid"]), "done")
    assert done["status"] == "done" and done["finished_at"]
    with pytest.raises(InvalidArgument) as exc:
        records.set_make_status(conn, str(make["uid"]), "burning")
    assert "queued, printing, done, failed" in str(exc.value)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert "library.make.updated" in kinds and "library.make.created" in kinds


def test_references_resolve_by_full_uid_unique_prefix_or_design_name(
    conn: sqlite3.Connection,
) -> None:
    make, _ = records.add_make(conn, "mole")
    uid = str(make["uid"])
    assert records.resolve(conn, "make", uid)["id"] == make["id"]
    assert records.resolve(conn, "make", uid[:12])["id"] == make["id"]
    assert records.resolve(conn, "design", "mole")["name"] == "mole"
    with pytest.raises(records.RecordNotFound):
        records.resolve(
            conn, "make", uid[:5]
        )  # shorter than 8 characters: never guessed
    with pytest.raises(records.RecordNotFound):
        records.resolve(conn, "make", "0199a1c2-0000-0000-0000-000000000000")


def test_a_photo_belongs_to_exactly_one_make_or_design_and_keeps_its_z(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    img = tmp_path / "t.jpg"
    img.write_bytes(b"jpg")
    make, _ = records.add_make(conn, "mole")
    row, created = records.add_photo(
        conn, str(img), make=str(make["uid"]), kind="table", z_mm=70.0
    )
    assert created and row["z_mm"] == 70.0 and row["make_id"] == make["id"]
    with pytest.raises(InvalidArgument):
        records.add_photo(conn, str(img), make=str(make["uid"]), design="mole")
    with pytest.raises(InvalidArgument):
        records.add_photo(conn, str(img))
    with pytest.raises(InvalidArgument):
        records.add_photo(conn, str(img), make=str(make["uid"]), kind="selfie")
    design = records.resolve(conn, "design", "mole")
    # the photos of a design include those of its makes
    assert [
        p["kind"] for p in records.photos_of(conn, design_id=int(design["id"]))
    ] == ["table"]


def test_a_source_key_makes_a_photo_idempotent(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    img = tmp_path / "r.png"
    img.write_bytes(b"png")
    records.ensure_design(conn, "mole")
    first, c1 = records.add_photo(
        conn, str(img), design="mole", kind="render", source_key="render:1"
    )
    second, c2 = records.add_photo(
        conn, str(img), design="mole", kind="render", source_key="render:1"
    )
    assert c1 and not c2 and first["uid"] == second["uid"]


def test_publications_change_state_but_are_never_deleted(
    conn: sqlite3.Connection,
) -> None:
    pub = records.add_publication(conn, "mole", "printables", state="planned")
    assert pub["state"] == "planned" and pub["url"] is None
    done = records.set_publication(
        conn,
        str(pub["uid"]),
        state="published",
        url="https://www.printables.com/model/9",
    )
    assert done["state"] == "published" and done["url"].endswith("/9")
    failed = records.add_publication(conn, "mole", "cults", state="failed")
    assert [p["state"] for p in records.list_publications(conn, "mole")] == [
        "published",
        "failed",
    ]
    assert failed["state"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM publication").fetchone()[0] == 2


def test_relate_is_idempotent_and_rejects_unknown_endpoints(
    conn: sqlite3.Connection,
) -> None:
    src = source(conn)
    design = records.ensure_design(conn, "mole")
    first, c1 = records.relate(
        conn, "remix_of", "design", str(design["uid"]), "model_source", str(src["uid"])
    )
    again, c2 = records.relate(
        conn, "remix_of", "design", str(design["uid"]), "model_source", str(src["uid"])
    )
    assert c1 and not c2 and first["uid"] == again["uid"]
    with pytest.raises(records.RecordNotFound):
        records.relate(
            conn,
            "remix_of",
            "design",
            str(design["uid"]),
            "model_source",
            "0199a1c2-0000-0000-0000-000000000000",
        )
    with pytest.raises(InvalidArgument):
        records.relate(
            conn, "loves", "design", str(design["uid"]), "model_source", str(src["uid"])
        )


def test_the_graph_shows_source_remix_make_in_both_directions(
    conn: sqlite3.Connection,
) -> None:
    src = source(conn)
    design = records.ensure_design(conn, "mole")
    records.relate(
        conn, "remix_of", "design", str(design["uid"]), "model_source", str(src["uid"])
    )
    make, _ = records.add_make(conn, "mole", printer="k1se")
    graph = records.graph(conn, "mole")
    assert graph["schema"] == "3d.library.graph/1"
    labels = {n["label"] for n in graph["nodes"]}
    assert {
        "design mole",
        "source https://www.printables.com/model/311253-spoon",
    } <= labels
    assert any(label.startswith("make of mole") for label in labels)
    rels = {(e["rel"], e["from"]["kind"], e["to"]["kind"]) for e in graph["edges"]}
    assert rels == {
        ("remix_of", "design", "model_source"),
        ("make_of", "make", "design"),
    }
    assert "remix_of" in graph["text"] and "make_of" in graph["text"]
    # a second design remixing the same source is found from the first through the shared source
    other = records.ensure_design(conn, "other")
    records.relate(
        conn, "remix_of", "design", str(other["uid"]), "model_source", str(src["uid"])
    )
    reached = records.graph(conn, "mole")
    assert other["uid"] in {n["uid"] for n in reached["nodes"]}
    assert any(
        e["rel"] == "remix_of"
        and e["from"]["uid"] == other["uid"]
        and e["to"]["uid"] == src["uid"]
        for e in reached["edges"]
    )


def test_makes_list_counts_photos_and_names_the_source(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    src = source(conn)
    make, _ = records.add_make(conn, "mole", source_uid=str(src["uid"]))
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    records.add_photo(conn, str(img), make=str(make["uid"]))
    (row,) = records.list_makes(conn, "mole")
    assert (
        row["photos"] == 1
        and row["source_url"].endswith("311253-spoon")
        and row["source"] == src["uid"]
    )


def test_project_dir_is_recorded_absolute(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    make, _ = records.add_make(conn, "mole", project_dir="projects/mole")
    design = conn.execute(
        "SELECT project_dir FROM design WHERE id = ?", (make["design_id"],)
    ).fetchone()
    assert design["project_dir"] == str((tmp_path / "projects" / "mole").resolve())


def test_ensure_design_survives_a_concurrent_first_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "race.db"
    first = librarydb.open_library(db_path)
    second = librarydb.open_library(db_path)

    class Racing:
        """Lets `second` create the design right after `first` saw none."""

        def __init__(self) -> None:
            self.raced = False

        def __getattr__(self, name: str):  # noqa: ANN204
            return getattr(first, name)

        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
            cur = first.execute(sql, params)
            if "FROM design WHERE name" in sql and not self.raced:
                self.raced = True
                records.ensure_design(second, "mole")
            return cur

    row = records.ensure_design(Racing(), "mole")  # type: ignore[arg-type]
    assert row["name"] == "mole"
    count = first.execute("SELECT COUNT(*) FROM design").fetchone()[0]
    first.close()
    second.close()
    assert count == 1
