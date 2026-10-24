"""An unsent pause can recover; an entered actuator can never be repeated."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_printer_guard import JOB, Clock, ScriptedPrinter, arm, score, tick, until_candidate

from printer_ops import guard as policy
from printer_ops.base import PrinterCommError, PrinterRefused
from printer_ops.vision import Frame


@pytest.mark.parametrize("failure", ["telemetry", "expired"])
def test_definitely_unsent_claim_is_released_durably_and_new_evidence_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    original_status, original_sync = printer.status, policy._sync_directory
    fault_pending = True
    synced: list[bool] = []

    def status() -> Any:
        nonlocal fault_pending
        if failure == "telemetry" and fault_pending and guard.attempt_path.exists():
            fault_pending = False
            assert printer.pauses == 0
            raise PrinterCommError("final job check timed out", command="printer guard")
        return original_status()

    def sync(path: Path) -> None:
        nonlocal fault_pending
        original_sync(path)
        synced.append(guard.attempt_path.exists())
        if failure == "expired" and fault_pending and guard.attempt_path.exists():
            fault_pending = False
            clock.sleep(31.0)

    monkeypatch.setattr(printer, "status", status)
    monkeypatch.setattr(policy, "_sync_directory", sync)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "blind" and result["terminal"] is False
    assert result["camera_ready"] is False and result["confirmations"] == 0
    assert guard.tracker.frames == 0 and printer.pauses == 0
    assert not guard.attempt_path.exists() and synced[-2:] == [True, False]
    for i in range(2):
        result = tick(guard, clock, 100 + i, score(q=0.4, p=0.5))
        assert result["confirmations"] == 0 and printer.pauses == 0
    for i in range(40):
        result = tick(guard, clock, 200 + i)
        if result["terminal"]:
            break
    assert result["outcome"] == "paused" and printer.pauses == 1
    assert json.loads(guard.attempt_path.read_text())["outcome"] == "paused"


@pytest.mark.parametrize("precheck", ["unreachable", "changed", "same_job"])
def test_precheck_never_removes_or_overwrites_a_replacement_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, precheck: str,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    original_status = printer.status
    replacement = b'{"owner":"another guard"}\n'

    def status() -> Any:
        if guard.attempt_path.exists():
            other = guard.attempt_path.with_suffix(".replacement")
            other.write_bytes(replacement)
            other.replace(guard.attempt_path)
            if precheck == "changed":
                raise PrinterRefused("job changed", command="printer guard")
            if precheck == "unreachable":
                raise PrinterCommError("final job check timed out", command="printer guard")
        return original_status()

    monkeypatch.setattr(printer, "status", status)
    result = tick(guard, clock, n + 1)
    assert result["terminal"] is True and result["outcome"] == "disarmed"
    assert printer.pauses == 0 and guard.attempt_path.read_bytes() == replacement


def test_failed_release_durability_does_not_report_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    original_status, original_sync = printer.status, policy._sync_directory

    def status() -> Any:
        if guard.attempt_path.exists():
            raise PrinterCommError("final job check timed out", command="printer guard")
        return original_status()

    def sync(path: Path) -> None:
        if not guard.attempt_path.exists():
            raise OSError("claim removal was not durable")
        original_sync(path)

    monkeypatch.setattr(printer, "status", status)
    monkeypatch.setattr(policy, "_sync_directory", sync)
    with pytest.raises(OSError, match="not durable"):
        tick(guard, clock, n + 1)
    assert printer.pauses == 0


def test_released_claim_does_not_allow_two_guards_to_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, printer, clock = arm(tmp_path)
    second = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state",
                                   evidence_dir=tmp_path / "evidence", clock=clock, sleep=clock.sleep)
    printer.pause_changes_state = False  # uncertain PAUSE must remain exclusive while printing
    original_status = printer.status
    fault_pending = True

    def status() -> Any:
        nonlocal fault_pending
        if fault_pending and first.attempt_path.exists():
            fault_pending = False
            raise PrinterCommError("first guard final read timed out", command="printer guard")
        return original_status()

    monkeypatch.setattr(printer, "status", status)
    released = False
    for i in range(60):
        clock.sleep(5.0)
        frame = Frame(clock.now, f"concurrent-{i}".encode(), clock.now - 1)
        a = first.observe(frame, score())
        if a["outcome"] == "blind" and not fault_pending:
            released = True
            assert not first.attempt_path.exists()
        b = second.observe(frame, score())
        if a["terminal"] and b["terminal"]:
            break
    assert released and printer.pauses == 1
    assert a["outcome"] == "disarmed" and b["outcome"] == "pause_unverified"
    with pytest.raises(PrinterRefused, match="already attempted"):
        policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state",
                             evidence_dir=tmp_path / "evidence", clock=clock, sleep=clock.sleep)


@pytest.mark.parametrize("paused", [False, True])
def test_refused_actuator_keeps_claim_and_reads_back_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paused: bool,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)

    def refuse() -> None:
        printer.pauses += 1
        if paused:
            printer.raw["print_stats"]["state"] = "paused"
        raise PrinterRefused("firmware rejected pause", command="printer guard")

    monkeypatch.setattr(printer, "pause", refuse)
    result = tick(guard, clock, n + 1)
    assert result["terminal"] is True
    assert result["outcome"] == ("paused" if paused else "pause_unverified")
    assert result["pause_acknowledged"] is False and "rejected pause" in result["error"]
    assert json.loads(guard.attempt_path.read_text())["outcome"] == result["outcome"]
    assert guard.observe(None, None) == result and printer.pauses == 1


def test_post_request_audit_preserves_a_replacement_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    original_pause = printer.pause
    replacement = b'{"owner":"another guard"}\n'

    def pause() -> None:
        original_pause()
        other = guard.attempt_path.with_suffix(".replacement")
        other.write_bytes(replacement)
        other.replace(guard.attempt_path)

    monkeypatch.setattr(printer, "pause", pause)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "paused" and "ownership changed" in result["claim_error"]
    assert guard.attempt_path.read_bytes() == replacement
    assert guard.observe(None, None) == result and printer.pauses == 1


@pytest.mark.parametrize("gap", [30.001, 600.0])
def test_default_interval_does_not_keep_failure_evidence_across_outages(tmp_path: Path, gap: float) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    clock.sleep(gap)
    result = guard.observe(Frame(clock.now, f"after-gap-{n}".encode(), clock.now - 1), score())
    assert result["outcome"] == "blind" and result["confirmations"] == 0
    assert guard.tracker.frames == 0 and printer.pauses == 0


@pytest.mark.parametrize("gap,continuous", [(32.0, True), (32.001, False)])
def test_maximum_interval_has_a_bounded_acquisition_margin(tmp_path: Path, gap: float, continuous: bool) -> None:
    printer, clock = ScriptedPrinter(), Clock()
    guard = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state", interval=30.0,
                                 evidence_dir=tmp_path / "evidence", clock=clock, sleep=clock.sleep)
    n = until_candidate(guard, clock)
    clock.sleep(gap)
    result = guard.observe(Frame(clock.now, f"maximum-gap-{n}".encode(), clock.now - 1), score())
    assert result["outcome"] == ("paused" if continuous else "blind")
    assert printer.pauses == int(continuous)


@pytest.mark.parametrize("kind,reason", [
    ("capture_future", "acquisition time is in the future"),
    ("source_future", "Last-Modified is more than 5 seconds ahead"),
    ("source_old", "Last-Modified is older than 30 seconds"),
    ("native_future", "predates the native job start"),
])
def test_clock_incompatibility_is_explicitly_blind_without_weakening_freshness(
    tmp_path: Path, kind: str, reason: str,
) -> None:
    printer, clock = ScriptedPrinter(), Clock()
    if kind == "native_future":
        printer.record.start_time = clock.now + 120.0
    guard = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state",
                                 evidence_dir=tmp_path / "evidence", clock=clock, sleep=clock.sleep)
    ts = clock.now + (0.01 if kind == "capture_future" else 0.0)
    modified = clock.now + {"source_future": 5.01, "source_old": -30.01}.get(kind, -1.0)
    result = guard.observe(Frame(ts, b"clock-test", modified), score())
    assert result["outcome"] == "blind" and result["camera_ready"] is False
    assert reason in result["reason"] and "clock" in result["reason"]
    assert printer.pauses == 0 and not guard.attempt_path.exists()


@pytest.mark.parametrize("capture_age,source_age,accepted", [
    (30.0, 30.0, True), (30.001, 0.0, False), (0.0, 30.001, False),
    (-0.001, 0.0, False), (0.0, -5.0, True), (0.0, -5.001, False),
])
def test_existing_frame_age_and_future_tolerances_remain_strict(
    tmp_path: Path, capture_age: float, source_age: float, accepted: bool,
) -> None:
    guard, printer, clock = arm(tmp_path)
    result = guard.observe(Frame(clock.now - capture_age, b"age-boundary", clock.now - source_age), score())
    assert (result["outcome"] == "observing") == accepted
    assert printer.pauses == 0


def test_nonfinite_host_clock_cannot_accept_evidence(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    frame = Frame(clock.now, b"invalid-host-clock", clock.now - 1)
    clock.now = float("nan")
    result = guard.observe(frame, score())
    assert result["outcome"] == "blind" and "clock" in result["reason"]
    assert printer.pauses == 0


def test_interrupted_warmups_cannot_poison_detection_of_later_incident(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    for cycle in range(40):
        for frame in range(6):
            result = tick(guard, clock, cycle * 10 + frame, score(p=8, q=8))
            assert not result['terminal'] and printer.pauses == 0
        clock.sleep(5)
        assert guard.observe(None, None)['outcome'] == 'blind'
    # These are the incident's actual verified per-frame scores. An interrupted
    # warmup must not become a lifetime baseline that suppresses this failure.
    for i in range(100):
        result = tick(guard, clock, 1000 + i)
        if result['terminal']:
            break
    assert result['outcome'] == 'paused' and printer.pauses == 1
    assert i < 12, 'a fresh trend should protect the job after its normal warmup'


@pytest.mark.parametrize('failure', ['manual_pause', 'duration_reset', 'changed_job'])
def test_refused_final_precheck_releases_our_unsent_claim_and_disarms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    original_status, original_sync = printer.status, policy._sync_directory
    synced: list[bool] = []

    def status() -> Any:
        if guard.attempt_path.exists():
            if failure == 'manual_pause':
                printer.raw['print_stats']['state'] = 'paused'
            elif failure == 'duration_reset':
                printer.raw['print_stats']['total_duration'] = 1.0
            else:
                printer.record.job_id = '00006E'
        return original_status()

    def sync(path: Path) -> None:
        original_sync(path)
        synced.append(guard.attempt_path.exists())

    monkeypatch.setattr(printer, 'status', status)
    monkeypatch.setattr(policy, '_sync_directory', sync)
    result = tick(guard, clock, n + 1)
    assert result['outcome'] == 'disarmed' and result['terminal'] is True
    assert 'unsent pause claim released' in result['reason']
    assert printer.pauses == 0 and not guard.attempt_path.exists()
    assert synced[-2:] == [True, False]
    assert guard.observe(None, None) == result
    if failure != 'changed_job':
        # An operator may later resume; this test changes only an in-memory fake.
        printer.raw['print_stats'].update(state='printing', total_duration=100.0)
        rearmed = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / 'state',
                                       evidence_dir=tmp_path / 'evidence', clock=clock, sleep=clock.sleep)
        assert rearmed.result is None and printer.pauses == 0
    else:
        with pytest.raises(PrinterRefused, match='current printing job'):
            policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / 'state',
                                 evidence_dir=tmp_path / 'evidence', clock=clock, sleep=clock.sleep)


def test_refused_precheck_release_must_be_durable_before_terminal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    original_status, original_sync = printer.status, policy._sync_directory

    def status() -> Any:
        if guard.attempt_path.exists():
            raise PrinterRefused('manually paused', command='printer guard')
        return original_status()

    def sync(path: Path) -> None:
        if not guard.attempt_path.exists():
            raise OSError('refused claim removal was not durable')
        original_sync(path)

    monkeypatch.setattr(printer, 'status', status)
    monkeypatch.setattr(policy, '_sync_directory', sync)
    with pytest.raises(OSError, match='not durable'):
        tick(guard, clock, n + 1)
    assert printer.pauses == 0 and guard.result is None


def test_two_second_polling_of_five_second_camera_still_pauses_once(tmp_path: Path) -> None:
    printer, clock = ScriptedPrinter(), Clock()
    guard = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / 'state', interval=2,
                                 evidence_dir=tmp_path / 'evidence', clock=clock, sleep=clock.sleep)
    started = clock.now
    waits = 0
    for poll in range(60):
        clock.now = started + 2 * (poll + 1)
        image_index = int((clock.now - started) // 5)
        frame = Frame(clock.now, f'cached-image-{image_index}'.encode(), started + image_index * 5)
        result = guard.observe(frame, score())
        if result['outcome'] == 'waiting':
            waits += 1
            assert result['camera_ready'] is False and printer.pauses == 0
        if result['terminal']:
            break
    assert result['outcome'] == 'paused' and printer.pauses == 1
    assert waits > 0 and poll < 30


def test_fresh_duplicate_never_counts_or_advances_last_distinct_frame(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    accepted_time, frames, trend = guard.last_frame_ts, guard.tracker.frames, guard.tracker.adjusted
    clock.sleep(2)
    result = guard.observe(Frame(clock.now, f'frame-{n}'.encode(), clock.now - 2), score())
    assert result['outcome'] == 'waiting' and result['camera_ready'] is False
    assert result['confirmations'] == 1 and printer.pauses == 0
    assert guard.last_frame_ts == accepted_time and guard.tracker.frames == frames
    assert guard.tracker.adjusted == trend and not guard.attempt_path.exists()
    assert tick(guard, clock, n + 1)['outcome'] == 'paused'
    assert printer.pauses == 1


def test_frozen_frame_waits_briefly_then_remains_blind_without_refreshing_anchor(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    first = Frame(clock.now, b'frozen-without-last-modified', None)
    assert guard.observe(first, score())['outcome'] == 'observing'
    accepted_time = guard.last_frame_ts
    for seconds in range(2, 65, 2):
        clock.now = first.ts + seconds
        result = guard.observe(Frame(clock.now, first.jpeg, None), score())
        assert result['outcome'] == ('waiting' if seconds <= 30 else 'blind')
        assert result['camera_ready'] is False and printer.pauses == 0
        assert guard.last_frame_ts == accepted_time
    assert guard.tracker.frames == 0 and guard.tracker.long_mean == 0
    assert guard.tracker.lifetime == guard.tracker.params.prior_frames


@pytest.mark.parametrize('kind', ['source_stale', 'invalid_score'])
def test_duplicate_with_unusable_current_evidence_is_blind(tmp_path: Path, kind: str) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    clock.sleep(2)
    modified = clock.now - 31 if kind == 'source_stale' else clock.now - 1
    value = score(q=None) if kind == 'invalid_score' else score()
    result = guard.observe(Frame(clock.now, f'frame-{n}'.encode(), modified), value)
    assert result['outcome'] == 'blind' and result['confirmations'] == 0
    assert guard.tracker.frames == 0 and guard.tracker.long_mean == 0 and printer.pauses == 0


@pytest.mark.parametrize('source_time', ['absent', 'fixed', 'refreshed'])
def test_alternating_cached_images_cannot_authorize_pause_or_refresh_continuity(
    tmp_path: Path, source_time: str,
) -> None:
    printer, clock = ScriptedPrinter(), Clock()
    guard = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / 'state', interval=2,
                                 evidence_dir=tmp_path / 'evidence', clock=clock, sleep=clock.sleep)
    started = clock.now
    observations = []
    for poll in range(50):
        clock.sleep(2)
        image = poll % 2
        modified = None if source_time == 'absent' else (
            started + image if source_time == 'fixed' else clock.now - 1)
        result = guard.observe(Frame(clock.now, f'cached-{image}'.encode(), modified), score())
        observations.append((result, guard.last_frame_ts))
        if result['terminal']:
            break
    assert not result['terminal'] and printer.pauses == 0
    for repeated, accepted_time in observations[2:]:
        assert repeated['outcome'] in ('waiting', 'blind') and repeated['camera_ready'] is False
        assert accepted_time == started + 4
    assert result['outcome'] == 'blind' and guard.tracker.frames == 0
    assert not guard.attempt_path.exists()
    # A live camera can recover with new images after the bounded replay outage.
    for n in range(20):
        result = tick(guard, clock, 1000 + n)
        if result['terminal']:
            break
    assert result['outcome'] == 'paused' and printer.pauses == 1


def test_backward_source_time_is_blind_even_for_distinct_images_and_survives_reset(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    until_candidate(guard, clock)
    backward = clock.now - 1.5  # only 0.5 seconds before the last accepted source timestamp
    for n in range(5):
        clock.sleep(2)
        result = guard.observe(Frame(clock.now, f'backward-source-{n}'.encode(), backward), score())
        assert result['outcome'] == 'blind' and 'Last-Modified' in result['reason']
        assert result['confirmations'] == 0 and guard.tracker.frames == 0 and printer.pauses == 0
    assert not guard.attempt_path.exists()
    for n in range(20):
        result = tick(guard, clock, 2000 + n)
        if result['terminal']:
            break
    assert result['outcome'] == 'paused' and printer.pauses == 1


def test_equal_source_timestamps_allow_distinct_images(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    modified = clock.now - 1
    for n in range(12):
        clock.sleep(2)
        result = guard.observe(Frame(clock.now, f'equal-source-{n}'.encode(), modified), score())
        if result['terminal']:
            break
    assert result['outcome'] == 'paused' and printer.pauses == 1


def test_blind_reset_and_elapsed_time_do_not_forget_cached_images(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    until_candidate(guard, clock)
    accepted_time = guard.last_frame_ts
    assert guard.observe(None, None)['outcome'] == 'blind'
    clock.sleep(31)
    for n in range(40):
        clock.sleep(2)
        # Even rewritten HTTP timestamps cannot turn two known images into new evidence.
        result = guard.observe(Frame(clock.now, f'frame-{n % 2}'.encode(), clock.now - 1), score())
        assert result['outcome'] == 'blind' and not result['terminal']
        assert guard.last_frame_ts == accepted_time and printer.pauses == 0
    assert not guard.attempt_path.exists() and guard.tracker.frames == 0


def test_recent_image_cache_is_bounded_by_unique_insertions_not_repeat_access(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    for n in range(64):
        assert tick(guard, clock, n, score(p=0, q=0))['outcome'] == 'observing'
    assert tick(guard, clock, 0, score(p=0, q=0))['outcome'] == 'waiting'
    assert tick(guard, clock, 64, score(p=0, q=0))['outcome'] == 'observing'
    assert tick(guard, clock, 1, score(p=0, q=0))['outcome'] == 'waiting'
    # Repeated reads did not refresh image 0's FIFO position; 64 newer unique images evicted it.
    assert tick(guard, clock, 0, score(p=0, q=0))['outcome'] == 'observing'
    assert len(guard.recent_digests) == 64 and printer.pauses == 0


def test_duplicate_source_timestamp_cannot_poison_next_distinct_image(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    accepted_source = guard.latest_source_modified
    clock.sleep(2)
    rewritten_source = clock.now
    duplicate = guard.observe(Frame(clock.now, f'frame-{n}'.encode(), rewritten_source), score())
    assert duplicate['outcome'] == 'waiting' and duplicate['confirmations'] == 1
    clock.sleep(2)
    # The proxy rewrote an old JPEG's header. This new image is newer than the last
    # accepted image, but 0.5 seconds older than that duplicate's untrusted rewrite.
    source = rewritten_source - 0.5
    assert accepted_source is not None and source > accepted_source
    result = guard.observe(Frame(clock.now, b'genuine-after-rewritten-duplicate', source), score())
    assert result['outcome'] == 'paused' and printer.pauses == 1
    assert guard.latest_source_modified == source


def test_moonraker_half_read_reply_is_a_comm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    from printer_ops import moonraker
    from printer_ops.moonraker import MoonrakerAdapter

    def half_read(*_a: Any, **_k: Any) -> Any:
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(moonraker, "http_json", half_read)
    adapter = MoonrakerAdapter("u1", "127.0.0.1", 1)
    with pytest.raises(PrinterCommError):
        adapter.pause()
    with pytest.raises(PrinterCommError):
        adapter.status()


def test_readback_failure_does_not_hide_the_actuator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    calls = {"n": 0}
    original_status = printer.status

    def refuse() -> None:
        printer.pauses += 1
        raise PrinterRefused("firmware rejected pause", command="printer guard")

    def status() -> Any:
        if printer.pauses:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PrinterCommError("readback timed out", command="printer guard")
        return original_status()

    monkeypatch.setattr(printer, "pause", refuse)
    monkeypatch.setattr(printer, "status", status)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "pause_unverified"
    assert "rejected pause" in result["error"]
    assert "readback timed out" in result["readback_error"]


def test_guard_honours_configured_dark_luma(tmp_path: Path) -> None:
    from dataclasses import replace

    from printer_ops.vision import MonitorLimits

    printer, clock = ScriptedPrinter(), Clock()
    guard = policy.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state", evidence_dir=tmp_path / "e",
                                  clock=clock, sleep=clock.sleep, limits=replace(MonitorLimits(), dark_luma=8.0))
    result = tick(guard, clock, 1, score(luma=15.0))
    assert result["outcome"] == "observing"
