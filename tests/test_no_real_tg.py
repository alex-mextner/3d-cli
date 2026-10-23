"""Guard: no test can run a real tg / tg-ctl / omp / tmux / systemd-run / omp-steer.

The autouse `_fake_tool_bins` fixture (tests/fake_tool_bins.py) puts recording fakes first on PATH; a
pytest run once sent a real "CRITICAL u1 ..." Telegram message to the user through the tg_fallback
sink (ticket https://github.com/alex-mextner/3d-cli/issues/536)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fake_tool_bins import FAKE_TOOLS

from printer_ops import printer_agent, watch_sinks
from printer_ops.watch import Event
from printer_ops.watch_sinks import TgSink


def _fake_dir() -> Path:
    return Path(shutil.which("tg") or "/nonexistent").resolve().parent


def _inside(path: str | None, directory: Path) -> bool:
    return path is not None and Path(path).resolve().parent == directory


def _lines(log: Path) -> list[dict[str, object]]:
    return (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


@pytest.mark.parametrize("name", FAKE_TOOLS)
def test_tool_resolves_inside_the_fixture_dir(name: str, _fake_bin_dir: Path) -> None:
    fake = (_fake_bin_dir / "bin").resolve()
    assert _inside(shutil.which(name), fake), (
        f"real {name} reachable: {shutil.which(name)}"
    )
    for module in (printer_agent, watch_sinks):
        find_tool = getattr(module, "find_tool", None)
        if find_tool is not None:
            assert _inside(find_tool(name), fake), (
                f"{module.__name__}.find_tool({name!r}) escaped"
            )
        assert getattr(module, "_TOOL_DIRS", ()) == ()


def test_which_tg_is_never_outside_the_fixture_dir(_fake_bin_dir: Path) -> None:
    assert _fake_dir() == (_fake_bin_dir / "bin").resolve()


def test_tg_sink_with_a_runner_records_into_the_fake_log(_fake_tool_bins: Path) -> None:
    event = Event(
        printer="u1", kind="print_failed", severity="critical", text="boom", key="k"
    )
    TgSink(runner=subprocess.run).deliver([event])
    calls = _lines(_fake_tool_bins)
    assert [c["tool"] for c in calls] == ["tg"]
    assert os.environ["FAKE_BIN_LOG"] == str(_fake_tool_bins)


def test_tg_sink_default_runner_refuses_the_real_binary_under_pytest() -> None:
    event = Event(
        printer="u1", kind="print_failed", severity="critical", text="boom", key="k"
    )
    with pytest.raises(RuntimeError, match="refusing to run the real tg from a test"):
        TgSink().deliver([event])


def test_fake_tg_ctl_and_tmux_behave(_fake_tool_bins: Path) -> None:
    out = subprocess.run(
        ["tg-ctl", "omp-live", "status", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == []
    assert (
        subprocess.run(["tmux", "has-session", "-t", "x"], check=False).returncode == 1
    )
    assert subprocess.run(["tmux", "new-session"], check=False).returncode == 0
    assert [c["tool"] for c in _lines(_fake_tool_bins)] == ["tg-ctl", "tmux", "tmux"]


def test_fake_systemd_run_runs_the_wrapped_command(_fake_tool_bins: Path) -> None:
    """lib/cli/heavy.py wraps commands in `systemd-run --scope`; the fake must still run them."""
    args = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=1G", "--", "sh", "-c", "exit 7"]
    assert subprocess.run(args, check=False).returncode == 7
    assert [c["tool"] for c in _lines(_fake_tool_bins)] == ["systemd-run"]
