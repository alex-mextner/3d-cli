"""Real smoke of the alert delivery chain (#529): a synthetic warning alert, no live subscriber.

The watcher side runs as a real process (a transient systemd user service when the user manager is
there, which is exactly the incident environment: the unit's cgroup is torn down when it ends);
tmux and systemd-run are the real binaries, `omp` / `tg-ctl` / `tg` are tiny fakes that record what
they get. HOME, every XDG dir and TMUX_TMPDIR are in tmp_path and the agent's tmux server is named
after that state dir, so the production `3d-printer-agent` server is never touched.

Proves: the agent session really starts, is registered as a live omp host, received the alert text
and the frame, and is STILL RUNNING after the watcher process (and its unit) are gone; and that when
no agent can start, the alert reaches the user's `tg` with the frame.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "lib"
TMUX = shutil.which("tmux")
SYSTEMD_RUN = shutil.which("systemd-run")


def _systemd_scope_works() -> bool:
    if not SYSTEMD_RUN:
        return False
    try:
        r = subprocess.run(
            [SYSTEMD_RUN, "--user", "--scope", "--quiet", "true"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


FAKE_OMP = """#!/bin/bash
# a fake interactive omp: record argv (NUL separated: the alert has newlines), cwd and pid, then
# idle like a live session
d={rec}
printf '%s\\0' "$@" > "$d/omp-argv.txt"
pwd > "$d/omp-cwd.txt"
echo $$ > "$d/omp-pid.txt"
exec sleep 3000
"""

FAKE_TGCTL = """#!{python}
import json, os, sys
rec = {rec!r}
if sys.argv[1:3] == ["omp-live", "status"]:
    hosts = []
    if os.path.exists(rec + "/omp-pid.txt"):
        pid = int(open(rec + "/omp-pid.txt").read())
        if os.path.exists("/proc/%d" % pid):   # kill(pid, 0) is EPERM across sandbox domains
            hosts.append({{"pid": pid, "sessionId": "fake-agent-session", "busy": False, "title": "fake agent",
                          "cwd": open(rec + "/omp-cwd.txt").read().strip()}})
    print(json.dumps(hosts))
else:
    a = sys.argv
    print(json.dumps({{"ok": True, "accepted": True, "status": "accepted",
                      "requestId": a[a.index("--request-id") + 1], "sessionId": a[a.index("--session") + 1]}}))
"""

FAKE_TG = """#!{python}
import json, sys
open({rec!r} + "/tg-calls.jsonl", "a").write(json.dumps(sys.argv[1:]) + "\\n")
"""

DRIVER = """
import os, sys
sys.path.insert(0, {lib!r})
from printer_ops.printer_agent import PrinterAgents, PrinterAgentSink, tmux_socket, unavailable_reason
from printer_ops.subscriptions import DeliveryState, SubscriptionSink
from printer_ops.watch import Event
from printer_ops.watch_sinks import Dispatcher, TgSink

open({rec!r} + "/socket", "w").write(tmux_socket())
state = DeliveryState()
agents = PrinterAgents(state=state, ready_timeout_s=30)
tg = TgSink(binary={tg!r})
tg.ledger = state
tg.attach("u1", why=lambda: unavailable_reason("u1", state))
d = Dispatcher(dedup_s=0)
d.add("u1", [SubscriptionSink(state=state), PrinterAgentSink(agents=agents), tg])
event = Event("u1", "vision_spaghetti", "warning", "u1: possible spaghetti on camera (smoke)",
              {{"frame": {frame!r}}}, key="u1:smoke")
d.process([event])
d.close(40)
print("driver done", flush=True)
"""


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.rec = tmp / "rec"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        # a unix socket path is limited to ~107 bytes: pytest's tmp dirs are too deep for tmux's
        self.tmux_dir = Path(tempfile.mkdtemp(prefix="adtmux-"))
        for d in (self.rec, self.bin, self.home, tmp / "state"):
            d.mkdir(parents=True, exist_ok=True)
        self.frame = tmp / "u1-smoke.jpg"
        self.frame.write_bytes(b"\xff\xd8 smoke frame")
        (self.bin / "tg-ctl").write_text(
            FAKE_TGCTL.format(python=sys.executable, rec=str(self.rec))
        )
        (self.bin / "tg").write_text(
            FAKE_TG.format(python=sys.executable, rec=str(self.rec))
        )
        for name in ("tg-ctl", "tg"):
            (self.bin / name).chmod(0o755)

    def with_omp(self) -> None:
        omp = self.bin / "omp"
        omp.write_text(FAKE_OMP.format(rec=str(self.rec)))
        omp.chmod(0o755)

    @property
    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "TMUX_TMPDIR": str(self.tmux_dir),
            "THREED_TEST_REAL_PRINTER_AGENT": "1",
            "PYTHONUNBUFFERED": "1",
        }

    def driver(self) -> Path:
        path = self.tmp / "driver.py"
        path.write_text(
            textwrap.dedent(DRIVER).format(
                lib=str(LIB),
                rec=str(self.rec),
                tg=str(self.bin / "tg"),
                frame=str(self.frame),
            )
        )
        return path

    def run_as_service(self, unit: str) -> subprocess.CompletedProcess[str]:
        """The watcher process as a transient user service: when it exits systemd tears down its cgroup."""
        props = [x for k, v in self.env.items() for x in ("-p", f"Environment={k}={v}")]
        done = subprocess.run(
            [
                SYSTEMD_RUN or "systemd-run",
                "--user",
                "--wait",
                "--collect",
                "--quiet",
                f"--unit={unit}",
                *props,
                sys.executable,
                str(self.driver()),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        journal = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        return subprocess.CompletedProcess(
            done.args, done.returncode, done.stdout, done.stderr + journal
        )

    def run_plain(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.driver())],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def tg_calls(self) -> list[list[str]]:
        path = self.rec / "tg-calls.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.is_file()
            else []
        )

    def kill_agent_server(self) -> None:
        sock = self.rec / "socket"
        if TMUX and sock.is_file():
            subprocess.run(
                [TMUX, "-L", sock.read_text(), "kill-server"],
                env={**os.environ, **self.env},
                capture_output=True,
                timeout=20,
            )


@pytest.fixture()
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    yield r
    r.kill_agent_server()
    shutil.rmtree(r.tmux_dir, ignore_errors=True)


def _alive(pid: int) -> bool:
    # kill(pid, 0) says EPERM for a process in another sandbox/signal domain: that is "alive" too
    return os.path.exists(f"/proc/{pid}")


@pytest.mark.skipif(
    not TMUX or not _systemd_scope_works(),
    reason="needs tmux and a systemd user manager",
)
def test_a_warning_alert_with_no_subscriber_starts_an_agent_that_outlives_the_watcher_unit(
    rig: Rig,
) -> None:
    rig.with_omp()
    unit = f"alert-delivery-smoke-{os.getpid()}"
    done = rig.run_as_service(unit)
    assert done.returncode == 0, done.stderr
    # the watcher unit has ended and its cgroup was torn down; the agent must still be there
    argv = (rig.rec / "omp-argv.txt").read_text().split("\0")[:-1]
    pid = int((rig.rec / "omp-pid.txt").read_text())
    sock = (rig.rec / "socket").read_text()
    time.sleep(1.0)
    assert _alive(pid), "the agent died with the watcher unit"
    has = subprocess.run(
        [TMUX or "tmux", "-L", sock, "has-session", "-t", "=3d-printer-agent-u1"],
        env={**os.environ, **rig.env},
        capture_output=True,
        timeout=20,
    )
    assert has.returncode == 0
    # it received the alert text and the frame as its opening message
    assert argv[0] == "--append-system-prompt" and argv[1].endswith(
        "printer-agents/u1/role.md"
    )
    assert (
        "[3d printer alert -> temporary u1 agent]" in argv[2]
        and "possible spaghetti on camera (smoke)" in argv[2]
    )
    assert f"@{rig.frame}" in argv
    assert Path((rig.rec / "omp-cwd.txt").read_text().strip()).name == "u1"
    # the user was NOT messaged: an agent took it
    assert rig.tg_calls() == []
    # the same state a later `3d printer agent list` shows
    status = subprocess.run(
        [rig.bin / "tg-ctl", "omp-live", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(status.stdout)[0]["pid"] == pid


def test_when_no_agent_can_start_the_alert_reaches_the_user_through_tg_with_the_frame(
    rig: Rig,
) -> None:
    # no omp on PATH (nor in the tmp HOME): the temporary agent cannot be spawned
    done = rig.run_plain()
    assert done.returncode == 0, done.stderr
    assert (
        "ALERT NOT DELIVERED TO AN AGENT (agent_start_failed): omp not found"
        in done.stderr
    )
    (call,) = rig.tg_calls()
    assert call[:2] == ["--tag", "problem"] and call[2:4] == ["--photo", str(rig.frame)]
    assert call[-1].startswith(
        "[агент недоступен] printer [WARNING] u1: possible spaghetti on camera (smoke)"
    )
    assert "omp not found" in call[-1]
    state = json.loads(
        (rig.tmp / "state" / "3d-cli" / "printer-subscriptions.json").read_text()
    )
    assert state["counters"]["u1"] == {"agent_start_failed": 1, "fallback_tg_sent": 1}
