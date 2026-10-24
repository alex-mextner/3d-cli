"""The watcher tells the operator at start whether Telegram works, with the real `3d printer watch` and a fake `tg`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from printer_fakes import free_port

from .workflow_helper import run_cli


def _u1_on_a_closed_port(tmp_path: Path) -> None:
    port = free_port()
    d = tmp_path / "xdg-config" / "3d-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "printer-devices.yaml").write_text(
        f"u1:\n  adapter: moonraker\n  host: 127.0.0.1\n  port: {port}\n", encoding="utf-8"
    )


def _fake_tg(tmp_path: Path, exit_code: int) -> dict[str, str]:
    """A `tg` on PATH that logs its argv and exits `exit_code`; PYTEST_CURRENT_TEST is blanked so the
    watcher's guard against messaging the real user lets the fake run."""
    bindir = tmp_path / "tgbin"
    bindir.mkdir(exist_ok=True)
    tg = bindir / "tg"
    tg.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"with open({str(tmp_path / 'tg-calls.jsonl')!r}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.stderr.write('tg: --tag answer needs --reply-to' if {exit_code} else '')\nsys.exit({exit_code})\n",
        encoding="utf-8",
    )
    tg.chmod(0o755)
    # PATH holds only the fake and the system dirs: the owner's real tg (~/.files/bin) cannot be reached by any
    # lookup while the pytest guard (watch_sinks.run_real_tg: PYTEST_CURRENT_TEST set) is lifted for the fake.
    return {"PATH": f"{bindir}:/usr/bin:/bin", "PYTEST_CURRENT_TEST": ""}


def _tg_calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "tg-calls.jsonl"
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def test_watcher_start_sends_one_test_message_and_says_it_was_delivered(tmp_path: Path) -> None:
    """The operator starts the watcher for u1; Telegram works.

    One `[TEST]` message goes to tg with the same flags an alert uses, and the log says it was delivered.
    """
    _u1_on_a_closed_port(tmp_path)
    watch = run_cli(
        tmp_path,
        "printer",
        "watch",
        "u1",
        "--duration",
        "2",
        "--heartbeat",
        str(tmp_path / "hb.json"),
        env_extra=_fake_tg(tmp_path, 0),
    )
    tests = [c for c in _tg_calls(tmp_path) if c[-1].startswith("[TEST]")]
    assert watch.returncode == 0, watch.stderr[-1500:]
    assert len(tests) == 1 and tests[0][:2] == ["--tag", "problem"]
    assert "Telegram test message for u1 delivered" in watch.stderr
    assert "FAILED" not in watch.stderr


def test_watcher_with_a_tg_that_refuses_says_so_loudly_and_keeps_watching(tmp_path: Path) -> None:
    """Telegram is broken (the Chekhov case: tg exits non-zero for every message).

    The watcher logs an ERROR at start, keeps watching, and the heartbeat file carries the failure for `doctor` and humans.
    """
    _u1_on_a_closed_port(tmp_path)
    heartbeat = tmp_path / "hb.json"
    watch = run_cli(
        tmp_path,
        "printer",
        "watch",
        "u1",
        "--duration",
        "2",
        "--heartbeat",
        str(heartbeat),
        env_extra=_fake_tg(tmp_path, 1),
    )
    beat = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert watch.returncode == 0, watch.stderr[-1500:]
    assert "ERROR: the Telegram test message FAILED" in watch.stderr and "--tag answer needs --reply-to" in watch.stderr
    assert beat["sinks"]["tg"]["failed"] >= 1
    assert beat["sinks"]["tg"]["last_error"]


def test_dry_run_watcher_sends_no_test_message(tmp_path: Path) -> None:
    """`--dry-run` prints what would be sent and delivers nothing, so a Telegram test message would be a lie.

    The fake tg is never called.
    """
    _u1_on_a_closed_port(tmp_path)
    watch = run_cli(
        tmp_path,
        "printer",
        "watch",
        "u1",
        "--duration",
        "2",
        "--dry-run",
        "--heartbeat",
        str(tmp_path / "hb.json"),
        env_extra=_fake_tg(tmp_path, 0),
    )
    assert watch.returncode == 0, watch.stderr[-1500:]
    assert _tg_calls(tmp_path) == []
