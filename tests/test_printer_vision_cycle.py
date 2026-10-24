"""printer_ops/vision_cycle.py — one poll cycle of `3d printer vision` (#639, #640, #641, #642).

Every collaborator is a fake with a fake clock: no printer, no camera, no model, no sleep, no real `tg`.
What is proved here: a job is supervised whatever started it; a printer that is off is not an alarm (one
log line out, one back, none while the intentional-off marker is set, <= 30 s polling, no camera work, the
marker cleared on the first answer); a power cycle / a new print of the same file restarts the per-print
state (including the detached-part baseline) while a network blip keeps it; the status file carries the
liveness keys; a verified critical event reaches the real Dispatcher -> AutoPauser -> a fake `tg` and pauses
exactly once per job whoever started the job."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from printer_ops import intentional_off
from printer_ops.base import Adapter, JobRecord, PrinterCommError, PrinterStatus
from printer_ops.vision import Box, Frame, FrameScore, VisionMonitor
from printer_ops.vision_cycle import UNREACHABLE_MAX_S, VisionCycle
from printer_ops.vision_status import VisionStatus, read_status

T0 = 1_792_600_000.0
FILE = "panel-0123456789abcdef.gcode"
JPEG = b"\xff\xd8\xff\xe0jpeg"


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePrinter(Adapter):
    """Answers `status()` from the script below. It can only be asked its state: whatever the test
    does to it, the cycle must never start, cancel or send G-code (a print is started by someone else)."""

    kind = "moonraker"

    def __init__(self) -> None:
        super().__init__("u1", "127.0.0.1", 7125)
        self.state = "standby"
        self.filename: str | None = None
        self.progress: float | None = None
        self.down = False
        self.record: JobRecord | None = None
        self.pauses = 0
        self.polls = 0

    def status(self) -> PrinterStatus:
        self.polls += 1
        if self.down:
            raise PrinterCommError("connection refused", command="printer status")
        return PrinterStatus(
            self.state, self.state, filename=self.filename, progress=self.progress
        )

    def history(self, limit: int) -> list[JobRecord]:
        return [self.record] if self.record else []

    def pause(self) -> None:
        self.pauses += 1
        self.state = "paused"

    def start(self, remote_name: str) -> None:
        pytest.fail("the vision cycle must never start a print")

    def cancel(self) -> None:
        pytest.fail("the vision cycle must never cancel a print")

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        pytest.fail("the vision cycle must never run G-code")


class FakeCamera:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.keepalives: list[float] = []
        self.grabs = 0
        self.fail = False
        self.payload: Callable[[], bytes] | None = (
            None  # None: a distinct fake JPEG each grab
        )

    def keepalive(self, now: float | None = None) -> bool:
        self.keepalives.append(self.clock() if now is None else now)
        return False

    def grab(self) -> Frame:
        self.grabs += 1
        if self.fail:
            raise PrinterCommError("no frame", command="printer vision")
        body = self.payload() if self.payload else JPEG + bytes([self.grabs % 250])
        return Frame(self.clock(), body, None)


class Scores:
    """The pixel side: a scripted spaghetti score per frame (0.0 once the script is used up)."""

    def __init__(
        self, values: list[float] | None = None, verified: bool = False
    ) -> None:
        self.values = list(values or [])
        self.verified = verified

    def __call__(self, jpeg: bytes) -> FrameScore:
        p = self.values.pop(0) if self.values else 0.0
        box = [Box(0.1, 0.2, 0.3, 0.4, 0.9)]
        if self.verified:
            return FrameScore(p, box, 90.0, probs=[0.9], q=p * 0.9)
        return FrameScore(p, box, 90.0)


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        interval: float = 10.0,
        score: Callable[[bytes], FrameScore] | None = None,
        dispatcher: Any = None,
        quiet: Callable[[], bool] | None = None,
        reset_print: Callable[[], None] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.clock = clock or Clock()
        self.printer = FakePrinter()
        self.camera = FakeCamera(self.clock)
        self.monitor = VisionMonitor("u1", score or Scores())
        self.logs: list[str] = []
        self.resets = 0
        self.cleared = 0
        self.contexts: list[Any] = []
        self.quiet_flag = False
        self.status_path = tmp_path / "vision" / "u1.json"

        def clear() -> None:
            self.cleared += 1

        def reset() -> None:
            self.resets += 1

        self.cycle = VisionCycle(
            "u1",
            adapter=self.printer,
            camera=self.camera,
            monitor=self.monitor,
            reset_print=reset_print or reset,
            interval_s=interval,
            status=VisionStatus("u1", self.status_path, started=T0),
            dispatcher=dispatcher,
            clock=self.clock,
            quiet=quiet or (lambda: self.quiet_flag),
            clear_quiet=clear,
            log=self.logs.append,
        )

    def tick(self, seconds: float = 10.0) -> Any:
        self.clock.advance(seconds)
        return self.cycle.cycle()

    def doc(self) -> dict[str, Any]:
        doc = read_status("u1", self.status_path)
        assert doc is not None
        return doc

    def print_file(self, progress: float = 0.1) -> None:
        self.printer.state, self.printer.filename, self.printer.progress = (
            "printing",
            FILE,
            progress,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# ---- a job is supervised however it was started ----------------------------------------------------


def test_a_print_nobody_registered_is_supervised_from_its_first_poll(
    tmp_path: Path,
) -> None:
    """The printer goes idle -> printing on its own (touchscreen, Fluidd, Orca, Home Assistant, a
    macro): nothing calls `3d printer start`, and frames, events and the status file appear."""
    rig = Rig(tmp_path, score=Scores([0.0] * 10 + [2.5] * 14))
    for _ in range(3):  # idle: no camera work at all
        rep = rig.tick()
        assert rep.state == "standby" and rep.frame is None and rep.score is None
    assert rig.camera.grabs == 0 and rig.camera.keepalives == []
    idle = rig.doc()
    assert (idle["supervising"], idle["job"], idle["reachable"]) == (False, None, True)

    rig.print_file(0.01)
    rep = rig.tick()
    assert rep.new_print and rig.resets == 1
    assert rep.frame is not None and rep.score is not None
    started = rig.doc()
    assert started["supervising"] is True
    assert started["job"] == {"file": FILE, "started": rig.clock()}
    assert started["frames"] == 1 and started["last_frame_ts"] == rig.clock()

    kinds = [e.kind for _ in range(24) for e in rig.tick().events]
    assert kinds == [
        "vision_spaghetti",
        "vision_spaghetti",
    ]  # the warning, then the critical
    assert rig.printer.pauses == 0  # the cycle itself never touches the printer
    assert rig.camera.grabs == 25 and rig.resets == 1


def test_a_paused_print_keeps_its_job_but_the_camera_rests(rig: Rig) -> None:
    rig.print_file()
    rig.tick()
    rig.printer.state = "paused"
    rep = rig.tick()
    doc = rig.doc()
    assert not rep.new_print and rig.camera.grabs == 1
    assert doc["job"]["file"] == FILE and doc["supervising"] is False
    rig.printer.state = "printing"
    assert not rig.tick().new_print and rig.resets == 1


def test_a_finished_job_clears_the_job_and_the_next_one_is_new(rig: Rig) -> None:
    rig.print_file()
    rig.tick()
    rig.printer.state = "complete"
    rig.tick()
    assert rig.doc()["job"] is None
    rig.print_file(0.0)
    assert rig.tick().new_print and rig.resets == 2


# ---- a printer that is off ----------------------------------------------------------------------------


def test_an_unreachable_printer_is_one_line_out_one_back_and_no_camera_work(
    rig: Rig,
) -> None:
    rig.print_file(0.4)
    rig.tick()
    grabs, last_ok = rig.camera.grabs, rig.doc()["last_poll_ok"]
    rig.printer.down = True
    reports = [rig.tick(30.0) for _ in range(6)]

    assert all(r.error == "connection refused" and not r.reachable for r in reports)
    assert (
        len(rig.logs) == 1
        and "does not answer" in rig.logs[0]
        and "connection refused" in rig.logs[0]
    )
    assert (
        rig.camera.grabs == grabs and rig.camera.keepalives == rig.camera.keepalives[:1]
    )
    assert all(r.events == [] and r.frame is None for r in reports)
    doc = rig.doc()
    assert (doc["reachable"], doc["state"], doc["supervising"], doc["job"]) == (
        False,
        None,
        False,
        None,
    )
    assert doc["unreachable_since"] == T0 + 10 + 30 and doc["last_poll_ok"] == last_ok
    assert doc["interval_s"] == 10.0

    rig.printer.down = False
    rep = rig.tick(30.0)
    assert rep.state == "printing" and rep.reachable
    assert (
        len(rig.logs) == 2 and "answers again" in rig.logs[1] and "180 s" in rig.logs[1]
    )
    back = rig.doc()
    assert (back["reachable"], back["unreachable_since"]) == (True, None)
    assert back["last_poll_ok"] == rig.clock()
    rig.printer.down = True
    rig.tick()
    rig.tick()
    assert len(rig.logs) == 3  # a second outage is announced once more, not per cycle


@pytest.mark.parametrize(
    ("interval", "expected"), [(100.0, UNREACHABLE_MAX_S), (10.0, 10.0)]
)
def test_an_unreachable_printer_is_polled_at_most_every_30_seconds(
    tmp_path: Path, interval: float, expected: float
) -> None:
    rig = Rig(tmp_path, interval=interval)
    rig.printer.down = True
    assert rig.tick().next_delay_s == expected
    rig.printer.down = False
    assert rig.tick().next_delay_s == interval


def test_the_delay_is_the_interval_less_what_the_cycle_took(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.print_file()
    original = rig.camera.grab

    def slow() -> Frame:
        rig.clock.advance(4.0)
        return original()

    rig.camera.grab = slow  # type: ignore[method-assign]
    assert rig.tick().next_delay_s == 6.0
    rig.camera.grab = lambda: rig.clock.advance(30) or original()  # type: ignore[method-assign,func-returns-value]
    assert rig.tick().next_delay_s == 0.5  # never a busy loop


def test_the_intentional_off_marker_silences_both_log_lines_and_nothing_else(
    rig: Rig,
) -> None:
    rig.quiet_flag = True
    rig.printer.down = True
    for _ in range(3):
        rig.tick()
    assert rig.doc()["reachable"] is False and rig.doc()["unreachable_since"] == T0 + 10
    rig.printer.down = False
    rig.tick()
    assert rig.logs == []
    assert rig.cleared == 1  # the first answer ends "switched off on purpose"


def test_the_marker_is_cleared_by_the_first_answer_only_not_by_every_poll(
    rig: Rig,
) -> None:
    for _ in range(4):
        rig.tick()
    assert rig.cleared == 1
    rig.printer.down = True
    rig.tick()
    rig.printer.down = False
    rig.tick()
    rig.tick()
    assert (
        rig.cleared == 2
    )  # once per return, so a marker set while the printer is up survives


def test_the_default_marker_is_the_real_file_and_a_started_printer_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    clock, printer = Clock(), FakePrinter()
    printer.down = True
    logs: list[str] = []
    cycle = VisionCycle(
        "u1",
        adapter=printer,
        camera=FakeCamera(clock),
        monitor=VisionMonitor("u1", Scores()),
        reset_print=lambda: None,
        interval_s=10.0,
        clock=clock,
        log=logs.append,
    )
    intentional_off.set_marker("u1", "switched off at night")
    cycle.cycle()
    assert logs == [] and intentional_off.is_set(
        "u1"
    )  # quiet while the marker is there
    printer.down = False
    cycle.cycle()
    assert not intentional_off.is_set("u1") and logs == []


def test_an_unreachable_printer_first_seen_at_start_is_announced_once(rig: Rig) -> None:
    rig.printer.down = True
    rig.tick()
    rig.tick()
    assert len(rig.logs) == 1 and rig.cleared == 0


def test_a_camera_failure_is_reported_and_does_not_stop_the_cycle(rig: Rig) -> None:
    rig.print_file()
    rig.camera.fail = True
    rep = rig.tick()
    assert rep.camera_error == "no frame" and rep.frame is None
    assert (
        rig.doc()["camera_error"] == "no frame" and rig.doc()["last_frame_ts"] is None
    )
    rig.camera.fail = False
    rig.tick()
    assert rig.doc()["last_frame_ts"] == rig.clock()


# ---- who is a new print -------------------------------------------------------------------------------


def test_a_network_blip_keeps_the_per_print_state_and_a_power_cycle_resets_it(
    rig: Rig,
) -> None:
    rig.print_file(0.5)
    rig.tick()
    rig.monitor.tracker.frames = 50
    assert rig.resets == 1

    rig.printer.down = True  # a blip: 30 s, the same job answers again
    rig.tick(10.0)
    rig.tick(10.0)
    rig.printer.down = False
    rig.printer.progress = 0.52
    assert not rig.tick(10.0).new_print
    assert rig.resets == 1 and rig.monitor.tracker.frames > 50

    rig.printer.down = True  # switched off during the job; on again 2 min later, same file, starting over
    for _ in range(4):
        rig.tick(30.0)
    rig.printer.down = False
    rig.printer.progress = 0.01
    rep = rig.tick(30.0)
    assert rep.new_print and rig.resets == 2
    assert rig.monitor.tracker.frames == 1  # reset, then this cycle's frame
    assert rig.doc()["job"] == {"file": FILE, "started": rig.clock()}


def test_the_same_job_after_a_long_outage_is_not_a_new_print(rig: Rig) -> None:
    rig.print_file(0.5)
    rig.tick()
    rig.printer.down = True
    for _ in range(6):
        rig.tick(30.0)
    rig.printer.down = False
    rig.printer.progress = 0.55  # e.g. the printer only was unreachable on the network
    assert not rig.tick(30.0).new_print
    assert rig.resets == 1 and rig.doc()["job"]["started"] == T0 + 10


def test_a_failure_of_the_status_poll_other_than_comm_error_propagates(
    rig: Rig,
) -> None:
    def boom() -> PrinterStatus:
        raise ValueError("a bug is not an outage")

    rig.printer.status = boom  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="not an outage"):
        rig.cycle.cycle()


# ---- the detached-part baseline belongs to the print ---------------------------------------------------


def test_after_a_new_print_the_detach_judge_learns_its_baseline_from_the_new_print(
    tmp_path: Path,
) -> None:
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("scipy")
    from vision_detach import FixedBedDetach
    from vision_detect import FrameScorer

    plate, part_colour = (40, 110, 190), (225, 225, 225)
    roi = [(0.05, 0.3), (0.95, 0.3), (0.95, 0.95), (0.05, 0.95)]
    first, second = (235, 335), (115, 215)  # where the part of print 1 / print 2 stands

    def bed(rows: tuple[int, int], i: int) -> bytes:
        img = np.full((360, 640, 3), plate, np.uint8)
        img[rows[0] : rows[1], 200:420] = part_colour
        img[120:260, 40 + 40 * i : 120 + 40 * i] = (
            50,
            50,
            50,
        )  # the toolhead walks: never twice in one place
        ok, buf = cv2.imencode(".png", img)
        assert ok
        return bytes(buf)

    class Detector:
        def score_bgr(self, bgr: Any) -> FrameScore:
            return FrameScore(0.0, [], 90.0)

    fixed = FixedBedDetach(roi)
    scorer = FrameScorer(Detector(), fixed_bed=fixed)  # type: ignore[arg-type]
    clock = Clock()

    def score_at(jpeg: bytes) -> FrameScore:
        scorer.now = clock()  # replay time: the judge's cadence follows the fake clock
        return scorer(jpeg)

    rig = Rig(
        tmp_path,
        interval=20.0,
        score=score_at,
        reset_print=scorer.reset_print,
        clock=clock,
    )

    rows = {"now": first}
    step = {"i": 0}

    def payload() -> bytes:
        step["i"] += 1
        return bed(rows["now"], step["i"] % 12)

    rig.camera.payload = payload
    rig.print_file(0.2)
    for _ in range(8):
        rig.tick(20.0)
    learned = fixed.part
    assert learned is not None and bool(learned[first[0] + 10 : first[1] - 10].any())

    rig.printer.down = True  # a blip keeps the baseline of the running print
    rig.tick(20.0)
    rig.printer.down = False
    rig.printer.progress = 0.25
    rig.tick(20.0)
    assert fixed.part is learned

    rig.printer.down = (
        True  # switched off, back after 2 min with the same file starting over
    )
    for _ in range(6):
        rig.tick(20.0)
    rig.printer.down = False
    rig.printer.progress = 0.01
    rows["now"] = second  # the new print stands elsewhere on the plate
    rep = rig.tick(20.0)
    assert (
        rep.new_print and fixed.part is None
    )  # forgotten; this frame is the first of the new baseline
    for _ in range(8):
        rig.tick(20.0)
    relearned = fixed.part
    assert relearned is not None and relearned is not learned
    assert bool(relearned[second[0] + 10 : second[1] - 10].any())
    assert not bool(
        (relearned & learned).any()
    )  # nothing of the previous print's footprint survived


# ---- the status file ---------------------------------------------------------------------------------


def test_the_status_file_is_written_every_cycle_with_the_liveness_keys(
    rig: Rig,
) -> None:
    rig.printer.down = True
    rig.tick()
    first = rig.doc()
    rig.tick()
    second = rig.doc()
    assert first["schema"] == 1 and second["ts"] > first["ts"]
    for key in (
        "last_poll_ok",
        "unreachable_since",
        "reachable",
        "last_frame_ts",
        "supervising",
        "job",
        "interval_s",
    ):
        assert key in second
    assert (second["last_poll_ok"], second["reachable"]) == (None, False)


# ---- the alert chain: one pause per job, whoever started it --------------------------------------------


def test_a_verified_critical_for_an_unregistered_job_pauses_once_and_sends_one_tg_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printer_ops.auto_pause import AutoPauser
    from printer_ops.watch import CRITICAL
    from printer_ops.watch_sinks import Dispatcher, TgSink

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    clock = Clock()
    tg_calls: list[list[str]] = []

    def fake_tg(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        tg_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "OK tg#1", "")

    sink = TgSink(
        runner=fake_tg, clock=clock
    )  # a fake runner: the real `tg` is never reached
    dispatcher = Dispatcher(dedup_s=0, clock=clock)
    dispatcher.add("u1", [sink], CRITICAL)
    rig = Rig(
        tmp_path,
        score=Scores([0.0] * 10 + [3.0] * 40, verified=True),
        dispatcher=dispatcher,
        clock=clock,
    )
    printer = rig.printer
    printer.record = JobRecord(FILE, "in_progress", T0 - 100, 80.0, 1000.0, "00006D")
    pauser = AutoPauser(
        "u1",
        adapter=lambda: printer,
        claim_dir=tmp_path / "claims",
        clock=clock,
        sleep=clock.advance,
    )
    sink.attach("u1", pauser=pauser)

    rig.print_file(0.3)  # started by nobody this process knows of
    for _ in range(40):
        rig.tick()
        if printer.pauses:
            break
    assert printer.pauses == 1 and printer.state == "paused"
    (line,) = tg_calls
    text = line[-1]
    assert (
        "Watcher PAUSED the print" in text
        and FILE in text
        and len(text.splitlines()) <= 5
    )
    assert [p.name for p in (tmp_path / "claims").iterdir()] == ["u1-00006D.json"]

    printer.state = "printing"  # the owner resumed; the failure goes on: a second critical in this job
    for _ in range(20):
        rig.tick()
    assert printer.pauses == 1  # the claim file: once per job
    assert sum("Watcher PAUSED the print" in c[-1] for c in tg_calls) == 1
    assert len(list((tmp_path / "claims").iterdir())) == 1


# ---- stdlib only; run_live around the cycle -------------------------------------------------------------


def test_the_cycle_module_imports_without_the_heavy_vision_stack() -> None:
    lib = str(Path(__file__).resolve().parent.parent / "lib")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {lib!r})\n"
        "import printer_ops.vision_cycle\n"
        "bad = sorted({'numpy', 'cv2', 'onnxruntime', 'scipy'} & set(sys.modules))\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


class _FakeScorer:
    geometry = None
    fixed_bed = None
    model = None
    context = None

    def __init__(self) -> None:
        self.resets = 0

    def reset_print(self) -> None:
        self.resets += 1

    def __call__(self, jpeg: bytes) -> FrameScore:
        return FrameScore(
            0.25, [Box(0.1, 0.2, 0.3, 0.4, 0.5)], 77.0, probs=[0.4], q=0.1
        )


def _patch_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    printer: FakePrinter,
    camera: FakeCamera,
) -> _FakeScorer:
    import vision_detect
    from printer_ops import devices
    from printer_ops import watch_sinks

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    device = SimpleNamespace(
        name="u1",
        host="127.0.0.1",
        port=7125,
        adapter="moonraker",
        watch={"vision": {"npu": False}},
    )
    scorer = _FakeScorer()
    monkeypatch.setattr(devices, "get_device", lambda name: device)
    monkeypatch.setattr("printer_ops.make_adapter", lambda dev: printer)
    monkeypatch.setattr(vision_detect, "_device_camera", lambda dev, args: camera)
    monkeypatch.setattr(vision_detect, "_calibration", lambda dev, args: None)
    monkeypatch.setattr(vision_detect, "build_scorer", lambda *a, **k: scorer)
    monkeypatch.setattr(
        watch_sinks.Dispatcher,
        "for_devices",
        classmethod(lambda cls, *a, **k: _Recorder()),
    )
    return scorer


class _Recorder:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []
        self.min_severity: dict[str, str] = {}

    def start_startup_test(self, printers: list[str], role: str) -> None:
        pass

    def process(self, batch: list[Any]) -> list[Any]:
        self.batches.append(batch)
        return []


def _live_args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "printer": "u1",
        "interval": 10.0,
        "once": False,
        "save": None,
        "verbose": False,
        "snapshot": None,
        "no_keepalive": False,
        "no_npu": False,
        "no_geometry": True,
        "no_verify": True,
        "scene": False,
        "no_detach": True,
        "model": False,
        "evidence": None,
        "dry_run": False,
        "min_severity": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_run_live_once_prints_the_same_report_keys_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vision_detect

    printer, camera = FakePrinter(), FakeCamera(Clock())
    printer.state, printer.filename, printer.progress = "standby", "a.gcode", 0.0
    scorer = _patch_live(monkeypatch, tmp_path, printer, camera)
    assert vision_detect.run_live(_live_args(once=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert list(report) == [
        "printer",
        "state",
        "file",
        "p",
        "q",
        "luma",
        "masked",
        "boxes",
        "findings",
        "trend",
        "events",
    ]
    assert (
        report["state"],
        report["file"],
        report["p"],
        report["q"],
        report["luma"],
    ) == ("standby", "a.gcode", 0.25, 0.1, 77.0)
    assert report["boxes"][0]["verified"] == 0.4 and report["events"] == []
    assert (
        scorer.resets == 0 and not (tmp_path / "state" / "3d-cli" / "vision").exists()
    )


def test_run_live_once_on_a_silent_printer_reports_the_error_and_still_looks_at_the_camera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vision_detect

    printer, camera = FakePrinter(), FakeCamera(Clock())
    printer.down = True
    _patch_live(monkeypatch, tmp_path, printer, camera)
    intentional_off.set_marker("u1")
    assert vision_detect.run_live(_live_args(once=True)) == 0
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert (
        report["error"] == "connection refused"
        and "state" not in report
        and report["p"] == 0.25
    )
    assert camera.grabs == 1 and out.err == ""
    assert intentional_off.is_set(
        "u1"
    )  # a one-shot look never clears the owner's marker


def test_run_live_loops_the_cycle_and_hands_events_to_the_dispatcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vision_detect

    clock = Clock()
    printer, camera = FakePrinter(), FakeCamera(clock)
    scorer = _patch_live(monkeypatch, tmp_path, printer, camera)
    delays: list[float] = []

    class Stop(Exception):
        pass

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 3:
            printer.down = True
        if len(delays) == 5:
            raise Stop

    monkeypatch.setattr(vision_detect.time, "sleep", fake_sleep)
    printer.state, printer.filename, printer.progress = "printing", FILE, 0.1
    with pytest.raises(Stop):
        vision_detect.run_live(_live_args(verbose=True))
    err = capsys.readouterr().err.splitlines()
    assert scorer.resets == 1 and camera.grabs == 3
    assert (
        delays[:3] == [pytest.approx(10.0, abs=0.5)] * 3
        and delays[3:] == [pytest.approx(10.0, abs=0.5)] * 2
    )
    assert (
        sum("does not answer" in line for line in err) == 1
    )  # one line, not one per cycle
    verbose = [json.loads(line) for line in err if line.startswith("{")]
    assert (
        verbose[0]["state"] == "printing"
        and verbose[-1]["error"] == "connection refused"
    )
    doc = read_status("u1")
    assert doc is not None and doc["reachable"] is False and doc["job"] is None
