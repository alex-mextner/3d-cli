from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import run_cli


def test_library_status_works_offline_on_a_fresh_machine(tmp_path: Path) -> None:
    """An agent with no network asks for the library status: the DB is created and migrated on the spot."""
    db = tmp_path / "lib" / "library.db"

    result = run_cli(tmp_path, "library", "status", "--json", "--db", str(db))

    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["schema"] == "3d.library/1" and doc["path"] == str(db)
    assert doc["migrations"] == [1] and doc["migrations_pending"] == []
    assert doc["counts"]["search_result"] == 0 and db.is_file()


def test_library_migrate_twice_and_export(tmp_path: Path) -> None:
    db = str(tmp_path / "library.db")

    first = run_cli(tmp_path, "library", "migrate", "--db", db)
    second = run_cli(tmp_path, "library", "init", "--db", db, "--json")
    exported = run_cli(tmp_path, "library", "export", "--db", db)

    assert first.returncode == second.returncode == exported.returncode == 0
    assert "migrations: [1]" in first.stdout
    assert json.loads(second.stdout)["migrations"] == [1]
    tables = json.loads(exported.stdout)["tables"]
    assert set(tables) >= {"search_session", "search_result", "model_source", "design", "make", "photo",
                           "publication", "relation"}


def test_library_path_uses_the_environment_and_never_creates_the_file(tmp_path: Path) -> None:
    db = tmp_path / "elsewhere" / "library.db"

    result = run_cli(tmp_path, "library", "path", "--json", env_extra={"THREED_LIBRARY_DB": str(db)})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == str(db) and not db.exists()


def test_library_rejects_unknown_subcommand_with_exit_2(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "library", "drop")
    assert result.returncode == 2 and "accepted: path, migrate, init, status, export" in result.stderr


def test_library_help_lists_every_subcommand(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "library", "--help")
    assert result.returncode == 0
    for word in ("path", "migrate", "status", "export", "--db PATH", "--json"):
        assert word in result.stdout
