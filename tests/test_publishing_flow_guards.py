"""Guards of the publish flow: renders never follow symlinks out of the project, a dry run never touches the
library schema, and a `--design` that contradicts `--make` is refused before anything is prepared."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from commands import photo
from errors import UsageError
from library import db as librarydb
from library import records
from publish_fixtures import make_project
from publishing import flow


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))


def _renders(project: Path, capsys: pytest.CaptureFixture[str]) -> list[str]:
    db = Path(librarydb.library_db_path())
    conn = librarydb.open_library(db)
    records.ensure_design(conn, "plug")
    conn.close()
    assert (
        photo.run(["renders", str(project), "--design", "plug", "--yes", "--json"]) == 0
    )
    doc = json.loads(capsys.readouterr().out)
    return [Path(r["path"]).name for r in doc["renders"]]


def test_photo_renders_skip_a_symlinked_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"not a render")
    (project / "previews" / "leak.jpg").symlink_to(secret)
    assert _renders(project, capsys) == ["iso.png"]
    conn = librarydb.open_library(tmp_path / "library.db")
    paths = [r["path"] for r in conn.execute("SELECT path FROM photo")]
    conn.close()
    assert all(str(secret) not in p for p in paths)


def test_photo_renders_skip_a_symlinked_previews_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path, images=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.jpg").write_bytes(b"x")
    (project / "previews").rmdir()
    (project / "previews").symlink_to(outside, target_is_directory=True)
    assert _renders(project, capsys) == []


def test_dry_run_planning_leaves_an_old_library_unmigrated(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    conn = librarydb.open_library(db)
    conn.execute("DELETE FROM schema_migrations WHERE version IN (10, 30)")
    conn.close()
    before = db.read_bytes()
    with flow.library(str(db), create=False) as ro:
        assert ro is not None
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE nope (x)")
    check = sqlite3.connect(db)
    versions = {r[0] for r in check.execute("SELECT version FROM schema_migrations")}
    check.close()
    assert versions == {1}
    assert db.exists() and before


def test_dry_run_planning_on_a_library_without_tables_plans_without_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    with flow.library(str(db), create=False) as conn:
        assert conn is None


def test_a_design_that_contradicts_the_make_is_refused(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    db = tmp_path / "library.db"
    conn = librarydb.open_library(db)
    records.ensure_design(conn, "A")
    make, _ = records.add_make(conn, "B")
    uid = str(make["uid"])
    conn.close()
    with pytest.raises(UsageError, match="A.*B|B.*A"):
        flow.prepare(
            project, platform="thingiverse", design="A", make=uid, db_path=str(db)
        )
    prep = flow.prepare(
        project, platform="thingiverse", design="B", make=uid, db_path=str(db)
    )
    assert prep.design_name == "B"
