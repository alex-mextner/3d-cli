"""A watcher that talks to the user proves at start that it can (Chekhov 2026-10-23: the watcher's `tg` refused
every message, the error sat in a log, and a paused print waited 6 h for a warning that was never delivered)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from printer_ops.subscriptions import DeliveryState
from printer_ops.watch_sinks import STARTUP_TEST_EVERY_S, Dispatcher, DryRunSink, TgSink


class Tg:
    """The `tg` CLI: records every call; the first `fail` calls exit 1."""

    def __init__(self, fail: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.fail > 0:
            self.fail -= 1
            return subprocess.CompletedProcess(cmd, 1, "", "tg: --tag answer needs --reply-to")
        return subprocess.CompletedProcess(cmd, 0, "OK", "")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DeliveryState:
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    return DeliveryState()


def _sink(tg: Tg, state: DeliveryState, clock: Clock | None = None) -> TgSink:
    sink = TgSink(runner=tg, clock=clock or Clock())
    sink.ledger = state
    return sink


def test_a_test_message_goes_out_through_the_command_line_a_real_alert_uses(state: DeliveryState) -> None:
    tg = Tg()
    assert _sink(tg, state).startup_test(["u1", "k1se"]) is True
    (call,) = tg.calls
    assert call[:3] == ["tg", "--tag", "problem"]
    assert call[-1].startswith("[TEST] u1, k1se: the printer watcher has started")
    assert state.read().get("counters", {}) == {}  # a test is not an alert: no fallback_tg_sent


def test_a_tg_that_refuses_is_an_error_in_the_log_and_in_the_sink_stats(
    state: DeliveryState, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = _sink(Tg(fail=9), state)
    assert sink.startup_test(["u1"]) is False
    err = capsys.readouterr().err
    assert "ERROR: the Telegram test message FAILED" in err
    assert "--tag answer needs --reply-to" in err
    assert sink.stats.failed == 1
    assert (sink.stats.last_error or "").startswith("startup test: ")
    assert state.read().get("counters", {}) == {}  # no alert was lost: the alert chain's counters stay clean


def test_a_failed_test_is_retried_at_the_next_start_not_an_hour_later(state: DeliveryState) -> None:
    tg = Tg(fail=2)  # the tagged send and its plain retry both fail once
    sink = _sink(tg, state)
    assert sink.startup_test(["u1"]) is False
    assert sink.startup_test(["u1"]) is True
    assert len(tg.calls) == 3


def test_a_refused_tag_that_the_plain_retry_delivers_counts_as_delivered(
    state: DeliveryState, capsys: pytest.CaptureFixture[str]
) -> None:
    tg = Tg(fail=1)
    assert _sink(tg, state).startup_test(["u1"]) is True
    assert tg.calls[0][:3] == ["tg", "--tag", "problem"]
    assert "--tag" not in tg.calls[1]
    assert "tg refused the tagged/photo message" in capsys.readouterr().err


def test_a_service_restarting_in_a_loop_sends_one_test_per_hour(state: DeliveryState) -> None:
    tg, clock = Tg(), Clock()
    sink = _sink(tg, state, clock)
    assert sink.startup_test(["u1"]) is True
    clock.advance(30)
    assert sink.startup_test(["u1"]) is None
    clock.advance(STARTUP_TEST_EVERY_S)
    assert sink.startup_test(["u1"]) is True
    assert len(tg.calls) == 2


def test_another_service_is_not_vouched_for_by_the_first_ones_success(state: DeliveryState) -> None:
    """watch and vision are separate units with their own environment (PATH, HOME): each proves its own tg."""
    tg = Tg()
    sink = _sink(tg, state)
    assert sink.startup_test(["u1"], role="watch") is True
    assert sink.startup_test(["u1"], role="vision") is True
    assert sink.startup_test(["u1"], role="vision") is None
    assert len(tg.calls) == 2


def test_an_unreadable_state_file_turns_the_throttle_off_but_not_the_test(state: DeliveryState) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(json.dumps({"warnings": None}), encoding="utf-8")
    tg = Tg()
    assert _sink(tg, state).startup_test(["u1"]) is True
    assert len(tg.calls) == 1


def test_the_dispatcher_tests_only_the_printers_on_the_tg_route(state: DeliveryState) -> None:
    tg = Tg()
    d = Dispatcher()
    d.add("u1", [_sink(tg, state)])
    assert d.startup_test(["u1", "k1se"]) == {"tg": True}
    assert "[TEST] u1: " in tg.calls[0][-1]
    assert "k1se" not in tg.calls[0][-1]


def test_a_dry_run_route_is_not_tg_and_sends_no_test(state: DeliveryState) -> None:
    tg = Tg()
    d = Dispatcher()
    d.add("u1", [DryRunSink(TgSink(runner=tg))])
    assert d.startup_test(["u1"]) == {}
    assert tg.calls == []


def test_the_test_runs_off_the_main_thread_and_close_waits_for_it(state: DeliveryState) -> None:
    """A hung tg (60 s per call) must not delay watching, and the process must not exit under a half-sent message."""
    tg = Tg()
    d = Dispatcher()
    d.add("u1", [_sink(tg, state)])
    d.start_startup_test(["u1"], "watch")
    d.close(timeout=5)
    assert len(tg.calls) == 1


def test_a_failed_real_alert_is_still_counted_as_a_failed_emergency_message(state: DeliveryState) -> None:
    """The counters moved out of `_send` (so a [TEST] is not booked as an alert): deliver() books both outcomes."""
    from printer_ops.watch import CRITICAL, Event

    ok, dead = _sink(Tg(), state), _sink(Tg(fail=9), state)
    event = Event("u1", "vision_part_detached", CRITICAL, "u1: detached", {})
    ok.deliver([event])
    with pytest.raises(RuntimeError, match="plain retry"):
        dead.deliver([Event("k1se", "vision_part_detached", CRITICAL, "k1se: detached", {})])
    counters = state.read()["counters"]
    assert counters["u1"] == {"fallback_tg_sent": 1}
    assert counters["k1se"] == {"fallback_tg_failed": 1}


def test_an_unwritable_state_directory_sends_the_test_every_time_rather_than_none(
    state: DeliveryState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service with a read-only or wrongly owned state dir is exactly the one whose tg may be broken."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the state directory should be", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))
    tg = Tg()
    sink = _sink(tg, DeliveryState())
    assert sink.startup_test(["u1"]) is True
    assert sink.startup_test(["u1"]) is True
    assert len(tg.calls) == 2
