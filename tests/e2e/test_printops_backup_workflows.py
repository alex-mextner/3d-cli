"""`3d printops backup` end to end through bin/3d: run -> ls -> verify -> prune -> restore -> selftest, the wrong-disk
guard with its notification + anomaly, and the 3d-api route Home Assistant calls."""

from __future__ import annotations

import datetime
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from printops_fixtures import LiveApi
from printops_ha_fakes import TOKEN, FakeHa

from printops import backup_store as store
from printops import db

from .workflow_helper import run_cli

UTC = datetime.timezone.utc


def make_world(tmp_path: Path) -> dict[str, str]:
    """A PrintOps DB (migrated), a Spoolman-like DB with 12 spools, a tokens file and an empty disk dir."""
    data = tmp_path / "data"
    data.mkdir()
    conn = db.open_db(data / "printops.db")
    db.migrate(conn)
    conn.close()
    spool = sqlite3.connect(data / "spoolman.db")
    spool.execute("CREATE TABLE spool (id INTEGER PRIMARY KEY, comment TEXT)")
    spool.executemany(
        "INSERT INTO spool (comment) VALUES (?)", [(f"s{i}",) for i in range(12)]
    )
    spool.commit()
    spool.close()
    (data / "tokens.json").write_text('{"tokens": []}', encoding="utf-8")
    (tmp_path / "disk").mkdir()
    return {
        "PRINTOPS_DB": str(data / "printops.db"),
        "SPOOLMAN_DB": str(data / "spoolman.db"),
        "PRINTOPS_TOKENS_FILE": str(data / "tokens.json"),
        "PRINTOPS_BACKUP_CONFIG": str(data / "backup.yaml"),
        "PRINTOPS_HA_URL": "",
        "PRINTOPS_HA_TOKEN": "",
    }


def backup(tmp_path: Path, env: dict[str, str], *argv: str):  # type: ignore[no-untyped-def]
    return run_cli(tmp_path, "printops", "backup", *argv, env_extra=env)


def test_full_cycle_run_ls_verify_prune_restore_selftest(tmp_path: Path) -> None:
    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    refused = backup(tmp_path, env, "target", "add", "default", str(disk))
    assert (
        refused.returncode == 1
        and "--init-marker" in refused.stderr
        and not (disk / store.MARKER).exists()
    )
    added = backup(
        tmp_path, env, "target", "add", "default", str(disk), "--init-marker"
    )
    ran = backup(
        tmp_path,
        env,
        "run",
        "--target",
        "default",
        "--json",
        "--keep",
        "daily=7,weekly=4,monthly=6",
    )
    assert added.returncode == 0 and (disk / store.MARKER).is_file()
    assert ran.returncode == 0, ran.stderr
    result = json.loads(ran.stdout)
    assert result["status"] == "ok" and set(result["items"]) == {
        "printops",
        "spoolman",
        "tokens",
    }
    assert result["items"]["spoolman"]["tables"] == {"spool": 12} and result[
        "retention"
    ] == {"daily": 7, "weekly": 4, "monthly": 6}

    dest = disk / "3d-printops"
    for i in range(
        1, 31
    ):  # 30 older dated sets from the real writer + things that are not ours
        store.write_set(
            dest,
            [store.Source("spoolman", Path(env["SPOOLMAN_DB"]), True)],
            now=datetime.datetime(2026, 10, 31, 4, tzinfo=UTC)
            - datetime.timedelta(days=i - 1),
        )
    (dest / "notes.txt").write_text("mine", encoding="utf-8")
    (dest / "photos").mkdir()
    (dest / "3d-backup-keepme").mkdir()

    listing = backup(tmp_path, env, "ls", "--target", "default")
    verified = backup(tmp_path, env, "verify", "latest", "--target", "default")
    dry = backup(tmp_path, env, "prune", "--target", "default", "--dry-run")
    after_dry = len(list(dest.glob("3d-backup-2026*Z")))
    pruned = backup(tmp_path, env, "prune", "--target", "default")
    assert (
        listing.returncode == 0
        and "31 set(s)" in listing.stdout
        and "prune" in listing.stdout
    )
    assert (
        verified.returncode == 0
        and "integrity_check and row counts match" in verified.stdout
    )
    assert "would remove" in dry.stdout and after_dry == 31  # a dry run removes nothing
    assert pruned.returncode == 0 and "keep 10, removed 21" in pruned.stdout.replace(
        "policy 7 daily + 4 weekly + 6 monthly: ", ""
    )
    assert len(list(dest.glob("3d-backup-2026*Z"))) == 10
    assert (
        (dest / "notes.txt").read_text(encoding="utf-8") == "mine"
        and (dest / "photos").is_dir()
        and (dest / "3d-backup-keepme").is_dir()
    )

    restored = backup(
        tmp_path,
        env,
        "restore",
        "latest",
        "--target",
        "default",
        "--into",
        str(tmp_path / "out"),
        "--layout",
        "data",
    )
    unsafe = backup(
        tmp_path,
        env,
        "restore",
        "latest",
        "--target",
        "default",
        "--into",
        str(tmp_path / "out"),
        "--layout",
        "data",
        "--yes",
    )
    assert restored.returncode == 2 and "--yes" in restored.stderr
    assert unsafe.returncode == 0, unsafe.stderr
    assert sqlite3.connect(tmp_path / "out" / "spoolman" / "spoolman.db").execute(
        "SELECT count(*) FROM spool"
    ).fetchone() == (12,)

    selftest = backup(tmp_path, env, "selftest", "--target", "default")
    assert (
        selftest.returncode == 0
        and "selftest ok" in selftest.stdout
        and "restore-check  spoolman: restored, integrity ok" in selftest.stdout
    )


def _anomaly_rows(env: dict[str, str]) -> list[tuple[str, ...]]:
    conn = db.open_db(Path(env["PRINTOPS_DB"]))
    try:
        return [
            tuple(r)
            for r in conn.execute("SELECT kind, subject, scope, status FROM anomaly")
        ]
    finally:
        conn.close()


def _assert_ha_got_the_payload(payload: dict[str, str], ha: FakeHa) -> None:
    assert (
        payload["notification_id"] == "printops_backup_failed"
        and "not the backup disk" in payload["message"]
    )
    create = [
        r for r in ha.requests if r[1] == "/api/services/persistent_notification/create"
    ]
    assert (
        len(create) == 1 and create[0][2] == payload
    )  # the same payload reached Home Assistant


def _disk_returns_and_the_next_run_recovers(
    tmp_path: Path, env: dict[str, str], disk: Path, ha: FakeHa
) -> None:
    """The marker comes back with the SAME id: the next run succeeds, resolves the anomaly and dismisses the note."""
    spec = Path(env["PRINTOPS_BACKUP_CONFIG"]).read_text(encoding="utf-8")
    ident = next(
        line.split(":")[1].strip() for line in spec.splitlines() if "expect_id" in line
    )
    (disk / store.MARKER).write_text(json.dumps({"id": ident}), encoding="utf-8")
    good = backup(tmp_path, env, "run", "--target", "default", "--json")
    assert good.returncode == 0, good.stderr
    assert [row[3] for row in _anomaly_rows(env)] == ["resolved"]
    assert any(
        r[1] == "/api/services/persistent_notification/dismiss" for r in ha.requests
    )


def test_wrong_disk_writes_nothing_and_notifies_ha_and_opens_an_anomaly(
    tmp_path: Path,
) -> None:
    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    assert (
        backup(
            tmp_path, env, "target", "add", "default", str(disk), "--init-marker"
        ).returncode
        == 0
    )
    (
        disk / store.MARKER
    ).unlink()  # the disk is not mounted: an empty directory stands in its place
    before = sorted(p.name for p in disk.rglob("*"))
    ha = FakeHa()
    try:
        env.update({"PRINTOPS_HA_URL": ha.url, "PRINTOPS_HA_TOKEN": TOKEN})
        failed = backup(tmp_path, env, "run", "--target", "default", "--json")
        assert failed.returncode == 1
        assert (
            json.loads(failed.stdout)["status"] == "failed"
            and "is not the backup disk" in failed.stdout
        )
        assert (
            sorted(p.name for p in disk.rglob("*")) == before == []
        )  # nothing written: no sub-dir, no lock, no state
        payload_line = next(
            line
            for line in failed.stderr.splitlines()
            if line.startswith("notification: ")
        )
        payload = json.loads(payload_line.removeprefix("notification: "))
        _assert_ha_got_the_payload(payload, ha)
        assert _anomaly_rows(env) == [
            ("backup_failed", "backup:default", "backup", "open")
        ]
        last = json.loads(
            (Path(env["PRINTOPS_DB"]).parent / "backup-last.json").read_text(
                encoding="utf-8"
            )
        )
        assert last["status"] == "failed"

        _disk_returns_and_the_next_run_recovers(tmp_path, env, disk, ha)
    finally:
        ha.close()


def test_another_disk_with_a_marker_is_refused_too(tmp_path: Path) -> None:
    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    backup(tmp_path, env, "target", "add", "default", str(disk), "--init-marker")
    (disk / store.MARKER).write_text(json.dumps({"id": "f" * 32}), encoding="utf-8")
    r = backup(tmp_path, env, "run", "--target", "default")
    assert (
        r.returncode == 1
        and "another disk is mounted there" in r.stderr
        and not (disk / "3d-printops").exists()
    )


def test_unconfigured_target_and_missing_disk_are_clear_errors(tmp_path: Path) -> None:
    env = make_world(tmp_path)
    unconfigured = backup(tmp_path, env, "run")
    assert (
        unconfigured.returncode == 1
        and "is not configured" in unconfigured.stderr
        and "3d init --env" in unconfigured.stderr
    )
    adhoc = backup(tmp_path, env, "run", "--dest", str(tmp_path / "nope"))
    assert adhoc.returncode == 1 and "is not there (not mounted?)" in adhoc.stderr


def test_a_full_disk_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    backup(tmp_path, env, "target", "add", "default", str(disk), "--init-marker")
    r = backup(
        tmp_path, env, "run", "--target", "default", "--min-free-gb", "100000000"
    )
    assert (
        r.returncode == 1
        and "nothing was written" in r.stderr
        and not list((disk / "3d-printops").glob("3d-backup-*"))
    )


def test_help_and_discover_are_wired(tmp_path: Path) -> None:
    env = make_world(tmp_path)
    helped = backup(tmp_path, env, "--help")
    assert helped.returncode == 0
    for verb in (
        "run",
        "ls",
        "verify",
        "prune",
        "restore",
        "target",
        "selftest",
        "discover",
        ".3d-backup-disk",
    ):
        assert verb in helped.stdout
    bad = backup(tmp_path, env, "frobnicate")
    assert bad.returncode == 2 and "unknown backup subcommand" in bad.stderr
    found = backup(tmp_path, env, "discover", "--json", "--data-dir", str(tmp_path))
    assert found.returncode == 0, found.stderr
    body = json.loads(found.stdout)
    assert {"candidates", "skipped", "data_device"} <= set(body)


def test_the_api_route_runs_the_backup_niced_child_and_reports_failure_as_an_answer(
    tmp_path: Path, live_api: LiveApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_world(tmp_path)
    env.update(
        {
            "PRINTOPS_DB": str(live_api.settings.db_path),
            "PRINTOPS_TOKENS_FILE": str(live_api.settings.tokens_file),
        }
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    disk = tmp_path / "disk"
    assert (
        backup(
            tmp_path, env, "target", "add", "default", str(disk), "--init-marker"
        ).returncode
        == 0
    )

    def call(
        method: str, path: str, body: dict[str, object] | None = None, token: str = "rw"
    ) -> tuple[int, Any]:
        req = urllib.request.Request(
            live_api.url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {live_api.tokens[token]}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — loopback fake
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    status, ok = call(
        "POST",
        "/v1/backup/run",
        {
            "target": "default",
            "subdir": "ha",
            "keep_daily": 3,
            "keep_weekly": 1,
            "keep_monthly": 1,
        },
    )
    assert (
        status == 200
        and ok["status"] == "ok"
        and ok["retention"] == {"daily": 3, "weekly": 1, "monthly": 1}
    )
    assert len(list((disk / "ha").glob("3d-backup-*"))) == 1

    state, current = call("GET", "/v1/backup")
    assert (
        state == 200
        and current["last"]["status"] == "ok"
        and current["targets"]["default"]["state"] == "OK"
    )  # type: ignore[index]

    (disk / store.MARKER).unlink()
    status, failed = call("POST", "/v1/backup/run", {"target": "default"})
    assert (
        status == 200
        and failed["status"] == "failed"
        and "not the backup disk" in str(failed["error"])
    )  # an answer, not an HTTP error
    assert call("POST", "/v1/backup/run", {"target": "../etc"})[0] == 422
    assert call("POST", "/v1/backup/run", {"subdir": "../x"})[0] == 422
    assert call("POST", "/v1/backup/run", {}, token="read")[0] == 403
