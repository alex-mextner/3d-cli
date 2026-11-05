"""`3d printops backup` end to end through bin/3d: run -> ls -> verify -> prune -> restore -> selftest, the wrong-disk
guard with its notification + anomaly, and the 3d-api route Home Assistant calls."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from printops_fixtures import LiveApi
from printops_ha_fakes import TOKEN, FakeHa

from printops import backup_store as store
from printops import backup_notify as notify
from printops import db
from printops.backup_policy import Retention, plan_prune

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
    # the run's own set carries the real clock, so how many the policy keeps depends on today's calendar position
    keep_names, drop_names = plan_prune(
        [p.name for p in dest.glob("3d-backup-2026*Z")], Retention()
    )
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
    assert (
        pruned.returncode == 0
        and f"keep {len(keep_names)}, removed {len(drop_names)}" in pruned.stdout
    )
    assert sorted(p.name for p in dest.glob("3d-backup-2026*Z")) == sorted(keep_names)
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


def test_restoring_a_backup_taken_without_tokens_leaves_no_authorised_token(
    tmp_path: Path,
) -> None:
    """Story: the operator revoked every token (file gone), the nightly run recorded that, and a later restore
    into the live data dir must not bring the old, still-authorised token file back (3d-api would accept it)."""
    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    assert (
        backup(
            tmp_path, env, "target", "add", "default", str(disk), "--init-marker"
        ).returncode
        == 0
    )
    tokens = Path(env["PRINTOPS_TOKENS_FILE"])
    tokens.unlink()
    ran = backup(tmp_path, env, "run", "--target", "default", "--json")
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)["items"].keys() == {"printops", "spoolman"}
    live = tmp_path / "live"
    (live / "api").mkdir(parents=True)
    (live / "api" / "tokens.json").write_text(
        '{"tokens": [{"name": "stale"}]}', encoding="utf-8"
    )
    restored = backup(
        tmp_path,
        env,
        "restore",
        "latest",
        "--target",
        "default",
        "--into",
        str(live),
        "--layout",
        "data",
        "--yes",
    )
    assert restored.returncode == 0, restored.stderr
    assert not (live / "api" / "tokens.json").exists()
    assert "stale" in (live / "api" / "tokens.json.pre-restore").read_text(
        encoding="utf-8"
    )
    assert "has no tokens" in restored.stdout


def _anomaly_rows(env: dict[str, str]) -> list[tuple[str, ...]]:
    conn = db.open_db(Path(env["PRINTOPS_DB"]))
    try:
        return [
            tuple(r)
            for r in conn.execute("SELECT kind, subject, scope, status FROM anomaly")
        ]
    finally:
        conn.close()


def test_a_targets_good_run_never_closes_another_targets_failure(
    tmp_path: Path,
) -> None:
    """Two targets (e.g. a USB disk and a NAS): A fails, B fails, then only B recovers. A is still broken, so
    its anomaly must stay open — and A's own later recovery must still resolve it."""
    env = make_world(tmp_path)
    db_path = Path(env["PRINTOPS_DB"])

    def quiet(_line: str) -> None:
        return None

    def ctx(target: str) -> notify.Context:
        return notify.Context(target, db_path, {})

    def status() -> dict[str, str]:
        return {row[1]: row[3] for row in _anomaly_rows(env)}

    notify.report_failure(ctx("a"), "disk a missing", quiet)
    notify.report_failure(ctx("b"), "disk b missing", quiet)
    assert status() == {"backup:a": "open", "backup:b": "open"}
    notify.report_recovered(ctx("b"), quiet)
    assert status() == {"backup:a": "open", "backup:b": "resolved"}
    notify.report_failure(
        ctx("a"), "disk a still missing", quiet
    )  # a repeat does not resolve the other either
    assert status() == {"backup:a": "open", "backup:b": "resolved"}
    notify.report_recovered(ctx("a"), quiet)
    assert status() == {"backup:a": "resolved", "backup:b": "resolved"}


def test_one_targets_recovery_dismisses_only_its_own_ha_notification(
    tmp_path: Path,
) -> None:
    """Targets a and b both failed; b's good run (or an ad-hoc one) must leave a's notification up."""
    env = make_world(tmp_path)
    ha = FakeHa()
    try:
        env.update({"PRINTOPS_HA_URL": ha.url, "PRINTOPS_HA_TOKEN": TOKEN})
        db_path = Path(env["PRINTOPS_DB"])

        def quiet(_line: str) -> None:
            return None

        for target in ("a", "b"):
            notify.report_failure(
                notify.Context(target, db_path, env), f"disk {target} missing", quiet
            )
        notify.report_recovered(notify.Context("b", db_path, env), quiet)
        notify.report_recovered(notify.Context("adhoc", db_path, env), quiet)
        created = [
            r[2]["notification_id"]
            for r in ha.requests
            if r[1].endswith("/persistent_notification/create")
        ]
        dismissed = [
            r[2]["notification_id"]
            for r in ha.requests
            if r[1].endswith("/persistent_notification/dismiss")
        ]
    finally:
        ha.close()
    assert created == ["printops_backup_failed_a", "printops_backup_failed_b"]
    assert dismissed == ["printops_backup_failed_b", "printops_backup_failed_adhoc"]


def _assert_ha_got_the_payload(payload: dict[str, str], ha: FakeHa) -> None:
    assert (
        payload["notification_id"] == "printops_backup_failed_default"
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
            ("backup_failed", "backup:default", "backup:default", "open")
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

    lowest = {
        "target": "default",
        "subdir": "ha",
        "keep_daily": 1,
        "keep_weekly": 0,
        "keep_monthly": 0,
    }
    status, raised = call("POST", "/v1/backup/run", lowest)
    assert status == 200 and raised["status"] == "ok"
    assert raised["retention"] == {
        "daily": 7,
        "weekly": 4,
        "monthly": 6,
    }  # a write token cannot go below the default
    assert store.read_state(disk / "ha") == Retention(
        7, 4, 6
    )  # and a lower policy is never recorded

    config = Path(env["PRINTOPS_BACKUP_CONFIG"])
    config.write_text(
        config.read_text(encoding="utf-8")
        + "retention_floor: {daily: 3, weekly: 1, monthly: 1}\n",
        encoding="utf-8",
    )
    status, ok = call(
        "POST",
        "/v1/backup/run",
        {
            **lowest,
            "subdir": "ha2",
            "keep_daily": 3,
            "keep_weekly": 1,
            "keep_monthly": 1,
        },
    )
    assert status == 200 and ok["status"] == "ok"
    assert ok["retention"] == {
        "daily": 3,
        "weekly": 1,
        "monthly": 1,
    }  # the host lowered its own floor
    status, clamped = call("POST", "/v1/backup/run", {**lowest, "subdir": "ha3"})
    assert clamped["retention"] == {"daily": 3, "weekly": 1, "monthly": 1}
    assert store.read_state(disk / "ha3") == Retention(3, 1, 1)
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

    config.write_text("targets: [unclosed\n", encoding="utf-8")
    broken_status, _detail = call("GET", "/v1/backup")
    assert broken_status == 422  # a bad backup.yaml is the operator's to fix: not a 500
    status, answer = call("POST", "/v1/backup/run", {"target": "default"})
    assert (
        status == 200
        and answer["status"] == "failed"
        and "not valid YAML" in str(answer["error"])
    )
    assert call("POST", "/v1/backup/run", {"target": "../etc"})[0] == 422
    assert call("POST", "/v1/backup/run", {"subdir": "../x"})[0] == 422
    assert call("POST", "/v1/backup/run", {}, token="read")[0] == 403


def test_a_live_stack_is_restored_from_the_host_with_dest_and_subdir(
    tmp_path: Path,
) -> None:
    """Story: the stack is down, the operator is on the HOST. `--target default` names the container's /backup in a
    backup.yaml that does not exist here, so the runbook is `--dest <host disk root> --subdir 3d-printops`; a second
    restore keeps the first restore's safety copy."""
    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    assert (
        backup(
            tmp_path, env, "target", "add", "default", str(disk), "--init-marker"
        ).returncode
        == 0
    )
    assert backup(tmp_path, env, "run", "--target", "default", "--json").returncode == 0
    host_env = {
        **env,
        "PRINTOPS_BACKUP_CONFIG": str(tmp_path / "host-home" / "backup.yaml"),
    }
    live = tmp_path / "live"
    (live / "spoolman").mkdir(parents=True)
    (live / "spoolman" / "spoolman.db").write_text("the live one", encoding="utf-8")
    (live / "spoolman" / "spoolman.db-wal").write_text(
        "committed rows", encoding="utf-8"
    )

    by_target = backup(
        tmp_path,
        host_env,
        "restore",
        "latest",
        "--target",
        "default",
        "--into",
        str(live),
        "--layout",
        "data",
        "--yes",
    )
    assert by_target.returncode == 1 and "not configured" in by_target.stderr

    restore = (
        "restore",
        "latest",
        "--dest",
        str(disk),
        "--subdir",
        "3d-printops",
        "--into",
        str(live),
        "--layout",
        "data",
        "--yes",
    )
    first = backup(tmp_path, host_env, *restore)
    assert first.returncode == 0, first.stderr
    assert sqlite3.connect(live / "spoolman" / "spoolman.db").execute(
        "SELECT count(*) FROM spool"
    ).fetchone() == (12,)
    assert (live / "spoolman" / "spoolman.db.pre-restore").read_text(
        encoding="utf-8"
    ) == "the live one"
    assert (live / "spoolman" / "spoolman.db.pre-restore-wal").read_text(
        encoding="utf-8"
    ) == "committed rows"
    second = backup(tmp_path, host_env, *restore)
    assert second.returncode == 0, second.stderr
    assert (live / "spoolman" / "spoolman.db.pre-restore").read_text(
        encoding="utf-8"
    ) == "the live one"
    assert (
        live / "spoolman" / "spoolman.db.pre-restore.1"
    ).is_file()  # the first restore's result, kept too


@pytest.mark.parametrize("bad", ["inf", "-1", "nan", "ten"])
def test_min_free_gb_must_be_a_finite_number_not_below_zero(
    tmp_path: Path, bad: str
) -> None:
    env = make_world(tmp_path)
    r = backup(tmp_path, env, "run", "--target", "default", "--min-free-gb", bad)
    assert r.returncode == 2 and "--min-free-gb needs a finite number" in r.stderr
    assert "Traceback" not in r.stderr


def test_a_prune_waits_for_no_one_and_refuses_while_a_run_holds_the_disk_lock(
    tmp_path: Path,
) -> None:
    import fcntl

    env = make_world(tmp_path)
    disk = tmp_path / "disk"
    backup(tmp_path, env, "target", "add", "default", str(disk), "--init-marker")
    assert backup(tmp_path, env, "run", "--target", "default").returncode == 0
    with (disk / "3d-printops" / ".3d-backup.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # what a running backup holds
        blocked = backup(tmp_path, env, "prune", "--target", "default")
        peek = backup(tmp_path, env, "prune", "--target", "default", "--dry-run")
    assert blocked.returncode == 1 and "another backup run holds" in blocked.stderr
    assert peek.returncode == 0  # a dry run removes nothing, so it needs no lock
    assert backup(tmp_path, env, "prune", "--target", "default").returncode == 0


def test_a_broken_backup_yaml_is_a_clear_error_for_every_verb(tmp_path: Path) -> None:
    env = make_world(tmp_path)
    Path(env["PRINTOPS_BACKUP_CONFIG"]).write_text(
        "targets: [unclosed\n", encoding="utf-8"
    )
    for verb in (
        ("target", "ls"),
        ("run", "--target", "default"),
        ("ls", "--target", "default"),
    ):
        r = backup(tmp_path, env, *verb)
        assert (
            r.returncode == 1
            and "not valid YAML" in r.stderr
            and "Traceback" not in r.stderr
        ), verb


def test_discover_over_ssh_does_not_use_this_machines_data_dir(tmp_path: Path) -> None:
    """Story: `backup discover --ssh me@box` on a laptop. The laptop's ~/.local/share/3d-cli means nothing on the box,
    so without --data-dir no disk is marked as 'the data disk' and the output says it was not checked."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(
        '#!/bin/sh\nfor last; do :; done\nexec bash -c "$last"\n', encoding="utf-8"
    )
    ssh.chmod(0o755)
    env = {**make_world(tmp_path), "PATH": f"{bindir}:{os.environ['PATH']}"}
    found = backup(tmp_path, env, "discover", "--ssh", "me@box", "--json")
    assert found.returncode == 0, found.stderr
    body = json.loads(found.stdout)
    assert body["data_device"] == "" and not any(
        c["same_as_data"] for c in body["candidates"]
    )
    text = backup(tmp_path, env, "discover", "--ssh", "me@box")
    assert "No --data-dir" in text.stdout
    with_dir = backup(
        tmp_path,
        env,
        "discover",
        "--ssh",
        "me@box",
        "--data-dir",
        str(tmp_path),
        "--json",
    )
    assert (
        json.loads(with_dir.stdout)["data_device"] != ""
    )  # a data dir that exists on the box is honoured
