"""Readable `bin/3d printer subscriptions` story for issue #526: alerts that go nowhere are visible.

A fake `tg-ctl` first on PATH lists the live omp sessions; the delivery state is the JSON file the
watchers write. No printer is contacted (the fleet points at a closed loopback port)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fleet_fixtures import u1_entry, write_fleet
from printer_fakes import FakeMoonraker

from .workflow_helper import run_cli

LIVE = {
    "pid": 433553,
    "sessionId": "01a12cd6-7b39-7356",
    "cwd": "/tmp",
    "title": "MODEL: Finish 3D model",
}


def _fake_tgctl(tmp_path: Path, sessions: list[dict[str, Any]]) -> dict[str, str]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "tg-ctl"
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "if sys.argv[1:3] == ['omp-live', 'status']:\n"
        f"    print({json.dumps(sessions)!r})\n"
        "else:\n    print('{\"ok\":true}')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
    }


def _env(tmp_path: Path) -> dict[str, str]:
    moon = FakeMoonraker()
    moon.port = 7125  # never contacted: `subscriptions` only reads the registry
    fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
    return {**_fake_tgctl(tmp_path, [LIVE]), "THREED_FLEET": str(fleet)}


def _record_failures(tmp_path: Path, sid: str, failed: int) -> None:
    state = tmp_path / "xdg-state" / "3d-cli" / "printer-subscriptions.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"subscriptions": {sid: {"failed": failed, "consecutive_failed": failed, "delivered": 12,
                                            "last_ts": "2026-10-17T09:00:00+00:00",
                                            "last_error": "no live omp session matches 'dead0001'",
                                            "last_error_ts": "2026-10-23T10:00:00+00:00"}}, "agents": {}}),
        encoding="utf-8",
    )  # fmt: skip


def test_a_dead_subscription_is_flagged_and_the_unwatched_printer_is_called_out_until_a_live_session_subscribes(
    tmp_path: Path,
) -> None:
    """Three days after a session died, the owner runs `3d printer subscriptions`: the dead entry shows STALE with its
    failure count, last success and the command that removes it, and the camera-equipped U1 gets a warning because
    no live session receives its alerts. The exit code stays 0. Subscribing the live session clears the warning."""
    env = _env(tmp_path)
    dead = run_cli(
        tmp_path,
        "printer",
        "subscribe",
        "--session",
        "dead0001",
        "--printer",
        "u1",
        "--json",
        env_extra=env,
    )
    sid = json.loads(dead.stdout)["id"]
    _record_failures(tmp_path, sid, 89)

    listed = run_cli(tmp_path, "printer", "subscriptions", env_extra=env)
    as_json = run_cli(tmp_path, "printer", "subscriptions", "--json", env_extra=env)
    live = run_cli(
        tmp_path,
        "printer",
        "subscribe",
        "--session",
        "01a12cd6",
        "--printer",
        "u1",
        env_extra=env,
    )
    after = run_cli(tmp_path, "printer", "subscriptions", env_extra=env)

    assert dead.returncode == 0, dead.stderr
    assert listed.returncode == 0, listed.stderr
    out = listed.stdout
    assert (
        "STALE: session is not live and 89 deliveries failed in a row — 89 failed in total, last success 2026-10-17"
        in out
    )
    assert f"3d printer unsubscribe --id {sid}" in out
    assert "warning: u1 has a watcher but no live subscriber at warning level" in out
    rows = json.loads(as_json.stdout)
    assert (
        rows[0]["stale"] is True
        and rows[0]["consecutive_failed"] == 89
        and rows[0]["live"]["live"] is False
    )
    assert (
        "warning: u1 has a watcher" in as_json.stderr
        and "has a watcher" not in as_json.stdout
    )
    assert live.returncode == 0 and after.returncode == 0
    assert (
        "warning: u1" not in after.stdout and "STALE" in after.stdout
    )  # the dead entry is still listed until removed
