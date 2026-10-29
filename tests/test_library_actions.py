from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from library import actions
from library import db as librarydb

NAMES = ("search", "rank", "download", "open_slicer", "launch_agent", "queue", "photo", "publish")


@pytest.fixture
def dbpath(tmp_path: Path) -> str:
    return str(tmp_path / "library.db")


def test_registry_has_exactly_the_eight_actions_with_owners() -> None:
    assert tuple(actions.ACTIONS) == NAMES
    assert set(actions.OWNERS) == set(NAMES)
    for name, fn in actions.ACTIONS.items():
        assert fn.__name__ == name and fn.__doc__
        sig = inspect.signature(fn)
        assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values())
        assert sig.return_annotation in ("ActionResult", actions.ActionResult)


def test_actions_module_imports_without_heavy_deps_or_library_db() -> None:
    code = (
        "import sys; sys.modules['trimesh'] = None; sys.modules['numpy'] = None; sys.modules['cv2'] = None\n"
        "import library.actions\n"
        "assert 'library.db' not in sys.modules\n"
        "print('OK')\n"
    )
    lib = Path(__file__).resolve().parents[1] / "lib"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=lib,
                       env={"PYTHONPATH": str(lib), "PATH": ""})
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


@pytest.mark.parametrize("name,owner,kwargs", [
    ("search", 576, {"query": "case"}),
    ("rank", 577, {"session": "x"}),
    ("download", 576, {"url": "https://x"}),
    ("open_slicer", 578, {"path": "a.3mf"}),
    ("launch_agent", 578, {"prompt": "hi"}),
    ("photo", 579, {"capture": True}),
    ("publish", 579, {"design": "d"}),
])
def test_unimplemented_actions_name_their_owning_issue(name: str, owner: int, kwargs: dict[str, object]) -> None:
    with pytest.raises(actions.NotImplementedByDesign) as exc:
        actions.ACTIONS[name](**kwargs)
    assert exc.value.owner_issue == owner and f"#{owner}" in str(exc.value)
    assert actions.OWNERS[name] == owner
    res = actions.invoke(name, **kwargs)
    assert res["schema"] == "3d.action/1" and res["ok"] is False and res["action"] == name
    assert res["errors"][0]["code"] == "not_implemented" and f"#{owner}" in res["errors"][0]["remediation"][0]
    assert res["data"] == {} and res["warnings"] == []
    json.dumps(res)


def test_queue_creates_design_and_queued_make_idempotently(dbpath: str) -> None:
    res = actions.queue("soil-sensor-case", printer="k1se", filament="PLA", db_path=dbpath, source_key="k1")
    assert res["ok"] and res["schema"] == "3d.action/1" and res["errors"] == [] and res["warnings"] == []
    assert res["data"]["status"] == "queued" and res["data"]["created"] is True
    again = actions.queue("soil-sensor-case", printer="k1se", db_path=dbpath, source_key="k1")
    assert again["data"]["make"] == res["data"]["make"] and again["data"]["created"] is False
    third = actions.queue("soil-sensor-case", db_path=dbpath)  # a new make of the same design
    assert third["data"]["design"] == res["data"]["design"] and third["data"]["make"] != res["data"]["make"]
    conn = librarydb.open_library(dbpath)
    assert conn.execute("SELECT COUNT(*) FROM design").fetchone()[0] == 1
    assert [r["status"] for r in conn.execute("SELECT status FROM make")] == ["queued", "queued"]


def test_queue_reads_back_a_design_another_writer_created_first(dbpath: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real = librarydb.insert_row
    raced: list[str] = []

    def losing_race(conn: sqlite3.Connection, table: str, values: dict[str, Any], **kw: Any) -> Any:
        if table == "design" and not raced:
            raced.append(values["name"])
            real(conn, table, values)  # the other writer's insert lands between our SELECT and INSERT
        return real(conn, table, values, **kw)

    monkeypatch.setattr(librarydb, "insert_row", losing_race)
    res = actions.queue("case", db_path=dbpath)
    assert raced == ["case"] and res["ok"] is True and res["errors"] == []
    conn = librarydb.open_library(dbpath)
    assert conn.execute("SELECT COUNT(*) FROM design").fetchone()[0] == 1
    assert conn.execute("SELECT uid FROM design").fetchone()["uid"] == res["data"]["design"]


def test_queue_unknown_source_is_an_error_result(dbpath: str) -> None:
    res = actions.queue("d", source="0000", db_path=dbpath)
    assert res["ok"] is False and res["errors"][0]["code"] == "not_found"


def test_photo_registers_an_existing_file_for_a_make(dbpath: str, tmp_path: Path) -> None:
    made = actions.queue("case", db_path=dbpath)["data"]["make"]
    img = tmp_path / "table.jpg"
    img.write_bytes(b"x")
    res = actions.photo(str(img), make=made, kind="table", z_mm=12.5, db_path=dbpath)
    assert res["ok"] and res["data"]["kind"] == "table"
    row = librarydb.open_library(dbpath).execute("SELECT * FROM photo").fetchone()
    assert row["z_mm"] == 12.5 and row["make_id"] is not None and row["design_id"] is None
    missing = actions.photo(str(img), make="nope", db_path=dbpath)
    assert missing["ok"] is False and missing["errors"][0]["code"] == "not_found"


def test_queue_idempotency_hit_reports_the_existing_make_not_the_new_arguments(dbpath: str) -> None:
    first = actions.queue("design-a", db_path=dbpath, source_key="retry")
    conn = librarydb.open_library(dbpath)
    conn.execute("UPDATE make SET status = 'done'")
    again = actions.queue("design-b", db_path=dbpath, source_key="retry")
    assert again["data"]["make"] == first["data"]["make"] and again["data"]["created"] is False
    assert again["data"]["design"] == first["data"]["design"] and again["data"]["status"] == "done"


def test_queue_creates_no_design_for_an_idempotent_retry_or_an_unknown_source(dbpath: str) -> None:
    actions.queue("design-a", db_path=dbpath, source_key="retry")
    actions.queue("design-b", db_path=dbpath, source_key="retry")
    assert actions.queue("design-c", source="0000", db_path=dbpath)["errors"][0]["code"] == "not_found"
    names = [r["name"] for r in librarydb.open_library(dbpath).execute("SELECT name FROM design")]
    assert names == ["design-a"]


def test_photo_refuses_a_path_that_is_not_an_existing_file(dbpath: str, tmp_path: Path) -> None:
    made = actions.queue("case", db_path=dbpath)["data"]["make"]
    for bad in (str(tmp_path / "nope.jpg"), str(tmp_path)):
        res = actions.invoke("photo", path=bad, make=made, db_path=dbpath)
        assert res["ok"] is False and res["errors"][0]["code"] == "invalid_argument"
    assert librarydb.open_library(dbpath).execute("SELECT COUNT(*) FROM photo").fetchone()[0] == 0


def test_invoke_turns_storage_failures_and_null_design_into_error_results(tmp_path: Path) -> None:
    assert actions.invoke("queue", design=None)["errors"][0]["code"] == "invalid_argument"
    unusable = actions.invoke("queue", design="case", db_path=str(tmp_path))  # a directory is not a database
    assert unusable["ok"] is False and unusable["errors"][0]["code"] == "storage_error"


def test_invoke_turns_bad_input_into_results_never_exceptions(dbpath: str) -> None:
    assert actions.invoke("nope")["errors"][0]["code"] == "unknown_action"
    assert actions.invoke("photo", path="x.jpg", kind="selfie", make="m")["errors"][0]["code"] == "invalid_argument"
    assert actions.invoke("photo", path="x.jpg", db_path=dbpath)["errors"][0]["code"] == "invalid_argument"
    assert actions.invoke("launch_agent", prompt="x", backend="vim")["errors"][0]["code"] == "invalid_argument"
    assert actions.invoke("queue", bogus=1)["errors"][0]["code"] == "invalid_argument"


def test_invoke_turns_a_migration_mismatch_into_a_storage_error(dbpath: str) -> None:
    conn = librarydb.open_library(dbpath)
    conn.execute("UPDATE schema_migrations SET checksum = 'tampered'")
    res = actions.invoke("queue", design="case", db_path=dbpath)
    assert res["ok"] is False and res["errors"][0]["code"] == "storage_error"
    assert "MigrationError" in res["errors"][0]["message"]


def test_photo_with_a_missing_file_does_not_create_the_database(tmp_path: Path) -> None:
    db = tmp_path / "never.db"
    res = actions.invoke("photo", path=str(tmp_path / "nope.jpg"), make="m", db_path=str(db))
    assert res["errors"][0]["code"] == "invalid_argument" and not db.exists()
