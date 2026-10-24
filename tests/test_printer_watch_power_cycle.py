"""`3d printer watch` across printer power cycles (#640) and print starts after them (#641).

A printer that is switched off between prints must not make the watcher alarm, spam or give up, and the
first answer after power-on must find the watcher supervising: the outage is ONE event (info; warning only
when a job was running), polling stays bounded (<= 30 s), the intentional-off marker silences even that one
event and is cleared at the first answer, a print started from the touchscreen is `print_started`, and a
NEW job found after a power-cycle-long gap restarts the per-job state. Detector tests feed frames and a
clock; the stream tests drive `MoonrakerStream.run()` against a scripted websocket on a fake clock (the
retry wait is the injected stop event's `wait`), so nothing sleeps and no socket is opened.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from printer_fakes import U1_OBJECTS, k1_state, u1_ws_status

from printer_ops import devices, intentional_off, watch_run
from printer_ops.watch import (
    CRITICAL,
    INFO,
    WARNING,
    CrealityDetector,
    Event,
    MoonrakerDetector,
    WatchLimits,
)
from printer_ops.ws import WebSocketClosed

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The marker functions read XDG_STATE_HOME: never the owner's real state dir."""
    state = tmp_path / "state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


def kinds(events: list[Event]) -> list[tuple[str, str]]:
    return [(e.kind, e.severity) for e in events]


def status(
    state: str, file: str = "a.gcode", progress: float = 0.5, **over: Any
) -> dict[str, Any]:
    over.setdefault("print_stats", {})["filename"] = file
    over["virtual_sdcard"] = {
        "progress": progress,
        "file_position": int(progress * 1000),
    }
    return u1_ws_status(state, **over)


def printing_detector(
    state: str = "printing",
    progress: float = 0.5,
    file: str = "a.gcode",
    limits: WatchLimits | None = None,
) -> MoonrakerDetector:
    d = MoonrakerDetector("u1", limits or WatchLimits())
    d.on_connection(True, "", 999.0)
    d.on_status(status(state, file, progress), 1000.0)
    return d


def outage(
    d: MoonrakerDetector | CrealityDetector, start: float, length: float
) -> list[Event]:
    """The link drops at `start` and is back `length` seconds later: the connection events of both edges."""
    return d.on_connection(False, "WebSocketClosed: reset", start) + d.on_connection(
        True, "", start + length
    )


# ---- one event per outage ----------------------------------------------------------------------


def test_unreachable_at_start_is_one_info_event_then_one_info_when_it_answers() -> None:
    d = MoonrakerDetector("u1")
    first = d.on_connection(False, "refused", T0)
    assert kinds(first) == [("connection", INFO)]
    assert (
        first[0].text == "u1: waiting for the printer: refused"
        and first[0].severity == INFO
    )
    for i in range(1, 400):  # hours of polling every 30 s
        assert d.on_connection(False, "refused", T0 + 30 * i) == []
    up = d.on_connection(True, "", T0 + 12_000)
    assert (
        kinds(up) == [("connection", INFO)]
        and "connected (was unreachable at start)" in up[0].text
    )


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_link_lost_during_a_job_is_a_warning_that_says_so(state: str) -> None:
    d = printing_detector(state)
    down = d.on_connection(False, "WebSocketClosed: reset", 1100.0)
    assert kinds(down) == [("connection", WARNING)]
    assert (
        "connection lost" in down[0].text
        and "a job was running: check the printer" in down[0].text
    )
    assert d.on_connection(False, "refused", 1130.0) == []
    assert kinds(d.on_connection(True, "", 5000.0)) == [("connection", INFO)]


@pytest.mark.parametrize("state", ["standby", "complete", "cancelled"])
def test_link_lost_without_a_job_is_only_info(state: str) -> None:
    d = printing_detector(state)
    down = d.on_connection(False, "WebSocketClosed: reset", 1100.0)
    assert (
        kinds(down) == [("connection", INFO)]
        and "a job was running" not in down[0].text
    )
    up = d.on_connection(True, "", 5000.0)
    assert kinds(up) == [("connection", INFO)] and "reconnected" in up[0].text


def test_intentional_off_marker_means_no_down_event_but_still_one_answer_event() -> (
    None
):
    d = printing_detector("standby")
    assert d.on_connection(False, "refused", 1100.0, quiet=True) == []
    assert (
        d.on_connection(False, "refused", 1130.0) == []
    )  # the marker going away mid-outage adds nothing
    assert kinds(d.on_connection(True, "", 9000.0)) == [("connection", INFO)]


def test_flapping_sessions_that_deliver_nothing_do_not_spam() -> None:
    """A printer that boots (websocket opens, then drops before any data) for minutes: two events at most."""
    d = MoonrakerDetector("u1")
    events = d.on_connection(False, "refused", T0)
    for i in range(10):
        events += d.on_connection(True, "", T0 + 10 + 10 * i)
        events += d.on_connection(False, "WebSocketClosed: reset", T0 + 15 + 10 * i)
    assert kinds(events) == [("connection", INFO), ("connection", INFO)]
    d.on_connection(True, "", T0 + 200)
    d.on_status(status("standby"), T0 + 201, full=True)  # data at last
    again = d.on_connection(False, "WebSocketClosed: reset", T0 + 300)
    assert kinds(again) == [("connection", INFO)] and "connection lost" in again[0].text


# ---- the first frame after a power-cycle-long outage -------------------------------------------


def test_a_websocket_blip_shorter_than_the_gap_is_never_a_new_job() -> None:
    d = printing_detector("standby", progress=0.0)
    d.on_status({"print_stats": {"state": "printing"}}, 1050.0)
    assert d._job_started == 1050.0
    outage(d, 1100.0, 59.0)
    ev = d.on_status(status("printing", progress=0.1), 1159.0, full=True)  # progress even went back
    assert ev == [] and d._job_started == 1050.0


def test_the_same_job_after_a_long_outage_keeps_everything() -> None:
    d = MoonrakerDetector("u1", WatchLimits(milestones=(0.5,)))
    d.on_connection(True, "", 899.0)
    d.on_status(status("standby"), 900.0)
    d.on_status(status("printing", progress=0.4), 1000.0)
    assert kinds(d.on_status({"virtual_sdcard": {"progress": 0.55, "file_position": 550}}, 1010.0)) == [
        ("milestone", INFO)
    ]
    started_at, done = d._job_started, set(d._milestones_done)
    assert started_at == 1000.0 and done == {0.5}
    outage(d, 1030.0, 3600.0)
    ev = d.on_status(status("printing", progress=0.60), 4630.0, full=True)
    assert [e.kind for e in ev] == []  # no print_started, no second milestone
    assert d._job_started == started_at and d._milestones_done == done
    assert d.job_info() == {"file": "a.gcode", "started": 1000.0, "adopted": False}


def test_another_file_after_a_long_outage_is_a_new_job_and_restarts_the_per_job_state() -> (
    None
):
    d = MoonrakerDetector("u1", WatchLimits(milestones=(0.5,)))
    d.on_connection(True, "", 899.0)
    d.on_status(status("standby"), 900.0)
    d.on_status(status("printing", progress=0.4), 1000.0)
    d.on_status({"virtual_sdcard": {"progress": 0.55, "file_position": 550}}, 1010.0)
    d.on_status({"heater_bed": {"target": 0.0}}, 1020.0)
    assert d._held and d._milestones_done
    jobs = d.jobs
    outage(d, 1030.0, 3600.0)
    ev = d.on_status(
        status("printing", file="b.gcode", progress=0.01), 4630.0, full=True
    )
    assert kinds(ev) == [("print_started", INFO)]
    e = ev[0]
    assert (
        e.data["file"] == "b.gcode"
        and e.data["adopted"] is True
        and e.data["job_started"] == 4630.0
    )
    assert e.text.startswith(
        "u1: printing (adopted: the watcher was not running at its start)"
    )
    assert d._held == {} and d._milestones_done == set() and d._reason is None
    assert d._job_started == 4630.0
    assert (
        d.jobs == jobs + 1 and d.file_body is None
    )  # the stream asks for b.gcode's body bounds
    assert d.job_info() == {"file": "b.gcode", "started": 4630.0, "adopted": True}


@pytest.mark.parametrize(
    ("progress", "new_job"), [(0.55, True), (0.0, True), (0.59, False), (0.60, False)]
)
def test_progress_that_went_back_by_more_than_two_percent_is_a_new_job(
    progress: float, new_job: bool
) -> None:
    d = printing_detector(progress=0.60)
    outage(d, 1100.0, 600.0)
    ev = d.on_status(status("printing", progress=progress), 1700.0, full=True)
    assert kinds(ev) == ([("print_started", INFO)] if new_job else [])


@pytest.mark.parametrize("before", ["standby", "complete", "cancelled", "error"])
def test_a_job_on_the_first_frame_after_an_outage_that_began_idle_is_a_new_adopted_job(
    before: str,
) -> None:
    d = printing_detector(before, file="a.gcode")
    outage(d, 1100.0, 7200.0)
    ev = d.on_status(
        status("printing", file="a.gcode", progress=0.02), 8300.0, full=True
    )
    assert kinds(ev) == [("print_started", INFO)]
    assert (
        ev[0].data["adopted"] is True
        and ev[0].data["job_started"] == 8300.0
        and d._job_started == 8300.0
    )


def test_printing_seen_after_an_idle_frame_following_the_reconnect_is_a_plain_start() -> (
    None
):
    """The watcher saw the non-job state after the reconnect, so the start is witnessed: adopted false."""
    d = printing_detector("complete")
    outage(d, 1100.0, 7200.0)
    assert d.on_status(status("standby", file=""), 8300.0, full=True) == []
    ev = d.on_status(
        {"print_stats": {"state": "printing", "filename": "touchscreen.gcode"}}, 8400.0
    )
    assert kinds(ev) == [("print_started", INFO)]
    assert ev[0].data["adopted"] is False and ev[0].data["job_started"] == 8400.0
    assert ev[0].data["file"] == "touchscreen.gcode"


def test_a_start_edge_across_a_short_gap_is_a_plain_start() -> None:
    d = printing_detector("standby")
    outage(d, 1100.0, 20.0)
    ev = d.on_status(
        status("printing", file="x.gcode", progress=0.0), 1120.0, full=True
    )
    assert kinds(ev) == [("print_started", INFO)] and ev[0].data["adopted"] is False


def test_a_paused_job_that_is_printing_again_after_the_outage_is_a_resume() -> None:
    d = printing_detector("paused", progress=0.5)
    outage(d, 1100.0, 3600.0)
    ev = d.on_status(status("printing", progress=0.5), 4700.0, full=True)
    assert kinds(ev) == [("state", INFO)] and ev[0].text.startswith("u1: resumed")


def test_klippy_starting_after_the_outage_does_not_use_up_the_gap() -> None:
    d = printing_detector(progress=0.8)
    outage(d, 1100.0, 3600.0)
    booting = status("printing")
    booting["webhooks"] = {"state": "startup", "state_message": "Printer is starting"}
    assert d.on_status(booting, 4700.0, full=True) == []  # klippy startup: unknown state, nothing to report
    ev = d.on_status(
        status("printing", file="b.gcode", progress=0.0), 4760.0, full=True
    )
    assert (
        kinds(ev) == [("print_started", INFO)] and ev[0].data["job_started"] == 4760.0
    )


def test_heater_history_of_the_old_job_cannot_raise_a_drift_after_a_power_cycle() -> (
    None
):
    """The extruder was at target before the cut; the new job's cold nozzle on the first frame is heating up."""
    limits = WatchLimits(drift_hold_s=30, heat_timeout_s=600)
    cold: dict[str, Any] = {"extruder": {"temperature": 25.0, "target": 220.0}}

    same = printing_detector(limits=limits)  # control: a 20 s blip keeps its history
    outage(same, 1100.0, 20.0)
    same.on_status(status("printing", progress=0.5, **cold), 1120.0, full=True)
    assert [(e.kind, e.severity) for e in same.tick(1200.0)] == [
        ("temperature", CRITICAL)
    ]

    cycled = printing_detector(limits=limits)
    outage(cycled, 1100.0, 3600.0)
    cycled.on_status(
        status("printing", file="b.gcode", progress=0.0, **cold), 4700.0, full=True
    )
    assert cycled.tick(4800.0) == []


def test_creality_new_job_after_a_power_cycle_and_a_busy_frame_in_between() -> None:
    d = CrealityDetector("k1se")
    d.on_connection(True, "", 999.0)
    d.on_push({**k1_state(1, 1, "part.gcode"), "printProgress": 80}, 1000.0)
    outage(d, 1100.0, 3600.0)
    assert (
        d.on_push({"state": 7, "deviceState": 0}, 4700.0) == []
    )  # a cancel still finishing: busy
    ev = d.on_push(
        {
            "state": 1,
            "deviceState": 1,
            "printFileName": "/usr/data/printer_data/gcodes/new.gcode",
            "printProgress": 0,
        },
        4710.0,
    )
    assert kinds(ev) == [("print_started", INFO)]
    assert (
        ev[0].data["file"] == "new.gcode"
        and ev[0].data["adopted"] is True
        and ev[0].data["job_started"] == 4710.0
    )


def test_creality_same_job_after_an_outage_says_nothing_and_a_reprint_of_the_file_is_new() -> (
    None
):
    d = CrealityDetector("k1se")
    d.on_connection(True, "", 999.0)
    d.on_push({**k1_state(1, 1, "part.gcode"), "printProgress": 40}, 1000.0)
    outage(d, 1100.0, 3600.0)
    assert d.on_push({"state": 1, "printProgress": 44}, 4700.0) == []
    outage(d, 5000.0, 3600.0)
    ev = d.on_push(
        {**k1_state(1, 1, "part.gcode"), "printProgress": 3}, 8600.0
    )  # same name, progress 44 -> 3
    assert kinds(ev) == [("print_started", INFO)] and ev[0].data["file"] == "part.gcode"


# ---- the stream: bounded polling, heartbeat keys, marker ---------------------------------------


class World:
    """One printer's power history on one fake clock: off until `on_at`, a touchscreen print at `print_at`,
    cut at `cut_at`, the scenario ends at `end_at`. Records every connect attempt and wait."""

    def __init__(
        self, *, on_at: float, print_at: float, cut_at: float, end_at: float
    ) -> None:
        self.now = T0
        self.on_at, self.print_at, self.cut_at, self.end_at = (
            T0 + s for s in (on_at, print_at, cut_at, end_at)
        )
        self.attempts: list[float] = []
        self.waits: list[float] = []
        self.methods: list[str] = []
        self.print_delivered: float | None = None
        self.cut_seen: float | None = None
        self.hb: watch_run.Heartbeat | None = None
        self.hb_states: list[
            dict[str, Any]
        ] = []  # distinct consecutive (connected, unreachable_since, quiet, job)

    def wall(self) -> float:
        return self.now

    def powered(self) -> bool:
        return self.on_at <= self.now < self.cut_at

    def probe(self) -> None:
        assert self.hb is not None
        p = self.hb.printers["u1"]
        seen = {k: p.get(k) for k in ("connected", "unreachable_since", "quiet", "job")}
        if not self.hb_states or self.hb_states[-1] != seen:
            self.hb_states.append(seen)

    def idle_status(self) -> dict[str, Any]:
        return u1_ws_status(
            "standby",
            print_stats={"filename": ""},
            extruder={"temperature": 24.0, "target": 0.0},
            heater_bed={"temperature": 24.0, "target": 0.0},
            virtual_sdcard={"progress": 0.0},
            idle_timeout={"state": "Idle"},
        )


class FakeStop(threading.Event):
    """The stop event the stream waits on between attempts: waiting advances the fake clock instead of sleeping."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.world.waits.append(timeout)
        self.world.probe()
        self.world.now += timeout
        if self.world.now >= self.world.end_at:
            self.set()
        return self.is_set()


class FakeWS:
    def __init__(self, world: World) -> None:
        self.world = world
        self.replies: list[str] = []
        self.print_pushed = False

    def send_text(self, text: str) -> None:
        req = json.loads(text)
        self.world.methods.append(req["method"])
        results: dict[str, Any] = {
            "server.info": {"klippy_state": "ready"},
            "printer.objects.list": {"objects": list(U1_OBJECTS)},
            "printer.objects.subscribe": {"status": self.world.idle_status()},
        }
        if req["method"] in results:
            self.replies.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": results[req["method"]],
                    }
                )
            )
        else:  # server.files.metadata: unknown to this fake
            self.replies.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "error": {"code": -32601, "message": "not found"},
                    }
                )
            )

    def recv(self, timeout: float) -> str | None:
        w = self.world
        w.probe()
        assert w.now < w.end_at, (
            "the scenario overran: a session outlived the fake clock"
        )
        if self.replies:
            return self.replies.pop(0)
        if w.now >= w.cut_at:
            w.cut_seen = w.now
            raise WebSocketClosed("connection reset by peer")
        if not self.print_pushed and w.now >= w.print_at:
            self.print_pushed, w.print_delivered = True, w.now
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notify_status_update",
                    "params": [
                        {
                            "print_stats": {
                                "state": "printing",
                                "filename": "k1_slate.gcode",
                            }
                        },
                        w.now,
                    ],
                }
            )
        w.now += 1.0  # nothing for a second; Moonraker's own proc stats keep the stream alive
        return json.dumps(
            {"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]}
        )

    def close(self) -> None:
        pass


def run_world(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: watch_run.IntentionalOffMarker | None = None,
) -> list[Event]:
    """`MoonrakerStream.run()` on the calling thread until the scenario's end; -> the events it submitted."""

    class Socket:
        @staticmethod
        def connect(
            host: str, port: int, path: str = "/", *, timeout: float = 5.0
        ) -> FakeWS:
            world.attempts.append(world.now)
            if not world.powered():
                raise ConnectionRefusedError(111, "Connection refused")
            return FakeWS(world)

    monkeypatch.setattr(watch_run, "WebSocket", Socket)
    dev = devices.Device("u1", "moonraker", "127.0.0.1", 7125, None, tmp_path)
    world.hb = watch_run.Heartbeat(tmp_path / "hb.json")
    got: list[Event] = []
    stream = watch_run.MoonrakerStream(
        dev,
        got.append,
        FakeStop(world),
        world.hb,
        WatchLimits(),
        marker=marker,
        wall=world.wall,
        mono=world.wall,
    )
    stream.run()
    return got


def first_attempt_at_or_after(world: World, t: float) -> float:
    return next(a for a in world.attempts if a >= t)


HOUR = 3600.0


def test_off_for_hours_then_a_touchscreen_print_then_off_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner's day: the printer is off for 3 h, switched on, idle for 5 min, a print is started on its
    touchscreen, the power is cut 10 min later, and it stays off for 2 h."""
    world = World(
        on_at=3 * HOUR,
        print_at=3 * HOUR + 300,
        cut_at=3 * HOUR + 900,
        end_at=5 * HOUR + 900,
    )
    got = run_world(world, tmp_path, monkeypatch)

    up_at = first_attempt_at_or_after(world, world.on_at)
    assert world.print_delivered is not None and world.cut_seen is not None
    assert kinds(got) == [
        ("connection", INFO),
        ("connection", INFO),
        ("snapshot", INFO),
        ("print_started", INFO),
        ("connection", WARNING),
    ]
    waiting, answered, snapshot, started, lost = got
    assert (
        waiting.ts == T0
        and waiting.text
        == "u1: waiting for the printer: ConnectionRefusedError: [Errno 111] Connection refused"
    )
    assert (
        answered.ts == up_at and "connected (was unreachable at start)" in answered.text
    )
    assert snapshot.data["state"] == "idle"
    assert started.data["file"] == "k1_slate.gcode" and started.data["adopted"] is False
    assert started.data["job_started"] == world.print_delivered == started.ts
    assert (
        lost.ts == world.cut_seen
        and "a job was running: check the printer" in lost.text
    )

    # polling: 2, 4, 8, 16, then every 30 s; never further apart, hours of it
    before_up = [a for a in world.attempts if a < up_at]
    assert len(before_up) > 300
    assert (
        world.waits[:6] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        and max(world.waits) == 30.0
    )
    gaps = [b - a for a, b in zip(world.attempts, world.attempts[1:], strict=False)]
    assert max(gaps[: len(before_up) - 1]) <= 30.0
    # a session that ran for 10+ minutes resets the wait to 2 s, then it doubles to the cap again
    after = world.waits[len(before_up) :]
    assert after[:6] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0] and max(after) == 30.0
    assert (
        max(
            b - a
            for a, b in zip(
                world.attempts[len(before_up) + 1 :],
                world.attempts[len(before_up) + 2 :],
                strict=False,
            )
        )
        <= 30.0
    )
    assert world.methods[:3] == [
        "server.info",
        "printer.objects.list",
        "printer.objects.subscribe",
    ]

    # the heartbeat contract, in the order the printer went through it
    job = {"file": "k1_slate.gcode", "started": world.print_delivered, "adopted": False}
    assert world.hb_states == [
        {"connected": False, "unreachable_since": T0, "quiet": False, "job": None},
        {"connected": True, "unreachable_since": None, "quiet": False, "job": None},
        {"connected": True, "unreachable_since": None, "quiet": False, "job": job},
        {
            "connected": False,
            "unreachable_since": world.cut_seen,
            "quiet": False,
            "job": job,
        },
    ]
    assert world.hb is not None
    world.hb.write({}, {})
    beat = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))["printers"][
        "u1"
    ]
    assert beat["connected"] is False and beat["unreachable_since"] == world.cut_seen
    assert (
        beat["last_poll_ok"] == world.cut_seen
        and beat["quiet"] is False
        and beat["job"] == job
    )
    assert "intentional-off" not in capsys.readouterr().err


def test_the_marker_silences_the_outage_and_is_cleared_at_the_first_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    intentional_off.set_marker("u1", "off for the night", now=T0)
    world = World(
        on_at=3 * HOUR,
        print_at=3 * HOUR + 300,
        cut_at=3 * HOUR + 900,
        end_at=5 * HOUR + 900,
    )
    got = run_world(world, tmp_path, monkeypatch)

    # no 'waiting for the printer' for the whole first outage; the answer, the start and the cut are told
    assert kinds(got) == [
        ("connection", INFO),
        ("snapshot", INFO),
        ("print_started", INFO),
        ("connection", WARNING),
    ]
    assert "connected (was unreachable at start)" in got[0].text
    assert not intentional_off.is_set("u1")
    assert (
        capsys.readouterr().err.count(
            "watch: u1: answered again, cleared the intentional-off marker"
        )
        == 1
    )
    assert world.hb_states[0] == {
        "connected": False,
        "unreachable_since": T0,
        "quiet": True,
        "job": None,
    }
    assert (
        world.hb_states[1]["quiet"] is False and world.hb_states[-1]["quiet"] is False
    )


def test_marker_seam_is_injectable_and_a_set_marker_silences_a_cut_during_a_job_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    cleared: list[str] = []

    def is_set(printer: str) -> bool:
        asked.append(printer)
        return True

    def clear(printer: str) -> bool:
        cleared.append(printer)
        return False  # nothing to remove: no log line

    world = World(on_at=HOUR, print_at=HOUR + 300, cut_at=HOUR + 900, end_at=2 * HOUR)
    got = run_world(
        world,
        tmp_path,
        monkeypatch,
        watch_run.IntentionalOffMarker(is_set=is_set, clear=clear),
    )
    assert kinds(got) == [
        ("connection", INFO),
        ("snapshot", INFO),
        ("print_started", INFO),
    ]
    assert set(asked) == {"u1"} and cleared == ["u1"]
    assert world.hb_states[0]["quiet"] is True and world.hb_states[-1]["quiet"] is True


def test_polling_an_unreachable_printer_is_bounded_and_says_one_thing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(on_at=10**9, print_at=10**9, cut_at=10**9 + 1, end_at=6 * HOUR)
    got = run_world(world, tmp_path, monkeypatch)
    assert kinds(got) == [("connection", INFO)]
    assert world.waits[:6] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0] and set(
        world.waits[4:]
    ) == {30.0}
    assert (
        max(b - a for a, b in zip(world.attempts, world.attempts[1:], strict=False))
        <= 30.0
    )
    assert len(world.attempts) > 600 and world.hb_states == [
        {"connected": False, "unreachable_since": T0, "quiet": False, "job": None}
    ]
