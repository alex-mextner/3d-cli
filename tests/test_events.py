from __future__ import annotations

import json

import pytest

import events
from commands import events as events_cmd
from errors import InvalidArgument, UsageError


@pytest.fixture(autouse=True)
def _sandbox_data(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def test_record_event_appends_jsonl_entry() -> None:
    event = events.record_event(
        "cli.render",
        subject="examples/cube.scad",
        status="pass",
        message="rendered left view",
        data={"view": "left"},
    )

    assert event["id"]
    assert event["type"] == "cli.render"
    assert event["source"] == "cli"
    assert event["subject"] == "examples/cube.scad"
    assert event["status"] == "pass"
    assert event["message"] == "rendered left view"
    assert event["data"] == {"view": "left"}

    lines = events.events_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == event


def test_query_events_filters_and_orders_newest_first() -> None:
    older = events.record_event("model.match", subject="a.scad", status="fail")
    newer = events.record_event("cli.render", subject="b.scad", status="pass")

    result = events.query_events(event_type="cli.render", limit=5)

    assert result == [newer]
    assert events.query_events(status="fail") == [older]
    assert events.query_events(subject="missing.scad") == []


def test_query_events_since_filters_by_timestamp() -> None:
    events.record_event("cli.render", subject="old.scad", timestamp="2026-01-01T00:00:00+00:00")
    recent = events.record_event("cli.render", subject="new.scad", timestamp="2026-01-02T00:00:00+00:00")

    result = events.query_events(since="2026-01-01T12:00:00+00:00")

    assert result == [recent]


def test_query_events_sorts_explicit_timestamps_before_limiting() -> None:
    newer = events.record_event("cli.render", subject="new.scad", timestamp="2026-01-02T00:00:00+00:00")
    older = events.record_event("cli.render", subject="old.scad", timestamp="2026-01-01T00:00:00+00:00")

    assert events.query_events(limit=1) == [newer]
    assert events.query_events() == [newer, older]


def test_record_rejects_empty_event_type() -> None:
    with pytest.raises(InvalidArgument):
        events.record_event("")


def test_cmd_help_returns_zero(capsys) -> None:
    assert events_cmd.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "events" in out
    assert "record" in out


def test_cmd_no_args_shows_usage_nonzero(capsys) -> None:
    assert events_cmd.run([]) == 1
    assert "3d events" in capsys.readouterr().out


def test_cmd_record_then_list(capsys) -> None:
    assert events_cmd.run(
        [
            "record",
            "--type",
            "cli.render",
            "--subject",
            "examples/cube.scad",
            "--status",
            "pass",
            "--message",
            "rendered left view",
            "--data",
            "view=left",
        ]
    ) == 0
    capsys.readouterr()

    assert events_cmd.run(["list", "--type", "cli.render"]) == 0
    out = capsys.readouterr().out
    assert "cli.render" in out
    assert "examples/cube.scad" in out
    assert "rendered left view" in out


def test_cmd_query_outputs_jsonl(capsys) -> None:
    events.record_event("model.match", subject="part.scad", data={"round": "2"})

    assert events_cmd.run(["query", "--type", "model.match"]) == 0
    out = capsys.readouterr().out.strip()

    row = json.loads(out)
    assert row["type"] == "model.match"
    assert row["subject"] == "part.scad"
    assert row["data"] == {"round": "2"}


def test_cmd_invalid_limit_raises_structured_error() -> None:
    with pytest.raises(InvalidArgument):
        events_cmd.run(["list", "--limit", "zero"])


def test_cmd_rejects_value_flag_followed_by_another_flag() -> None:
    with pytest.raises(UsageError):
        events_cmd.run(["record", "--type", "--status", "pass"])
