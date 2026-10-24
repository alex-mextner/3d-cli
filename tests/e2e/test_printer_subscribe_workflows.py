"""Readable `bin/3d printer subscribe` stories: printer alerts reach an AI agent before the user.

A fake `tg-ctl` first on PATH stands in for the omp-live bridge: `omp-live status --json`
lists two sessions, `omp-live send` appends its argv to sent.jsonl. No printer is contacted.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from printer_fakes import FakeMoonrakerWS, u1_ws_status

from .workflow_helper import agent_fakebin, agent_spawns, run_cli

SESSIONS = [
    {"pid": 433553, "sessionId": "01a12cd6-7b39-7356", "cwd": "/tmp", "title": "MODEL: Finish 3D model"},
    {"pid": 14610, "sessionId": "01a11af0-c495-768c", "cwd": "/tmp", "title": "FREEZE: Diagnose"},
]


def _fake_tgctl(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    sent = tmp_path / "sent.jsonl"
    script = bindir / "tg-ctl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:3] == ['omp-live', 'status']:\n"
        f"    print({json.dumps(SESSIONS)!r})\n"
        "else:\n"
        f"    open({str(sent)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "    a = sys.argv\n"
        "    print(json.dumps({'ok': True, 'accepted': True, 'status': 'accepted', "
        "'requestId': a[a.index('--request-id') + 1], 'sessionId': a[a.index('--session') + 1]}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp_path / "xdg-state")}, sent


def test_agent_subscribes_its_session_and_receives_a_u1_camera_alert_with_the_frame(tmp_path: Path) -> None:
    """The main agent subscribes its omp session to U1 camera alerts, checks it is live, then a
    synthetic noodle alert arrives in the session with the frame path and the triage steps."""
    env, sent = _fake_tgctl(tmp_path)
    frame = tmp_path / "u1-noodle.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xd9")

    sub = run_cli(tmp_path, "printer", "subscribe", "--session", "01a12cd6", "--printer", "u1",
                  "--kinds", "vision_*,printer_noodle_verdict", "--json", env_extra=env)
    again = run_cli(tmp_path, "printer", "subscribe", "--session", "01a12cd6", "--printer", "u1",
                    "--kinds", "printer_noodle_verdict,vision_*", "--note", "main session", env_extra=env)
    listed = run_cli(tmp_path, "printer", "subscriptions", "--json", env_extra=env)
    test = run_cli(tmp_path, "printer", "subscriptions", "test", "u1", "--frame", str(frame), "--send",
                   env_extra=env)
    human = run_cli(tmp_path, "printer", "subscriptions", env_extra=env)

    assert sub.returncode == 0, sub.stderr
    entry = json.loads(sub.stdout)
    assert entry["new"] is True and entry["live"]["pid"] == 433553
    assert again.stdout.startswith(f"updated {entry['id']}: session 01a12cd6")
    (row,) = json.loads(listed.stdout)
    assert row["kinds"] == ["printer_noodle_verdict", "vision_*"] and row["note"] == "main session"
    assert row["live"]["live"] is True
    registry = (tmp_path / "xdg-config" / "3d-cli" / "subscriptions.yaml").read_text(encoding="utf-8")
    assert "session: 01a12cd6" in registry
    assert test.returncode == 0, test.stderr
    assert "a live subscriber took it: yes" in test.stdout
    (send,) = [json.loads(line) for line in sent.read_text(encoding="utf-8").splitlines()]
    assert send[:3] == ["omp-live", "send", "433553"] and "01a12cd6-7b39-7356" in send
    message = send[-1]
    assert message.startswith(f"[3d printer alert -> subscriber {entry['id']}] u1 vision_spaghetti (warning)")
    assert f"frames: {frame}" in message and "Open every frame above with your read tool" in message
    assert "do not message the user" in message and "Never pause, cancel or send G-code" in message
    assert "live: yes — pid 433553" in human.stdout and "(1 delivered)" in human.stdout


def test_dead_session_leaves_the_alert_to_the_printer_agent_and_unsubscribe_cleans_up(tmp_path: Path) -> None:
    """An agent's session went away: `subscriptions` says so, a test alert is not taken by any
    live subscriber (the printer's temporary agent would get it), and unsubscribing by session
    removes the entry."""
    env, sent = _fake_tgctl(tmp_path)

    run_cli(tmp_path, "printer", "subscribe", "--session", "deadbeef", "--kinds", "vision_*", env_extra=env)
    listed = run_cli(tmp_path, "printer", "subscriptions", env_extra=env)
    test = run_cli(tmp_path, "printer", "subscriptions", "test", "u1", "--send", env_extra=env)
    unmatched = run_cli(tmp_path, "printer", "subscriptions", "test", "u1", "--kind", "bed_off", env_extra=env)
    gone = run_cli(tmp_path, "printer", "unsubscribe", "--session", "deadbeef", env_extra=env)
    empty = run_cli(tmp_path, "printer", "subscriptions", env_extra=env)

    assert "live: NO — no live omp session matches 'deadbeef'" in listed.stdout
    assert test.returncode == 1 and "a live subscriber took it: no (the u1 printer agent would get it)" in test.stdout
    assert "no live omp session matches 'deadbeef'" in test.stderr
    assert not sent.exists()
    state = json.loads((tmp_path / "xdg-state" / "3d-cli" / "printer-subscriptions.json").read_text(encoding="utf-8"))
    assert "deadbeef" in next(iter(state["subscriptions"].values()))["last_error"]
    assert unmatched.returncode == 1 and "no subscription matches u1 bed_off" in unmatched.stdout
    assert gone.stdout.startswith("removed s") and empty.stdout.startswith("no subscriptions")


def test_subscribe_help_and_usage_errors_are_explicit(tmp_path: Path) -> None:
    """A user reads how to subscribe and how the temporary printer agent works, and a wrong
    call (a removed flag) says exactly what is wrong."""
    env, _ = _fake_tgctl(tmp_path)
    helped = run_cli(tmp_path, "printer", "subscribe", "--help")
    watch_help = run_cli(tmp_path, "printer", "watch", "--help")
    agent_help = run_cli(tmp_path, "printer", "agent", "--help")
    agents = run_cli(tmp_path, "printer", "agent", "list", env_extra=env)
    spawn = run_cli(tmp_path, "printer", "subscribe", "--session", "x", "--spawn")

    assert "3d printer subscribe --session 01a12cd6 --printer u1" in helped.stdout
    assert "watch.tg_fallback: false" in watch_help.stdout and "--tg" not in watch_help.stdout
    assert "tmux -L 3d-printer-agent attach -t 3d-printer-agent-u1" in agent_help.stdout
    assert agents.returncode == 0 and agents.stdout.strip() == "no temporary printer agents"
    assert spawn.returncode == 2 and "unknown option --spawn" in spawn.stderr
    assert agent_help.stdout.count("3d-printer-agent-<hash>") == 1   # the socket isolation is documented


@pytest.fixture()
def paused_u1(tmp_path: Path) -> Iterator[None]:
    """u1 = a loopback Moonraker whose job is paused: the watch's first reading is an `is PAUSED` WARNING,
    which no subscriber takes, so it goes to the temporary printer agent. (An unreachable printer is no
    warning any more: a switched-off printer is normal, #640.)"""
    with FakeMoonrakerWS(status=u1_ws_status("paused")).serve() as fake:
        d = tmp_path / "xdg-config" / "3d-cli"
        d.mkdir(parents=True, exist_ok=True)
        (d / "printer-devices.yaml").write_text(
            f"u1:\n  adapter: moonraker\n  host: 127.0.0.1\n  port: {fake.port}\n", encoding="utf-8")
        yield


def test_watch_under_pytest_never_starts_a_real_printer_agent(tmp_path: Path, paused_u1: None) -> None:
    """#399: `3d printer watch` in a test started a real omp agent as 3d-printer-agent-u1 on the
    production tmux server. Now the agent refuses under pytest: no tmux/omp/systemd-run runs (the
    recording fakes first on PATH see nothing) and the refusal is logged; the watch carries on."""
    watch = run_cli(tmp_path, "printer", "watch", "u1", "--duration", "4", "--heartbeat", str(tmp_path / "hb.json"))

    assert watch.returncode == 0, watch.stderr[-2000:]
    events = [json.loads(x) for x in watch.stdout.splitlines() if x.strip().startswith("{")]
    assert any(e["kind"] == "state" and e["severity"] == "warning" for e in events), watch.stdout
    assert "printer-agent: u1: refusing to run the real tmux/omp for the temporary printer agent under a test" \
        in watch.stderr, watch.stderr[-2000:]
    assert agent_spawns(tmp_path) == []
    assert not (tmp_path / "xdg-state" / "3d-cli" / "printer-agents" / "u1").exists()


def test_the_test_override_runs_the_agent_on_its_own_tmux_server(tmp_path: Path, paused_u1: None) -> None:
    """With THREED_TEST_REAL_PRINTER_AGENT=1 the spawn path runs — against the fake tmux/omp — and
    every tmux call goes to a server named after the tmp state dir, under the tmp TMUX_TMPDIR,
    never `-L 3d-printer-agent`. The fake tg-ctl reports the agent up once tmux created it."""
    agent_cwd = tmp_path / "xdg-state" / "3d-cli" / "printer-agents" / "u1"
    bindir = tmp_path / "tgbin"
    bindir.mkdir()
    tgctl = bindir / "tg-ctl"
    tgctl.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"log = {str(tmp_path / 'agent-spawns.jsonl')!r}\n"
        "rows = [json.loads(x) for x in open(log)] if os.path.exists(log) else []\n"
        "up = any(r['argv'][0] == 'tmux' and 'new-session' in r['argv'] for r in rows)\n"
        f"print(json.dumps([{{'pid': 4242, 'sessionId': 'agent', 'cwd': {str(agent_cwd)!r}, 'title': 'a'}}] if up else []))\n",
        encoding="utf-8")
    tgctl.chmod(0o755)
    env = {"THREED_TEST_REAL_PRINTER_AGENT": "1", "PATH": f"{bindir}:{os.environ['PATH']}"}
    watch = run_cli(tmp_path, "printer", "watch", "u1", "--duration", "4", "--heartbeat", str(tmp_path / "hb.json"),
                    env_extra=env)
    listed = run_cli(tmp_path, "printer", "agent", "list", env_extra=env)

    assert watch.returncode == 0, watch.stderr[-2000:]
    assert "printer-agent: u1: starting the u1 agent" in watch.stderr, watch.stderr[-2000:]
    spawns = agent_spawns(tmp_path)
    tmux = [r for r in spawns if r["argv"][0] == "tmux"]
    sockets = {r["argv"][2] for r in tmux}
    assert len(sockets) == 1 and all(r["argv"][1] == "-L" for r in tmux), spawns
    (sock,) = sockets
    assert sock.startswith("3d-printer-agent-") and sock != "3d-printer-agent"
    assert {r["TMUX_TMPDIR"] for r in tmux} == {str(tmp_path / "tmux")}
    (new,) = [r["argv"] for r in tmux if "new-session" in r["argv"]]
    assert new[new.index("-s") + 1] == "3d-printer-agent-u1" and new[new.index("-c") + 1] == str(agent_cwd)
    assert new[new.index("--") + 1] == str(agent_fakebin(tmp_path) / "omp")   # the fake omp, never a real one
    assert [r["argv"][0] for r in spawns if r["argv"][0] != "tmux"] == ["systemd-run"]   # omp itself never ran
    assert f"attach: tmux -L {sock} attach -t 3d-printer-agent-u1" in listed.stdout, listed.stdout + listed.stderr



def test_agent_stop_and_handoff_refuse_cleanly_under_pytest_without_touching_state(tmp_path: Path) -> None:
    """`printer agent stop`/`handoff` in a test: a one-line error (exit 1, no traceback) and the
    delivery state is exactly as it was: the refusal comes before the restart-limit slot is freed
    or `handed_over`/`stopped` is recorded for an agent nothing killed."""
    state = tmp_path / "xdg-state" / "3d-cli" / "printer-subscriptions.json"
    state.parent.mkdir(parents=True)
    body = json.dumps({"subscriptions": {}, "agents": {"u1": {"start_claimed": "2026-10-21T16:08:00+00:00"}}})
    state.write_text(body, encoding="utf-8")

    stop = run_cli(tmp_path, "printer", "agent", "stop", "u1")
    handoff = run_cli(tmp_path, "printer", "agent", "handoff", "u1", "--summary", "false alarm")

    for res in (stop, handoff):
        assert res.returncode == 1, res.stderr
        assert "Traceback" not in res.stderr and "refusing to run the real tmux/omp" in res.stderr, res.stderr
    assert state.read_text(encoding="utf-8") == body
    assert agent_spawns(tmp_path) == []
