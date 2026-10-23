"""Pause without an agent (#529): which events qualify, who may be paused, the once-per-job claim, outcomes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fakes import FakeSpoolman

from errors import InvalidArgument
from printer_ops import auto_pause
from printer_ops.auto_pause import (
    AutoPauser,
    PauseResult,
    qualifying_event,
    resolve_policy,
)
from printer_ops.base import (
    Adapter,
    JobRecord,
    PrinterCommError,
    PrinterRefused,
    PrinterStatus,
)
from printer_ops.devices import Device
from printer_ops.vision import VETO_RATIO
from printer_ops.watch import CRITICAL, INFO, WARNING, Event
from printops import calibrations as cal
from printops import history_buffer as hb
from printops import policy
from printops import settings_history as sh
from printops.calibration_catalog import load_catalog
from printops.fleet import Fleet, FleetPrinter, load_fleet
from printops.spoolman import ensure_extra_fields

JOB = "00006D"
FILE = "panel-0123456789abcdef.gcode"
START = 1_792_600_000.0
NOW = START + 100.0
CAMERA_WATCH = {"vision": {"camera": {"kind": "mjpg-streamer", "port": 8081}}}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakePrinter(Adapter):
    """Records pause(); any other action is a test failure (the watcher may only pause)."""

    kind = "moonraker"

    def __init__(self) -> None:
        super().__init__("u1", "192.168.1.50", 7125)
        self.state = "printing"
        self.filename = FILE
        self.record: JobRecord | None = JobRecord(
            FILE, "in_progress", START, 80.0, 1000.0, JOB
        )
        self.pauses = 0
        self.pause_error: Exception | None = None
        self.pause_takes_effect = True
        self.status_error: Exception | None = None

    def status(self) -> PrinterStatus:
        if self.status_error is not None:
            raise self.status_error
        return PrinterStatus(self.state, self.state, filename=self.filename)

    def history(self, limit: int) -> list[JobRecord]:
        return [self.record] if self.record else []

    def pause(self) -> None:
        self.pauses += 1
        if self.pause_takes_effect:
            self.state = "paused"
        if self.pause_error is not None:
            raise self.pause_error

    def cancel(self) -> None:
        pytest.fail("the watcher must never cancel")

    def start(self, remote_name: str) -> None:
        pytest.fail("the watcher must never start")

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        pytest.fail("the watcher must never run G-code")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def spaghetti(
    *,
    ts: float = START + 50,
    severity: str = CRITICAL,
    p: float = 3.0,
    q: float | None = 2.0,
    boxes: list[dict[str, Any]] | None = None,
    **data: Any,
) -> Event:
    payload = {
        "p": p,
        "q": q,
        "boxes": [{"verified": 0.9}] if boxes is None else boxes,
        **data,
    }
    return Event(
        "u1",
        "vision_spaghetti",
        severity,
        "print looks like spaghetti",
        payload,
        key="u1:vision_spaghetti",
        ts=ts,
    )


def detached(*, ts: float = START + 50, severity: str = CRITICAL, **data: Any) -> Event:
    return Event(
        "u1",
        "vision_part_detached",
        severity,
        "a printed part came off the bed",
        data,
        key="u1:vision_part_detached",
        ts=ts,
    )


@pytest.fixture
def printer() -> FakePrinter:
    return FakePrinter()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def claims(env: Path) -> Path:
    return env / "claims"


def pauser(printer: FakePrinter, clock: Clock, claims: Path, **kw: Any) -> AutoPauser:
    return AutoPauser(
        "u1",
        adapter=lambda: printer,
        claim_dir=claims,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


# -- qualifying events ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event", [spaghetti(), detached(), detached(confidence="high")]
)
def test_qualifying_kinds_fire(event: Event) -> None:
    assert qualifying_event([event], now=NOW) is event


@pytest.mark.parametrize(
    "event",
    [
        spaghetti(severity=WARNING),
        spaghetti(severity=INFO),
        spaghetti(q=None),
        spaghetti(q=float("nan")),
        spaghetti(q=3.0 * VETO_RATIO - 0.01),
        spaghetti(boxes=[]),
        spaghetti(boxes=[{"verified": 0.49}, {"verified": None}]),
        spaghetti(boxes=[{"p": 0.9}]),
        detached(severity=WARNING),
        detached(confidence="low"),
        Event(
            "u1",
            "printer_noodle_verdict",
            CRITICAL,
            "firmware says noodle",
            {},
            ts=START + 50,
        ),
        Event("u1", "vision_camera_blind", CRITICAL, "camera dark", {}, ts=START + 50),
        Event("u1", "exception", CRITICAL, "printer error", {}, ts=START + 50),
        Event("u1", "filament_runout", CRITICAL, "runout", {}, ts=START + 50),
    ],
)
def test_non_qualifying_events_never_fire(event: Event) -> None:
    assert qualifying_event([event], now=NOW) is None


def test_exactly_the_veto_ratio_qualifies() -> None:
    assert qualifying_event([spaghetti(p=3.0, q=3.0 * VETO_RATIO)], now=NOW) is not None


def test_old_events_do_not_qualify_and_newest_wins() -> None:
    old, new = spaghetti(ts=NOW - 700), spaghetti(ts=NOW - 10)
    assert qualifying_event([old], now=NOW) is None
    assert qualifying_event([old], now=NOW, max_age_s=800) is old
    assert qualifying_event([old, new, spaghetti(ts=NOW - 20)], now=NOW) is new
    newest = qualifying_event([detached(ts=NOW - 5), new], now=NOW)
    assert newest is not None and newest.kind == "vision_part_detached"


def test_qualifying_kinds_are_one_table() -> None:
    assert sorted(auto_pause.QUALIFYING) == ["vision_part_detached", "vision_spaghetti"]


# -- the pause ---------------------------------------------------------------------------------


def test_no_qualifying_event_reports_nothing(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    assert pauser(printer, clock, claims).pause([spaghetti(severity=WARNING)]) is None
    assert printer.pauses == 0 and not claims.exists()


def test_pauses_a_printing_job_and_claims_it(
    printer: FakePrinter, clock: Clock, claims: Path, env: Path
) -> None:
    frame = env / "frame.jpg"
    frame.write_bytes(b"jpeg")
    event = spaghetti(frame=str(frame))
    res = pauser(printer, clock, claims).pause([event])
    assert res is not None and res.outcome == "paused"
    assert (res.job_id, res.filename, res.frame, res.event) == (
        JOB,
        FILE,
        str(frame),
        event,
    )
    assert printer.pauses == 1 and printer.state == "paused"
    claim = json.loads((claims / f"u1-{JOB}.json").read_text())
    assert (
        claim["outcome"] == "paused"
        and claim["terminal"] is True
        and claim["action"] == "pause"
    )
    assert (
        claim["job_id"] == JOB
        and claim["source"] == "watch_auto_pause"
        and claim["start_time"] == START
    )


def test_detached_part_pauses_too(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    res = pauser(printer, clock, claims).pause([detached()])
    assert res is not None and res.outcome == "paused" and printer.pauses == 1


def test_frame_that_does_not_exist_is_dropped(
    printer: FakePrinter, clock: Clock, claims: Path, env: Path
) -> None:
    res = pauser(printer, clock, claims).pause([spaghetti(frame=str(env / "gone.jpg"))])
    assert res is not None and res.frame is None


@pytest.mark.parametrize(
    "state", ["paused", "standby", "error", "complete", "cancelled"]
)
def test_only_while_printing(
    printer: FakePrinter, clock: Clock, claims: Path, state: str
) -> None:
    printer.state = state
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None and res.outcome == "skipped" and state in res.detail
    assert printer.pauses == 0 and not claims.exists()


def test_unreadable_status_is_failed_not_skipped(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    printer.status_error = PrinterCommError("offline", command="printer")
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None and res.outcome == "failed" and "offline" in res.detail
    assert printer.pauses == 0


@pytest.mark.parametrize(
    "record",
    [
        None,
        JobRecord(FILE, "completed", START, 80.0, 1000.0, JOB),
        JobRecord("other.gcode", "in_progress", START, 80.0, 1000.0, JOB),
        JobRecord(FILE, "in_progress", None, 80.0, 1000.0, JOB),
        JobRecord(FILE, "in_progress", START, 80.0, 1000.0, None),
        JobRecord(FILE, "in_progress", START, 80.0, 1000.0, "../escape"),
    ],
)
def test_unbound_job_is_skipped(
    printer: FakePrinter, clock: Clock, claims: Path, record: JobRecord | None
) -> None:
    printer.record = record
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None and res.outcome == "skipped" and "bind" in res.detail
    assert printer.pauses == 0 and not claims.exists()


def test_alert_older_than_the_job_never_pauses_it(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    res = pauser(printer, clock, claims).pause([spaghetti(ts=START - 1)])
    assert (
        res is not None
        and res.outcome == "skipped"
        and "older than the current job" in res.detail
    )
    assert printer.pauses == 0 and not claims.exists()


def test_alert_older_than_max_age_is_not_even_reported(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    assert (
        pauser(printer, clock, claims, max_event_age_s=30).pause(
            [spaghetti(ts=NOW - 31)]
        )
        is None
    )
    assert printer.pauses == 0


def test_once_per_job(printer: FakePrinter, clock: Clock, claims: Path) -> None:
    p = pauser(printer, clock, claims)
    first = p.pause([spaghetti()])
    assert first is not None and first.outcome == "paused"
    printer.state = "printing"  # resumed by a person: still the same job
    again = p.pause([spaghetti()])
    assert (
        again is not None
        and again.outcome == "skipped"
        and "already attempted" in again.detail
    )
    assert printer.pauses == 1


def test_a_vision_guard_claim_blocks_the_watcher(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    claims.mkdir(parents=True)
    (claims / f"u1-{JOB}.json").write_text('{"outcome": "paused"}\n')
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert (
        res is not None
        and res.outcome == "skipped"
        and "already attempted" in res.detail
    )
    assert printer.pauses == 0


def test_the_watcher_claim_blocks_vision_guard(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    from printer_ops.guard import PauseGuard

    pauser(printer, clock, claims).pause([spaghetti()])
    printer.state = "printing"
    with pytest.raises(PrinterRefused, match="already attempted"):
        PauseGuard.arm(
            printer,
            JOB,
            state_dir=claims,
            evidence_dir=claims / "ev",
            clock=clock,
            sleep=clock.sleep,
        )


def test_a_new_job_is_a_new_claim(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    p = pauser(printer, clock, claims)
    p.pause([spaghetti()])
    printer.state = "printing"
    printer.record = JobRecord(FILE, "in_progress", START + 10, 0.0, 0.0, "00006E")
    res = p.pause([spaghetti(ts=START + 60)])
    assert res is not None and res.outcome == "paused" and res.job_id == "00006E"
    assert printer.pauses == 2


def test_refused_pause_is_failed_and_never_retried(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    printer.pause_takes_effect = False
    printer.pause_error = PrinterRefused("klipper is busy", command="printer pause")
    p = pauser(printer, clock, claims)
    res = p.pause([spaghetti()])
    assert (
        res is not None and res.outcome == "failed" and "klipper is busy" in res.detail
    )
    assert json.loads((claims / f"u1-{JOB}.json").read_text())["outcome"] == "failed"
    again = p.pause([spaghetti()])
    assert again is not None and again.outcome == "skipped"
    assert printer.pauses == 1


def test_lost_response_that_did_pause_is_reported_paused(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    printer.pause_error = PrinterCommError(
        "response timed out", command="printer pause"
    )
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None and res.outcome == "paused" and "timed out" in res.detail
    assert printer.pauses == 1


def test_unexpected_adapter_error_is_failed_not_raised(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    printer.pause_takes_effect = False
    printer.pause_error = RuntimeError("x" * 500)
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None and res.outcome == "failed" and len(res.detail) < 400


def test_acknowledged_but_never_paused_is_unverified(
    printer: FakePrinter, clock: Clock, claims: Path
) -> None:
    printer.pause_takes_effect = False
    res = pauser(printer, clock, claims, verify_s=3.0).pause([spaghetti()])
    assert res is not None and res.outcome == "pause_unverified"
    assert clock.now == NOW + 3.0  # polled every 0.5 s until verify_s
    assert printer.pauses == 1
    assert (
        json.loads((claims / f"u1-{JOB}.json").read_text())["outcome"]
        == "pause_unverified"
    )


def test_claim_dir_defaults_to_the_guard_state_dir(
    printer: FakePrinter, clock: Clock, env: Path
) -> None:
    p = AutoPauser("u1", adapter=lambda: printer, clock=clock, sleep=clock.sleep)
    assert p.pause([spaghetti()]) is not None
    assert (env / "state" / "3d-cli" / "guard" / f"u1-{JOB}.json").is_file()


def test_unwritable_claim_dir_sends_nothing(
    printer: FakePrinter, clock: Clock, env: Path
) -> None:
    blocker = env / "blocker"
    blocker.write_text("not a dir")
    res = pauser(printer, clock, blocker / "guard").pause([spaghetti()])
    assert (
        res is not None and res.outcome == "failed" and "nothing was sent" in res.detail
    )
    assert printer.pauses == 0


# -- result rendering --------------------------------------------------------------------------


def test_to_event_and_message(printer: FakePrinter, clock: Clock, claims: Path) -> None:
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None
    e = res.to_event()
    assert (e.kind, e.severity, e.printer, e.key) == (
        "auto_pause",
        WARNING,
        "u1",
        f"u1:auto_pause:{JOB}",
    )
    assert (
        e.data["outcome"] == "paused" and e.data["trigger_kind"] == "vision_spaghetti"
    )
    assert (
        e.data["trigger_key"] == "u1:vision_spaghetti"
        and e.data["job_id"] == JOB
        and "\n" not in e.text
    )
    msg = res.message()
    assert "print looks like spaghetti" in msg and "http://192.168.1.50" in msg and ":7125" not in msg
    assert "Resume" in msg and "never resumes or cancels" in msg


@pytest.mark.parametrize(
    ("outcome", "severity"),
    [
        ("paused", WARNING),
        ("skipped", WARNING),
        ("failed", CRITICAL),
        ("pause_unverified", CRITICAL),
    ],
)
def test_severity_by_outcome(outcome: str, severity: str) -> None:
    res = PauseResult("u1", outcome, "why", None, None, spaghetti(), None)
    e = res.to_event()
    assert e.severity == severity and e.key == "u1:auto_pause:none"


def test_message_for_failure_does_not_promise_a_pause() -> None:
    msg = PauseResult(
        "u1", "failed", "klipper is busy", JOB, FILE, spaghetti(), None
    ).message()
    assert (
        "could NOT pause" in msg and "klipper is busy" in msg and "To resume" not in msg
    )


# -- the settings journal ----------------------------------------------------------------------


def journal_lines(env: Path) -> list[hb.Line]:
    path = hb.buffer_dir() / "u1.jsonl"
    return hb.parse_lines(path.read_text(encoding="utf-8").splitlines())


def test_journal_line_parses(
    printer: FakePrinter, clock: Clock, claims: Path, env: Path
) -> None:
    frame = env / "f.jpg"
    frame.write_bytes(b"x")
    pauser(printer, clock, claims).pause([spaghetti(frame=str(frame))])
    (line,) = journal_lines(env)
    d = line.data
    assert d["schema"] == "printops.settings_change/1" and d["printer"] == "u1"
    assert (d["kind"], d["key"], d["status"]) == ("other", "print.pause", "observed")
    assert d["old"] == {"state": "printing"} and d["new"] == {"state": "paused"}
    assert (
        d["source"] == "watch_auto_pause"
        and d["actor"] == "3d-printer-watch (auto pause without agent)"
    )
    assert d["at"].endswith("Z") and d["recorded_at"] == d["at"]
    assert d["evidence"] == [str(frame), str(claims / f"u1-{JOB}.json"), JOB]


def test_unpaused_outcome_is_journaled_as_not_applied(
    printer: FakePrinter, clock: Clock, claims: Path, env: Path
) -> None:
    printer.pause_takes_effect = False
    pauser(printer, clock, claims).pause([spaghetti()])
    (line,) = journal_lines(env)
    assert line.data["status"] == "measured_not_applied" and line.data["new"] == {
        "state": "printing"
    }


def test_skipped_pause_writes_no_journal(
    printer: FakePrinter, clock: Clock, claims: Path, env: Path
) -> None:
    printer.state = "paused"
    pauser(printer, clock, claims).pause([spaghetti()])
    assert not (hb.buffer_dir() / "u1.jsonl").exists()


def test_failing_journal_write_does_not_change_the_outcome(
    printer: FakePrinter,
    clock: Clock,
    claims: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(printer: str, record: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(hb, "append", boom)
    res = pauser(printer, clock, claims).pause([spaghetti()])
    assert res is not None and res.outcome == "paused" and "journal" in res.detail
    assert capsys.readouterr().err.startswith("auto-pause:")
    assert json.loads((claims / f"u1-{JOB}.json").read_text())["outcome"] == "paused"


@pytest.fixture
def import_ctx(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_db: sqlite3.Connection,
    fake_spoolman: FakeSpoolman,
) -> cal.Ctx:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)
    ensure_extra_fields(fake_spoolman)
    fleet = load_fleet()
    assert fleet is not None and "u1" in fleet.printers
    return cal.Ctx(
        conn=printops_db,
        spoolman=fake_spoolman,
        actor="importer",
        catalog=load_catalog(),
        policy=policy.section("calibrations"),
        fleet=fleet,
    )


def test_importer_accepts_the_line_as_journal_only(
    printer: FakePrinter, clock: Clock, claims: Path, env: Path, import_ctx: cal.Ctx
) -> None:
    pauser(printer, clock, claims).pause([spaghetti()])
    (line,) = journal_lines(env)
    report = sh.import_lines(import_ctx, [line.raw], dry_run=True)
    (result,) = report.results
    assert (result.outcome, result.detail) == ("imported", "journal only")


# -- policy ------------------------------------------------------------------------------------


def device(**kw: Any) -> Device:
    base: dict[str, Any] = {
        "name": "u1",
        "adapter": "moonraker",
        "host": "10.0.0.5",
        "port": 7125,
        "model": None,
        "source": Path("printer-devices.yaml"),
        "watch": CAMERA_WATCH,
        "access": "full",
    }
    return Device(**{**base, **kw})


def fleet_of(owner: str = "own", access: str = "full", name: str = "u1") -> Fleet:
    entry = FleetPrinter(
        name,
        "Snapmaker U1",
        owner,
        access,
        (),
        None,
        None,
        None,
        None,
        None,
        (),
        {},
        Path("printers.yaml"),
    )
    return Fleet(Path("printers.yaml"), {name: entry})


def test_u1_like_printer_defaults_to_critical_only() -> None:
    assert resolve_policy(device(), None, fleet=fleet_of()) == "critical-only"


@pytest.mark.parametrize(
    ("dev", "fleet"),
    [
        (device(access="offline"), fleet_of(access="offline")),
        (device(access="read-only"), fleet_of(access="read-only")),
        (device(), fleet_of(access="read-only")),
        (device(access="read-only"), fleet_of()),
        (device(), fleet_of(owner="neighbour")),
        (device(), fleet_of(owner="boris")),
        (device(), fleet_of(owner="hackerspace")),
        (device(watch={}), fleet_of()),
        (device(adapter="creality-stock"), fleet_of()),
        (device(), fleet_of(name="other")),
        (device(), Fleet(Path("printers.yaml"), {})),
    ],
)
def test_default_is_off_unless_every_condition_holds(dev: Device, fleet: Fleet) -> None:
    assert resolve_policy(dev, None, fleet=fleet) == "off"


def test_default_without_a_fleet_registry_is_off(
    monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    monkeypatch.setenv("THREED_FLEET", str(env / "no-such-fleet.yaml"))
    monkeypatch.setattr("printops.fleet.load_fleet", lambda *a, **k: None)
    assert resolve_policy(device(), None) == "off"


@pytest.mark.parametrize(
    ("dev", "fleet"),
    [
        (device(access="offline"), fleet_of(access="offline")),
        (device(access="read-only"), fleet_of()),
        (device(), fleet_of(access="read-only")),
        (device(), fleet_of(owner="neighbour")),
        (device(), fleet_of(owner="boris")),
        (device(), fleet_of(owner="hackerspace")),
        (device(adapter="creality-stock"), fleet_of()),
    ],
)
def test_explicit_critical_only_on_somebody_elses_or_limited_printer_is_loud(
    dev: Device, fleet: Fleet
) -> None:
    with pytest.raises(InvalidArgument) as err:
        resolve_policy(dev, "critical-only", fleet=fleet)
    assert err.value.exit_code == 2 and "pause_without_agent" in err.value.flag


def test_explicit_critical_only_is_honoured_when_allowed() -> None:
    assert (
        resolve_policy(device(watch={}), "critical-only", fleet=fleet_of())
        == "critical-only"
    )
    assert (
        resolve_policy(
            device(), "critical-only", fleet=Fleet(Path("printers.yaml"), {})
        )
        == "critical-only"
    )


def test_explicit_off_wins() -> None:
    assert resolve_policy(device(), "off", fleet=fleet_of()) == "off"
    assert (
        resolve_policy(
            device(access="offline"), "off", fleet=fleet_of(access="offline")
        )
        == "off"
    )


@pytest.mark.parametrize("value", ["on", "always", "", True, False, 1, ["off"]])
def test_other_values_are_refused_listing_the_policies(value: object) -> None:
    with pytest.raises(InvalidArgument) as err:
        resolve_policy(device(), value, fleet=fleet_of())
    assert err.value.accepted == list(auto_pause.POLICIES)
