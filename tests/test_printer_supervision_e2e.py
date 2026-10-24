"""Acceptance of the always-on supervision (#639 #640 #641 #642), on ONE fake timeline with no real printer, tg,
omp, tmux, systemctl or Home Assistant:

    the printer is switched off for hours -> it is switched on -> a job is started from its TOUCHSCREEN (nobody
    called `3d printer start`) -> the watcher detects the start edge (`print_started`) -> the vision loop
    supervises the job -> a critical finding appears while NO agent exists (no live subscriber, tmux/omp
    unusable) -> the print is paused once and the user gets a short tg line that says so -> the failure goes on
    after a resume: nothing is paused again -> `supervise status` / the doctor read the liveness of both units
    from the heartbeat files.

The watch half is `MoonrakerStream.run()` against the scripted websocket of test_printer_watch_power_cycle.py;
the vision half is `VisionCycle` (test_printer_vision_cycle.py's fakes) feeding the REAL Dispatcher chain of
test_printer_alert_delivery.py (subscriptions -> temporary agent -> tg -> AutoPauser)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from printer_supervise_fakes import Runner, healthy
from test_printer_alert_delivery import Chain
from test_printer_subscribe import Fake
from test_printer_vision_cycle import FILE, Clock, Rig, Scores
from test_printer_watch_power_cycle import HOUR, T0, World, run_world

from printer_ops import supervise as sv
from printer_ops import supervise_units as su
from printer_ops.auto_pause import AutoPauser
from printer_ops.base import JobRecord
from printer_ops.subscriptions import DeliveryState
from printer_ops.watch import CRITICAL, INFO, WARNING, Event
from printer_ops.watch_run import Heartbeat

ON_AT, PRINT_AT, END_AT = 3 * HOUR, 3 * HOUR + 60, 3 * HOUR + 600


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@dataclass
class Day:
    world: World
    watch_events: list[Event]
    rig: Rig
    chain: Chain
    clock: Clock
    state_dir: Path
    claims: Path
    first_frame_at: float | None = None
    unreachable_cycles: int = 0
    delays: list[float] = field(default_factory=list)

    def pause_lines(self) -> list[str]:
        return [c[-1] for c in self.chain.tg.calls if "PAUSED the print" in c[-1]]


def critical_finding(clock: Clock, n: int) -> Event:
    """A verified, confident critical `vision_spaghetti` — the evidence bar of the unattended pause."""
    return Event(
        "u1", "vision_spaghetti", CRITICAL, f"u1: print looks like spaghetti on camera (synthetic #{n})",
        {"p": 3.0, "q": 2.7, "boxes": [{"x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4, "conf": 0.9, "verified": 0.9}],
         "state": "printing", "file": FILE},
        key=f"u1:vision_spaghetti:synthetic:{n}", ts=clock.now,
    )  # fmt: skip


def the_day(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, scores: list[float], *, synthetic: bool
) -> Day:
    world = World(on_at=ON_AT, print_at=PRINT_AT, cut_at=END_AT - 1, end_at=END_AT)

    # ---- the watcher: unreachable for hours (one event), then the start edge --------------------------
    watch_events = run_world(world, xdg, monkeypatch)

    # ---- the vision loop on the same timeline, with no agent anywhere --------------------------------
    state_dir = xdg / "state" / "3d-cli"
    clock = Clock(T0)
    chain = Chain(
        Fake(tmux_ok=False), clock=None
    )  # no live subscriber; the temporary agent cannot start
    rig = Rig(
        state_dir, score=Scores(scores, verified=True), dispatcher=chain.d, clock=clock
    )
    printer = rig.printer
    printer.down = True
    claims = xdg / "claims"
    chain.tg_sink.attach(
        "u1",
        pauser=AutoPauser(
            "u1",
            adapter=lambda: printer,
            claim_dir=claims,
            clock=clock,
            sleep=clock.advance,
        ),
    )
    day = Day(world, watch_events, rig, chain, clock, state_dir, claims)
    resumed, injected = False, 0
    while (
        clock.now < T0 + END_AT - 90
    ):  # the vision loop watches until just before the power is cut
        t = clock.now - T0
        if t >= ON_AT:
            printer.down = False
        if t >= PRINT_AT and printer.state == "standby":
            # started on the touchscreen: only the printer knows
            printer.state, printer.filename, printer.progress = "printing", FILE, 0.02
            printer.record = JobRecord(
                FILE, "in_progress", clock.now, 0.0, 0.0, "00006D"
            )
        report = rig.cycle.cycle()
        day.delays.append(report.next_delay_s)
        day.unreachable_cycles += report.error is not None
        if report.frame is not None and day.first_frame_at is None:
            day.first_frame_at = clock.now
        if (
            synthetic
            and printer.state in ("printing", "paused")
            and t >= PRINT_AT + 120
            and (injected == 0 or (injected == 1 and resumed))
        ):
            injected += 1
            chain.d.process([critical_finding(clock, injected)])
        chain.agents.join(30)
        if printer.pauses == 1 and not resumed and printer.state == "paused":
            printer.state, resumed = (
                "printing",
                True,
            )  # the owner resumed; the finding persists
        clock.advance(report.next_delay_s)
    return day


def test_off_for_hours_then_a_touchscreen_print_is_detected_and_supervised(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = the_day(xdg, monkeypatch, [0.0] * 200, synthetic=False)
    world, events = day.world, day.watch_events
    assert [(e.kind, e.severity) for e in events] == [
        ("connection", INFO),  # ONE line for three hours of a dark printer
        ("connection", INFO),  # it answers
        ("snapshot", INFO),
        ("print_started", INFO),  # the touchscreen print
        ("connection", WARNING),  # the harness cuts the power while a job runs
    ]
    (started,) = [e for e in events if e.kind == "print_started"]
    assert started.data["file"] == "k1_slate.gcode" and started.data["adopted"] is False
    assert started.data["job_started"] == world.print_delivered == T0 + PRINT_AT
    assert (
        max(world.waits) <= 30.0
    )  # polled the dark printer at most every 30 s for 3 hours
    assert not (day.state_dir / "u1.intentional-off").exists()

    # vision supervision started within one poll of the start edge; the dark printer was polled <= 30 s apart
    assert (
        day.first_frame_at is not None
        and 0 <= day.first_frame_at - (T0 + PRINT_AT) <= 10.0
    )
    assert day.unreachable_cycles > 3 * HOUR / 30 - 5 and max(day.delays) <= 30.0
    doc = json.loads((day.state_dir / "vision" / "u1.json").read_text())
    assert (
        doc["supervising"] is True
        and doc["reachable"] is True
        and doc["job"]["file"] == FILE
    )
    assert (
        doc["last_frame_ts"] is not None
        and day.clock.now - doc["last_frame_ts"] <= 10.0
    )
    assert day.rig.printer.pauses == 0  # a healthy print is never touched


def test_a_critical_finding_with_nobody_to_judge_it_pauses_once_and_sends_one_short_line(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = the_day(xdg, monkeypatch, [0.0] * 200, synthetic=True)
    assert (
        day.rig.printer.pauses == 1
    )  # the second critical of the same job pauses nothing
    (line,) = (
        day.chain.tg.calls
    )  # ONE message: the second critical was folded by the limiter
    text = line[-1]
    assert "PAUSED the print" in text and FILE in text and len(text.splitlines()) <= 5
    assert [p.name for p in day.claims.iterdir()] == [
        "u1-00006D.json"
    ]  # once per job, bound from the printer's history
    assert DeliveryState().read()["counters"]["u1"]["auto_pause_paused"] == 1


def test_a_finding_that_ramps_up_is_a_warning_first_then_the_pause_message(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic path: the tracker climbs, so the user first reads the warning, then ONE line that says the
    print was paused. Never a second pause, never a third line."""
    day = the_day(xdg, monkeypatch, [0.0] * 6 + [3.0] * 80, synthetic=False)
    assert day.rig.printer.pauses == 1
    texts = [c[-1] for c in day.chain.tg.calls]
    assert len(day.pause_lines()) == 1 and len(texts) <= 2
    assert (
        texts[0].startswith("u1: possible spaghetti") or "PAUSED the print" in texts[0]
    )


def test_supervise_status_shows_the_liveness_of_both_units_while_the_job_runs(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = the_day(xdg, monkeypatch, [0.0] * 200, synthetic=False)
    world, state_dir, now = day.world, day.state_dir, day.clock.now
    before_cut = next(
        h for h in world.hb_states if h["job"] is not None and h["connected"]
    )
    hb_path = state_dir / "watch" / "u1.json"
    hb = Heartbeat(hb_path)
    hb.update("u1", state="printing", last_poll_ok=now - 1, **before_cut)  # as the watcher wrote it then
    hb.write({}, {})
    watch_doc = json.loads(hb_path.read_text())  # `updated` carries the real time: align it with the fake clock
    watch_doc["updated"] = now - 3
    hb_path.write_text(json.dumps(watch_doc))
    plan = su.PrinterPlan("u1", "moonraker", True)
    layout = su.Layout(
        Path("/srv/3d-cli"), Path("/srv/3d-cli/.venv/bin/python"), xdg / "units", None
    )
    env = sv.Env(
        show=healthy("u1"), run=Runner(), state_dir=state_dir, unit_dir=xdg / "units", clock=lambda: now,
        state_probe=lambda _n: "printing", alive=lambda _p: True, plans=lambda: [plan],
    )  # fmt: skip
    a = sv.gather(plan, env, live=True, layout=layout, linger=True)
    assert a.level == sv.OK, [f.text for f in a.findings]
    job = a.to_json()["job"]
    assert job["file"] == "k1_slate.gcode" and job["started"] == world.print_delivered

    # the same files ten minutes later, nobody writing: FAIL while the printer prints
    env.clock = lambda: now + 600
    stale = sv.gather(plan, env, live=True, layout=layout, linger=True)
    assert stale.level == sv.FAIL and any(
        "heartbeat is 10 min old" in f.text for f in stale.findings
    )
