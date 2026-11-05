"""Small edges of the backup commands: the one target-name rule and the backup line of `3d printops health`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from printops import backup_config
from printops.backup_store import BackupRefused
from printops.cli import health


# ---------------------------------------------------------------------------- target names


@pytest.mark.parametrize("name", ["défaut", "a b", "", "x" * 65, "ñ", "a/b", "a\n"])
def test_target_names_are_ascii_only(tmp_path: Path, name: str) -> None:
    disk = tmp_path / "disk"
    disk.mkdir()
    with pytest.raises(BackupRefused, match="target name"):
        backup_config.add_target(tmp_path / "backup.yaml", name, disk, init_marker=True)


def test_ascii_target_names_are_accepted(tmp_path: Path) -> None:
    disk = tmp_path / "disk"
    disk.mkdir()
    assert backup_config.add_target(
        tmp_path / "backup.yaml", "nas-2_b", disk, init_marker=True
    )


# ---------------------------------------------------------------------------- the backup health line


def test_health_says_loudly_that_no_backup_is_configured() -> None:
    line = health._backup_line({"configured": False, "targets": [], "last": None})
    assert (
        "NOT CONFIGURED" in line
        and "NO backups are taken" in line
        and "--stage-only --spoolman" in line
    )


def test_health_reports_targets_and_the_last_run() -> None:
    ok = health._backup_line(
        {
            "configured": True,
            "targets": ["default"],
            "last": {"status": "ok", "at": "2026-11-01T04:00:03+00:00"},
        }
    )
    assert "default" in ok and "last run ok" in ok
    failed = health._backup_line(
        {
            "configured": True,
            "targets": ["default"],
            "last": {"status": "failed", "at": "t", "error": "disk missing"},
        }
    )
    assert "failed" in failed and "disk missing" in failed
    assert "unknown" in health._backup_line(None)


def test_the_health_summary_reads_backup_yaml_and_the_last_run(tmp_path: Path) -> None:
    from printops import backup_run
    from printops.settings import Settings

    settings = Settings(
        db_path=tmp_path / "printops.db",
        tokens_file=tmp_path / "tokens.json",
        version="t",
    )
    env: dict[str, str] = {}
    assert backup_run.health_summary(env, settings) == {
        "configured": False,
        "targets": [],
        "last": None,
    }
    disk = tmp_path / "disk"
    disk.mkdir()
    backup_config.add_target(
        tmp_path / "backup.yaml", "default", disk, init_marker=True
    )
    (tmp_path / backup_run.LAST_FILE).write_text(
        json.dumps({"status": "ok", "at": "x", "set_name": "s"}), encoding="utf-8"
    )
    summary = backup_run.health_summary(env, settings)
    assert summary == {
        "configured": True,
        "targets": ["default"],
        "last": {"status": "ok", "at": "x"},
    }
    (tmp_path / "backup.yaml").write_text("targets: [unclosed\n", encoding="utf-8")
    broken = backup_run.health_summary(env, settings)
    assert broken["configured"] is False and "not valid YAML" in broken["error"]
