"""`3d printer supervise` (printer_ops/supervise.py, supervise_units.py): the always-on supervision is shipped from
the repo, installed without breaking hand-made files, judged from systemd + heartbeats + the printer, and a dead
supervisor is reported once. systemd, loginctl, tg and the printer are fakes: nothing here reaches the real ones."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from errors import GateFailure, UsageError
from printer_doctor_fakes import device, fleet_printer
from printer_supervise_fakes import (
    CAMERA,
    LAYOUT,
    NOW,
    Runner,
    Systemd,
    Tg,
    assess_status,
    beats,
    env_for,
    healthy,
    plan,
    unit_show,
    write_vision_beat,
    write_watch_beat,
)
from printer_ops import intentional_off
from printer_ops import supervise as sv
from printer_ops import supervise_units as su
from printops.fleet import Fleet

# ---- the shipped units -------------------------------------------------------------------------


WATCH_SNAPSHOT = """# managed-by: 3d printer supervise install (do not edit; override with a drop-in)
[Unit]
Description=3D printer watch %i (always on: Klipper state, errors, pauses, print start; AI alerts + auto pause)
Documentation=https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer.md#always-on-supervision
After=network-online.target hsw-observer-agent.service
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=PATH=/srv/3d-cli/.venv/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/srv/3d-cli
UMask=0077
ExecStart=/srv/3d-cli/.venv/bin/python /srv/3d-cli/bin/3d printer watch %i
ExecStopPost=-/srv/3d-cli/.venv/bin/python /srv/3d-cli/bin/3d printer supervise notify-failure %i watch
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""


def test_the_watch_unit_is_a_snapshot_of_the_shipped_definition() -> None:
    assert su.render_template("watch", LAYOUT) == WATCH_SNAPSHOT


def test_the_vision_unit_differs_only_in_command_description_and_restart() -> None:
    watch, vision = (
        su.render_template("watch", LAYOUT),
        su.render_template("vision", LAYOUT),
    )
    assert (
        "ExecStart=/srv/3d-cli/.venv/bin/python /srv/3d-cli/bin/3d printer vision %i\n"
        in vision
    )
    assert (
        "notify-failure %i vision" in vision
        and "RestartSec=30" in vision
        and "Description=3D printer vision %i" in vision
    )
    assert [
        ln
        for ln in watch.splitlines()
        if ln.startswith(("After=", "StartLimit", "Restart=", "WantedBy"))
    ] == [
        ln
        for ln in vision.splitlines()
        if ln.startswith(("After=", "StartLimit", "Restart=", "WantedBy"))
    ]


@pytest.mark.parametrize("kind", su.KINDS)
def test_no_unit_can_hold_alerts_gate_on_a_marker_or_depend_on_ha_or_an_agent_session(
    kind: str,
) -> None:
    text = su.render_template(kind, LAYOUT)
    assert (
        "--dry-run" not in text
        and "ConditionPath" not in text
        and "intentional" not in text
    )
    assert "Restart=always" in text and "StartLimitIntervalSec=0" in text
    assert (
        "Wants=" not in text and "Requires=" not in text and "BindsTo=" not in text
    )  # ordering after the HSW observer only
    assert "hsw-observer-agent.service" in text.split("After=", 1)[1].splitlines()[0]
    assert "homeassistant" not in text.lower() and "home-assistant" not in text.lower()
    assert su.is_managed(text)


def test_a_pinned_config_home_reaches_the_service() -> None:
    text = su.render_template("watch", replace(LAYOUT, xdg_config="/home/me/.cfg"))
    assert "Environment=XDG_CONFIG_HOME=/home/me/.cfg\n" in text


# ---- which printers --------------------------------------------------------------------------


def _fleet(**entries: Any) -> Fleet:
    return Fleet(Path("fleet.yaml"), entries)


def test_plan_printers_takes_owned_full_access_printers_with_a_watch_adapter_and_a_camera_flag() -> (
    None
):
    own_cam, own_nocam = fleet_printer("a"), fleet_printer("b")
    neighbour = replace(fleet_printer("c"), owner="neighbour")
    readonly = fleet_printer("d", access="read-only")
    offline = fleet_printer("e", access="offline")
    other_adapter = fleet_printer("f")
    fleet = _fleet(
        a=own_cam, b=own_nocam, c=neighbour, d=readonly, e=offline, f=other_adapter
    )
    devices: dict[str, object] = {
        "a": device("a", watch=CAMERA),
        "b": device("b"),
        "c": device("c"),
        "d": device("d", access="read-only"),
        "f": device("f", adapter="octoprint"),
    }
    plans = su.plan_printers(fleet, devices)
    assert [(p.name, p.camera, p.kinds) for p in plans] == [
        ("a", True, ("watch", "vision")),
        ("b", False, ("watch",)),
    ]
    assert plans[0].units() == [
        "3d-printer-watch@a.service",
        "3d-printer-vision@a.service",
    ]


def test_no_fleet_means_no_printers() -> None:
    assert su.plan_printers(None, {}) == []


# ---- install ---------------------------------------------------------------------------------------


def run_install(
    tmp_path: Path,
    systemd: Systemd,
    *args: str,
    runner: Runner | None = None,
    plans: list[su.PrinterPlan] | None = None,
) -> tuple[int, Runner]:
    runner = runner or Runner(linger=True)
    env = env_for(tmp_path, systemd, runner, plans=plans)
    try:
        code = sv.cmd_install(
            ["--repo", str(LAYOUT.repo), "--unit-dir", str(tmp_path / "units"), *args],
            env,
        )
    except GateFailure:
        code = 1
    return code, runner


def test_a_fresh_install_writes_the_templates_reloads_and_enables_every_unit(
    tmp_path: Path, capsys: Any
) -> None:
    code, runner = run_install(tmp_path, Systemd(), "--all")
    assert code == 0
    units = tmp_path / "units"
    assert su.is_managed((units / "3d-printer-watch@.service").read_text())
    assert su.is_managed((units / "3d-printer-vision@.service").read_text())
    systemctl = [c for c in runner.calls if c[0] == "systemctl"]
    assert systemctl == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "3d-printer-watch@p1.service"],
        ["systemctl", "--user", "enable", "--now", "3d-printer-vision@p1.service"],
    ]
    assert "check: 3d printer supervise status" in capsys.readouterr().out


def test_a_printer_without_a_camera_gets_no_vision_unit(tmp_path: Path) -> None:
    code, runner = run_install(tmp_path, Systemd(), "--all", plans=[plan(camera=False)])
    assert (
        code == 0 and not (tmp_path / "units" / "3d-printer-vision@.service").exists()
    )
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "3d-printer-watch@p1.service",
    ] in runner.calls
    assert not any("vision" in " ".join(c) for c in runner.calls)


def test_install_is_idempotent(tmp_path: Path, capsys: Any) -> None:
    run_install(tmp_path, Systemd(), "--all")
    capsys.readouterr()
    code, runner = run_install(tmp_path, healthy(), "--all")
    assert code == 0 and "nothing to do" in capsys.readouterr().out
    assert all(c[0] == "loginctl" for c in runner.calls) or not [
        c for c in runner.calls if c[:2] == ["systemctl", "--user"] and c[2] != "show"
    ]


def test_a_hand_made_dry_run_template_is_replaced_only_with_yes_and_a_copy_stays(
    tmp_path: Path, capsys: Any
) -> None:
    units = tmp_path / "units"
    units.mkdir()
    old = "[Service]\nExecStart=/x/3d printer watch %i --dry-run\n"
    (units / "3d-printer-watch@.service").write_text(old)
    code, runner = run_install(tmp_path, Systemd(), "--all")
    assert code == 1
    assert (units / "3d-printer-watch@.service").read_text() == old  # untouched
    assert not [
        c for c in runner.calls if c[0] == "systemctl"
    ]  # nothing enabled on an old template
    out = capsys.readouterr().out
    assert (
        "[needs --yes]" in out
        and "carries --dry-run" in out
        and "nothing was changed" in out
    )
    code, _ = run_install(tmp_path, Systemd(), "--all", "--yes")
    assert code == 0
    assert su.is_managed((units / "3d-printer-watch@.service").read_text())
    backups = list(units.glob("3d-printer-watch@.service.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == old


def test_plan_only_changes_nothing_and_says_what_it_would_do(
    tmp_path: Path, capsys: Any
) -> None:
    code, runner = run_install(tmp_path, Systemd(), "--all", "--plan")
    assert code == 0 and not (tmp_path / "units").exists()
    assert not [c for c in runner.calls if c[0] == "systemctl"]
    assert "plan    write    3d-printer-watch@.service" in capsys.readouterr().out


GATE = "[Unit]\nConditionPathExists=!/home/me/.local/state/3d-cli/p1.intentional-off\n"


def test_the_intentional_off_gate_dropin_is_reported_retired_only_with_yes_and_other_dropins_are_kept(
    tmp_path: Path,
) -> None:
    units = tmp_path / "units"
    for kind in su.KINDS:
        d = units / f"3d-printer-{kind}@p1.service.d"
        d.mkdir(parents=True)
        (d / "intentional-off.conf").write_text(GATE)
        (d / "live-observer.conf").write_text(
            "[Unit]\nAfter=hsw-observer-agent.service\nWants=hsw-observer-agent.service\n\n[Service]\nExecStart=\n"
            f"ExecStart=/srv/3d-cli/.venv/bin/python /srv/3d-cli/bin/3d printer {kind} p1 \n"
        )
    code, _ = run_install(tmp_path, Systemd(), "--all")
    assert (
        code == 1
        and (units / "3d-printer-watch@p1.service.d" / "intentional-off.conf").exists()
    )
    code, _ = run_install(tmp_path, Systemd(), "--all", "--yes")
    assert code == 0
    for kind in su.KINDS:
        d = units / f"3d-printer-{kind}@p1.service.d"
        assert not (d / "intentional-off.conf").exists()
        assert (
            len(list(d.glob("intentional-off.conf.retired-*"))) == 1
        )  # a copy of what the owner wrote
        assert (d / "live-observer.conf").exists()  # the HSW After=/Wants= keep working


def test_dropin_classification() -> None:
    canon = [
        "/srv/3d-cli/.venv/bin/python",
        "/srv/3d-cli/bin/3d",
        "printer",
        "watch",
        "p1",
    ]

    def kind(text: str) -> str:
        return su.classify_dropin(Path("x.conf"), text, canon).kind

    assert kind(GATE) == "gate"
    assert kind(GATE + "[Service]\nNice=5\n") == "other"
    assert (
        kind(
            "[Service]\nExecStart=\nExecStart=/srv/3d-cli/.venv/bin/python /srv/3d-cli/bin/3d printer watch p1 \n"
        )
        == "redundant-exec"
    )
    assert (
        kind(
            "[Service]\nExecStart=/srv/3d-cli/.venv/bin/python /srv/3d-cli/bin/3d printer watch p1 --dry-run\n"
        )
        == "dry-run"
    )
    assert kind("[Service]\nExecStart=/bin/true\n") == "custom-exec"
    assert kind("[Unit]\nAfter=hsw-observer-agent.service\n") == "other"


def test_install_enables_linger_only_with_yes_and_tells_how_otherwise(
    tmp_path: Path, capsys: Any
) -> None:
    code, runner = run_install(
        tmp_path, Systemd(), "--all", runner=Runner(linger=False)
    )
    assert code == 0 and ["loginctl", "enable-linger"] not in runner.calls
    assert "linger is off" in capsys.readouterr().out
    code, runner = run_install(
        tmp_path, healthy(), "--all", "--yes", runner=Runner(linger=False)
    )
    assert code == 0 and ["loginctl", "enable-linger"] in runner.calls


def test_a_running_unit_with_dry_run_is_restarted_by_install_and_a_healthy_one_is_not(
    tmp_path: Path,
) -> None:
    run_install(tmp_path, Systemd(), "--all")
    broken = healthy()
    broken.units["3d-printer-vision@p1.service"] = unit_show(
        args="printer vision p1 --dry-run"
    )
    code, runner = run_install(tmp_path, broken, "--all")
    assert (
        code == 0
        and ["systemctl", "--user", "restart", "3d-printer-vision@p1.service"]
        in runner.calls
    )
    assert not any(
        c[:3] == ["systemctl", "--user", "restart"] and "watch" in c[-1]
        for c in runner.calls
    )


def test_a_failing_systemctl_is_reported_and_fails_the_install(
    tmp_path: Path, capsys: Any
) -> None:
    runner = Runner()
    runner.fail = {"systemctl --user daemon-reload"}
    code, _ = run_install(tmp_path, Systemd(), "--all", runner=runner)
    assert code == 1 and "FAILED  reload systemd --user" in capsys.readouterr().out


def test_uninstall_disables_units_and_removes_managed_templates_only_with_yes(
    tmp_path: Path, capsys: Any
) -> None:
    run_install(tmp_path, Systemd(), "--all")
    units = tmp_path / "units"
    hand = units / "3d-printer-vision@.service"
    hand.write_text("[Service]\nExecStart=/x\n")  # no managed mark: ours to leave alone
    runner = Runner()
    env = env_for(tmp_path, healthy(), runner)
    with pytest.raises(GateFailure):
        sv.cmd_uninstall(["--all", "--unit-dir", str(units)], env)
    assert (units / "3d-printer-watch@.service").exists()
    sv.cmd_uninstall(["--all", "--yes", "--unit-dir", str(units)], env)
    assert not (units / "3d-printer-watch@.service").exists() and hand.exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "3d-printer-watch@p1.service",
    ] in runner.calls


# ---- status: the verdict -----------------------------------------------------------------------


def findings(a: sv.Assessment, level: str) -> list[str]:
    return [f.text for f in a.findings if f.level == level]


def test_a_printing_printer_with_live_units_fresh_heartbeats_and_linger_is_supervised(
    tmp_path: Path,
) -> None:
    beats(tmp_path)
    a = assess_status(tmp_path, healthy())
    assert a.level == sv.OK and findings(a, sv.OK) == ["supervised"]


def test_a_printing_printer_without_a_live_watcher_fails(tmp_path: Path) -> None:
    beats(tmp_path)
    systemd = healthy()
    systemd.units["3d-printer-watch@p1.service"] = unit_show("inactive", sub="dead")
    a = assess_status(tmp_path, systemd)
    assert (
        a.level == sv.FAIL
        and "3d-printer-watch@p1.service is inactive" in findings(a, sv.FAIL)[0]
    )


def test_no_unit_at_all_fails_when_the_printer_answers_and_only_warns_when_it_is_off(
    tmp_path: Path,
) -> None:
    on = assess_status(tmp_path, Systemd(), state="idle")
    off = assess_status(tmp_path, Systemd(), state=None)
    assert on.level == sv.FAIL and off.level == sv.WARN
    assert (
        "is not installed" in on.findings[0].text
        or "is not installed" in on.findings[1].text
    )
    assert any("supervise install" in f.fix for f in off.findings)


def test_a_stale_watcher_heartbeat_fails_while_printing(tmp_path: Path) -> None:
    beats(tmp_path, age=200.0)
    a = assess_status(tmp_path, healthy())
    assert a.level == sv.FAIL and any(
        "heartbeat is 3 min old" in t for t in findings(a, sv.FAIL)
    )


def test_a_missing_heartbeat_of_a_running_unit_fails(tmp_path: Path) -> None:
    a = assess_status(tmp_path, healthy())
    assert a.level == sv.FAIL and any(
        "never written a heartbeat" in t for t in findings(a, sv.FAIL)
    )


def test_a_wedged_vision_loop_and_a_missing_camera_frame_fail_while_printing(
    tmp_path: Path,
) -> None:
    beats(tmp_path, v_age=400.0)
    assert any(
        "vision status is 7 min old" in t
        for t in findings(assess_status(tmp_path, healthy()), sv.FAIL)
    )
    beats(tmp_path, v_frame_age=500.0)
    assert any(
        "no frame for 8 min" in t
        for t in findings(assess_status(tmp_path, healthy()), sv.FAIL)
    )
    beats(tmp_path, v_frame_age=None)
    assert any(
        "no frame for never" in t
        for t in findings(assess_status(tmp_path, healthy()), sv.FAIL)
    )


def test_an_idle_vision_loop_without_frames_is_fine(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_watch_beat(state, "p1", job=None)
    write_vision_beat(state, "p1", supervising=False, frame_age=None)
    assert assess_status(tmp_path, healthy(), state="idle").level == sv.OK


def test_a_watcher_disconnected_from_a_printer_that_answers_fails_but_not_from_one_that_is_off(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    write_watch_beat(
        state, "p1", job=None, connected=False, unreachable_since=NOW - 600
    )
    write_vision_beat(state, "p1")
    assert any(
        "disconnected for 10 min" in t
        for t in findings(assess_status(tmp_path, healthy(), state="idle"), sv.FAIL)
    )
    off = assess_status(tmp_path, healthy(), state=None)
    assert off.level == sv.OK and "not answering" in off.findings[0].text


def test_linger_off_fails_and_unknown_linger_warns(tmp_path: Path) -> None:
    beats(tmp_path)
    assert any(
        "linger is off" in t
        for t in findings(
            assess_status(tmp_path, healthy(), runner=Runner(linger=False)), sv.FAIL
        )
    )
    assert any(
        "cannot tell whether linger" in t
        for t in findings(
            assess_status(tmp_path, healthy(), runner=Runner(linger=None)), sv.WARN
        )
    )


def test_a_dry_run_unit_and_a_second_watcher_fail(tmp_path: Path) -> None:
    beats(tmp_path)
    systemd = healthy()
    systemd.units["3d-printer-watch@p1.service"] = unit_show(
        args="printer watch p1 --dry-run"
    )
    assert any(
        "--dry-run" in t for t in findings(assess_status(tmp_path, systemd), sv.FAIL)
    )
    state = tmp_path / "state"
    (state / "watch" / "k1se+p1.json").write_text(
        json.dumps({"pid": 99, "updated": NOW - 3, "printers": {"p1": {}, "k1se": {}}})
    )
    assert any(
        "another watcher covers this printer" in t and "k1se+p1.json" in t
        for t in findings(assess_status(tmp_path, healthy()), sv.FAIL)
    )


def test_a_dead_pid_or_stale_foreign_heartbeat_is_not_a_second_watcher(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    (state / "watch").mkdir(parents=True)
    (state / "watch" / "all.json").write_text(
        json.dumps({"pid": 99, "updated": NOW - 3, "printers": {"p1": {}}})
    )
    assert sv.duplicate_watchers("p1", state, NOW, lambda _p: False) == []
    (state / "watch" / "all.json").write_text(
        json.dumps({"pid": 99, "updated": NOW - 900, "printers": {"p1": {}}})
    )
    assert sv.duplicate_watchers("p1", state, NOW, lambda _p: True) == []


def test_the_marker_no_longer_gates_anything_it_is_only_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    intentional_off.set_marker("p1", "switched off for the week", NOW)
    write_watch_beat(tmp_path / "state", "p1", job=None)
    write_vision_beat(tmp_path / "state", "p1")
    a = assess_status(tmp_path, healthy(), state=None)
    assert (
        a.level == sv.OK
        and a.marker
        and a.marker["reason"] == "switched off for the week"
    )
    assert any("intentional-off marker set" in t for t in findings(a, sv.OK))


def test_a_unit_held_by_a_gate_dropin_says_why(tmp_path: Path) -> None:
    d = tmp_path / "units" / "3d-printer-watch@p1.service.d"
    d.mkdir(parents=True)
    (d / "intentional-off.conf").write_text(GATE)
    systemd = Systemd(
        {"3d-printer-watch@p1.service": unit_show("inactive", sub="dead")}
    )
    a = assess_status(tmp_path, systemd, state="idle", camera=False)
    texts = [f.text for f in a.findings]
    assert any(
        "held by an intentional-off ConditionPathExists drop-in" in t for t in texts
    )
    assert any(t.startswith("obsolete drop-in") for t in texts)


def test_systemctl_unavailable_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    a = assess_status(tmp_path, lambda _u: "")  # type: ignore[arg-type]
    assert (
        a.level == sv.FAIL
        and "cannot be read here" in a.findings[-1].text
        or a.level == sv.FAIL
    )


def test_restart_loops_and_unenabled_units_warn(tmp_path: Path) -> None:
    beats(tmp_path)
    a = assess_status(tmp_path, healthy(enabled="disabled", restarts=9))
    assert a.level == sv.WARN
    assert any("not enabled" in t for t in findings(a, sv.WARN)) and any(
        "restarted 9 times" in t for t in findings(a, sv.WARN)
    )


# ---- status: the command ---------------------------------------------------------------------------


def test_status_exits_1_on_a_fail_and_0_otherwise_with_json(
    tmp_path: Path, capsys: Any
) -> None:
    beats(tmp_path)
    env = env_for(tmp_path, healthy())
    assert sv.run(["status", "--json"], env) == 0
    doc = json.loads(capsys.readouterr().out)
    assert (
        doc[0]["printer"] == "p1"
        and doc[0]["level"] == "ok"
        and doc[0]["job"]["file"] == "a.gcode"
    )
    assert doc[0]["units"]["watch"]["enabled"] is True and doc[0]["linger"] is True
    with pytest.raises(GateFailure):
        sv.run(["status"], env_for(tmp_path, Systemd()))
    text = capsys.readouterr().out
    assert (
        text.startswith("FAIL p1: printer printing")
        and "$ 3d printer supervise install --all --yes" in text
    )


def test_status_names_must_be_supervised_printers(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not a supervised printer"):
        sv.run(["status", "nope"], env_for(tmp_path, healthy()))


def test_status_does_not_ask_the_printer_with_no_live(
    tmp_path: Path, capsys: Any
) -> None:
    beats(tmp_path)
    env = env_for(tmp_path, healthy())
    env.state_probe = lambda _n: (_ for _ in ()).throw(
        AssertionError("the printer was contacted")
    )
    assert sv.run(["status", "--no-live"], env) == 0
    assert "printer printing" in capsys.readouterr().out  # the watcher's own fresh reading stands in
    quiet = env_for(tmp_path / "other", healthy())
    assert sv.run(["status", "--no-live"], quiet) == 0  # no heartbeat, printer not asked: a warning
    assert "printer unknown (not asked)" in capsys.readouterr().out


# ---- quiet -----------------------------------------------------------------------------------------


def test_quiet_writes_and_clears_the_marker_and_never_touches_a_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    runner = Runner()
    env = env_for(tmp_path, healthy(), runner)
    assert sv.run(["quiet", "p1", "--reason", "off for the week"], env) == 0
    marker = intentional_off.read_marker("p1")
    assert marker is not None and marker["reason"] == "off for the week" and "at" in marker
    assert "keep running" in capsys.readouterr().out and runner.calls == []
    assert sv.run(["quiet", "p1", "--clear"], env) == 0 and not intentional_off.is_set(
        "p1"
    )


# ---- notify-failure: ONE short tg line ------------------------------------------------------------


def notify(
    tmp_path: Path,
    tg: Tg,
    result: str = "exit-code",
    env_vars: dict[str, str] | None = None,
    kind: str = "watch",
) -> int:
    env = env_for(tmp_path, healthy())
    return sv.cmd_notify_failure(
        ["p1", kind],
        env,
        {"SERVICE_RESULT": result, "EXIT_STATUS": "1", **(env_vars or {})},
        tg,
    )


def test_a_watcher_that_dies_during_a_print_sends_one_short_line_and_a_crash_loop_sends_no_more(
    tmp_path: Path,
) -> None:
    write_watch_beat(
        tmp_path / "state",
        "p1",
        job={"file": "a.gcode", "started": NOW - 60, "adopted": False},
    )
    tg = Tg()
    assert (
        notify(tmp_path, tg) == 0
        and notify(tmp_path, tg) == 0
        and notify(tmp_path, tg) == 0
    )
    assert len(tg.sent) == 1
    line = tg.sent[0][-1]
    assert (
        tg.sent[0][1:3] == ["--tag", "problem"] and len(line) < 200 and "\n" not in line
    )
    assert line.startswith(
        "[p1] the watch supervisor stopped during a print (exit-code, exit 1)"
    )
    assert "3d printer supervise status p1" in line


def test_the_vision_supervisor_is_judged_by_its_own_status_and_has_its_own_slot(
    tmp_path: Path,
) -> None:
    write_vision_beat(
        tmp_path / "state", "p1", supervising=True, printer_state="printing"
    )
    write_watch_beat(tmp_path / "state", "p1", job={"file": "a"})
    tg = Tg()
    notify(tmp_path, tg, kind="vision")
    notify(tmp_path, tg, kind="watch")
    assert [t[-1].split()[2] for t in tg.sent] == ["vision", "watch"]


@pytest.mark.parametrize("result", ["success", ""])
def test_a_clean_stop_is_silent(tmp_path: Path, result: str) -> None:
    write_watch_beat(tmp_path / "state", "p1", job={"file": "a"})
    tg = Tg()
    assert notify(tmp_path, tg, result=result) == 0 and tg.sent == []


def test_a_death_while_no_print_runs_is_silent(tmp_path: Path) -> None:
    write_watch_beat(tmp_path / "state", "p1", job=None)
    tg = Tg()
    notify(tmp_path, tg)
    assert tg.sent == []


def test_an_unreadable_or_ancient_heartbeat_does_not_hide_a_death(
    tmp_path: Path,
) -> None:
    tg = Tg()
    notify(tmp_path, tg)  # no heartbeat at all
    assert len(tg.sent) == 1
    write_watch_beat(tmp_path / "state", "p1", age=5000.0, job=None)  # ancient: says nothing about the print now
    assert sv.printing_when_stopped("p1", "watch", tmp_path / "state", NOW) is True


def test_a_refused_tg_frees_the_slot_so_the_next_stop_tries_again(
    tmp_path: Path,
) -> None:
    write_watch_beat(tmp_path / "state", "p1", job={"file": "a"})
    refused, ok = Tg(code=1), Tg()
    notify(tmp_path, refused)
    notify(tmp_path, ok)
    assert len(refused.sent) == 1 and len(ok.sent) == 1


def test_notify_failure_never_raises_even_when_tg_blows_up(tmp_path: Path) -> None:
    write_watch_beat(tmp_path / "state", "p1", job={"file": "a"})

    def boom(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("no tg")

    assert (
        sv.cmd_notify_failure(
            ["p1", "watch"],
            env_for(tmp_path, healthy()),
            {"SERVICE_RESULT": "signal"},
            boom,
        )
        == 0
    )


def test_the_default_tg_runner_refuses_to_run_under_pytest(
    tmp_path: Path, capsys: Any
) -> None:
    write_watch_beat(tmp_path / "state", "p1", job={"file": "a"})
    env = env_for(tmp_path, healthy())
    assert (
        sv.cmd_notify_failure(["p1", "watch"], env, {"SERVICE_RESULT": "signal"}) == 0
    )  # no runner injected
    assert "refusing to run the real tg from a test" in capsys.readouterr().err


def test_the_default_systemctl_runner_refuses_to_run_under_pytest() -> None:
    with pytest.raises(RuntimeError, match="refusing to run systemctl from a test"):
        su.real_runner(["systemctl", "--user", "daemon-reload"])
