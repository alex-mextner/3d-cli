"""Job-bound camera protection: offline scripted adapters and a loopback Moonraker only."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from printer_fakes import FakeMoonraker, u1_status

from printer_ops.base import Adapter, JobRecord, PrinterCommError, PrinterRefused, PrinterStatus
from printer_ops.moonraker import MoonrakerAdapter, parse_status
from printer_ops.vision import Box, Frame, FrameScore

JOB = "00006D"
FILE = "panel-0123456789abcdef.gcode"
START = 1_792_600_000.0


class Clock:
    def __init__(self) -> None:
        self.now = START + 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedPrinter(Adapter):
    kind = "moonraker"

    def __init__(self) -> None:
        super().__init__("u1", "127.0.0.1", 1)
        self.raw = u1_status("printing", FILE)
        self.raw["print_stats"]["total_duration"] = 100.0
        self.record = JobRecord(FILE, "in_progress", START, 80.0, 1000.0, JOB)
        self.pauses = 0
        self.pause_error = False
        self.pause_changes_state = True
        self.history_before_switch: int | None = None
        self.unreachable = False

    def status(self) -> PrinterStatus:
        if self.unreachable:
            raise PrinterCommError("offline", command="printer guard")
        return parse_status(self.raw)

    def history(self, limit: int) -> list[JobRecord]:
        if self.history_before_switch is not None:
            if self.history_before_switch == 0:
                self.record = JobRecord(FILE, "in_progress", START + 1.0, 0.0, 0.0, "00006E")
            self.history_before_switch -= 1
        return [self.record]

    def pause(self) -> None:
        self.pauses += 1
        if self.pause_changes_state:
            self.raw["print_stats"]["state"] = "paused"
        if self.pause_error:
            raise PrinterCommError("pause response timed out", command="printer guard")


def arm(tmp_path: Path, *, dry_run: bool = False) -> tuple[Any, ScriptedPrinter, Clock]:
    # A missing implementation is an explicit failing assertion, not a collection/import error.
    assert importlib.util.find_spec("printer_ops.guard") is not None, "no automatic pause guard exists"
    module = importlib.import_module("printer_ops.guard")
    printer, clock = ScriptedPrinter(), Clock()
    guard = module.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state",
                                  evidence_dir=tmp_path / "evidence", clock=clock,
                                  sleep=clock.sleep, dry_run=dry_run)
    return guard, printer, clock


def score(*, q: float | None = 2.168, p: float = 3.31, luma: float = 128.0) -> FrameScore:
    # The October 21 incident's confirmed score, with a verified region as in the live event.
    return FrameScore(p, [Box(0.1, 0.1, 0.9, 0.9, p)], luma, [0.9], q)


def tick(guard: Any, clock: Clock, n: int, value: FrameScore | None = None,
         *, modified: float | None = None, jpeg: bytes | None = None) -> dict[str, Any]:
    clock.now += 5.0
    frame = Frame(clock.now, jpeg if jpeg is not None else f"frame-{n}".encode(),
                  clock.now - 1 if modified is None else modified)
    return guard.observe(frame, value or score())


def until_candidate(guard: Any, clock: Clock) -> int:
    for n in range(40):
        result = tick(guard, clock, n)
        if result.get("confirmations") == 1:
            return n
        assert not result.get("terminal"), result
    pytest.fail("the incident's verified failure never became a pause candidate")


def test_verified_failure_pauses_exact_job_once_and_records_readback(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "paused" and result["terminal"] is True
    assert printer.pauses == 1
    assert guard.observe(None, None)["outcome"] == "paused"
    assert printer.pauses == 1
    saved = json.loads(guard.attempt_path.read_text())
    assert saved["job_id"] == JOB and saved["filename"] == FILE
    assert saved["outcome"] == "paused" and saved["pause_acknowledged"] is True
    assert Path(saved["frame"]).read_bytes() == f"frame-{n + 1}".encode()


@pytest.mark.parametrize("kind", ["duplicate", "stale", "previous-job", "dark", "unverified", "vetoed", "nan"])
def test_bad_camera_evidence_cannot_pause(tmp_path: Path, kind: str) -> None:
    guard, printer, clock = arm(tmp_path)
    for n in range(40):
        kwargs: dict[str, Any] = {}
        value = score()
        if kind == "duplicate":
            kwargs["jpeg"] = b"frozen frame"
        elif kind == "stale":
            kwargs["modified"] = START + 1
        elif kind == "previous-job":
            kwargs["modified"] = START - 1
        elif kind == "dark":
            value = score(luma=1)
        elif kind == "unverified":
            value = score(q=None)
        elif kind == "vetoed":
            value = score(p=100)
        else:
            value = score(q=float("nan"))
        result = tick(guard, clock, n, value, **kwargs)
        assert result["outcome"] != "paused"
    assert printer.pauses == 0
    assert not guard.attempt_path.exists()


@pytest.mark.parametrize("state", ["standby", "cancelled", "complete", "paused", "error"])
def test_no_action_after_job_leaves_printing(tmp_path: Path, state: str) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    printer.raw["print_stats"]["state"] = state
    result = tick(guard, clock, n + 1)
    assert result["terminal"] is True and result["outcome"] == "disarmed"
    assert printer.pauses == 0


def test_same_filename_with_another_native_job_id_is_not_authorized(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    printer.record = JobRecord(FILE, "in_progress", START + 50, 0.0, 0.0, "00006E")
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "disarmed" and printer.pauses == 0


def test_job_is_reread_immediately_before_pause(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    printer.history_before_switch = 1  # first read still matches; pre-write read sees replacement
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "disarmed" and printer.pauses == 0


def test_lost_telemetry_resets_confirmation_without_actuating(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    printer.unreachable = True
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "blind" and result["confirmations"] == 0
    assert printer.pauses == 0


@pytest.mark.parametrize("ack_error", [False, True])
def test_unverified_pause_is_not_retried_even_after_restart(tmp_path: Path, ack_error: bool) -> None:
    guard, printer, clock = arm(tmp_path)
    printer.pause_changes_state = False
    printer.pause_error = ack_error
    n = until_candidate(guard, clock)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "pause_unverified" and result["terminal"] is True
    assert printer.pauses == 1
    guard.observe(None, None)
    assert printer.pauses == 1
    module = importlib.import_module("printer_ops.guard")
    with pytest.raises(PrinterRefused, match="already attempted"):
        module.PauseGuard.arm(printer, JOB, state_dir=tmp_path / "state",
                              evidence_dir=tmp_path / "evidence", clock=clock, sleep=clock.sleep)
    assert printer.pauses == 1


def test_timeout_can_be_verified_by_same_job_paused_readback(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    printer.pause_error = True
    n = until_candidate(guard, clock)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "paused" and result["pause_acknowledged"] is False
    assert printer.pauses == 1


def test_dry_run_reports_would_pause_without_writes_or_claim(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path, dry_run=True)
    n = until_candidate(guard, clock)
    result = tick(guard, clock, n + 1)
    assert result["outcome"] == "would_pause" and printer.pauses == 0
    assert not guard.attempt_path.exists()


def test_healthy_frames_clear_old_failure_trend_before_a_new_episode(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    n = until_candidate(guard, clock)
    healthy = FrameScore(0.0, q=0.0)
    for offset in range(1, 41):
        tick(guard, clock, n + offset, healthy)
    tick(guard, clock, n + 41)
    tick(guard, clock, n + 42)
    assert printer.pauses == 0, "old critical trend survived forty healthy frames"


def test_outage_discards_old_critical_trend_before_weak_returning_frames(tmp_path: Path) -> None:
    guard, printer, clock = arm(tmp_path)
    for n in range(80):
        tick(guard, clock, n, FrameScore(0.0, q=0.0))
    assert tick(guard, clock, 81, score(q=8, p=8))["confirmations"] == 1
    clock.now += 600.0
    for n in range(82, 85):
        tick(guard, clock, n, score(q=0.4, p=0.5))
    assert printer.pauses == 0, "the outage retained an old critical trend"


def test_moonraker_pause_uses_native_endpoint_and_changes_no_settings() -> None:
    with FakeMoonraker(status=u1_status("printing", FILE)).serve() as fake:
        adapter = MoonrakerAdapter("u1", "127.0.0.1", fake.port)
        assert hasattr(adapter, "pause"), "the Moonraker adapter has no pause actuator"
        adapter.pause()
        assert adapter.status().state == "paused"
    assert [r.path for r in fake.writes()] == ["/printer/print/pause"]


@pytest.mark.parametrize("interval,jitter,refused", [(10.0, 0.0, False), (30.0, 0.001, False), (10.0, 0.0, True)])
def test_guard_live_loop_pauses_without_notifications_or_agent_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    interval: float, jitter: float, refused: bool,
) -> None:
    import printer_ops
    import vision_detect
    from printer_ops import devices
    from printer_ops.watch_sinks import Dispatcher

    printer, clock = ScriptedPrinter(), Clock()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(devices, "get_device", lambda _name: SimpleNamespace(
        name="u1", adapter="moonraker", access="full", watch={}, host="127.0.0.1", port=1))
    monkeypatch.setattr(printer_ops, "make_adapter", lambda _device: printer)
    monkeypatch.setattr(vision_detect.time, "time", clock)
    monkeypatch.setattr(vision_detect.time, "sleep", clock.sleep)
    monkeypatch.setattr(vision_detect, "_calibration", lambda _device, _args: None)
    monkeypatch.setattr(vision_detect, "_frame_score_errors", lambda: ())

    def refuse_pause() -> None:
        printer.pauses += 1
        raise PrinterRefused("firmware rejected pause", command="printer guard")

    if refused:
        monkeypatch.setattr(printer, "pause", refuse_pause)

    class Camera:
        calls = 0

        def keepalive(self, _now: float) -> bool:
            return False

        def grab(self) -> Frame:
            self.calls += 1
            assert self.calls < 50, "supported capture interval never confirms the failure"
            clock.sleep(self.calls * jitter)
            return Frame(clock.now, f"frame-{clock.now}".encode(), clock.now - 1)

    class Scorer:
        geometry = None
        verifier = object()

        def __call__(self, _jpeg: bytes) -> FrameScore:
            return score()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("guard must not depend on subscribers, Telegram or agent delivery")

    monkeypatch.setattr(vision_detect, "_device_camera", lambda _device, _args: Camera())
    monkeypatch.setattr(vision_detect, "build_scorer", lambda *_args, **_kwargs: Scorer())
    monkeypatch.setattr(Dispatcher, "for_devices", forbidden)
    assert vision_detect.main(["guard", "u1", "--job-id", JOB, "--yes", "--interval", str(interval)]) == int(refused)
    assert printer.pauses == 1
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expected = "pause_unverified" if refused else "paused"
    assert reports[0]["outcome"] == "armed" and reports[-1]["outcome"] == expected
    state = json.loads((tmp_path / "state/3d-cli/guard/u1.status.json").read_text())
    assert state["outcome"] == expected and state["job_id"] == JOB
    assert state["terminal"] is True
    claim = json.loads((tmp_path / f"state/3d-cli/guard/u1-{JOB}.json").read_text())
    assert claim["outcome"] == expected

