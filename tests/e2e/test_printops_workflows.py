from __future__ import annotations

import json
import sqlite3
import subprocess
import time
import sys
from pathlib import Path

from printops_fixtures import LiveApi

from printops import db, events

from .workflow_helper import THREED, isolated_env, run_cli


def _env(live: LiveApi, token: str | None = "rw") -> dict[str, str]:
    env = {"PRINTOPS_API": live.url}
    if token:
        env["PRINTOPS_TOKEN"] = live.tokens[token]
    return env


def _emit(live: LiveApi, kind: str, subject: str, payload: dict[str, object] | None = None) -> None:
    conn = db.open_db(live.settings.db_path)
    try:
        events.emit(conn, kind, subject, payload, "e2e")
    finally:
        conn.close()


def test_printops_health_reports_api_db_and_spoolman_fields(tmp_path: Path, live_api: LiveApi) -> None:
    """The operator checks the freshly deployed 3d-api: DB migrated, Spoolman up, our extra
    fields registered; then Spoolman goes away and health says so with a failing exit code."""
    ok = run_cli(tmp_path, "printops", "health", env_extra=_env(live_api, None))
    as_json = run_cli(tmp_path, "printops", "health", "--json", env_extra=_env(live_api, None))
    live_api.spoolman.down = True
    degraded = run_cli(tmp_path, "printops", "health", env_extra=_env(live_api, None))

    assert ok.returncode == 0, ok.stderr
    assert f"3d-api {live_api.url}  ok  (version live-test)" in ok.stdout
    assert "spoolman  ok  v0.27.0  extra fields ok" in ok.stdout and "migrations 1" in ok.stdout
    body = json.loads(as_json.stdout)
    assert body["status"] == "ok" and body["spoolman"]["extra_fields"]["missing"] == []
    assert degraded.returncode == 1 and "UNREACHABLE" in degraded.stdout


def test_printops_events_lists_pages_and_needs_a_token(tmp_path: Path, live_api: LiveApi) -> None:
    """A user reads the latest PrintOps events, pages on with the printed cursor, and gets a
    clear 401 with the fix when no token is configured."""
    _emit(live_api, "spool.measured", "spool:7", {"gross_g": 812})
    _emit(live_api, "slot.mismatch", "printer:u1/T3", {"expected": 7, "rfid": 9})
    listed = run_cli(tmp_path, "printops", "events", env_extra=_env(live_api))
    newest = run_cli(tmp_path, "printops", "events", "--last", "1", "--json", env_extra=_env(live_api))
    after = run_cli(tmp_path, "printops", "events", "--since", "2", env_extra=_env(live_api))
    anonymous = run_cli(tmp_path, "printops", "events", env_extra=_env(live_api, None))

    assert listed.returncode == 0, listed.stderr
    assert "#1  " in listed.stdout and "spool.measured  spool:7  by e2e  {\"gross_g\":812}" in listed.stdout
    assert "slot.mismatch  printer:u1/T3" in listed.stdout and "--since 2" in listed.stderr
    assert json.loads(newest.stdout)["kind"] == "slot.mismatch"
    assert after.returncode == 0 and after.stdout == ""
    assert anonymous.returncode == 1 and "HTTP 401" in anonymous.stderr and "PRINTOPS_TOKEN" in anonymous.stderr


def test_printops_events_follow_streams_new_events(tmp_path: Path, live_api: LiveApi) -> None:
    """A bot operator tails the event log live and sees an event the moment 3d-api records it."""
    env = isolated_env(tmp_path)
    env.update(_env(live_api, "read"))
    proc = subprocess.Popen([sys.executable, str(THREED), "printops", "events", "--follow", "--json"],
                            cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Without --since the follower starts at the newest event once connected, so keep
        # emitting until the first one comes through.
        deadline = time.monotonic() + 20
        line = ""
        while not line and time.monotonic() < deadline:
            _emit(live_api, "drying.due", "spool:3", {"hours_over": 2})
            line = _readline(proc, 1.0)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    event = json.loads(line)
    assert (event["kind"], event["subject"], event["payload"]) == ("drying.due", "spool:3", {"hours_over": 2})


def _readline(proc: subprocess.Popen[str], timeout: float) -> str:
    import selectors

    assert proc.stdout is not None
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    ready = sel.select(timeout)
    sel.close()
    return proc.stdout.readline() if ready else ""


def test_printops_token_create_then_use_then_revoke(tmp_path: Path, live_api: LiveApi) -> None:
    """The operator mints a token for the HA integration, the API accepts it immediately,
    and revoking it locks the client out again — no API restart in between."""
    tokens_file = str(live_api.settings.tokens_file)
    created = run_cli(tmp_path, "printops", "token", "create", "ha", "--scopes", "read", "--file", tokens_file)
    token = created.stdout.strip()
    with_token = run_cli(tmp_path, "printops", "events", env_extra={"PRINTOPS_API": live_api.url,
                                                                   "PRINTOPS_TOKEN": token})
    listed = run_cli(tmp_path, "printops", "token", "list", "--file", tokens_file)
    revoked = run_cli(tmp_path, "printops", "token", "revoke", "ha", "--file", tokens_file)
    after = run_cli(tmp_path, "printops", "events", env_extra={"PRINTOPS_API": live_api.url, "PRINTOPS_TOKEN": token})
    bad_scope = run_cli(tmp_path, "printops", "token", "create", "x", "--scopes", "root", "--file", tokens_file)

    assert created.returncode == 0, created.stderr
    assert token.startswith("po_") and "only time it is shown" in created.stderr
    assert token not in Path(tokens_file).read_text(encoding="utf-8")
    assert with_token.returncode == 0, with_token.stderr
    assert "ha" in listed.stdout and "read" in listed.stdout
    assert revoked.returncode == 0 and "revoked 'ha'" in revoked.stdout
    assert after.returncode == 1 and "unknown or revoked token" in after.stderr
    assert bad_scope.returncode == 2 and "accepted: read, write, printer-control, camera" in bad_scope.stderr


def test_printops_backup_writes_consistent_snapshots(tmp_path: Path, live_api: LiveApi) -> None:
    """The nightly backup snapshots the live PrintOps DB and the tokens file into the backup dir
    that Home Assistant's own backup picks up."""
    _emit(live_api, "spool.moved", "spool:1", {"to": "WH:A/S1"})
    dest = tmp_path / "printops-backup"
    env = {"PRINTOPS_DB": str(live_api.settings.db_path), "PRINTOPS_TOKENS_FILE": str(live_api.settings.tokens_file)}
    result = run_cli(tmp_path, "printops", "backup", "--dest", str(dest), env_extra=env)
    missing = run_cli(tmp_path, "printops", "backup", "--dest", str(dest), "--spoolman-db",
                      str(tmp_path / "nope.db"), env_extra=env)

    assert result.returncode == 0, result.stderr
    assert "printops.db" in result.stdout and "tokens.json" in result.stdout
    snap = sqlite3.connect(dest / "printops.db")
    assert snap.execute("SELECT subject FROM events").fetchall() == [("spool:1",)]
    snap.close()
    assert json.loads((dest / "tokens.json").read_text(encoding="utf-8"))["tokens"]
    assert missing.returncode == 1 and "FAIL  spoolman" in missing.stdout


def test_printops_help_lists_every_sub(tmp_path: Path) -> None:
    """`3d printops --help` names every subcommand so a user can discover them."""
    r = run_cli(tmp_path, "printops", "--help")
    assert r.returncode == 0
    for sub in ("health", "events", "token", "deploy", "backup"):
        assert f"  {sub}" in r.stdout
    assert "unknown printops subcommand" in run_cli(tmp_path, "printops", "nope").stderr


def test_printops_backup_refuses_to_overwrite_a_live_db(tmp_path: Path, live_api: LiveApi) -> None:
    """Pointing the backup at the live data dir must not swap the running DB for a snapshot."""
    data_dir = live_api.settings.db_path.parent
    env = {"PRINTOPS_DB": str(live_api.settings.db_path), "PRINTOPS_TOKENS_FILE": str(live_api.settings.tokens_file)}
    before = live_api.settings.db_path.stat().st_ino
    clash = run_cli(tmp_path, "printops", "backup", "--dest", str(data_dir), env_extra=env)
    assert clash.returncode == 2 and "would overwrite a live source" in clash.stderr
    assert live_api.settings.db_path.stat().st_ino == before
