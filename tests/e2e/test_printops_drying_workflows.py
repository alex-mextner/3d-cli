"""`3d printops dry` end to end: bin/3d → real uvicorn 3d-api → FakeSpoolman over HTTP (#162)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from printops_fixtures import LiveApi

from printops import db, locations
from printops.spoolman import decode_extra, encode_extra

from .workflow_helper import run_cli


def _env(live: LiveApi, token: str = "rw") -> dict[str, str]:
    return {"PRINTOPS_API": live.url, "PRINTOPS_TOKEN": live.tokens[token]}


def _spool(live: LiveApi, material: str, opened_days_ago: float | None = None) -> int:
    sp = live.spoolman
    vendor = sp.create("vendor", {"name": f"V {material}"})
    fil = sp.create(
        "filament",
        {
            "name": material,
            "vendor_id": vendor["id"],
            "material": material,
            "density": 1.2,
            "diameter": 1.75,
            "weight": 1000,
        },
    )
    extra = {}
    if opened_days_ago is not None:
        opened = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=opened_days_ago
        )
        extra["opened_at"] = encode_extra(opened.isoformat())
    return int(sp.create("spool", {"filament_id": fil["id"], "extra": extra})["id"])


def _add_dryer(live: LiveApi) -> None:
    conn = db.open_db(live.settings.db_path)
    try:
        locations.add(conn, "dryer:1", "dryer", actor="e2e")
    finally:
        conn.close()


def test_dry_session_start_ls_finish_marks_spools_fresh(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """The user puts a PETG and a PA spool into the dryer, checks it is running, takes them out
    with a scale reading, and Spoolman shows both as freshly dried."""
    _add_dryer(live_api)
    petg, pa = _spool(live_api, "PETG"), _spool(live_api, "PA")
    env = _env(live_api)

    started = run_cli(
        tmp_path,
        "printops",
        "dry",
        "start",
        "dryer:1",
        str(petg),
        str(pa),
        "--weights",
        f"{pa}=1180",
        env_extra=env,
    )
    running = run_cli(tmp_path, "printops", "dry", "ls", "--open", env_extra=env)
    finished = run_cli(
        tmp_path,
        "printops",
        "dry",
        "finish",
        "1",
        "--weights",
        f"{pa}=1172.5",
        "--rh",
        "11",
        env_extra=env,
    )
    as_json = run_cli(tmp_path, "printops", "dry", "ls", "--json", env_extra=env)

    assert started.returncode == 0, started.stderr
    assert "started #1  dryer:1  55 °C × 12 h  running, ends" in started.stdout
    assert "rated 80 °C: at 55 °C it may not dry fully" in started.stdout
    assert running.returncode == 0 and running.stdout.startswith("#1  dryer:1")
    assert finished.returncode == 0, finished.stderr
    assert "finished #1" in finished.stdout and f"{pa} 1180→1172.5 g" in finished.stdout
    assert "RH ?→11%" in finished.stdout
    sessions = json.loads(as_json.stdout)["sessions"]
    assert sessions[0]["open"] is False
    for sid in (petg, pa):
        extra = live_api.spoolman.get("spool", sid)["extra"]
        assert decode_extra(extra["dry_state"]) == "fresh"
        assert decode_extra(extra["last_dried_at"]) == sessions[0]["ended_at"]


def test_dry_due_lists_overdue_spools_and_check_emits_drying_due(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """A PETG spool open for 40 days is due; `due --check` writes dry_state and the event log
    carries drying.due; a read-only token can list but not check."""
    petg = _spool(live_api, "PETG", opened_days_ago=40)
    _spool(live_api, "PLA", opened_days_ago=5)
    env = _env(live_api)

    listed = run_cli(tmp_path, "printops", "dry", "due", env_extra=env)
    checked = run_cli(tmp_path, "printops", "dry", "due", "--check", env_extra=env)
    read_only = run_cli(
        tmp_path, "printops", "dry", "due", "--check", env_extra=_env(live_api, "read")
    )
    every = run_cli(
        tmp_path, "printops", "dry", "due", "--all", "--json", env_extra=env
    )
    log = run_cli(tmp_path, "printops", "events", "--json", env_extra=env)

    assert listed.returncode == 0, listed.stderr
    assert f"due     spool {petg} (V PETG PETG PETG)  [medium]" in listed.stdout
    assert "opened 40 d ago ≥ 30 d (medium)" in listed.stdout
    assert checked.returncode == 0, checked.stderr
    assert "dry_state changed: 2; drying.due emitted: 1" in checked.stdout
    assert read_only.returncode == 1 and "HTTP 403" in read_only.stderr
    states = {a["spool_id"]: a["state"] for a in json.loads(every.stdout)["spools"]}
    assert states[petg] == "due" and sorted(states.values()) == ["due", "ok"]
    kinds = [json.loads(line)["kind"] for line in log.stdout.splitlines()]
    assert "drying.due" in kinds
    assert (
        decode_extra(live_api.spoolman.get("spool", petg)["extra"]["dry_state"])
        == "due"
    )


def test_dry_usage_errors_exit_2(tmp_path: Path, live_api: LiveApi) -> None:
    env = _env(live_api)
    bad_weights = run_cli(
        tmp_path,
        "printops",
        "dry",
        "start",
        "dryer:1",
        "1",
        "--weights",
        "1:900",
        env_extra=env,
    )
    unknown = run_cli(tmp_path, "printops", "dry", "bake", env_extra=env)
    helped = run_cli(tmp_path, "printops", "dry", "--help", env_extra=env)
    assert bad_weights.returncode == 2 and "ID=GRAMS" in bad_weights.stderr
    assert unknown.returncode == 2 and "start, finish, ls, due" in unknown.stderr
    assert (
        helped.returncode == 0
        and "3d printops dry start DRYER SPOOL_ID..." in helped.stdout
    )
