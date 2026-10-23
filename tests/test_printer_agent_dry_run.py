"""The temporary printer agent and a `--dry-run` watcher (Refs #526).

On 2026-10-22 a watcher running with `--dry-run` journalled `[3d printer alert -> temporary u1 agent]` for a
PAUSED alert while no agent started: the sink's dry-run branch reported the alert as reached and left no trace
anywhere `3d printer agent list` looks. These tests pin the trace, the error streak and their reset."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from printer_ops import printer_agent
from printer_ops.printer_agent import PrinterAgents, PrinterAgentSink, agent_dir
from printer_ops.subscriptions import DeliveryState
from printer_ops.watch import Event


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


def nop(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, "", "")


def paused(printer: str = "u1") -> Event:
    return Event(
        printer,
        "printer_paused",
        "warning",
        f"{printer}: PAUSED",
        {},
        key=f"{printer}:paused",
    )


def test_a_dry_run_watcher_leaves_a_trace_and_starts_nothing(
    xdg: Path, capsys: Any
) -> None:
    events = [paused(), paused()]
    assert PrinterAgentSink(dry_run=True).deliver(events) == events  # reported as reached: holds the tg fallback back
    err = capsys.readouterr().err
    assert (
        "dry-run temporary u1 agent" in err
        and "[3d printer alert -> temporary u1 agent]" in err
    )
    row = DeliveryState().agent("u1")
    assert row["dry_run_events"] == 2 and row["dry_run_ts"]
    assert not agent_dir("u1").exists()  # no events.jsonl, no role.md, no tmux


def test_dry_run_counts_accumulate_per_printer(xdg: Path) -> None:
    sink = PrinterAgentSink(dry_run=True)
    sink.deliver([paused("u1")])
    sink.deliver([paused("u1"), paused("k1se")])
    assert DeliveryState().agent("u1")["dry_run_events"] == 2
    assert DeliveryState().agent("k1se")["dry_run_events"] == 1


def test_a_real_delivery_clears_the_dry_run_marker_and_the_error_streak(
    xdg: Path,
) -> None:
    agents = PrinterAgents(runner=nop)
    agents.record_dry_run("u1", [paused()])
    agents.record_error("u1", "tmux new-session failed: boom")
    agents.record_error("u1", "tmux new-session failed: boom")
    row = agents.state.agent("u1")
    assert (row["consecutive_errors"], row["dry_run_events"]) == (2, 1)
    agents.record_success("u1", [paused()])
    row = agents.state.agent("u1")
    assert (
        row["consecutive_errors"] == 0
        and row["dry_run_events"] is None
        and row["dry_run_ts"] is None
    )
    assert row["last_ts"] and row["last_kinds"] == ["printer_paused"]
    assert row["last_error"] == "tmux new-session failed: boom"  # kept for the record


def test_a_failed_delivery_counts_in_a_row(xdg: Path) -> None:
    class Boom(PrinterAgents):
        def deliver(self, *a: Any, **k: Any) -> str:
            raise RuntimeError(
                "tmux not found: the temporary printer agent runs in a tmux session"
            )

    sink = PrinterAgentSink(agents=Boom(runner=nop))
    for _ in range(3):
        assert sink.deliver([paused()]) == []
    row = DeliveryState().agent("u1")
    assert row["consecutive_errors"] == 3 and "tmux not found" in row["last_error"]


def test_agent_list_says_when_a_dry_run_watcher_swallowed_alerts(
    xdg: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir("u1").mkdir(parents=True)
    PrinterAgentSink(dry_run=True).deliver([paused(), paused(), paused()])
    PrinterAgents().record_error("u1", "boom")
    PrinterAgents().record_error("u1", "boom")
    monkeypatch.setattr(PrinterAgents, "running", lambda self, p: False)
    monkeypatch.setattr(PrinterAgents, "host", lambda self, p, hosts=None: None)
    from printer_ops.watch_sinks import OmpLive

    monkeypatch.setattr(OmpLive, "sessions", lambda self: [])
    capsys.readouterr()
    assert printer_agent.run(["list"]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN: 3 alert(s) were swallowed by a `--dry-run` watcher" in out
    assert "3d printers doctor u1 --check watch-units-dry-run" in out
    assert "last error:" in out and "(2 in a row)" in out
    printer_agent.run(["list", "--json"])
    assert '"dry_run_events": 3' in capsys.readouterr().out


def test_help_mentions_the_dry_run_line() -> None:
    assert (
        "DRY-RUN" in printer_agent.USAGE
        and "alert-fallback-agent" in printer_agent.USAGE
    )
