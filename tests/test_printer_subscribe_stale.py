"""`3d printer subscriptions` flags stale entries and warns about printers nobody receives alerts for
(issue #526). In-process: temp registry/state via XDG, a fake `tg-ctl`, a patched device registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from printer_doctor_fakes import FakeTgCtl, device
from printer_ops import subscriptions as subs
from printer_ops.devices import Device
from printer_ops.watch_sinks import OmpLive

LIVE = {
    "pid": 4242,
    "sessionId": "0aaa1111-live-session",
    "cwd": "/tmp",
    "title": "main",
}
CAMERA = {
    "vision": {"camera": {"kind": "snapshot-url", "url": "http://127.0.0.1:1/snap"}}
}


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
            monkeypatch.setenv(var, str(tmp_path / sub))
        self.tg = FakeTgCtl([LIVE])
        tg = self.tg

        class Live(
            OmpLive
        ):  # `liveness` calls OmpLive.resolve: keep the real class, fake the runner
            def __init__(self) -> None:
                super().__init__(runner=tg)

        monkeypatch.setattr(subs, "OmpLive", Live)
        self.devices: dict[str, Device] = {}
        monkeypatch.setattr(
            "printer_ops.devices.load_devices", lambda *a, **k: self.devices
        )

    def watcher(self, name: str, *, camera: bool = True, access: str = "full") -> None:
        self.devices[name] = device(name, watch=CAMERA if camera else {}, access=access)

    def subscribe(
        self,
        session: str,
        *printers: str,
        failed: int | None = None,
        ok_at: str | None = None,
    ) -> str:
        entry, _ = subs.Registry().put(
            subs.make(
                session=session,
                printers=list(printers),
                created="2026-10-18T10:00:00+00:00",
            )
        )
        row: dict[str, Any] = {}
        if failed is not None:
            row |= {"failed": failed, "consecutive_failed": failed, "last_error": "no live omp session matches",
                    "last_error_ts": "2026-10-23T10:00:00+00:00"}  # fmt: skip
        if ok_at:
            row |= {"delivered": 4, "last_ts": ok_at}
        if row:
            subs.DeliveryState().update(
                lambda data: data["subscriptions"].__setitem__(entry.id, row)
            )
        return entry.id


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def test_a_dead_failing_entry_is_flagged_stale_with_counts_and_the_removal_command(
    env: Env, capsys: Any
) -> None:
    sid = env.subscribe("dead0001", "p1", failed=89, ok_at="2026-10-17T09:00:00+00:00")
    assert subs.run("subscriptions", []) == 0  # exit code unchanged
    out = capsys.readouterr().out
    assert f"{sid}  session dead0001" in out and "live: NO" in out
    assert (
        "STALE: session is not live and 89 deliveries failed in a row — 89 failed in total"
        in out
    )
    assert "last success 2026-10-17T09:00:00+00:00" in out
    assert f"3d printer unsubscribe --id {sid}" in out


def test_a_live_healthy_entry_is_not_flagged(env: Env, capsys: Any) -> None:
    env.subscribe("0aaa1111", "p1", failed=0, ok_at="2026-10-23T09:00:00+00:00")
    assert subs.run("subscriptions", []) == 0
    assert "STALE" not in capsys.readouterr().out


def test_a_live_entry_whose_deliveries_keep_failing_is_stale_too(
    env: Env, capsys: Any
) -> None:
    env.subscribe("0aaa1111", "p1", failed=subs.STALE_AFTER_FAILURES)
    subs.run("subscriptions", [])
    out = capsys.readouterr().out
    assert (
        "live: yes" in out
        and f"STALE: {subs.STALE_AFTER_FAILURES} deliveries failed in a row" in out
    )


def test_fewer_failures_than_the_threshold_on_a_live_session_is_fine(
    env: Env, capsys: Any
) -> None:
    env.subscribe("0aaa1111", "p1", failed=subs.STALE_AFTER_FAILURES - 1)
    subs.run("subscriptions", [])
    assert "STALE" not in capsys.readouterr().out


def test_a_watched_printer_without_a_live_subscriber_gets_a_warning_and_exit_stays_0(
    env: Env, capsys: Any
) -> None:
    env.watcher("p1")
    env.watcher("p2")
    env.subscribe("dead0001", "p1", failed=89)
    env.subscribe("0aaa1111", "p2")
    assert subs.run("subscriptions", []) == 0
    out = capsys.readouterr().out
    assert (
        "warning: p1 has a watcher but no live subscriber" in out
        and "--printer p1" in out
    )
    assert "warning: p2" not in out


def test_the_warning_covers_a_registry_with_no_subscriptions_and_skips_printers_without_a_watcher(
    env: Env, capsys: Any
) -> None:
    env.watcher("cam")
    env.watcher("nocam", camera=False)
    env.watcher("offline", access="offline")
    assert subs.run("subscriptions", []) == 0
    out = capsys.readouterr().out
    assert "no subscriptions" in out and "warning: cam has a watcher" in out
    assert "nocam" not in out and "offline" not in out


def test_no_warning_when_tg_ctl_cannot_say_who_is_live(env: Env, capsys: Any) -> None:
    env.watcher("p1")
    env.subscribe("dead0001", "p1", failed=89)
    env.tg.missing = True
    assert subs.run("subscriptions", []) == 0
    out = capsys.readouterr().out
    assert "warning:" not in out and "live: unknown" in out


def test_json_gains_keys_without_breaking_the_array_and_warnings_go_to_stderr(
    env: Env, capsys: Any
) -> None:
    env.watcher("p1")
    sid = env.subscribe("dead0001", "p1", failed=89, ok_at="2026-10-17T09:00:00+00:00")
    assert subs.run("subscriptions", ["--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    assert {
        "id",
        "session",
        "printers",
        "kinds",
        "min_severity",
        "live",
        "state",
    } <= set(row)  # the old keys
    assert row["id"] == sid and row["stale"] is True and row["consecutive_failed"] == 89
    assert row["stale_reasons"] == [
        "session is not live",
        "89 deliveries failed in a row",
    ]
    assert row["last_success"] == "2026-10-17T09:00:00+00:00"
    assert "warning: p1 has a watcher but no live subscriber" in captured.err


def test_unreadable_devices_never_break_the_listing(
    env: Env, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from errors import UsageError

    def broken(*a: Any, **k: Any) -> dict[str, Device]:
        raise UsageError("printer-devices.yaml is broken", command="printer")

    monkeypatch.setattr("printer_ops.devices.load_devices", broken)
    env.subscribe("0aaa1111", "p1")
    assert subs.run("subscriptions", []) == 0
    assert "printer-devices.yaml is broken" in capsys.readouterr().err


def test_help_documents_the_stale_flag() -> None:
    assert "STALE" in subs.USAGE and "alert-subscribers" in subs.USAGE
    assert f"last {subs.STALE_AFTER_FAILURES} deliveries" in subs.USAGE
