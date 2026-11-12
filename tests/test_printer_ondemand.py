"""`3d printer ondemand` (printer_ops/ondemand.py, #747): the one-shot check a systemd timer runs every 30 s that
starts the watch and vision workers only while a print runs. The printers are the loopback fakes, systemd is a
recording fake and the clock is manual: nothing here reaches a real printer or `systemctl`."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from errors import UsageError
from printer_fakes import FakeCreality, FakeMoonraker, free_port, k1_state, u1_status

from printer_ops import ondemand as od

U1_UNITS = ("3d-printer-watch@u1.service", "3d-printer-vision@u1.service")
GRACE = 300.0
POLL = 30.0  # the timer interval


def target(**over: object) -> od.Target:
    base: dict[str, object] = {
        "name": "u1",
        "adapter": "moonraker",
        "host": "127.0.0.1",
        "port": 1,
    }
    return od.Target(**{**base, **over})  # type: ignore[arg-type]


# ---- the decision ------------------------------------------------------------------------------


def test_every_poll_of_a_running_print_asks_for_the_workers() -> None:
    m = od.Machine(GRACE)
    assert [m.observe(t, True) for t in (0, 30, 60)] == [od.START] * 3
    assert m.phase == od.ACTIVE


def test_the_workers_stay_for_the_grace_period_then_stop() -> None:
    m = od.Machine(GRACE)
    m.observe(0, True)
    assert (
        m.observe(100, False) is None and m.phase == od.GRACE
    )  # the grace ends at 400
    assert m.observe(399, False) is None
    assert m.observe(400, False) == od.STOP
    assert m.phase == od.GRACE  # idle only once the caller confirmed the stop
    m.stopped()
    assert m.phase == od.IDLE
    assert m.observe(430, False) is None


def test_a_print_that_starts_again_inside_the_grace_period_cancels_it() -> None:
    m = od.Machine(GRACE)
    m.observe(0, True)
    m.observe(10, False)
    assert m.observe(200, True) == od.START and m.phase == od.ACTIVE
    assert m.observe(215, False) is None  # a fresh grace period starts at THIS poll
    assert m.observe(214 + GRACE, False) is None
    assert m.observe(215 + GRACE, False) == od.STOP


def test_grace_zero_stops_on_the_first_poll_without_a_print() -> None:
    m = od.Machine(0.0)
    m.observe(0, True)
    assert m.observe(30, False) == od.STOP


def test_a_lost_printer_counts_like_an_ended_print_but_an_idle_one_never_starts_anything() -> (
    None
):
    m = od.Machine(GRACE)
    m.observe(0, True)
    assert m.observe(30, None) is None and m.phase == od.GRACE
    assert m.observe(30 + GRACE, None) == od.STOP
    quiet = od.Machine(GRACE)
    assert [quiet.observe(t, None) for t in range(0, 600, 30)] == [None] * 20
    assert quiet.phase == od.IDLE


def test_adopted_workers_get_one_fresh_grace_period() -> None:
    m = od.Machine(GRACE)
    m.adopt(1000)
    assert m.observe(1100, False) is None
    assert m.observe(1300, False) == od.STOP


# ---- the check with fake systemd and a manual clock -----------------------------------------------


class FakeSystemd:
    """`systemctl --user is-active|start|stop UNIT...` over a set of running units."""

    def __init__(self, running: Sequence[str] = ()) -> None:
        self.running = set(running)
        self.calls: list[list[str]] = []
        self.refuse_start: str | None = None  # stderr of a refused start
        self.condition_held: set[str] = (
            set()
        )  # start exits 0 but the unit stays inactive
        self.refuse_stop: str | None = None
        self.mute_is_active = False  # `is-active` times out or the bus is gone: no output at all

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["systemctl", "--user"]
        verb, units = argv[2], list(argv[3:])
        self.calls.append([verb, *units])
        if verb == "is-active":
            return self._is_active(argv, units)
        refused = self.refuse_start if verb == "start" else self.refuse_stop
        if refused:
            return subprocess.CompletedProcess(list(argv), 1, "", refused)
        if verb == "start":
            self.running |= set(units) - self.condition_held
        else:
            self.running -= set(units)
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def _is_active(
        self, argv: Sequence[str], units: list[str]
    ) -> subprocess.CompletedProcess[str]:
        if self.mute_is_active:
            return subprocess.CompletedProcess(list(argv), 124, "", "timed out after 60 s")
        out = "".join(
            ("active" if u in self.running else "inactive") + "\n" for u in units
        )
        return subprocess.CompletedProcess(
            list(argv), 0 if all(u in self.running for u in units) else 3, out, ""
        )

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


T0 = 1_800_000_100.0


class Rig:
    """The one-shot check of a scripted printer. Every `poll` is a FRESH `run_check` (a new process in
    production): all it knows of the previous one is the state file. `answer` is True/False (a print runs or
    not) or None (down)."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        systemd: FakeSystemd | None = None,
        grace: float = GRACE,
        **over: object,
    ) -> None:
        self.systemd = systemd or FakeSystemd()
        self.path = str(tmp_path / "runtime" / "ondemand" / "u1.json")
        self.now = T0
        self.stray = (
            False  # is this check the one that looks for workers somebody started?
        )
        self.answer: bool | None = False
        self.grace = grace
        self.log: list[str] = []
        self.target = target(**over)
        self.asked: list[
            float
        ] = []  # the clock of every check that really asked the printer
        self.resolved = 0
        self.writes = 0
        real_save = od.save_state

        def counting_save(path: str, doc: Mapping[str, Any]) -> None:
            self.writes += 1
            real_save(path, doc)

        monkeypatch.setattr(od, "save_state", counting_save)

    def _resolve(self, _name: str) -> od.Target:
        self.resolved += 1
        return self.target

    def _probe(self, _t: od.Target) -> bool:
        self.asked.append(self.now)
        if self.answer is None:
            raise od.Unreachable("127.0.0.1:1: Connection refused")
        return self.answer

    def poll(self, answer: bool | None, advance: float = POLL) -> int:
        """Advance the clock, let the printer say `answer`, run one scheduled check."""
        self.now += advance
        self.answer = answer
        return od.run_check(
            "u1",
            path=self.path,
            grace_s=self.grace,
            resolver=self._resolve,
            probe=self._probe,
            runner=self.systemd,
            clock=lambda: self.now,
            stray_due=lambda: self.stray,
            log=self.log.append,
        )

    def state(self) -> dict[str, Any]:
        return od.load_state(self.path)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_an_idle_printer_costs_one_look_at_the_first_check_no_write_and_one_log_line(
    rig: Rig,
) -> None:
    for _ in range(200):  # more than an hour of ticks
        rig.poll(False)
    assert rig.log == [
        "u1: supervising moonraker 127.0.0.1:1, workers watch, vision"
    ]  # once, not per tick
    assert (
        rig.writes == 1 and rig.resolved == 1
    )  # the first check saved target + state; then nothing changed
    # the only systemd traffic: the first check's one look at workers that are already running
    assert rig.systemd.calls == [["is-active", *U1_UNITS]]


def test_the_only_file_an_idle_check_ever_writes_is_its_state_file_in_the_runtime_directory(
    rig: Rig,
) -> None:
    rig.poll(False)
    assert sorted(p.name for p in Path(rig.path).parent.iterdir()) == [
        "u1.json",
        "u1.json.lock",
    ]  # the state and its lock, created once
    doc = rig.state()
    assert (doc["phase"], doc["seen"], doc["fails"]) == ("idle", "idle", 0)
    assert doc["target"]["host"] == "127.0.0.1" and doc["v"] == od.STATE_VERSION


def test_workers_started_by_hand_or_a_retry_timer_while_idle_are_stopped_after_a_grace_period(
    rig: Rig,
) -> None:
    rig.poll(False)  # the first check, workers down
    rig.systemd.running.add(
        U1_UNITS[0]
    )  # somebody ran `systemctl --user start 3d-printer-watch@u1`
    rig.poll(False)  # not a stray-look check: unnoticed
    assert rig.state()["phase"] == od.IDLE
    rig.stray = True
    rig.poll(False)  # a stray-look check
    assert "workers run with no print" in rig.log[-1]
    assert rig.state()["phase"] == od.GRACE
    rig.stray = False
    rig.poll(False, GRACE - 1)
    assert rig.systemd.running == {U1_UNITS[0]}
    rig.poll(False, 1)
    assert rig.systemd.running == set() and rig.state()["phase"] == od.IDLE


def test_a_stray_look_never_happens_while_a_print_or_its_grace_period_is_running(
    rig: Rig,
) -> None:
    rig.stray = True
    rig.poll(True)
    rig.poll(False)  # the print ended on a stray-look check: grace, not "stray"
    assert rig.state()["phase"] == od.GRACE
    assert all("no print" not in line for line in rig.log)


def test_a_print_starts_both_workers_once_and_ends_in_a_full_stop_after_the_grace_period(
    rig: Rig,
) -> None:
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)
    assert rig.log[-1] == "u1: print started: starting " + ", ".join(U1_UNITS)
    writes = rig.writes
    rig.poll(True)  # running: one is-active, no second start, no second line, no write
    assert rig.systemd.verbs() == ["is-active", "start", "is-active", "is-active"]
    assert rig.writes == writes
    rig.poll(False)
    assert rig.log[-1] == "u1: print ended: workers stay 300 s, then stop"
    rig.poll(False, 299)
    assert rig.systemd.running == set(U1_UNITS)
    rig.poll(False, 1)
    assert (
        rig.systemd.running == set()
        and rig.log[-1] == "u1: grace over: workers stopped"
    )
    calls = len(rig.systemd.calls)
    rig.poll(False)
    assert (
        len(rig.systemd.calls) == calls
    )  # idle again: silent (outside a stray window)


def test_the_state_says_what_the_last_check_saw_for_supervise_status(rig: Rig) -> None:
    rig.poll(False)
    assert od.read_status("u1", rig.path) == {
        "printer": "idle",
        "phase": "idle",
        "error": "",
        "config_error": "",
        "config_kept": False,
        "refused": False,
        "last_probe_mono": None,
    }
    rig.poll(True)
    assert od.read_status("u1", rig.path)["printer"] == "printing"
    assert od.read_status("u1", rig.path)["phase"] == "active"
    rig.poll(None)
    assert od.read_status("u1", rig.path)["printer"] == "unreachable"
    assert od.read_status("u1", rig.path)["phase"] == "grace"
    assert od.read_status("nobody", rig.path + ".missing") == {}


def test_a_print_already_running_at_the_first_check_starts_the_workers(
    rig: Rig,
) -> None:
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)


def test_workers_that_run_at_the_first_check_get_one_fresh_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch, FakeSystemd(U1_UNITS))
    r.poll(False)
    assert "already running" in r.log[-1]
    r.poll(False, 100)
    assert r.systemd.running == set(U1_UNITS)
    r.poll(False, 200)
    assert r.systemd.running == set()


def test_workers_left_running_with_a_print_still_running_are_not_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch, FakeSystemd(U1_UNITS))
    r.poll(True)
    assert "start" not in r.systemd.verbs() and r.state()["phase"] == od.ACTIVE


def test_a_check_after_a_reboot_loses_nothing_that_matters(
    rig: Rig, tmp_path: Path
) -> None:
    """The runtime directory is gone (a reboot) in the middle of a print: the next check finds the print, starts
    the workers and rebuilds the state; nothing else of the earlier state was needed."""
    rig.poll(True)
    Path(rig.path).unlink()
    rig.systemd.running.clear()  # the workers are gone with the reboot too
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS) and rig.state()["phase"] == od.ACTIVE
    assert rig.resolved == 2  # the target was resolved again, nothing was cached


def test_an_unreachable_printer_is_retried_once_then_every_two_minutes_and_never_starts_workers(
    rig: Rig,
) -> None:
    for _ in range(16):  # 8 minutes of 30 s ticks
        rig.poll(None)
    # the next tick after the first miss, then a 120 s backoff (5 s of slack for a tick that is a little early)
    assert rig.asked == [T0 + 30, T0 + 60, T0 + 180, T0 + 300, T0 + 420]
    assert rig.systemd.verbs() == [
        "is-active"
    ]  # the first check's one look; never a start
    assert len([line for line in rig.log if "does not answer" in line]) == 1
    rig.poll(False, 120)  # back: asked at the first tick past the deadline
    assert rig.log[-1] == "u1: printer answers again"
    rig.poll(False)  # answering: asked at every tick again
    assert rig.asked[-1] == rig.now


@pytest.mark.parametrize("tick", [30.0, 30.2, 45.0, 60.02, 90.0, 300.0])
def test_a_backed_off_printer_is_asked_again_for_any_timer_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tick: float
) -> None:
    """The backoff is a deadline in the state, not a time window: whatever interval the owner gave the timer,
    the printer is asked again within the backoff plus one tick (a fixed window starved some intervals for
    hours)."""
    r = Rig(tmp_path, monkeypatch)
    for _ in range(400):
        r.poll(None, tick)
    gaps = [b - a for a, b in zip(r.asked, r.asked[1:], strict=False)]
    assert gaps and max(gaps) <= od.BACKOFF_S + tick, (tick, max(gaps))
    assert r.asked[-1] > r.now - od.BACKOFF_S - tick  # still being asked at the end
    r.poll(False, tick)  # it comes back and is seen within a tick or the backoff
    r.poll(False, od.BACKOFF_S + tick)
    assert r.log[-1] == "u1: printer answers again"


def test_the_skipped_ticks_of_a_backed_off_printer_write_nothing(rig: Rig) -> None:
    rig.poll(None)
    rig.poll(None)  # the second miss: the deadline is written
    writes = rig.writes
    for _ in range(3):  # ticks inside the backoff
        rig.poll(None)
    assert rig.writes == writes and len(rig.asked) == 2
    assert rig.state()["fails"] == od.MAX_FAILS


def test_a_printer_lost_during_a_print_keeps_the_workers_for_the_grace_period_at_every_tick(
    rig: Rig,
) -> None:
    rig.poll(True)
    rig.poll(None)  # workers run: no backoff, the printer may be back any moment
    assert "printer lost during a print" in rig.log[-1]
    for _ in range(3):
        rig.poll(None)
    assert len(rig.asked) == 5  # asked at EVERY tick
    rig.poll(None, GRACE)
    assert rig.systemd.running == set()


def test_a_worker_that_died_or_was_stopped_by_hand_comes_back_while_the_print_runs(
    rig: Rig,
) -> None:
    rig.poll(True)
    rig.systemd.running.discard(U1_UNITS[1])
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)
    assert ["start", U1_UNITS[1]] in rig.systemd.calls  # only the missing one


def test_a_refused_start_is_logged_once_across_checks_and_retried_every_tick(
    rig: Rig,
) -> None:
    rig.systemd.refuse_start = "Unit 3d-printer-watch@u1.service is masked."
    for _ in range(3):
        rig.poll(True)
    errors = [line for line in rig.log if "cannot start" in line]
    assert len(errors) == 1 and "masked" in errors[0]
    assert rig.systemd.verbs().count("start") == 3
    rig.systemd.refuse_start = None
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)


def test_a_unit_held_by_a_condition_is_reported_not_silently_accepted(rig: Rig) -> None:
    rig.systemd.condition_held = {U1_UNITS[0]}
    rig.poll(True)
    assert any(
        "not running after start" in line and U1_UNITS[0] in line for line in rig.log
    )


def test_a_failed_stop_keeps_the_grace_state_and_is_retried_at_the_next_check(
    rig: Rig,
) -> None:
    rig.poll(True)
    rig.poll(False)
    rig.systemd.refuse_stop = "Failed to stop: Interactive authentication required."
    rig.poll(False, GRACE)
    assert rig.systemd.running == set(U1_UNITS) and rig.state()["phase"] == od.GRACE
    rig.systemd.refuse_stop = None
    rig.poll(False)
    assert rig.systemd.running == set() and rig.state()["phase"] == od.IDLE
    assert sum("cannot stop" in line for line in rig.log) == 1


def test_a_stop_is_issued_when_is_active_gives_no_answer_at_the_end_of_grace(
    rig: Rig,
) -> None:
    rig.poll(True)
    rig.poll(False)
    rig.systemd.mute_is_active = True  # cannot ask: must not be taken for "already stopped"
    rig.poll(False, GRACE)
    assert rig.systemd.calls[-1] == ["stop", *U1_UNITS]
    assert rig.systemd.running == set() and rig.state()["phase"] == od.IDLE


def test_a_timeout_of_systemctl_is_a_failed_action_not_a_crash(
    rig: Rig,
) -> None:
    def slow(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 60)

    rig.systemd = slow  # type: ignore[assignment]
    rig.poll(True)
    assert any("timed out" in line for line in rig.log)


def test_a_config_change_is_picked_up_at_the_next_check_and_keeps_the_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(True)
    r.poll(True)
    assert r.resolved == 1  # cached: the config files did not change
    cfg.write_text("longer")
    os.utime(
        cfg, ns=(10**9, 10**9)
    )  # a different mtime however coarse the filesystem clock is
    r.target = target(workers=("watch",), files=(str(cfg),))
    r.poll(False)
    assert r.resolved == 2 and r.log[-1].startswith("u1: print ended")
    assert (
        r.state()["target"]["workers"] == ["watch"] and r.state()["phase"] == od.GRACE
    )
    r.poll(False, GRACE)
    # the vision worker the config dropped is stopped too: it must not run for ever
    assert r.systemd.running == set() and r.state()["phase"] == od.IDLE


def test_a_config_file_saved_while_the_first_resolution_runs_is_resolved_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    os.utime(cfg, ns=(10**9, 10**9))
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    resolve = r._resolve
    saves = iter([True])  # only the first resolution is overtaken by an edit

    def saved_during_the_loader(name: str) -> od.Target:
        found = resolve(name)
        if next(saves, False):
            cfg.write_text("corrected")  # after the loader read it, before the stamps are kept
            later = time.time_ns() + 10**9  # a time past the loader's start however coarse the file clock is
            os.utime(cfg, ns=(later, later))
        return found

    r._resolve = saved_during_the_loader  # type: ignore[method-assign,assignment]
    r.poll(False)
    r.poll(False)
    assert r.resolved == 2  # the first stamps could not be trusted
    r.poll(False)
    assert r.resolved == 2  # the second resolution stamped before it ran: cached again


def _check(r: Rig, resolver: Callable[[str], od.Target], advance: float = POLL) -> int:
    r.now += advance
    return od.run_check(
        "u1",
        path=r.path,
        grace_s=r.grace,
        resolver=resolver,
        probe=r._probe,
        runner=r.systemd,
        clock=lambda: r.now,
        log=r.log.append,
    )


def test_a_printer_the_config_stops_supervising_is_not_probed_and_not_re_resolved_until_a_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(True)  # supervised, a print runs: the workers are up
    assert r.systemd.running == set(U1_UNITS)
    cfg.write_text("access: offline")
    os.utime(cfg, ns=(10**9, 10**9))
    calls: list[str] = []

    def refuse(name: str) -> od.Target:
        calls.append(name)
        raise od.NotSupervised(f"{name}: not a supervised printer", [str(cfg)])

    asked = len(r.asked)
    for _ in range(3):
        _check(r, refuse)
    assert len(r.asked) == asked  # the printer is not contacted any more
    assert r.systemd.running == set(
        U1_UNITS
    )  # the print's workers wait out the grace period
    _check(r, refuse, advance=r.grace)
    assert r.systemd.running == set() and r.state()["phase"] == od.IDLE
    for _ in range(100):  # idle: no probe, no resolution, no write
        _check(r, refuse)
    writes = r.writes
    _check(r, refuse)
    assert calls == ["u1"] and len(r.asked) == asked and r.writes == writes
    assert sum("not supervised any more" in line for line in r.log) == 1
    assert (
        od.read_status("u1", r.path)["config_error"] == "u1: not a supervised printer"
    )
    # the owner makes the printer supervised again: the file changed, the next check picks it up
    cfg.write_text("access: full")
    os.utime(cfg, ns=(2 * 10**9, 2 * 10**9))
    r.target = target(files=(str(cfg),))
    r.poll(False)
    assert len(r.asked) == asked + 1 and "refused" not in r.state()
    assert od.read_status("u1", r.path)["config_error"] == ""


def test_a_printer_refused_before_any_check_is_not_re_resolved_every_backoff_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a reboot the cached target is gone: the refusal itself must say which files to watch."""
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    r = Rig(tmp_path, monkeypatch)
    calls: list[str] = []

    def refuse(name: str) -> od.Target:
        calls.append(name)
        raise od.NotSupervised(f"{name}: not a supervised printer", [str(cfg)])

    for _ in range(50):
        _check(r, refuse, advance=od.BACKOFF_S)
    assert calls == ["u1"] and r.asked == [] and r.systemd.calls == []


def test_a_resolve_that_refuses_exits_with_a_config_exit_code_and_names_the_files(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert od.main(["n1", "--resolve"]) == od.EX_CONFIG
    refusal = json.loads(capsys.readouterr().out)
    assert "not a supervised printer" in refusal["message"] and refusal["files"]


def test_a_bad_grace_drop_in_fails_before_the_loader_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(od.ENV_GRACE, "15min")
    r = Rig(tmp_path, monkeypatch)
    calls: list[str] = []

    def resolver(name: str) -> od.Target:
        calls.append(name)
        return r.target

    with pytest.raises(UsageError, match=od.ENV_GRACE):
        od.run_check(
            "u1",
            path=r.path,
            resolver=resolver,
            probe=r._probe,
            runner=r.systemd,
            clock=lambda: r.now,
            log=r.log.append,
        )
    assert calls == []


def test_a_config_file_saved_while_the_loader_runs_is_seen_as_changed_at_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    os.utime(cfg, ns=(10**9, 10**9))
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(False)
    cfg.write_text("b")
    os.utime(cfg, ns=(2 * 10**9, 2 * 10**9))

    def resolver_with_a_save_in_flight(_name: str) -> od.Target:
        r.resolved += 1
        os.utime(cfg, ns=(3 * 10**9, 3 * 10**9))  # saved again while the loader runs
        return r.target

    _check(r, resolver_with_a_save_in_flight)
    assert r.resolved == 2
    r.poll(False)
    assert r.resolved == 3  # the save during the first resolution was not swallowed


class NotLoadedSystemd(FakeSystemd):
    """`systemctl stop` of a unit whose template is not installed (a printer without a camera has no vision
    template) stops the others and exits 5 like the real one."""

    def __init__(self, unloaded: Sequence[str]) -> None:
        super().__init__()
        self.unloaded = set(unloaded)

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = super().__call__(argv)
        if argv[2] == "stop" and self.unloaded & set(argv[3:]):
            return subprocess.CompletedProcess(
                list(argv), 5, "", "Unit 3d-printer-vision@u1.service not loaded."
            )
        return result


def test_a_printer_without_a_camera_leaves_grace_although_its_vision_template_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(
        tmp_path,
        monkeypatch,
        systemd=NotLoadedSystemd([U1_UNITS[1]]),
        workers=("watch",),
    )
    r.poll(True)
    assert r.systemd.running == {U1_UNITS[0]}
    r.poll(False)
    r.poll(False, GRACE)
    assert r.systemd.running == set() and r.state()["phase"] == od.IDLE
    assert not any("cannot stop" in line for line in r.log)


def test_a_configured_worker_whose_template_is_not_installed_does_not_hold_the_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch, systemd=NotLoadedSystemd([U1_UNITS[1]]))
    r.systemd.condition_held = {U1_UNITS[1]}  # start exits 0 but the unit never runs
    r.poll(True)
    assert r.systemd.running == {U1_UNITS[0]}
    r.poll(False)
    r.poll(False, GRACE)
    assert r.systemd.running == set() and r.state()["phase"] == od.IDLE


def test_a_broken_edit_after_a_refusal_does_not_bring_the_printer_back_under_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(False)
    cfg.write_text("access: offline")
    os.utime(cfg, ns=(10**9, 10**9))

    def refuse(name: str) -> od.Target:
        raise od.NotSupervised(f"{name}: not a supervised printer", [str(cfg)])

    def broken(_name: str) -> od.Target:
        raise UsageError("printers.yaml: bad indent", command="printer ondemand")

    _check(r, refuse)
    cfg.write_text("access: offline\n  typo")
    os.utime(cfg, ns=(2 * 10**9, 2 * 10**9))
    asked = len(r.asked)
    for _ in range(5):
        _check(r, broken)
    assert len(r.asked) == asked  # still not contacted
    assert od.read_status("u1", r.path)["config_kept"] is False


def test_every_watchable_adapter_has_an_on_demand_probe() -> None:
    """A watch adapter without a probe would get a timer whose check refuses it for ever."""
    from printer_ops.supervise_units import WATCH_ADAPTERS

    assert set(WATCH_ADAPTERS) <= set(od.PROBES)


def test_an_unreadable_config_is_logged_once_and_backed_off_without_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)

    def broken(_name: str) -> od.Target:
        raise UsageError(
            "printers.yaml: line 3: bad indent", command="printer ondemand"
        )

    def run(when: float) -> int:
        r.now = when
        return od.run_check(
            "u1",
            path=r.path,
            resolver=broken,
            probe=r._probe,
            runner=r.systemd,
            clock=lambda: r.now,
            log=r.log.append,
        )

    assert [run(T0 + 30 * i) for i in range(1, 17)] == [0] * 16
    assert len(r.log) == 1 and "cannot read the printer configuration" in r.log[0]
    assert r.asked == [] and r.systemd.calls == []  # nothing was asked, nothing started
    status = od.read_status("u1", r.path)  # no check ran, but the reason is not lost
    assert status["phase"] == "" and "bad indent" in status["config_error"]


def test_a_failed_reload_keeps_the_old_target_and_is_retried_once_per_backoff_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(False)
    cfg.write_text("broken")
    os.utime(cfg, ns=(10**9, 10**9))
    calls = []

    def broken(name: str) -> od.Target:
        calls.append(name)
        raise UsageError("bad yaml", command="printer ondemand")

    for _ in range(3):
        r.now += POLL
        od.run_check(
            "u1",
            path=r.path,
            resolver=broken,
            probe=r._probe,
            runner=r.systemd,
            clock=lambda: r.now,
            log=r.log.append,
        )
    assert calls == ["u1"]  # tried once for this edit
    assert sum("keeping the old one" in line for line in r.log) == 1
    assert len(r.asked) == 4  # the old target kept asking the printer
    status = od.read_status("u1", r.path)
    assert status["config_error"] == "bad yaml" and status["config_kept"] is True


def test_restoring_the_original_config_with_its_old_mtime_clears_the_kept_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    original = cfg.stat().st_mtime_ns
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(False)
    cfg.write_text("broken")
    os.utime(cfg, ns=(10**9, 10**9))

    def broken(name: str) -> od.Target:
        raise UsageError("bad yaml", command="printer ondemand")

    r.now += POLL
    od.run_check(
        "u1",
        path=r.path,
        resolver=broken,
        probe=r._probe,
        runner=r.systemd,
        clock=lambda: r.now,
        log=r.log.append,
    )
    assert od.read_status("u1", r.path)["config_kept"] is True
    cfg.write_text("a")
    os.utime(cfg, ns=(original, original))
    r.poll(False)
    status = od.read_status("u1", r.path)
    assert status["config_kept"] is False and not status["error"]


def test_a_changed_context_whose_resolution_fails_drops_the_cached_target_and_backs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THREED_FLEET="" disables the fleet layer: the cached address belongs to another configuration and must not
    be asked or have workers started for it. Resolution is retried with the usual backoff, not at every check."""
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.setenv("THREED_FLEET", "/fleet/a.yaml")
    r.poll(True)
    assert r.systemd.running == set(U1_UNITS)
    monkeypatch.setenv("THREED_FLEET", "")
    calls: list[str] = []

    def broken(name: str) -> od.Target:
        calls.append(name)
        raise UsageError("fleet supervision is disabled", command="printer ondemand")

    def run() -> None:
        r.now += POLL
        od.run_check(
            "u1",
            path=r.path,
            resolver=broken,
            probe=r._probe,
            runner=r.systemd,
            clock=lambda: r.now,
            log=r.log.append,
        )

    asked = len(r.asked)
    for _ in range(3):  # inside one backoff period
        run()
    assert len(r.asked) == asked  # the old address was not asked again
    assert len(calls) == od.MAX_FAILS  # then the backoff holds the resolver back
    assert not any("keeping the old one" in line for line in r.log)
    r.now += od.BACKOFF_S
    run()
    assert len(calls) == od.MAX_FAILS + 1


def test_an_error_from_a_hand_run_in_another_context_clears_when_the_good_context_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.setenv("THREED_FLEET", "/fleet/a.yaml")
    r.poll(False)

    def broken(_name: str) -> od.Target:
        raise UsageError("fleet file not found", command="printer ondemand")

    monkeypatch.setenv(
        "THREED_FLEET", "/fleet/b.yaml"
    )  # a manual check with a bad override
    r.now += POLL
    od.run_check(
        "u1",
        path=r.path,
        resolver=broken,
        probe=r._probe,
        runner=r.systemd,
        clock=lambda: r.now,
        log=r.log.append,
    )
    assert "not found" in od.read_status("u1", r.path)["config_error"]
    monkeypatch.setenv(
        "THREED_FLEET", "/fleet/a.yaml"
    )  # the timer's own context is fine again
    r.poll(False)
    assert od.read_status("u1", r.path)["config_error"] == ""
    assert r.resolved == 2  # resolved again for the returning context


def _check_with(
    r: Rig, resolver: Callable[[str], od.Target], advance: float = POLL
) -> None:
    r.now += advance
    od.run_check(
        "u1",
        path=r.path,
        grace_s=r.grace,
        resolver=resolver,
        probe=r._probe,
        runner=r.systemd,
        clock=lambda: r.now,
        stray_due=lambda: False,
        log=r.log.append,
    )


def _failing(_name: str) -> od.Target:
    raise UsageError("fleet file not found", command="printer ondemand")


def test_workers_in_their_grace_period_are_stopped_even_when_the_configuration_became_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.setenv("THREED_FLEET", "/fleet/a.yaml")
    r.poll(True)
    r.poll(False)  # the print ended: grace
    assert r.state()["phase"] == od.GRACE and r.systemd.running == set(U1_UNITS)
    monkeypatch.setenv(
        "THREED_FLEET", ""
    )  # the context changes to one that cannot be resolved
    asked = len(r.asked)
    for _ in range(3):
        _check_with(r, _failing, GRACE / 2)
    assert r.systemd.running == set()  # cleaned up without any configuration
    assert r.state()["phase"] == od.IDLE
    assert len(r.asked) == asked  # and nothing was asked at the old address


def test_a_failed_reload_is_retried_after_the_backoff_not_remembered_as_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "printers.yaml"
    cfg.write_text("a")
    r = Rig(tmp_path, monkeypatch, files=(str(cfg),))
    r.poll(False)
    cfg.write_text("longer")
    os.utime(cfg, ns=(10**9, 10**9))
    _check_with(r, _failing)  # one transient failure
    r.target = target(host="10.0.0.9", files=(str(cfg),))
    _check_with(r, r._resolve, od.BACKOFF_S)  # the retry time has passed
    assert r.state()["target"]["host"] == "10.0.0.9"
    assert "config_error" not in r.state()


def test_a_backoff_set_by_a_bad_context_does_not_hold_back_the_good_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.setenv("THREED_FLEET", "/fleet/a.yaml")
    r.poll(False)
    monkeypatch.setenv("THREED_FLEET", "/fleet/bad.yaml")
    for _ in range(od.MAX_FAILS):
        _check_with(r, _failing)
    assert "retry_at" in r.state()
    asked = len(r.asked)
    monkeypatch.setenv(
        "THREED_FLEET", "/fleet/a.yaml"
    )  # the timer's own context, inside the backoff period
    r.poll(True)
    assert len(r.asked) == asked + 1 and r.systemd.running == set(U1_UNITS)


def test_a_check_that_backs_off_remembers_when_it_really_asked_the_printer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)
    for _ in range(od.MAX_FAILS):
        r.poll(None)
    asked = r.asked[-1]
    assert od.read_status("u1", r.path)["last_probe_mono"] == pytest.approx(asked)
    r.poll(
        None
    )  # backing off: no probe, nothing written, the answer stays that of the real probe
    assert r.asked[-1] == asked
    assert od.read_status("u1", r.path)["last_probe_mono"] == pytest.approx(asked)
    r.poll(False, od.BACKOFF_S)  # the printer is back: no deadline left
    assert od.read_status("u1", r.path)["last_probe_mono"] is None


def test_a_check_that_overlaps_another_one_exits_at_once_and_touches_nothing(
    rig: Rig,
) -> None:
    """A hand-run `3d printer ondemand NAME` can overlap the timer's check: an older check that read an expired
    grace state must not stop workers a newer one just started (the review of #747 reproduced exactly that)."""
    import fcntl

    rig.poll(True)  # state: active, workers running
    fd = od._lock(rig.path)
    assert fd is not None
    try:
        calls, writes, asked = len(rig.systemd.calls), rig.writes, len(rig.asked)
        assert (
            rig.poll(False, GRACE * 2) == od.EX_TEMPFAIL
        )  # would be a stop; the lock is held by "another check": a failed run on purpose
        assert (len(rig.systemd.calls), rig.writes, len(rig.asked)) == (
            calls,
            writes,
            asked,
        )
        assert (
            rig.systemd.running == set(U1_UNITS) and rig.state()["phase"] == od.ACTIVE
        )
        assert od._lock(rig.path) is None  # a second locker is refused, not blocked
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    rig.poll(False)  # the lock is free again: the check runs
    assert rig.state()["phase"] == od.GRACE


def test_an_idle_check_does_not_write_its_lock_after_the_first_time(rig: Rig) -> None:
    rig.poll(False)
    lock = Path(rig.path + ".lock")
    before = lock.stat().st_mtime_ns
    rig.poll(False)
    assert lock.stat().st_mtime_ns == before and rig.writes == 1


def test_a_changed_fleet_selection_or_config_directory_invalidates_the_cached_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.setenv("THREED_FLEET", "/fleet/a.yaml")
    r.poll(False)
    r.poll(False)
    assert r.resolved == 1  # cached
    monkeypatch.setenv(
        "THREED_FLEET", "/fleet/b.yaml"
    )  # the unit's environment changed, files did not
    r.poll(False)
    assert r.resolved == 2
    monkeypatch.setenv("THREED_FLEET", "")
    r.poll(False)
    assert r.resolved == 3
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other"))
    r.poll(False)
    assert r.resolved == 4
    monkeypatch.setenv(
        "REPO_ROOT", str(tmp_path / "checkout-b")
    )  # the default fleet file lives in the checkout
    r.poll(False)
    assert r.resolved == 5


def test_a_hand_run_through_bin_3d_is_the_same_context_as_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPO_ROOT", raising=False)
    unit = od._context()  # the unit sets no REPO_ROOT
    monkeypatch.setenv("REPO_ROOT", str(Path(od.__file__).resolve().parents[2]))  # what bin/3d sets
    assert od._context() == unit


def test_a_changed_working_directory_or_unset_versus_empty_fleet_invalidates_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path, monkeypatch)
    monkeypatch.delenv("THREED_FLEET", raising=False)
    r.poll(False)
    r.poll(False)
    assert r.resolved == 1
    monkeypatch.setenv(
        "THREED_FLEET", ""
    )  # disables the fleet layer: not the same as unset
    r.poll(False)
    assert r.resolved == 2
    other = tmp_path / "project-b"
    other.mkdir()
    monkeypatch.chdir(other)  # a manual check from another project directory
    r.poll(False)
    assert r.resolved == 3


def test_changing_the_grace_applies_to_a_grace_period_already_running() -> None:
    m = od.Machine(300.0)
    m.observe(0, True)
    m.observe(100, False)  # the grace started at 100
    m.grace_s = 0.0  # the owner reran with --grace 0
    assert m.observe(101, False) == od.STOP
    m = od.Machine(10.0)
    m.observe(0, True)
    m.observe(100, False)
    m.grace_s = 500.0  # extended: the old, shorter deadline no longer stops the workers
    assert m.observe(200, False) is None
    assert m.observe(600, False) == od.STOP


def test_two_printers_never_share_a_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert od.state_path("u1") != od.state_path("k1se")
    assert od.state_path("u1").startswith(str(tmp_path / "run" / "3d-cli" / "ondemand"))


def test_the_stray_look_is_one_check_in_about_twenty_whatever_the_timer_interval() -> (
    None
):
    looks = sum(od._stray_due() for _ in range(20000))
    assert 0.03 * 20000 < looks < 0.07 * 20000


# ---- the question ------------------------------------------------------------------------------


@contextmanager
def moonraker(state: str = "standby", **over: object) -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker(status=u1_status(state))
    for k, v in over.items():
        setattr(fake, k, v)
    with fake.serve():
        yield fake


@pytest.mark.parametrize(
    ("klipper", "expected"),
    [
        ("standby", False),
        ("printing", True),
        ("paused", True),
        ("complete", False),
        ("cancelled", False),
        ("error", False),
        ("something-new", False),
    ],
)
def test_moonraker_probe_reads_print_stats_state(klipper: str, expected: bool) -> None:
    with moonraker(klipper) as fake:
        assert od.probe_moonraker(target(port=fake.port)) is expected


def test_moonraker_probe_sends_exactly_one_small_get() -> None:
    with moonraker("printing") as fake:
        od.probe_moonraker(target(port=fake.port))
    # (the fake drops the value-less `?print_stats&webhooks` query: one GET on the query endpoint is the point)
    assert [(r.method, r.path) for r in fake.requests] == [
        ("GET", "/printer/objects/query")
    ]


def test_klipper_not_ready_means_no_print_even_when_print_stats_says_printing() -> None:
    with moonraker("printing") as fake:
        fake.status["webhooks"]["state"] = "shutdown"
        assert od.probe_moonraker(target(port=fake.port)) is False


def test_moonraker_up_with_klipper_down_is_an_answer_not_an_outage() -> None:
    with moonraker(
        "printing", query_error=503
    ) as fake:  # Moonraker answers 503 while Klippy is disconnected
        assert od.probe_moonraker(target(port=fake.port)) is False


def test_other_http_errors_and_a_closed_port_are_unreachable() -> None:
    with (
        moonraker(query_error=500) as fake,
        pytest.raises(od.Unreachable, match="HTTP 500"),
    ):
        od.probe_moonraker(target(port=fake.port))
    with pytest.raises(od.Unreachable):
        od.probe_moonraker(target(port=free_port()))


@contextmanager
def raw_server(reply: bytes, *, hold_s: float = 0.0) -> Iterator[int]:
    """A TCP server that answers every connection with `reply` (after `hold_s`) and hangs up."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def serve() -> None:
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                conn.recv(4096)
                stop.wait(hold_s)
                with contextlib.suppress(OSError):
                    conn.sendall(reply)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield int(srv.getsockname()[1])
    finally:
        stop.set()
        t.join(2)
        srv.close()


def test_a_chunked_reply_from_a_proxy_is_read() -> None:
    body = b'{"result": {"status": {"print_stats": {"state": "printing"}, "webhooks": {"state": "ready"}}}}'
    reply = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n%s\r\n0\r\n\r\n"
        % (len(body), body)
    )
    with raw_server(reply) as port:
        assert od.probe_moonraker(target(port=port)) is True


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"SSH-2.0-OpenSSH_9.6\r\n",
        b"HTTP/1.0 200 OK\r\n\r\nnot json",
        b'HTTP/1.0 200 OK\r\n\r\n{"result": {}}',
    ],
)
def test_garbage_is_unreachable_not_a_verdict(reply: bytes) -> None:
    with raw_server(reply) as port, pytest.raises(od.Unreachable):
        od.probe_moonraker(target(port=port))


def test_a_server_that_never_answers_times_out_inside_the_deadline() -> None:
    with (
        raw_server(b"", hold_s=5.0) as port,
        pytest.raises(od.Unreachable, match="timed out"),
    ):
        od.http_get("127.0.0.1", port, "/", timeout=0.3)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, False), (1, True), (5, True), (7, True), (2, False), (4, False)],
)
def test_creality_probe_maps_the_state_codes_of_the_stock_firmware(
    code: int, expected: bool
) -> None:
    fake = FakeCreality(state=k1_state(code))
    with fake.serve():
        assert (
            od.probe_creality(target(adapter="creality-stock", ws_port=fake.ws_port))
            is expected
        )


def test_creality_probe_without_a_state_in_the_push_is_unreachable_not_idle() -> None:
    fake = FakeCreality(state={"hostname": "K1SE-85B6"})
    with fake.serve(), pytest.raises(od.Unreachable, match="pushed no state"):
        od.probe_creality(target(adapter="creality-stock", ws_port=fake.ws_port))
    with pytest.raises(od.Unreachable):
        od.probe_creality(target(adapter="creality-stock", ws_port=free_port()))


# ---- what to supervise -------------------------------------------------------------------------

FLEET = """
p1:
  model: Test Model
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 4711
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
  watch:
    vision: {camera: {kind: snapshot-url, url: "http://127.0.0.1:1/snap"}}
p2:
  model: Test Model
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 4712
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
n1:
  model: Test Model
  owner: neighbour
  access: read-only
  adapter: moonraker
  host: 127.0.0.1
  port: 4713
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
"""


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fleet file (p1 with a camera, p2 without, a neighbour's read-only n1) that THREED_FLEET points at; no
    printer-devices.yaml, no real fleet, no timing variables."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(od.ENV_GRACE, raising=False)
    path = tmp_path / "fleet.yaml"
    path.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(path))
    return path


def test_resolve_reads_address_workers_and_defaults_from_the_fleet(fleet: Path) -> None:
    t = od.resolve("p1")
    assert (t.name, t.adapter, t.host, t.port) == ("p1", "moonraker", "127.0.0.1", 4711)
    assert t.workers == ("watch", "vision")  # a camera: both workers
    assert od.grace_seconds() == od.DEFAULT_GRACE_S
    assert str(fleet) in t.files
    assert t.units() == ("3d-printer-watch@p1.service", "3d-printer-vision@p1.service")
    assert od.resolve("p2").workers == ("watch",)  # no camera: no vision worker


def test_the_grace_comes_from_the_environment_a_systemd_dropin_sets_and_the_flag_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(od.ENV_GRACE, "90")
    assert od.grace_seconds() == 90.0
    assert od.grace_seconds(0.0) == 0.0


@pytest.mark.parametrize("value", ["-5", "fast", "nan"])
def test_a_bad_grace_is_refused_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(od.ENV_GRACE, value)
    with pytest.raises(UsageError, match=od.ENV_GRACE):
        od.grace_seconds()


def test_resolve_refuses_a_printer_that_is_not_supervised(fleet: Path) -> None:
    with pytest.raises(od.NotSupervised, match="not a supervised printer") as exc:
        od.resolve("n1")
    from printer_ops.camera import MODELS_FILE

    assert "p1" in str(exc.value)
    assert str(MODELS_FILE) in exc.value.files


def test_a_target_survives_the_json_round_trip_through_the_state_file(
    fleet: Path,
) -> None:
    t = od.resolve("p1")
    assert od.Target.from_json(t.to_json()) == t


def test_resolve_watches_the_config_files_that_do_not_exist_yet(
    fleet: Path, tmp_path: Path
) -> None:
    """A printer-devices.yaml created later (a corrected address) must invalidate the cached target: its
    candidate paths are tracked even while absent."""
    from printer_ops.devices import search_paths

    t = od.resolve("p1")
    absent = [str(p) for p in search_paths()]
    assert not any(Path(p).exists() for p in absent)
    assert set(absent) <= set(t.files)
    seen = od.mtimes(t.files)
    assert not od.config_changed(t.files, seen)
    Path(absent[-1]).write_text("p1: {host: other}\n", encoding="utf-8")
    assert od.config_changed(t.files, seen)


def test_config_changed_sees_an_edit_a_deletion_and_a_new_file(tmp_path: Path) -> None:
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("x")
    t = target(files=(str(a), str(b)))
    seen = od.mtimes(t.files)
    assert not od.config_changed(t.files, seen)
    a.write_text("longer")
    assert od.config_changed(t.files, seen)
    seen = od.mtimes(t.files)
    b.write_text("new")
    assert od.config_changed(t.files, seen)
    seen = od.mtimes(t.files)
    a.unlink()
    assert od.config_changed(t.files, seen)
