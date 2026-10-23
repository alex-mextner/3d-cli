"""`alert-fallback-agent` (Refs #526): with no live subscriber, can the temporary printer agent take the alerts?
Temp state dir, fake tmux runner, fake tool paths, fake `tg-ctl`: never the real tmux, omp or state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from printer_doctor import render
from printer_doctor.checks.alert_fallback import GRACE_S, newest_watcher_alert
from printer_doctor.engine import Options, run_printer, summarize
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import (
    FakeTgCtl,
    FakeTmux,
    agent_sources,
    ev,
    rig,
    subscription_sources,
    target,
)
from printer_ops import subscriptions as subs
from printer_ops.printer_agent import session_name

CHECK = {c.id: c for c in discover_checks()}["alert-fallback-agent"]
LIVE = {
    "pid": 4242,
    "sessionId": "0aaa1111-live-session",
    "cwd": "/tmp",
    "title": "main",
}
CAMERA = {
    "vision": {"camera": {"kind": "snapshot-url", "url": "http://127.0.0.1:1/snap"}}
}
T0 = "2026-10-21T16:08:00+00:00"  # the agent's last alert (the real incident's)
T1 = "2026-10-22T20:22:59+00:00"  # the PAUSED alert that reached nothing


class Lab:
    def __init__(self, tmp_path: Path, *, hosts: list[dict[str, Any]] | None = None, tools: dict[str, str | None] | None = None,
                 running: bool = False) -> None:  # fmt: skip
        self.base = tmp_path
        self.tg = FakeTgCtl(hosts)
        self.tmux = FakeTmux({session_name("p1")} if running else set())
        self.subscriptions = subscription_sources(tmp_path, self.tg)
        self.agents = agent_sources(tmp_path, self.tmux, tools)
        assert self.agents.state is not None
        self.state = self.agents.state

    def agent_row(self, **fields: Any) -> None:
        self.state.update(
            lambda data: data["agents"].setdefault("p1", {}).update(fields)
        )

    def watcher_alert(
        self,
        ts: str,
        severity: str = "warning",
        kind: str = "printer_paused",
        via: str = "heartbeat",
    ) -> None:
        if via == "heartbeat":
            path = self.base / "watch" / "p1+p2.json"
            doc = {
                "pid": 1,
                "printers": {
                    "p1": {"last_event": {"ts": ts, "kind": kind, "severity": severity}}
                },
            }
        else:
            path = self.base / "vision" / "p1.json"
            doc = {
                "schema": 1,
                "printer": "p1",
                "last_event": {"kind": kind, "severity": severity, "ts": _epoch(ts)},
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def subscribe(self, session: str, **kw: Any) -> None:
        assert self.subscriptions.registry is not None
        self.subscriptions.registry.put(
            subs.make(
                session=session,
                printers=["p1"],
                created="2026-10-18T10:00:00+00:00",
                **kw,
            )
        )

    def run(self, options: Options | None = None, **tgt: Any) -> Any:
        r = rig(
            target(watch=tgt.pop("watch", CAMERA), **tgt),
            subscriptions=self.subscriptions,
            agents=self.agents,
        )
        return run_printer(r.ctx, [CHECK], options or Options()).records[0], r


def _epoch(ts: str) -> float:
    import datetime

    return datetime.datetime.fromisoformat(ts).timestamp()


# ---- passing --------------------------------------------------------------------------------------


def test_passes_when_a_live_subscriber_takes_the_alerts_whatever_the_agent_looks_like(
    tmp_path: Path,
) -> None:
    lab = Lab(
        tmp_path, hosts=[LIVE], tools={"tmux": None, "omp": None}
    )  # no tmux, no omp: irrelevant
    lab.subscribe("0aaa1111")
    lab.agent_row(last_error="tmux not found", last_error_ts=T1, consecutive_errors=9)
    rec, r = lab.run()
    assert rec.result.status == "pass" and "not needed" in rec.result.summary
    assert lab.tmux.calls == [] and r.journal == []


def test_passes_with_a_running_agent_that_took_the_newest_watcher_alert(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE], running=True)
    lab.agent_row(last_ts=T1, last_kinds=["printer_paused"], started=T0)
    lab.watcher_alert(T1)
    res = lab.run()[0].result
    assert res.status == "pass" and "running" in res.summary, res.summary
    assert ev(res)["agent"]["running"] is True and ev(res)["problems"] == []
    assert all(
        "has-session" in c for c in lab.tmux.calls
    )  # only asked whether the session exists


def test_passes_with_a_startable_agent_and_nothing_to_compare(tmp_path: Path) -> None:
    lab = Lab(
        tmp_path, hosts=[LIVE]
    )  # no state, no watcher alert yet, not running: starts on demand
    res = lab.run()[0].result
    assert (
        res.status == "pass"
        and "startable on the next alert" in res.summary
        and "no alert yet" in res.summary
    )


def test_an_agent_that_took_a_slightly_older_alert_is_within_the_grace(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.agent_row(last_ts="2026-10-22T20:21:00+00:00")
    lab.watcher_alert(
        "2026-10-22T20:22:00+00:00"
    )  # 60 s later: the agent is still coming up
    assert lab.run()[0].result.status == "pass"


# ---- failing --------------------------------------------------------------------------------------


def test_fails_with_no_subscriber_and_no_tmux(tmp_path: Path) -> None:
    lab = Lab(tmp_path, hosts=[LIVE], tools={"tmux": None, "omp": "/usr/bin/omp"})
    rec, _ = lab.run()
    res = rec.result
    assert (res.status, res.severity) == ("fail", "error")
    assert (
        "tmux is not installed" in res.summary
        and ev(res)["tmux"] is None
        and ev(res)["omp"] == "/usr/bin/omp"
    )
    assert (
        ev(res)["agent"]["running"] is False and lab.tmux.calls == []
    )  # no tmux: nothing to ask
    assert rec.fix is not None and rec.fix.manual_steps[0].startswith("Install tmux")


def test_fails_with_no_omp(tmp_path: Path) -> None:
    lab = Lab(tmp_path, hosts=[LIVE], tools={"tmux": "/usr/bin/tmux", "omp": None})
    res = lab.run()[0].result
    assert res.status == "fail" and "omp is not installed" in res.summary


def test_fails_when_the_agent_took_alerts_before_but_the_newest_watcher_alert_never_reached_it(
    tmp_path: Path,
) -> None:
    """The incident: the agent's last alert is from 10-21, the watcher saw a PAUSED alert on 10-22."""
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.agent_row(
        last_ts=T0, last_kinds=["printer_paused"], stopped="2026-10-21T17:00:00+00:00"
    )
    lab.watcher_alert(T1)
    res = lab.run()[0].result
    assert (res.status, res.severity) == ("fail", "error")
    assert "never reached the agent" in res.summary and "printer_paused" in res.summary
    e = ev(res)
    assert e["watcher_newest_alert"]["ts"].startswith("2026-10-22T") and e["agent"][
        "last_alert"
    ].startswith("2026-10-21T")
    assert (
        e["agent_lag_s"] == int(_epoch(T1) - _epoch(T0)) and e["agent_lag_s"] > GRACE_S
    )


def test_a_dry_run_watcher_is_named_as_the_cause(tmp_path: Path) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.agent_row(last_ts=T0, dry_run_ts=T1, dry_run_events=3)
    lab.watcher_alert(T1)
    rec, _ = lab.run()
    assert (
        rec.result.status == "fail"
        and "--dry-run swallowed 3 alert(s)" in rec.result.summary
    )
    assert rec.fix is not None
    steps = rec.fix.manual_steps
    assert any("systemctl --user cat 3d-printer-watch@p1.service" in s for s in steps)
    assert "$ 3d printer vision doctor p1" in steps


def test_fails_when_the_agent_never_took_any_alert_but_the_watcher_raised_one(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.watcher_alert(
        T1, via="vision", kind="vision_spaghetti"
    )  # the vision loop's status file
    res = lab.run()[0].result
    assert (
        res.status == "fail"
        and "never reached the agent" in res.summary
        and "ever" in res.summary
    )
    assert ev(res)["watcher_newest_alert"]["kind"] == "vision_spaghetti"


def test_fails_on_an_unresolved_start_error_and_reports_the_streak(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.agent_row(
        last_ts=T0,
        last_error="tmux new-session failed: no server",
        last_error_ts=T1,
        consecutive_errors=4,
    )
    res = lab.run()[0].result
    assert (
        res.status == "fail"
        and "(4 in a row)" in res.summary
        and "no server" in res.summary
    )
    assert ev(res)["agent"]["consecutive_errors"] == 4


def test_an_old_error_followed_by_a_success_is_not_a_failure(tmp_path: Path) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.agent_row(
        last_ts=T1,
        last_error="tmux new-session failed",
        last_error_ts=T0,
        consecutive_errors=0,
    )
    assert lab.run()[0].result.status == "pass"


def test_an_unrelated_info_event_never_makes_the_check_fail(tmp_path: Path) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    lab.agent_row(last_ts=T0)
    lab.watcher_alert(
        T1, severity="info", kind="printer_progress"
    )  # the agent never gets info events
    assert lab.run()[0].result.status == "pass"


# ---- skipping / applicability / read-only -------------------------------------------------------------


def test_skips_a_printer_without_a_watcher_an_offline_one_and_unknown_liveness(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE])
    assert lab.run(watch={})[0].result.status == "skip"
    assert lab.run(access="offline")[0].result.status == "skip"
    blind = Lab(tmp_path / "blind")
    blind.tg.missing = True
    blind.subscribe("dead0001")
    res = blind.run()[0].result
    assert (
        res.status == "skip"
        and "cannot tell whether the subscribed sessions are live" in res.summary
    )


def test_a_read_only_run_writes_nothing_and_a_fix_run_executes_nothing(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE], tools={"tmux": None, "omp": None})
    lab.agent_row(last_ts=T0)
    before = lab.state.path.read_text()
    rec, r = lab.run(Options(fix=True, yes=True))
    assert (
        rec.result.status == "fail"
        and rec.fix is not None
        and not rec.fix.applied
        and rec.fix.risk == "manual-only"
    )
    assert (
        lab.state.path.read_text() == before
        and r.journal == []
        and lab.tmux.calls == []
        and lab.tg.sends() == []
    )


def test_manual_steps_name_the_exact_tmux_and_subscribe_commands(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path, hosts=[LIVE], tools={"tmux": "/usr/bin/tmux", "omp": None})
    rec, _ = lab.run()
    assert rec.fix is not None
    steps = rec.fix.manual_steps
    sessions = [s for s in steps if s.startswith("$ tmux -L ")]
    assert len(sessions) == 3 and any(" ls" in s for s in sessions)
    assert any(
        s.startswith("$ tmux -L ") and "attach -t 3d-printer-agent-p1" in s
        for s in steps
    )
    assert any("kill-session -t =3d-printer-agent-p1" in s for s in steps)
    assert (
        "$ 3d printer agent stop p1" in steps
        and "then: 3d printer subscribe --session SESSION_ID --printer p1" in steps
    )
    assert any(s.startswith("Install omp") for s in steps)
    from printer_doctor.model import PrinterReport

    rep = PrinterReport("p1", "full", "moonraker", [rec])
    text = render.to_text([rep], summarize([rep], "error"))
    assert "re-run: 3d printers doctor p1 --check alert-fallback-agent" in text


def test_the_watchers_files_are_read_the_way_the_watchers_write_them(
    tmp_path: Path,
) -> None:
    (tmp_path / "watch").mkdir()
    beat = {
        "printers": {
            "p1": {"last_event": {"ts": T0, "kind": "a", "severity": "warning"}},
            "p2": {"last_event": {"ts": T1, "kind": "b", "severity": "critical"}},
        }
    }
    (tmp_path / "watch" / "p1+p2.json").write_text(json.dumps(beat))
    (tmp_path / "watch" / "broken.json").write_text("{not json")
    (tmp_path / "vision").mkdir()
    (tmp_path / "vision" / "p1.json").write_text(
        json.dumps(
            {"last_event": {"kind": "v", "severity": "critical", "ts": _epoch(T1)}}
        )
    )
    newest = newest_watcher_alert(tmp_path, "p1")
    assert (
        newest is not None
        and newest["kind"] == "v"
        and newest["severity"] == "critical"
    )
    only_beat = newest_watcher_alert(tmp_path, "p2")
    assert only_beat is not None and only_beat["kind"] == "b"
    assert newest_watcher_alert(tmp_path, "p3") is None
