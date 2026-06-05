"""Unit tests for commands.metrics — usage metrics."""
from __future__ import annotations

from typing import Any

import pytest
from commands.metrics import run
from errors import UsageError


def test_metrics_no_args() -> None:
    assert run([]) == 1


def test_metrics_help() -> None:
    assert run(["--help"]) == 0


def test_metrics_list(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("metrics.list_metric_files", lambda: [{"command": "render", "records": 5, "latest": "2026-01-01", "path": "/tmp/metrics.jsonl"}])
    assert run(["list"]) == 0


def test_metrics_show(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("metrics.read_records", lambda command=None, limit=None: [{"cmd": "render"}])
    assert run(["show"]) == 0


def test_metrics_show_limit() -> None:
    assert run(["show", "--limit", "5"]) == 0


def test_metrics_show_bad_limit() -> None:
    with pytest.raises(UsageError):
        run(["show", "--limit", "abc"])


def test_metrics_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["show", "--bogus"])
