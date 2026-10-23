"""The alert delivery chain must work by itself (#529).

2026-10-22 20:22:59 the U1 went PAUSED and nobody heard: the unit ran `watch --dry-run` ("delivery
held"), two dead subscriptions only logged a failure, the dry-run agent sink reported the alert as
taken, and `watch.tg_fallback` was off. These tests pin every link with fakes (tmux, omp, tg-ctl, tg,
clock): a verified-live subscriber gets it exactly once; a dead one is never sent to; no subscriber
-> the temporary agent starts (outside the unit's cgroup) and gets the alert and the frame; a failed
or unacknowledged start goes to the user through `tg` with the frame (and, for a critical finding,
after the automatic pause); every failure is counted where `agent list` / `subscriptions` show it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from errors import UsageError
from printer_ops import auto_pause, printer_agent, subscriptions, watch_run, watch_sinks
from printer_ops.printer_agent import (
    AgentUnavailable,
    PrinterAgents,
    PrinterAgentSink,
    agent_dir,
    session_name,
)
from printer_ops.subscriptions import DeliveryState, Registry, SubscriptionSink, make
from printer_ops.watch import CRITICAL, WARNING, Event
from printer_ops.watch_sinks import (
    FALLBACK,
    Dispatcher,
    OmpLive,
    TgSink,
    refuse_dry_run_service,
    stderr_is_journal,
)
from test_printer_subscribe import OTHER, Fake, agents, noodle


@pytest.fixture()
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def subscribe(session: str = "01a12cd6", **kw: Any) -> subscriptions.Subscription:
    return Registry().put(make(session=session, printers=["u1"], **kw))[0]


def frame_file(xdg: Path) -> str:
    frame = xdg / "u1-spaghetti.jpg"
    frame.write_bytes(b"\xff\xd8 not a real jpeg")
    return str(frame)


class Tg:
    """The `tg` CLI: records every call; `fail` = the first N calls exit 1."""

    def __init__(self, fail: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.fail > 0:
            self.fail -= 1
            return subprocess.CompletedProcess(cmd, 1, "", "tg: refused")
        return subprocess.CompletedProcess(cmd, 0, "OK tg#1", "")


class Chain:
    """u1 routed like `Dispatcher.add_settings` does it: subscriptions, the temporary agent, the
    emergency tg (with the DeliveryState ledger)."""

    def __init__(
        self,
        run: Fake,
        *,
        tg: Tg | None = None,
        clock: list[float] | None = None,
        pauser: Any = None,
    ) -> None:
        self.run = run
        self.tg = tg or Tg()
        self.state = DeliveryState()
        self.agents = agents(run, clock)
        self.agents.state = self.state
        self.now = [1000.0]  # the outgoing limiter's clock
        self.tg_sink = TgSink(runner=self.tg, clock=lambda: self.now[0])
        self.tg_sink.ledger = self.state
        self.d = Dispatcher(dedup_s=0)
        self.d.add(
            "u1",
            [
                SubscriptionSink(runner=run, state=self.state),
                PrinterAgentSink(agents=self.agents),
                self.tg_sink,
            ],
        )
        self.tg_sink.attach("u1", pauser=pauser)

    def process(self, *events: Event) -> list[Event]:
        got = self.d.process(list(events))
        self.agents.join(30)
        return got

    def counters(self) -> dict[str, int]:
        return dict(DeliveryState().read()["counters"].get("u1", {}))


# ---- live subscriber ------------------------------------------------------------------------


def test_a_live_subscriber_gets_exactly_one_send_and_nothing_else_fires(
    xdg: Path,
) -> None:
    subscribe()
    c = Chain(Fake())
    assert c.process(noodle(frame_file(xdg))) != []
    (send,) = c.run.sends()
    assert send[3] == "433553" and "01a12cd6-7b39" in send
    assert c.run.news() == [] and c.tg.calls == [] and c.counters() == {}


def test_dead_and_live_subscribers_together_only_the_live_one_is_sent_to(
    xdg: Path,
) -> None:
    dead = subscribe("deadbeef")
    live = subscribe("01a12cd6")
    c = Chain(Fake())
    c.process(noodle())
    (send,) = c.run.sends()
    assert send[3] == "433553"
    assert all("deadbeef" not in " ".join(call) for call in c.run.sends())
    assert c.run.news() == []  # a live subscriber took it: no agent
    assert c.counters() == {"subscriber_dead": 1}
    state = DeliveryState().read()["subscriptions"]
    assert "dead_since_ts" in state[dead.id] and "dead_since_ts" not in state[live.id]


# ---- dead subscriber -> temporary agent ------------------------------------------------------


def test_a_dead_subscriber_is_never_sent_to_and_the_agent_gets_the_alert_and_frame(
    xdg: Path,
) -> None:
    sub = subscribe("01a12cd6")
    frame = frame_file(xdg)
    run = Fake(hosts=[OTHER])  # the subscribed session is gone
    c = Chain(run)
    got = c.process(noodle(frame))
    assert got != []  # the agent took it
    assert (
        run.sends() == []
    )  # NOT ONE send to anyone: no session matched, the agent got it as its opening prompt
    (new,) = run.news()
    omp = new[new.index("--") + 1 :]
    assert omp[0] == "omp" and f"@{frame}" in omp
    assert "[3d printer alert -> temporary u1 agent]" in omp[3] and frame in omp[3]
    assert c.tg.calls == []
    assert c.counters() == {"subscriber_dead": 1}
    row = DeliveryState().read()["subscriptions"][sub.id]
    assert (
        row["dead_since"]
        and row["failed"] == 1
        and "no live omp session matches" in row["last_error"]
    )


def test_a_send_without_the_acceptance_receipt_counts_as_failed_and_falls_through(
    xdg: Path,
) -> None:
    sub = subscribe()
    run = Fake(
        receipt=False
    )  # exit 0, ok:true, but no `accepted` receipt for the request
    c = Chain(run)
    c.process(noodle())
    assert len(run.sends()) == 1  # it was tried once ...
    assert len(run.news()) == 1  # ... and did not count: the agent got the alert
    row = DeliveryState().read()["subscriptions"][sub.id]
    assert "was not acknowledged" in row["last_error"] and row.get("delivered", 0) == 0
    assert c.counters() == {"subscriber_send_failed": 1}


def test_a_send_that_times_out_is_not_delivered(xdg: Path) -> None:
    subscribe()
    run = Fake()

    def hanging(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["tg-ctl", "omp-live", "send"]:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return run(cmd, **kw)

    sink = SubscriptionSink(runner=hanging)
    assert sink.deliver([noodle()]) == []
    assert "no acknowledgement within 30 s" in str(sink.stats.last_error)


def test_a_broken_bridge_sends_nothing_counts_it_and_the_user_is_told(
    xdg: Path,
) -> None:
    """tg-ctl cannot tell who is live: nothing is sent blind, the agent (which also needs omp-live
    to be found) cannot take it either, so the emergency tg fires."""
    sub = subscribe()

    class Broken(Fake):
        def __call__(
            self, cmd: list[str], **kw: Any
        ) -> subprocess.CompletedProcess[str]:
            if cmd[:3] == ["tg-ctl", "omp-live", "status"]:
                return subprocess.CompletedProcess(cmd, 1, "", "bridge down")
            return super().__call__(cmd, **kw)

    broken = Broken()
    c = Chain(broken)
    c.process(noodle())
    assert broken.sends() == [] and broken.news() == []
    assert c.counters() == {
        "subscriber_unreachable": 1,
        "agent_start_failed": 1,
        "fallback_tg_sent": 1,
    }
    assert (
        "bridge down" in DeliveryState().read()["subscriptions"][sub.id]["last_error"]
    )
    (call,) = c.tg.calls
    assert call[-1].startswith("u1: possible spaghetti") and "агент" not in call[-1]


# ---- agent cannot start / does not answer -> tg ----------------------------------------------


def test_a_failed_spawn_is_retried_silently_then_one_short_tg_message_with_the_frame(
    xdg: Path,
) -> None:
    subscribe("deadbeef")
    frame = frame_file(xdg)
    c = Chain(Fake(tmux_ok=False))
    c.process(noodle(frame, sev=CRITICAL))
    (call,) = c.tg.calls
    assert call[:3] == ["tg", "--tag", "problem"] and call[3:5] == ["--photo", frame]
    text = call[-1]
    assert text == "u1: possible spaghetti on camera (score 0.83)"
    assert (
        "недоступен" not in text
        and "temporary agent" not in text
        and "tmux" not in text
    )
    assert c.counters() == {
        "subscriber_dead": 1,
        "agent_start_failed": 3,
        "fallback_tg_sent": 1,
    }
    assert DeliveryState().agent("u1")["start_failures"] == 3


def test_an_agent_that_never_comes_up_even_after_a_silent_restart_hands_the_alert_to_tg(
    xdg: Path,
) -> None:
    frame = frame_file(xdg)
    run = Fake(agent_comes_up=False)
    c = Chain(run)
    c.process(noodle(frame))
    (call,) = c.tg.calls  # after two acknowledgement windows, from the agent's thread
    assert call[-1].startswith("u1: possible spaghetti") and "--photo" in call
    assert len(run.news()) == 2  # the first start and the one silent restart
    assert session_name("u1") not in run.tmux  # the half-started session was stopped
    assert c.counters() == {"agent_not_acked": 2, "fallback_tg_sent": 1}


def test_tg_text_survives_a_refused_photo_or_tag(xdg: Path) -> None:
    tg = Tg(fail=1)
    sink = TgSink(runner=tg)
    sink.deliver([noodle(frame_file(xdg), sev=CRITICAL)])
    assert (
        len(tg.calls) == 2 and "--photo" in tg.calls[0] and "--photo" not in tg.calls[1]
    )
    assert tg.calls[1][0] == "tg" and tg.calls[1][1].startswith(
        "u1: possible spaghetti"
    )


def test_a_dead_tg_is_counted_and_raised_never_swallowed(xdg: Path) -> None:
    state = DeliveryState()
    sink = TgSink(runner=Tg(fail=9))
    sink.ledger = state
    with pytest.raises(RuntimeError, match="plain retry: tg exit 1"):
        sink.deliver([noodle()])
    assert state.read()["counters"]["u1"] == {"fallback_tg_failed": 1}


def test_tg_sends_at_most_three_frames_that_exist(xdg: Path) -> None:
    frames = []
    for i in range(5):
        p = xdg / f"f{i}.jpg"
        p.write_bytes(b"x")
        frames.append(str(p))
    e = Event(
        "u1",
        "vision_part_detached",
        CRITICAL,
        "u1: detached",
        {"frames": [*frames, str(xdg / "gone.jpg")]},
    )
    assert watch_sinks.tg_frames([e]) == frames[:3]


# ---- start limits: backoff, crash-loop guard ---------------------------------------------------


def test_failed_starts_back_off_30_60_120_up_to_the_ten_minute_cap_and_a_good_start_resets(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        printer_agent, "START_RETRY_DELAYS_S", ()
    )  # one failure per delivery
    run = Fake(tmux_ok=False)
    clock = [1000.0]
    a = agents(run, clock)
    windows = []
    for _ in range(7):
        windows.append(a.start_window_s("u1"))
        with pytest.raises(AgentUnavailable, match="tmux new-session failed"):
            a.deliver("u1", [noodle()])
        clock[0] += 700
    assert windows == [600, 30, 60, 120, 240, 480, 600]
    run.tmux_ok = True
    a.deliver("u1", [noodle()])
    a.join(10)
    assert (
        a.start_window_s("u1") == 600
        and DeliveryState().agent("u1")["start_failures"] == 0
    )


def test_a_good_start_that_died_is_not_restarted_for_ten_minutes(xdg: Path) -> None:
    run = Fake()
    clock = [1000.0]
    a = agents(run, clock)
    a.deliver("u1", [noodle()])
    a.join(10)
    run.tmux.clear()  # the agent died on its own (no stop: the restart slot is kept)
    run.hosts = [h for h in run.hosts if h.get("cwd") != str(agent_dir("u1"))]
    clock[0] += 300
    with pytest.raises(AgentUnavailable, match="restart limit") as exc:
        a.deliver("u1", [noodle()])
    assert exc.value.counter == "agent_restart_limit"
    assert DeliveryState().read()["counters"]["u1"] == {"agent_restart_limit": 1}
    clock[0] += 301
    assert "starting the u1 agent" in a.deliver("u1", [noodle()])


def test_the_spawn_runs_in_its_own_systemd_scope_so_a_unit_stop_cannot_kill_it(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmux 3.6 puts only the PANE PROCESS in its own scope: the tmux SERVER stays in the unit's
    cgroup and takes omp with it when the unit stops (proved in docs/research). The server must
    therefore be started through `systemd-run --user --scope`."""
    monkeypatch.setattr(
        printer_agent,
        "find_tool",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )
    run = Fake()
    a = PrinterAgents(
        runner=run,
        tmux="tmux",
        omp="omp",
        ready_timeout_s=10.0,
        clock=lambda: 1.0,
        sleep=lambda s: None,
    )
    a.deliver("u1", [noodle()])
    a.join(10)
    new = next(c for c in run.raw if "new-session" in c)
    assert new[:6] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "tmux",
    ]
    assert new[6:8] == ["-L", printer_agent.tmux_socket()]
    pipe = next(c for c in run.raw if "pipe-pane" in c)
    assert "systemd-run" not in pipe[0]  # only the server-creating call is scoped
    bare = PrinterAgents(
        runner=Fake(), tmux="tmux", omp="omp", scope=False, ready_timeout_s=10.0
    )
    assert bare.scope is False


# ---- OmpLive: fresh liveness, receipts, bounded ----------------------------------------------


def test_omplive_requires_the_receipt_for_this_request_and_session() -> None:
    host = {"pid": 7, "sessionId": "s-1"}

    def reply(**over: Any) -> OmpLive:
        def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            rid = cmd[cmd.index("--request-id") + 1]
            body = {
                "ok": True,
                "accepted": True,
                "status": "accepted",
                "requestId": rid,
                "sessionId": "s-1",
            }
            body.update(over)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

        return OmpLive(runner=run)

    reply().send(host, "hello")
    for bad in (
        {"accepted": False},
        {"requestId": "someone-else"},
        {"sessionId": "s-2"},
        {"ok": False},
    ):
        with pytest.raises(RuntimeError):
            reply(**bad).send(host, "hello")
    silent = OmpLive(
        runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    with pytest.raises(RuntimeError, match="not acknowledged"):
        silent.send(host, "hello")


def test_omplive_status_is_bounded_and_drops_non_hosts() -> None:
    seen: dict[str, Any] = {}

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kw)
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps([{"pid": 1, "sessionId": "a"}, "junk", 3]), ""
        )

    assert OmpLive(runner=run).sessions() == [{"pid": 1, "sessionId": "a"}]
    assert seen["timeout"] == OmpLive.STATUS_TIMEOUT_S <= 10.0

    def hang(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    with pytest.raises(RuntimeError, match="did not answer within 10 s"):
        OmpLive(runner=hang).sessions()
    with pytest.raises(RuntimeError, match="printed no JSON"):
        OmpLive(
            runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "<html>", "")
        ).sessions()


def test_liveness_is_asked_afresh_for_every_delivery(xdg: Path) -> None:
    subscribe()
    run = Fake()
    sink = SubscriptionSink(runner=run)
    assert sink.deliver([noodle()]) != []
    run.hosts = [OTHER]  # the session died between two alerts
    assert sink.deliver([noodle(sev=CRITICAL)]) == []
    statuses = [c for c in run.calls if c[:3] == ["tg-ctl", "omp-live", "status"]]
    assert len(statuses) == 2 and len(run.sends()) == 1


# ---- stale subscriptions: dead_since, prune, listing ------------------------------------------


def test_a_session_dead_longer_than_prune_after_days_is_removed_with_a_log_line(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old, fresh = subscribe("deadbeef"), subscribe("cafef00d")
    state = DeliveryState()
    now = 10_000_000.0
    state.mark_dead(old.id, now - 4 * 86400)
    state.mark_dead(fresh.id, now - 1 * 86400)
    sink = SubscriptionSink(runner=Fake(), clock=lambda: now)
    sink.maintain()
    assert [s.id for s in Registry().load()] == [fresh.id]
    err = capsys.readouterr().err
    assert f"pruned {old.id}" in err and "dead for 4.0 d (limit 3 d" in err
    data = state.read()
    assert data["pruned"][-1]["id"] == old.id and old.id not in data["subscriptions"]


def test_prune_after_days_is_configurable_off_and_survives_registry_edits(
    xdg: Path,
) -> None:
    sub = subscribe("deadbeef")
    reg = Registry()
    assert reg.prune_after_days() == 3.0
    reg.path.write_text(reg.path.read_text() + "prune_after_days: 0\n")
    subscribe("cafef00d")  # a registry edit must keep the key
    assert (
        "prune_after_days: 0" in reg.path.read_text() and reg.prune_after_days() == 0.0
    )
    now = 10_000_000.0
    DeliveryState().mark_dead(sub.id, now - 30 * 86400)
    SubscriptionSink(runner=Fake(), clock=lambda: now).maintain()
    assert sub.id in [s.id for s in Registry().load()]  # 0 = never
    reg.path.write_text(
        reg.path.read_text().replace("prune_after_days: 0", "prune_after_days: -1")
    )
    with pytest.raises(subscriptions.RegistryError, match="prune_after_days"):
        reg.load()


def test_a_session_that_comes_back_is_no_longer_dead(xdg: Path) -> None:
    sub = subscribe()
    state = DeliveryState()
    state.mark_dead(sub.id, 5.0)
    SubscriptionSink(runner=Fake(), state=state).deliver(
        [noodle()]
    )  # MAIN is live again
    assert "dead_since_ts" not in state.read()["subscriptions"][sub.id]


def test_a_failing_bridge_never_marks_anything_dead_or_prunes(xdg: Path) -> None:
    sub = subscribe()

    def broken(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "down")

    state = DeliveryState()
    state.mark_dead(sub.id, 1.0)
    SubscriptionSink(runner=broken, state=state, clock=lambda: 9e9).maintain()
    assert [s.id for s in Registry().load()] == [
        sub.id
    ]  # cannot tell who is live: nothing is pruned


def test_subscriptions_list_flags_dead_entries_and_shows_the_chain_counters(
    xdg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = subscribe("deadbeef")
    state = DeliveryState()
    state.mark_dead(sub.id, 1.0)
    state.bump("u1", "subscriber_dead", 3)
    state.bump("u1", "agent_start_failed")

    class DeadHere(OmpLive):  # the real class, so OmpLive.resolve still exists
        def __init__(self) -> None:
            super().__init__(runner=Fake(hosts=[OTHER]))

    monkeypatch.setattr(subscriptions, "OmpLive", DeadHere)
    assert subscriptions.run("subscriptions", []) == 0
    out = capsys.readouterr().out
    assert (
        "live: NO" in out
        and "STALE: dead since" in out
        and "removed automatically after 3 d" in out
    )
    assert "delivery failures u1: agent_start_failed 1, subscriber_dead 3" in out


def test_agent_list_shows_the_chain_counters_even_with_no_agent_running(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    DeliveryState().bump("u1", "fallback_tg_sent", 2)
    assert printer_agent.run(["list"]) == 0
    out = capsys.readouterr().out
    assert (
        "no temporary printer agents" in out
        and "delivery failures u1: fallback_tg_sent 2" in out
    )
    assert printer_agent.run(["list", "--json"]) == 0


# ---- a service never holds alerts --------------------------------------------------------------


def test_stderr_is_journal_needs_the_descriptor_to_match(tmp_path: Path) -> None:
    f = tmp_path / "stream"
    f.write_text("")
    fd = os.open(f, os.O_WRONLY)
    try:
        st = os.fstat(fd)
        assert (
            stderr_is_journal({"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino}"}, fd)
            is True
        )
        assert (
            stderr_is_journal({"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino + 1}"}, fd)
            is False
        )  # inherited, not ours
        assert stderr_is_journal({}, fd) is False
        assert stderr_is_journal({"JOURNAL_STREAM": "garbage"}, fd) is False
    finally:
        os.close(fd)


def test_dry_run_is_refused_when_stderr_is_the_journal(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {
        "sinks": {"stdout": True, "exec": [], "steer": [], "tg_fallback": True},
        "min_severity": "info",
        "dedup_s": 0.0,
    }
    monkeypatch.setattr(watch_sinks, "stderr_is_journal", lambda *a, **k: True)
    with pytest.raises(UsageError, match="this process is a service") as exc:
        Dispatcher().add_settings("u1", settings, dry_run=True)
    assert any("Remove --dry-run" in r for r in exc.value.remediation)
    with pytest.raises(UsageError):
        refuse_dry_run_service()
    Dispatcher().add_settings(
        "u1", settings, dry_run=False
    )  # a real delivery is never refused
    monkeypatch.setattr(watch_sinks, "stderr_is_journal", lambda *a, **k: False)
    Dispatcher().add_settings(
        "u1", settings, dry_run=True
    )  # a terminal dry run is fine


def test_a_refused_dry_run_service_tells_the_user_once_an_hour(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit restarts every 15-30 s: the refusal is in the journal every time, the phone hears
    about it once an hour, and a dead tg never turns the refusal into something else."""
    monkeypatch.setattr(watch_sinks, "stderr_is_journal", lambda *a, **k: True)
    tg = Tg()
    with pytest.raises(UsageError):
        refuse_dry_run_service("u1", runner=tg)
    (call,) = tg.calls
    assert call[0].endswith("tg") and call[1:3] == ["--tag", "problem"]
    assert "[u1]" in call[-1]
    assert "--dry-run" in call[-1] and "NOT being delivered" in call[-1]
    for _ in range(3):  # the restarts of the next hour
        with pytest.raises(UsageError):
            refuse_dry_run_service("u1", runner=tg)
    assert len(tg.calls) == 1
    assert (
        watch_sinks.notify_held_service("u1", runner=tg, now=time.time() + 3601) is True
    )
    assert len(tg.calls) == 2
    broken = Tg(fail=9)
    assert (
        watch_sinks.notify_held_service("u1", runner=broken, now=time.time() + 9000)
        is False
    )
    with pytest.raises(UsageError):
        refuse_dry_run_service("u1", runner=Tg(fail=9))


def test_a_test_never_messages_the_user_about_a_held_service(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("tg must not run from a test")

    monkeypatch.setattr(subprocess, "run", boom)
    assert watch_sinks.notify_held_service("u1") is False


# ---- the automatic pause is the LAST step -----------------------------------------------------


class Pauser:
    """auto_pause.AutoPauser stand-in: logs into `order`, pauses (or not) for any event."""

    def __init__(self, order: list[str], outcome: str = "paused") -> None:
        self.order, self.outcome, self.calls = order, outcome, 0

    def pause(self, events: list[Event]) -> auto_pause.PauseResult | None:
        self.calls += 1
        self.order.append("pause")
        return auto_pause.PauseResult(
            "u1",
            self.outcome,
            "pretend",
            "00006D",
            "a.gcode",
            events[0],
            None,
            "http://192.168.0.26",
        )


class OrderedTg(Tg):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.order.append("tg")
        return super().__call__(cmd, **kw)


def test_the_pause_only_happens_after_every_channel_failed_and_before_the_tg_message(
    xdg: Path,
) -> None:
    order: list[str] = []
    pauser = Pauser(order)
    # 1. a live subscriber takes it: no pause, no tg
    subscribe()
    c = Chain(Fake(), tg=OrderedTg(order), pauser=pauser)
    c.process(noodle(sev=CRITICAL))
    assert pauser.calls == 0 and order == []
    # 2. the subscriber is dead but the agent starts: still no pause, no tg
    Registry().remove(lambda s: True)
    subscribe("deadbeef")
    c = Chain(Fake(), tg=OrderedTg(order), pauser=pauser)
    c.process(noodle(sev=CRITICAL))
    assert pauser.calls == 0 and order == []
    # 3. nobody can judge: pause FIRST, then the message that says so
    c = Chain(Fake(tmux_ok=False), tg=OrderedTg(order), pauser=pauser)
    c.process(noodle(sev=CRITICAL))
    assert order == ["pause", "tg"] and pauser.calls == 1
    text = c.tg.calls[0][-1]
    assert (
        "PAUSED the print (a.gcode)" in text
        and "Resume:" in text
        and "http://192.168.0.26" in text
    )
    assert c.counters()["auto_pause_paused"] == 1


def test_a_crashing_pauser_never_stops_the_words_from_reaching_the_user(
    xdg: Path,
) -> None:
    class Boom:
        def pause(self, events: list[Event]) -> None:
            raise OSError("moonraker exploded")

    c = Chain(Fake(tmux_ok=False), pauser=Boom())
    c.process(noodle(sev=CRITICAL))
    (call,) = c.tg.calls
    assert "automatic pause crashed" in call[-1] and "NOT paused" in call[-1]
    assert (
        c.counters()["auto_pause_failed"] == 1 and c.counters()["fallback_tg_sent"] == 1
    )


def test_no_pause_for_a_batch_without_a_qualifying_event_but_the_message_still_goes(
    xdg: Path,
) -> None:
    class Quiet:
        def pause(self, events: list[Event]) -> None:
            return None

    c = Chain(Fake(tmux_ok=False), pauser=Quiet())
    c.process(noodle(sev=WARNING))
    (call,) = c.tg.calls
    assert "PAUSED" not in call[-1] and call[-1].startswith("u1: possible spaghetti")


def test_the_dispatcher_records_the_pause_as_an_event_and_wires_the_policy(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base: dict[str, Any] = {
        "sinks": {"stdout": False, "exec": [], "steer": [], "tg_fallback": True},
        "min_severity": "info",
        "dedup_s": 0.0,
        "agent_ack_s": 33.0,
        "message_cooldown_s": 120.0,
    }
    on, off = Dispatcher(), Dispatcher()
    on.add_settings("u1", {**base, "pause_policy": "critical-only"})
    off.add_settings("u1", {**base, "pause_policy": "off"})
    tg_on, tg_off = on.routes["tg"].sink, off.routes["tg"].sink
    assert isinstance(tg_on, TgSink) and isinstance(tg_off, TgSink)
    assert (
        isinstance(tg_on.pausers.get("u1"), auto_pause.AutoPauser)
        and "u1" not in tg_off.pausers
    )
    assert (
        tg_on.ledger is not None
        and tg_on.on_pause is not None
        and tg_on.cooldowns == {"u1": 120.0}
    )
    agent = on.routes["printer-agent"].sink
    assert (
        isinstance(agent, PrinterAgentSink)
        and agent.agents is not None
        and agent.agents.ready_timeout_s == 33.0
    )
    # no tg route (tg_fallback false) -> no pauser can exist
    quiet = Dispatcher()
    quiet.add_settings(
        "u1",
        {
            **base,
            "sinks": {**base["sinks"], "tg_fallback": False},
            "pause_policy": "critical-only",
        },
    )
    assert "tg" not in quiet.routes
    # the recorded event lands in events.jsonl for the next agent and on stderr for the journal
    result = auto_pause.PauseResult(
        "u1", "paused", "pretend", "00006D", "a.gcode", noodle(sev=CRITICAL), None
    )
    assert tg_on.on_pause is not None
    tg_on.on_pause(result)
    line = json.loads((agent_dir("u1") / "events.jsonl").read_text().splitlines()[-1])
    assert line["kind"] == "auto_pause" and line["data"]["outcome"] == "paused"


def test_settings_refuse_a_pause_nobody_would_be_told_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printer_ops import devices

    monkeypatch.setattr(
        auto_pause, "resolve_policy", lambda device, configured, **kw: "critical-only"
    )
    dev = devices.Device(
        "u1", "moonraker", "h", 7125, None, tmp_path, watch={"tg_fallback": False}
    )
    assert (
        watch_run.settings_for(dev, {})["pause_policy"] == "off"
    )  # the default never pauses silently
    explicit = devices.Device(
        "u1",
        "moonraker",
        "h",
        7125,
        None,
        tmp_path,
        watch={"tg_fallback": False, "pause_without_agent": "critical-only"},
    )
    with pytest.raises(Exception, match="a pause nobody is told about"):
        watch_run.settings_for(explicit, {})
    on = devices.Device(
        "u1", "moonraker", "h", 7125, None, tmp_path, watch={"agent_ack_seconds": 45}
    )
    s = watch_run.settings_for(on, {})
    assert (
        s["pause_policy"] == "critical-only"
        and s["agent_ack_s"] == 45.0
        and s["sinks"]["tg_fallback"] is True
    )
    with pytest.raises(Exception, match="agent_ack_seconds"):
        watch_run.settings_for(
            devices.Device(
                "u1",
                "moonraker",
                "h",
                7125,
                None,
                tmp_path,
                watch={"agent_ack_seconds": 1},
            ),
            {},
        )


def test_the_tg_route_is_the_only_fallback_stage_sink() -> None:
    assert TgSink(runner=Tg()).stage == FALLBACK and TgSink(runner=Tg()).agent is False


# ---- silent restart: the user hears nothing about an agent that is merely restarting ------------


class FlakyTmux(Fake):
    """`fail` new-session calls fail; then `dead` starts succeed without an omp ever showing up."""

    def __init__(self, fail: int = 0, dead: int = 0) -> None:
        super().__init__()
        self.fail, self.dead, self.started = fail, dead, 0

    def _tmux(self, args: list[str]) -> tuple[str, int]:
        if args[0] == "new-session":
            self.started += 1
            if self.started <= self.fail:
                return "boom", 1
            self.agent_comes_up = self.started > self.fail + self.dead
        return super()._tmux(args)


def test_a_start_that_fails_twice_then_works_runs_one_agent_and_never_messages_the_user(
    xdg: Path,
) -> None:
    c = Chain(FlakyTmux(fail=2))
    assert c.process(noodle(sev=CRITICAL)) != []  # the agent took it
    assert c.tg.calls == []
    assert list(c.run.tmux) == [session_name("u1")] and len(c.run.news()) == 3
    assert c.counters() == {"agent_start_failed": 2}
    assert (
        DeliveryState().agent("u1")["start_failures"] == 0
    )  # the good start reset the backoff


def test_the_retries_wait_two_then_five_seconds_through_the_injected_sleep(
    xdg: Path,
) -> None:
    slept: list[float] = []
    a = agents(FlakyTmux(fail=2))
    a.sleep = slept.append
    a.deliver("u1", [noodle()])
    a.join(10)
    assert slept[:2] == [2.0, 5.0]


def test_three_failed_starts_send_exactly_one_short_tg_line(xdg: Path) -> None:
    c = Chain(FlakyTmux(fail=3))
    c.process(noodle(sev=CRITICAL))
    (call,) = c.tg.calls
    assert call[-1] == "u1: possible spaghetti on camera (score 0.83)"
    assert (
        c.counters()["agent_start_failed"] == 3
        and c.counters()["fallback_tg_sent"] == 1
    )


def test_an_agent_not_up_in_the_window_is_restarted_silently_and_nobody_is_told(
    xdg: Path,
) -> None:
    c = Chain(FlakyTmux(dead=1))
    assert c.process(noodle(sev=CRITICAL)) != []
    assert c.tg.calls == []
    assert len(c.run.news()) == 2 and list(c.run.tmux) == [session_name("u1")]
    assert (
        c.run.sends() == []
    )  # the new agent got the alert as its opening prompt, once
    assert c.counters() == {"agent_not_acked": 1}
    assert DeliveryState().agent("u1")["start_failures"] == 0


# ---- one short message per incident ------------------------------------------------------------


def incident(frame: str | None = None) -> list[Event]:
    """The three events of the 2026-10-22 U1 incident, as one Dispatcher batch."""
    data: dict[str, Any] = {"frame": frame} if frame else {}
    return [
        Event(
            "u1",
            "exception",
            CRITICAL,
            "u1: printer error 2 (detected noodle) (layer 41, 63%)",
            data,
            key="e",
        ),
        Event(
            "u1",
            "gcode_error",
            CRITICAL,
            "u1: G-code error: !! MCU 'mcu' shutdown: Timer too close\n"
            + "mcu dump " * 300,
            {"line": "!! raw dump"},
            key="g",
        ),
        Event(
            "u1",
            "state",
            WARNING,
            "u1: printing -> paused (layer 41, 63%)",
            {"to": "paused"},
            key="s",
        ),
    ]


def test_the_three_events_of_one_incident_become_one_short_message(xdg: Path) -> None:
    order: list[str] = []
    frame = frame_file(xdg)
    c = Chain(Fake(tmux_ok=False), tg=OrderedTg(order), pauser=Pauser(order))
    c.process(*incident(frame))
    (call,) = c.tg.calls
    assert call[1:5] == ["--tag", "problem", "--photo", frame]
    lines = call[-1].splitlines()
    assert 1 < len(lines) <= 5 and len(set(lines)) == len(lines)
    assert lines[0] == "u1: printer error 2 (detected noodle) (layer 41, 63%)"
    assert lines[1] == "+2 related: gcode_error, state"
    assert (
        lines[2].startswith("Watcher PAUSED the print (a.gcode).")
        and "http://192.168.0.26" in lines[2]
    )
    assert (
        "mcu dump" not in call[-1]
        and "!!" not in call[-1]
        and "paused (layer" not in call[-1]
    )


def test_a_failed_pause_is_one_line_and_the_headline_is_cut_to_the_first_200_chars(
    xdg: Path,
) -> None:
    long_first = Event(
        "u1", "exception", CRITICAL, "u1: " + "x" * 400 + "\nsecond line", {}, key="l"
    )
    c = Chain(Fake(tmux_ok=False), pauser=Pauser([], outcome="failed"))
    c.process(long_first)
    lines = c.tg.calls[0][-1].splitlines()
    assert len(lines) == 2 and len(lines[0]) == 200 and lines[0].endswith("…")
    assert lines[1] == "Watcher could NOT pause the print — check the printer."


# ---- outgoing limiter ---------------------------------------------------------------------------

KINDS = ("vision_spaghetti", "gcode_error", "heater_drift")


def ev(kind: str, sev: str = WARNING, i: int = 0) -> Event:
    return Event("u1", kind, sev, f"u1: {kind} #{i}", {}, key=f"{kind}:{i}")


class Limited:
    """A TgSink with its limiter state, a fake clock and a recording `tg`."""

    def __init__(self, tg: Tg | None = None, cooldown_s: float = 600.0) -> None:
        self.now = [1000.0]
        self.tg = tg or Tg()
        self.state = DeliveryState()
        self.sink = TgSink(runner=self.tg, clock=lambda: self.now[0])
        self.sink.ledger = self.state
        self.sink.attach("u1", cooldown_s=cooldown_s)

    def texts(self) -> list[str]:
        return [call[-1] for call in self.tg.calls]


def test_ten_mixed_events_over_a_minute_are_one_message_then_one_summary_after_the_cooldown(
    xdg: Path,
) -> None:
    lim = Limited()
    for i in range(10):
        lim.now[0] = 1000.0 + 6 * i
        lim.sink.deliver([ev(KINDS[i % 3], i=i)])
    assert lim.texts() == ["u1: vision_spaghetti #0"]
    assert lim.state.read()["counters"]["u1"]["fallback_tg_folded"] == 9
    lim.now[0] = 1000.0 + 599
    lim.sink.maintain()
    assert len(lim.tg.calls) == 1  # the cooldown is not over
    lim.now[0] = 1000.0 + 601
    lim.sink.maintain()
    assert lim.texts()[1:] == [
        "u1: +9 more: gcode_error, heater_drift, vision_spaghetti"
    ]
    lim.sink.maintain()
    lim.now[0] += 3600
    lim.sink.maintain()
    assert len(lim.tg.calls) == 2  # said once, never again


def test_an_escalation_passes_once_and_the_rest_waits_for_the_summary(
    xdg: Path,
) -> None:
    lim = Limited()
    lim.sink.deliver([ev("vision_spaghetti")])
    for i in range(1, 6):
        lim.now[0] += 5
        lim.sink.deliver([ev("gcode_error", i=i)])
    assert len(lim.tg.calls) == 1
    lim.now[0] += 5
    lim.sink.deliver([ev("exception", CRITICAL)])
    assert len(lim.tg.calls) == 2 and lim.texts()[1] == "u1: exception #0"
    for i in range(
        3
    ):  # critical again: same severity as the last message now, so folded
        lim.now[0] += 5
        lim.sink.deliver([ev("exception", CRITICAL, i=i + 1)])
    assert len(lim.tg.calls) == 2
    lim.now[0] += 601
    lim.sink.maintain()
    assert lim.texts()[2:] == ["u1: +8 more: gcode_error, exception"]


def test_the_next_message_after_the_cooldown_carries_the_folded_line_and_starts_a_fresh_incident(
    xdg: Path,
) -> None:
    lim = Limited()
    lim.sink.deliver([ev("vision_spaghetti")])
    lim.now[0] += 60
    lim.sink.deliver([ev("gcode_error", i=1)])
    lim.sink.deliver([ev("heater_drift", i=2)])
    lim.now[0] += 700  # a second incident, no maintain() ran in between
    lim.sink.deliver([ev("vision_spaghetti", i=3)])
    assert lim.texts()[1:] == [
        "u1: vision_spaghetti #3\n+2 more: gcode_error, heater_drift"
    ]
    lim.sink.maintain()
    assert len(lim.tg.calls) == 2
    lim.now[0] += 30  # and its own cooldown is running
    lim.sink.deliver([ev("vision_spaghetti", i=4)])
    assert len(lim.tg.calls) == 2


def test_after_a_flushed_summary_the_next_alert_is_a_fresh_message_immediately(
    xdg: Path,
) -> None:
    lim = Limited()
    lim.sink.deliver([ev("vision_spaghetti")])
    lim.now[0] += 10
    lim.sink.deliver([ev("gcode_error", i=1)])
    lim.now[0] += 700
    lim.sink.maintain()
    lim.sink.deliver([ev("vision_spaghetti", i=2)])
    assert lim.texts() == [
        "u1: vision_spaghetti #0",
        "u1: +1 more: gcode_error",
        "u1: vision_spaghetti #2",
    ]


def test_the_limiter_is_shared_between_watcher_processes(xdg: Path) -> None:
    watch, vision = (
        Limited(),
        Limited(),
    )  # two processes: own sinks and clocks, one state file
    watch.sink.deliver([ev("vision_spaghetti")])
    vision.now[0] += 20
    vision.sink.deliver([ev("vision_spaghetti", i=1)])
    assert len(watch.tg.calls) == 1 and vision.tg.calls == []


def test_a_failed_send_hands_its_slot_back(xdg: Path) -> None:
    lim = Limited(Tg(fail=2))  # the tagged call and the plain retry both fail
    with pytest.raises(RuntimeError, match="plain retry"):
        lim.sink.deliver([ev("vision_spaghetti")])
    lim.now[0] += 5
    lim.sink.deliver([ev("vision_spaghetti", i=1)])
    assert lim.texts()[-1] == "u1: vision_spaghetti #1" and len(lim.tg.calls) == 3


def test_the_pause_is_never_limited_only_the_message_is(xdg: Path) -> None:
    order: list[str] = []
    pauser = Pauser(order)
    c = Chain(Fake(tmux_ok=False), pauser=pauser)
    c.process(noodle(sev=CRITICAL))
    c.now[0] += 30
    c.process(
        noodle(frame_file(xdg), sev=CRITICAL)
    )  # same severity inside the cooldown
    assert pauser.calls == 2 and len(c.tg.calls) == 1
    assert (
        c.counters()["auto_pause_paused"] == 2
        and c.counters()["fallback_tg_folded"] == 1
    )


def test_the_cooldown_setting_is_validated_and_defaults_to_ten_minutes(
    tmp_path: Path,
) -> None:
    from printer_ops import devices

    def cooldown(watch: dict[str, Any]) -> float:
        dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path, watch=watch)
        return float(watch_run.settings_for(dev, {})["message_cooldown_s"])

    assert cooldown({}) == 600.0 and cooldown({"message_cooldown_minutes": 3}) == 180.0
    with pytest.raises(Exception, match="message_cooldown_minutes"):
        cooldown({"message_cooldown_minutes": -1})


def test_the_agents_role_and_the_subscriber_footer_ask_for_one_message_per_incident(
    xdg: Path,
) -> None:
    role = printer_agent.ROLE.format(printer="u1", events="e", fresh="f")
    assert (
        "AT MOST ONE tg message" in role and "10 minutes" in role and "5 lines" in role
    )
    text = subscriptions.alert_text([noodle()], "[x]", [])
    assert "ONE short Russian message per incident" in text and "10 minutes" in text
