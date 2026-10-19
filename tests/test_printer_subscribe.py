"""Tests for `3d printer subscribe` (lib/printer_ops/subscriptions.py), the temporary printer
agent (lib/printer_ops/printer_agent.py) and the agent-first staging of the watch Dispatcher.

A U1 "noodle" alert (raw p 0.832) on a healthy print reached the user's phone directly; these
tests pin that alerts now reach a live subscribed agent first (with every frame/evidence path and
the triage instruction), that what no subscriber took goes to the printer's temporary omp agent
(tmux + omp-live), which hands over to a subscriber, and that tg (`watch.tg_fallback`) only gets
what no agent could take. tmux / omp / tg-ctl / tg are fake runners; XDG dirs are tmp.
Issues https://github.com/alex-mextner/3d-cli/issues/150 and #145.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from errors import InvalidArgument, UsageError
from printer_ops import devices, printer_agent, subscriptions, watch_run
from printer_ops.printer_agent import (
    PrinterAgents,
    PrinterAgentSink,
    agent_dir,
    session_name,
)
from printer_ops.subscriptions import (
    DeliveryState,
    Registry,
    RegistryError,
    Subscription,
    SubscriptionSink,
    agent_message,
    frames_of,
    make,
)
from printer_ops.watch import CRITICAL, INFO, WARNING, Event
from printer_ops.watch_sinks import (
    FALLBACK,
    FIRST,
    LAST_RESORT,
    Dispatcher,
    DryRunSink,
    OmpLive,
    Sink,
    SteerSink,
    TgSink,
)

MAIN = {
    "pid": 433553,
    "sessionId": "01a12cd6-7b39",
    "cwd": "/tmp",
    "title": "MODEL: Finish 3D model",
    "busy": False,
}
OTHER = {
    "pid": 14610,
    "sessionId": "01a11af0-c495",
    "cwd": "/tmp",
    "title": "FREEZE: Diagnose",
    "busy": True,
}
SESSIONS = json.dumps([MAIN, OTHER])


class Fake:
    """tg-ctl / tmux / tg stand-in. `hosts` is what `omp-live status` lists; a tmux new-session
    adds a live omp host with the session's cwd (as a real omp in tmux registers itself) unless
    `agent_comes_up` is False."""

    def __init__(
        self,
        hosts: list[dict[str, Any]] | None = None,
        *,
        send_ok: bool = True,
        agent_comes_up: bool = True,
        tmux_ok: bool = True,
    ) -> None:
        self.calls: list[list[str]] = []
        self.hosts = list(hosts if hosts is not None else [MAIN, OTHER])
        self.tmux: dict[str, str] = {}  # running tmux session -> cwd
        self.send_ok = send_ok
        self.agent_comes_up = agent_comes_up
        self.tmux_ok = tmux_ok

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        out, code = '{"ok":true}', 0
        if cmd[:3] == ["tg-ctl", "omp-live", "status"]:
            out = json.dumps(self.hosts)
        elif cmd[:3] == ["tg-ctl", "omp-live", "send"] and not self.send_ok:
            out, code = '{"ok":false,"error":"gone"}', 1
        elif cmd[0] == "tmux":
            out, code = self._tmux(cmd[1:])
        return subprocess.CompletedProcess(cmd, code, out, "")

    def _tmux(self, args: list[str]) -> tuple[str, int]:
        name = (
            args[args.index("-t") + 1].lstrip("=")
            if "-t" in args
            else args[args.index("-s") + 1]
        )
        if args[0] == "has-session":
            return "", 0 if name in self.tmux else 1
        if args[0] == "new-session":
            if not self.tmux_ok:
                return "boom", 1
            cwd = args[args.index("-c") + 1]
            self.tmux[name] = cwd
            if self.agent_comes_up:
                self.hosts.append(
                    {
                        "pid": 9000 + len(self.hosts),
                        "sessionId": f"agent-{name}-{len(self.calls)}",
                        "cwd": cwd,
                        "title": "agent",
                    }
                )
            return "", 0
        if args[0] == "kill-session":
            killed = self.tmux.pop(name) if name in self.tmux else None
            self.hosts = [
                h for h in self.hosts if killed is None or h.get("cwd") != killed
            ]
            return "", 0 if killed is not None else 1
        return "", 0

    def sends(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["tg-ctl", "omp-live", "send"]]

    def news(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["tmux", "new-session"]]


class Recorder(Sink):
    name = "rec"

    def __init__(self, *, stage: int = FIRST) -> None:
        super().__init__()
        self.stage = stage
        self.got: list[list[Event]] = []

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        self.got.append(events)


@pytest.fixture()
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def noodle(
    frame: str | None = None,
    sev: str = WARNING,
    printer: str = "u1",
    kind: str = "vision_spaghetti",
) -> Event:
    data: dict[str, Any] = {"p": 0.832}
    if frame:
        data["frame"] = frame
    return Event(
        printer,
        kind,
        sev,
        f"{printer}: possible spaghetti on camera (score 0.83)",
        data,
        key=f"{printer}:{kind}:{sev}:{frame}",
    )


def sink(run: Fake | None = None) -> SubscriptionSink:
    return SubscriptionSink(runner=run or Fake())


def agents(run: Fake, clock: list[float] | None = None) -> PrinterAgents:
    t = clock if clock is not None else [1000.0]
    return PrinterAgents(
        runner=run,
        tmux="tmux",
        omp="omp",
        clock=lambda: t[0],
        sleep=lambda s: t.__setitem__(0, t[0] + s),
        ready_timeout_s=10.0,
    )


def routed(run: Fake, *first: Sink) -> tuple[Dispatcher, Recorder]:
    """u1 routed like a watcher: subscriptions, the temporary agent, then a tg-like fallback."""
    tg = Recorder(stage=FALLBACK)
    d = Dispatcher(dedup_s=0)
    d.add("u1", [*first, sink(run), PrinterAgentSink(agents=agents(run)), tg])
    return d, tg


def put(**kw: Any) -> Subscription:
    return Registry().put(make(**kw))[0]


# ---- registry -----------------------------------------------------------------------------------


def test_registry_lives_in_the_config_dir_and_round_trips(xdg: Path) -> None:
    s = put(
        session="01a12cd6",
        printers=["u1"],
        kinds=["vision_*", "printer_noodle_verdict"],
        note="main",
    )
    path = xdg / "config" / "3d-cli" / "subscriptions.yaml"
    assert Registry().path == path and path.read_text().startswith(
        "# Agent subscriptions"
    )
    (back,) = Registry().load()
    assert back == s
    assert back.printers == ("u1",) and back.kinds == (
        "printer_noodle_verdict",
        "vision_*",
    )
    assert (
        back.min_severity == WARNING and back.id.startswith("s") and len(back.id) == 7
    )


def test_same_session_and_filters_update_the_entry(xdg: Path) -> None:
    reg = Registry()
    first, new1 = reg.put(
        make(session="01a12cd6", printers="u1", kinds=["vision_*"], created="T1")
    )
    again, new2 = reg.put(
        make(
            session="01a12cd6",
            printers=["u1"],
            kinds=["vision_*"],
            min_severity=INFO,
            note="n",
            created="T2",
        )
    )
    other, new3 = reg.put(make(session="01a12cd6", printers=["k1se"], created="T3"))
    assert (new1, new2, new3) == (True, False, True)
    assert (
        again.id == first.id
        and again.created == "T1"
        and again.min_severity == INFO
        and again.note == "n"
    )
    assert [s.id for s in reg.load()] == [first.id, other.id]


def test_remove_by_predicate_and_all(xdg: Path) -> None:
    reg = Registry()
    a = put(session="01a12cd6", printers=["u1"])
    b = put(session="01a12cd6-7b39", printers=["k1se"])
    c = put(session="x1", printers=["u1"])
    assert [s.id for s in reg.remove(lambda s: s.id == c.id)] == [c.id]
    assert {s.id for s in reg.remove(lambda s: s.session.startswith("01a12cd6"))} == {
        a.id,
        b.id,
    }
    put(session="x2")
    assert len(reg.remove(lambda s: True)) == 1 and reg.load() == []


def test_concurrent_writers_lose_nothing(xdg: Path) -> None:
    def add(i: int) -> None:
        Registry().put(make(session=f"sess{i}"))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s.session for s in Registry().load()) == sorted(
        f"sess{i}" for i in range(12)
    )
    assert not [
        p for p in (xdg / "config" / "3d-cli").iterdir() if p.name.endswith(".tmp")
    ]


@pytest.mark.parametrize(
    "body, why",
    [
        ("subscriptions: [", "invalid YAML"),
        ("- a\n- b\n", "top level"),
        ("subscriptions: [{printers: [u1]}]", "session: ID"),
        ("subscriptions: [{session: x, min_severity: loud}]", "min_severity"),
        ("subscriptions: [{session: all}]", "session"),
        ("subscriptions: [{id: a, session: x}, {id: a, session: y}]", "duplicate"),
    ],
)
def test_bad_registry_is_a_clear_error(xdg: Path, body: str, why: str) -> None:
    reg = Registry()
    reg.path.parent.mkdir(parents=True)
    reg.path.write_text(body)
    with pytest.raises(RegistryError, match=why) as info:
        reg.load()
    assert str(reg.path) in info.value.message and info.value.exit_code == 2


def test_validation_rejects_bad_fields() -> None:
    for kw in (
        {"session": "all"},
        {"session": ""},
        {"session": "01a1", "printers": ["u 1"]},
        {"session": "01a1", "kinds": ["a b"]},
        {"session": "01a1", "min_severity": "loud"},
    ):
        with pytest.raises(InvalidArgument):
            make(**kw)


# ---- matching and message -----------------------------------------------------------------------


def test_matching_by_printer_kind_glob_and_severity() -> None:
    s = make(session="x", printers=["u1"], kinds=["vision_*", "printer_noodle_verdict"])
    assert s.matches(noodle())
    assert s.matches(noodle(kind="printer_noodle_verdict"))
    assert s.matches(noodle(sev=CRITICAL))
    assert not s.matches(noodle(printer="k1se"))
    assert not s.matches(noodle(kind="exception"))
    assert not s.matches(noodle(sev=INFO))
    assert make(session="x", min_severity=INFO).matches(
        noodle(printer="k1se", kind="snapshot", sev=INFO)
    )


def test_frames_of_collects_frame_frames_crop_and_evidence() -> None:
    e = Event(
        "u1",
        "vision_spaghetti",
        WARNING,
        "t",
        {
            "frame": "/a.jpg",
            "frames": ["/b.jpg", "/a.jpg"],
            "crop": "/c.jpg",
            "evidence": "/d.png",
            "p": 1,
        },
    )
    assert frames_of(e) == ["/a.jpg", "/b.jpg", "/c.jpg", "/d.png"]
    assert frames_of(Event("u1", "state", WARNING, "t", {})) == []


def test_agent_message_lists_every_event_most_severe_first() -> None:
    s = make(session="x")
    text = agent_message(
        [noodle("/w.jpg"), noodle(sev=CRITICAL, kind="printer_noodle_verdict")], [s]
    )
    lines = text.splitlines()
    assert (
        lines[0]
        == f"[3d printer alert -> subscriber {s.id}] u1 printer_noodle_verdict (critical)"
    )
    assert (
        lines[1].startswith("- u1 printer_noodle_verdict (critical)")
        and lines[2] == "  frames: none attached"
    )
    assert "  frames: /w.jpg" in lines
    assert lines[-1] == f"Unsubscribe: 3d printer unsubscribe --id {s.id}"


def test_fresh_frame_hint_follows_the_installed_vision_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subscriptions, "has_vision", lambda: True)
    assert (
        "take one with `3d printer vision u1 --once --save /tmp/u1-now.jpg`"
        in agent_message([noodle()], [make(session="x")])
    )
    monkeypatch.setattr(subscriptions, "has_vision", lambda: False)
    assert "no `3d printer vision` (#150)" in agent_message(
        [noodle()], [make(session="x")]
    )


# ---- session delivery ---------------------------------------------------------------------------


def test_live_session_gets_an_agent_oriented_message(xdg: Path) -> None:
    s = put(session="01a12cd6", printers=["u1"], kinds=["vision_*"])
    run = Fake()
    e = noodle("/cache/u1-vision_spaghetti.jpg")
    assert sink(run).deliver([e]) == [e]
    (send,) = run.sends()
    assert send[3:5] == ["433553", "--request-id"] and send[6:8] == [
        "--session",
        "01a12cd6-7b39",
    ]
    text = send[-1]
    assert text.startswith(
        f"[3d printer alert -> subscriber {s.id}] u1 vision_spaghetti (warning)"
    )
    assert "frames: /cache/u1-vision_spaghetti.jpg" in text and '"p":0.832' in text
    assert "Open every frame above with your read tool" in text
    assert (
        "tell the user via `tg`" in text
        and "False alarm: do not message the user" in text
    )
    assert "Never pause, cancel or send G-code without the user's explicit ok" in text
    assert "`3d printer status u1`" in text
    row = DeliveryState().read()["subscriptions"][s.id]
    assert (
        row["delivered"] == 1
        and row["last_kinds"] == ["vision_spaghetti"]
        and "433553" in row["last_target"]
    )


def test_two_entries_of_one_session_get_one_message(xdg: Path) -> None:
    put(session="01a12cd6", kinds=["vision_*"])
    put(session="01a12cd6-7b39", printers=["u1"])
    run = Fake()
    sink(run).deliver([noodle("/f.jpg")])
    assert len(run.sends()) == 1


def test_unmatched_events_are_not_taken(xdg: Path) -> None:
    put(session="01a12cd6", printers=["k1se"])
    run = Fake()
    assert sink(run).deliver([noodle()]) == []
    assert run.calls == []


def test_dead_session_is_recorded_not_raised(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = put(session="deadbeef")
    snk = sink()
    assert snk.deliver([noodle()]) == []
    row = DeliveryState().read()["subscriptions"][s.id]
    assert (
        "no live omp session matches 'deadbeef'" in row["last_error"]
        and row["failed"] == 1
    )
    assert "deadbeef" in capsys.readouterr().err and "deadbeef" in str(
        snk.stats.last_error
    )


def test_missing_tg_ctl_and_failed_send_are_recorded(xdg: Path) -> None:
    s = put(session="01a12cd6")

    def missing(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    assert SubscriptionSink(runner=missing).deliver([noodle()]) == []
    assert (
        "tg-ctl not found"
        in DeliveryState().read()["subscriptions"][s.id]["last_error"]
    )
    assert sink(Fake(send_ok=False)).deliver([noodle()]) == []
    assert (
        "omp-live send to 433553 failed"
        in DeliveryState().read()["subscriptions"][s.id]["last_error"]
    )


def test_registry_is_reread_on_change_and_a_broken_one_keeps_the_last_good(
    xdg: Path,
) -> None:
    snk = sink()
    assert snk.deliver([noodle()]) == []
    put(session="01a12cd6")
    assert len(snk.deliver([noodle()])) == 1
    Registry().path.write_text("subscriptions: [")
    assert len(snk.deliver([noodle()])) == 1  # still the previous entry
    assert "invalid YAML" in str(snk.stats.last_error)
    Registry().path.unlink()
    assert snk.deliver([noodle()]) == []


def test_dry_run_prints_and_sends_nothing(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = put(session="01a12cd6", printers=["u1"])
    run = Fake()
    e = noodle("/f.jpg")
    assert SubscriptionSink(runner=run, dry_run=True).deliver([e]) == [e]
    assert run.calls == []
    err = capsys.readouterr().err
    assert f"dry-run {s.id} -> session 01a12cd6" in err and "frames: /f.jpg" in err
    assert not DeliveryState().path.exists()


# ---- the temporary printer agent ----------------------------------------------------------------


def test_unclaimed_warning_starts_the_printer_agent_in_tmux(xdg: Path) -> None:
    frame = xdg / "u1.jpg"
    frame.write_bytes(b"\xff\xd8")
    put(session="deadbeef")
    run = Fake()
    d, tg = routed(run)
    e = noodle(str(frame))
    assert d.process([e]) == [e] and tg.got == []
    (new,) = run.news()
    d_u1 = agent_dir("u1")
    assert new[2:8] == ["-d", "-s", "3d-printer-agent-u1", "-c", str(d_u1), "-x"]
    omp = new[new.index("--") + 1 :]
    assert (
        omp[:3] == ["omp", "--append-system-prompt", str(d_u1 / "role.md")]
        and omp[-1] == f"@{frame}"
    )
    assert omp[3].startswith(
        "[3d printer alert -> temporary u1 agent] u1 vision_spaghetti (warning)"
    )
    role = (d_u1 / "role.md").read_text()
    assert (
        "TEMPORARY alert agent for the 3D printer `u1`" in role
        and "3d printer agent handoff u1" in role
    )
    assert "policy D5" in role and "tg call me --say" in role
    assert json.loads((d_u1 / "events.jsonl").read_text())["kind"] == "vision_spaghetti"
    assert any(c[:2] == ["tmux", "pipe-pane"] for c in run.calls)
    st = DeliveryState().agent("u1")
    assert st["started"] and st["last_kinds"] == ["vision_spaghetti"]


def test_running_agent_gets_later_alerts_via_omp_live(xdg: Path) -> None:
    run = Fake()
    d, _ = routed(run)
    d.process([noodle("/a")])
    d.process([noodle("/b", kind="vision_detached")])
    assert len(run.news()) == 1
    (send,) = run.sends()
    assert send[3] == str(run.hosts[-1]["pid"])
    assert send[-1].startswith(
        "[3d printer alert -> temporary u1 agent] u1 vision_detached"
    )
    assert len((agent_dir("u1") / "events.jsonl").read_text().splitlines()) == 2


def test_info_events_and_events_a_subscriber_took_never_reach_the_agent(
    xdg: Path,
) -> None:
    put(session="01a12cd6", printers=["u1"], min_severity=INFO)
    run = Fake()
    d, tg = routed(run)
    d.process([noodle(), noodle(sev=INFO, kind="vision_no_growth")])
    assert run.news() == [] and tg.got == [] and len(run.sends()) == 1
    Registry().remove(lambda s: True)
    d.process([noodle(sev=INFO, kind="vision_spaghetti_vetoed")])
    assert run.news() == [] and [[e.kind for e in b] for b in tg.got] == [
        ["vision_spaghetti_vetoed"]
    ]


def test_agent_that_never_comes_up_is_killed_and_tg_gets_the_alert(xdg: Path) -> None:
    run = Fake(agent_comes_up=False)
    d, tg = routed(run)
    d.process([noodle()])
    assert run.tmux == {} and any(c[:2] == ["tmux", "kill-session"] for c in run.calls)
    assert [[e.kind for e in b] for b in tg.got] == [["vision_spaghetti"]]
    assert "did not come up within 10 s" in DeliveryState().agent("u1")["last_error"]


def test_agent_start_is_limited_per_printer_and_shared_by_processes(xdg: Path) -> None:
    run = Fake(tmux_ok=False)
    clock = [1000.0]
    a = agents(run, clock)
    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        a.deliver("u1", [noodle()])
    clock[0] = 1300.0
    with pytest.raises(RuntimeError, match="restart limit"):
        agents(run, clock).deliver(
            "u1", [noodle()]
        )  # another watcher process: same state file
    run.tmux_ok = True
    assert "started the k1se agent" in agents(run, clock).deliver(
        "k1se", [noodle(printer="k1se")]
    )
    clock[0] = 1601.0
    assert "started the u1 agent" in agents(run, clock).deliver("u1", [noodle()])


def test_agent_sink_dry_run_starts_nothing(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    e = noodle()
    assert PrinterAgentSink(dry_run=True).deliver([e]) == [e]
    assert "dry-run temporary u1 agent (3d-printer-agent-u1)" in capsys.readouterr().err
    assert not agent_dir("u1").exists()


def test_full_subscriber_triggers_handoff_once(xdg: Path) -> None:
    run = Fake()
    d, _ = routed(run)
    d.process([noodle("/a")])  # no subscriber: the agent starts
    agent_host = run.hosts[-1]
    put(session="01a12cd6", printers=["u1"])  # every kind of u1
    d.process([noodle("/b")])
    d.process([noodle("/c")])
    asks = [c for c in run.sends() if c[3] == str(agent_host["pid"])]
    (ask,) = asks
    assert (
        "Session 01a12cd6-7b39 (pid 433553" in ask[-1]
        and "3d printer agent handoff u1 --summary" in ask[-1]
    )
    assert DeliveryState().agent("u1")["handoff_asked"] == agent_host["sessionId"]


def test_kind_limited_subscriber_does_not_trigger_handoff(xdg: Path) -> None:
    run = Fake()
    d, _ = routed(run)
    d.process([noodle("/a")])
    agent_pid = str(run.hosts[-1]["pid"])
    put(session="01a12cd6", printers=["u1"], kinds=["vision_*"])
    d.process([noodle("/b")])
    assert [c for c in run.sends() if c[3] == agent_pid] == []


# ---- staging and tg fallback --------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, status, reached",
    [
        ("01a12cd6", SESSIONS, True),
        ("nope", SESSIONS, False),  # no such session
        ("all", "[]", False),  # broadcast to nobody is not a delivery
    ],
)
def test_steer_counts_as_an_agent_only_when_it_reached_one(
    target: str, status: str, reached: bool
) -> None:
    rec = Recorder(stage=FALLBACK)
    d = Dispatcher(dedup_s=0)
    d.add("u1", [SteerSink(target, runner=Fake(json.loads(status))), rec])
    d.process([noodle()])
    assert (rec.got == []) is reached


def test_tg_fallback_gets_only_what_no_agent_took_marked_as_such(xdg: Path) -> None:
    put(session="01a12cd6", printers=["u1"])
    tg = Fake()
    d = Dispatcher(dedup_s=0)
    d.add("u1", [sink(Fake()), TgSink(runner=tg)])
    d.process([noodle(sev=CRITICAL)])
    assert tg.calls == [] and d.stats()["tg"]["left_to_agent"] == 1
    d.process([noodle(printer="u1", kind="exception", sev=CRITICAL)])
    Registry().remove(lambda s: True)
    d.process([noodle("/z")])
    (call,) = tg.calls
    assert call[0] == "tg" and call[1].startswith(
        "[агент недоступен] printer [WARNING]"
    )


def test_stages_and_dry_run_keep_roles() -> None:
    assert (
        TgSink().stage == FALLBACK
        and PrinterAgentSink().stage == LAST_RESORT
        and SubscriptionSink().stage == FIRST
    )
    assert DryRunSink(TgSink()).stage == FALLBACK and DryRunSink(SteerSink("x")).agent
    assert PrinterAgentSink().agent and SubscriptionSink().agent and not TgSink().agent


def test_watch_tg_fallback_setting(tmp_path: Path) -> None:
    def fallback(watch: dict[str, Any]) -> bool:
        dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path, watch=watch)
        return bool(watch_run.settings_for(dev, {})["sinks"]["tg_fallback"])

    assert fallback({}) is False and fallback({"tg_fallback": True}) is True
    for bad in ({"tg_fallback": "yes"}, {"tg": True}):
        with pytest.raises(InvalidArgument):
            fallback(bad)
    for flag in ("--tg", "--tg-always"):
        with pytest.raises(UsageError):
            watch_run.parse_args(["u1", flag])


def test_for_devices_adds_subscriptions_and_the_printer_agent(xdg: Path) -> None:
    dev = devices.Device(
        "u1", "moonraker", "h", 7125, None, xdg, watch={"tg_fallback": True}
    )
    d = Dispatcher.for_devices([dev], dry_run=True)
    assert sorted(d.routes) == ["printer-agent", "stdout", "subscriptions", "tg"]
    subs, agent = d.routes["subscriptions"].sink, d.routes["printer-agent"].sink
    assert (
        isinstance(subs, SubscriptionSink)
        and isinstance(agent, PrinterAgentSink)
        and subs.dry_run
        and agent.dry_run
    )
    assert d.routes["tg"].sink.stage == FALLBACK


def test_vision_event_via_for_devices_reaches_the_subscriber_with_frame_and_evidence(
    xdg: Path,
) -> None:
    """`3d printer vision` (#186) emits through Dispatcher.for_devices: a vision_spaghetti event
    with data.frame + data.evidence reaches the subscribed session listing both paths."""
    put(session="01a12cd6", printers=["u1"], kinds=["vision_*"])
    dev = devices.Device(
        "u1", "moonraker", "h", 7125, None, xdg, watch={"stdout": False}
    )
    d = Dispatcher.for_devices([dev], dedup_s=0)
    run = Fake()
    subs = d.routes["subscriptions"].sink
    assert isinstance(subs, SubscriptionSink)
    subs.live = OmpLive(runner=run)
    e = Event(
        "u1",
        "vision_spaghetti",
        WARNING,
        "u1: spaghetti on camera (score 0.91)",
        {
            "frame": "/cache/u1-frame.jpg",
            "evidence": "/cache/u1-evidence.png",
            "score": 0.91,
        },
    )
    assert d.process([e]) == [e]
    (send,) = run.sends()
    assert "  frames: /cache/u1-frame.jpg /cache/u1-evidence.png" in send[-1]
    assert (
        '"score":0.91' in send[-1]
        and "evidence" not in send[-1].split("data: ")[1].splitlines()[0]
    )


# ---- CLI ----------------------------------------------------------------------------------------


@pytest.fixture()
def tgctl(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `tg-ctl` first on PATH: status prints SESSIONS (+ hosts.json if present), send
    appends its argv to sent.jsonl."""
    bindir = xdg / "bin"
    bindir.mkdir()
    sent = xdg / "sent.jsonl"
    extra = xdg / "hosts.json"
    script = bindir / "tg-ctl"
    script.write_text(f"""#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:3] == ["omp-live", "status"]:
    hosts = json.loads({SESSIONS!r})
    if os.path.exists({str(extra)!r}):
        hosts += json.load(open({str(extra)!r}))
    print(json.dumps(hosts))
else:
    open({str(sent)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
    print('{{"ok":true}}')
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return sent


def test_cli_subscribe_list_test_unsubscribe(
    tgctl: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        subscriptions.run(
            "subscribe",
            [
                "--session",
                "01a12cd6",
                "--printer",
                "u1",
                "--kinds",
                "vision_*,printer_noodle_verdict",
            ],
        )
        == 0
    )
    out = capsys.readouterr().out
    sid = out.split()[1].rstrip(":")
    assert (
        out.startswith(f"subscribed {sid}: session 01a12cd6")
        and "live: yes — pid 433553" in out
    )
    assert (
        subscriptions.run(
            "subscribe",
            [
                "--session",
                "01a12cd6",
                "--printer",
                "u1",
                "--kinds",
                "printer_noodle_verdict",
                "--kinds",
                "vision_*",
                "--note",
                "main",
            ],
        )
        == 0
    )
    assert capsys.readouterr().out.startswith(f"updated {sid}:")
    assert subscriptions.run("subscriptions", ["--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert (
        row["id"] == sid
        and row["live"]["live"] is True
        and row["note"] == "main"
        and row["state"] == {}
    )
    assert subscriptions.run("subscriptions", ["test", "u1"]) == 0
    assert "dry run" in capsys.readouterr().out and not tgctl.exists()
    assert subscriptions.run("subscriptions", ["test", "u1", "--send"]) == 0
    assert "a live subscriber took it: yes" in capsys.readouterr().out
    (sent,) = [json.loads(line) for line in tgctl.read_text().splitlines()]
    assert sent[:2] == ["omp-live", "send"] and "[TEST]" in sent[-1]
    assert subscriptions.run("subscriptions", ["list"]) == 0
    listed = capsys.readouterr().out
    assert (
        "last delivery: " in listed
        and "(1 delivered)" in listed
        and "note: main" in listed
    )
    assert subscriptions.run("subscriptions", ["test", "k1se"]) == 1
    assert subscriptions.run("unsubscribe", ["--session", "01a12cd6-7b39"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"removed {sid}")
    assert Registry().load() == []


def test_cli_subscribe_asks_a_running_agent_to_hand_over(
    tgctl: Path,
    xdg: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = agent_dir("u1")
    d.mkdir(parents=True)
    (xdg / "hosts.json").write_text(
        json.dumps([{"pid": 777, "sessionId": "agent-u1", "cwd": str(d)}])
    )
    monkeypatch.setattr(PrinterAgents, "running", lambda self, p: p == "u1")
    assert (
        subscriptions.run(
            "subscribe", ["--session", "01a12cd6", "--printer", "u1", "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["handoff_requested"] == ["u1"]
    (ask,) = [json.loads(line) for line in tgctl.read_text().splitlines()]
    assert ask[2] == "777" and "3d printer agent handoff u1 --summary" in ask[-1]


def test_cli_errors(xdg: Path) -> None:
    with pytest.raises(UsageError, match="needs --session ID"):
        subscriptions.run("subscribe", ["--printer", "u1"])
    for gone in ("--spawn", "--model"):
        with pytest.raises(UsageError, match="unknown option"):
            subscriptions.run("subscribe", ["--session", "x", gone, "m"])
    with pytest.raises(UsageError, match="needs a value"):
        subscriptions.run("subscribe", ["--session"])
    with pytest.raises(
        UsageError, match="exactly one of --session ID, --id SUBID, --all"
    ):
        subscriptions.run("unsubscribe", ["--id", "a", "--all"])
    with pytest.raises(UsageError, match="one PRINTER"):
        subscriptions.run("subscriptions", ["test"])
    cfg = xdg / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: 127.0.0.1}\n"
    )
    with pytest.raises(InvalidArgument):
        subscriptions.run("subscribe", ["--session", "x", "--printer", "u2"])
    (cfg / "subscriptions.yaml").write_text("subscriptions: [")
    with pytest.raises(RegistryError):
        subscriptions.run("subscriptions", [])


def test_cli_help_and_unsubscribe_all(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert subscriptions.run("subscribe", ["--help"]) == 0
    assert "3d printer subscribe --session ID" in capsys.readouterr().out
    assert subscriptions.run("subscribe", []) == 1
    put(session="x")
    capsys.readouterr()
    assert subscriptions.run("unsubscribe", ["--all"]) == 0
    assert "removed" in capsys.readouterr().out
    assert subscriptions.run("unsubscribe", ["--all"]) == 0
    assert "nothing to remove" in capsys.readouterr().out


class _CliAgents(PrinterAgents):
    """PrinterAgents for the `3d printer agent` CLI, wired to one shared Fake."""

    fake: Fake

    def __init__(self, **kw: Any) -> None:
        super().__init__(runner=self.fake, tmux="tmux", omp="omp", **kw)


@pytest.fixture()
def cli_agents(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    run = Fake()
    d = agent_dir("u1")
    d.mkdir(parents=True)
    run.tmux[session_name("u1")] = str(d)
    run.hosts.append(
        {"pid": 777, "sessionId": "agent-u1", "cwd": str(d), "title": "agent"}
    )
    monkeypatch.setattr(_CliAgents, "fake", run, raising=False)
    monkeypatch.setattr(printer_agent, "PrinterAgents", _CliAgents)
    return run


def test_agent_list_shows_running_agents(
    cli_agents: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    DeliveryState().set_agent(
        "u1", started="T0", last_ts="T1", last_kinds=["vision_spaghetti"]
    )
    assert printer_agent.run(["list", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert (
        row["printer"] == "u1"
        and row["running"] is True
        and row["live"] is True
        and row["pid"] == 777
    )
    assert row["session"] == "3d-printer-agent-u1" and row["state"]["started"] == "T0"
    assert printer_agent.run(["list"]) == 0
    out = capsys.readouterr().out
    assert (
        "u1  3d-printer-agent-u1: running, live pid 777" in out
        and "vision_spaghetti" in out
    )


def test_agent_handoff_forwards_the_summary_and_stops(
    cli_agents: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(UsageError, match="--summary"):
        printer_agent.run(["handoff", "u1"])
    assert (
        printer_agent.run(["handoff", "u1", "--summary", "one false noodle"]) == 1
    )  # nobody subscribed
    assert "the temporary agent stays" in capsys.readouterr().err and cli_agents.tmux
    put(session="01a12cd6", printers=["u1"])
    put(session="deadbeef", printers=["u1"])
    assert printer_agent.run(["handoff", "u1", "--summary", "one false noodle"]) == 0
    (send,) = cli_agents.sends()
    assert (
        send[3] == "433553"
        and "[3d printer agent handoff] u1" in send[-1]
        and "one false noodle" in send[-1]
    )
    assert cli_agents.tmux == {} and DeliveryState().agent("u1")["handed_over"]
    assert "handed over u1 to session 01a12cd6-7b39" in capsys.readouterr().out


def test_agent_stop_and_usage(
    cli_agents: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    assert printer_agent.run(["stop", "u1"]) == 0
    assert (
        capsys.readouterr().out.strip() == "stopped 3d-printer-agent-u1"
        and cli_agents.tmux == {}
    )
    assert printer_agent.run(["stop", "u1"]) == 0
    assert "no 3d-printer-agent-u1 running" in capsys.readouterr().out
    assert (
        printer_agent.run(["--help"]) == 0
        and "3d printer agent handoff PRINTER" in capsys.readouterr().out
    )
    assert printer_agent.run([]) == 1
    with pytest.raises(UsageError, match="unknown `printer agent` subcommand"):
        printer_agent.run(["bogus"])
    with pytest.raises(UsageError, match="one PRINTER"):
        printer_agent.run(["stop"])
