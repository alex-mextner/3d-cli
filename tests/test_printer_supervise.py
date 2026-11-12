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
    MONO,
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
Description=3D printer watch %i (while a print runs: Klipper state, errors, pauses; AI alerts + auto pause)
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
SuccessExitStatus=143
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
    assert plans[0].worker_units() == [
        "3d-printer-watch@a.service",
        "3d-printer-vision@a.service",
    ]
    assert plans[0].supervisor_unit() == "3d-printer-ondemand@a.timer"
    assert plans[0].units() == [plans[0].supervisor_unit(), *plans[0].worker_units()]


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
    for kind in su.TEMPLATE_KINDS:
        assert su.is_managed((units / su.template_name(kind)).read_text())
    systemctl = [c for c in runner.calls if c[0] == "systemctl"]
    assert systemctl == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "3d-printer-ondemand@p1.timer"],
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
        "3d-printer-ondemand@p1.timer",
    ] in runner.calls
    assert not any("vision" in " ".join(c) for c in runner.calls)
    assert not any("watch" in " ".join(c) for c in runner.calls)


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
        "3d-printer-ondemand@p1.timer",
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
    d = tmp_path / "units" / "3d-printer-ondemand@p1.timer.d"
    d.mkdir(parents=True)
    (d / "intentional-off.conf").write_text(GATE)
    systemd = Systemd(
        {"3d-printer-ondemand@p1.timer": unit_show("inactive", sub="dead")}
    )
    a = assess_status(tmp_path, systemd, state="idle", camera=False)
    texts = [f.text for f in a.findings]
    assert any(
        "held by an intentional-off ConditionPathExists drop-in" in t for t in texts
    )
    assert any(t.startswith("obsolete drop-in") for t in texts)


def test_systemctl_unavailable_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    class NoSystemctl(Systemd):
        def __call__(self, unit: str) -> str:
            return ""

    a = assess_status(tmp_path, NoSystemctl())
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
    assert doc[0]["units"]["ondemand"]["enabled"] is True and doc[0]["linger"] is True
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
    assert (
        "printer printing" in capsys.readouterr().out
    )  # the watcher's own fresh reading stands in
    quiet = env_for(tmp_path / "other", healthy())
    assert (
        sv.run(["status", "--no-live"], quiet) == 0
    )  # no heartbeat, printer not asked: a warning
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
    assert (
        marker is not None and marker["reason"] == "off for the week" and "at" in marker
    )
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
    write_watch_beat(
        tmp_path / "state", "p1", age=5000.0, job=None
    )  # ancient: says nothing about the print now
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


# ---- on demand supervision (#747): a supervisor that starts the workers only while a print runs ---------


TIMER_SNAPSHOT = """# managed-by: 3d printer supervise install (do not edit; override with a drop-in)
[Unit]
Description=3D printer supervisor %i (a timer: every 30 s a one-shot check, no process in between; starts the watch and vision workers only while a print runs)
Documentation=https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer.md#on-demand-supervision-747

[Timer]
OnActiveSec=5s
OnUnitInactiveSec=30s
AccuracySec=1s

[Install]
WantedBy=timers.target
"""

CHECK_SNAPSHOT = """# managed-by: 3d printer supervise install (do not edit; override with a drop-in)
[Unit]
Description=3D printer on-demand check %i (one-shot: asks the printer, starts or stops the watch and vision workers)
Documentation=https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer.md#on-demand-supervision-747
ConditionPathExists=/srv/3d-cli/lib/printer_ops/ondemand.py

[Service]
Type=oneshot
LogLevelMax=notice
SyslogLevel=notice
Environment=PATH=/srv/3d-cli/.venv/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH=/srv/3d-cli/lib
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/srv/3d-cli
UMask=0077
MemoryMax=96M
TimeoutStartSec=90
ExecStart=/srv/3d-cli/.venv/bin/python -S -m printer_ops.ondemand %i
"""


def test_the_supervisor_timer_is_a_snapshot_of_the_shipped_definition() -> None:
    assert su.render_template("ondemand", LAYOUT) == TIMER_SNAPSHOT


def test_the_check_service_is_a_snapshot_of_the_shipped_definition() -> None:
    assert su.render_template("ondemand-check", LAYOUT) == CHECK_SNAPSHOT


def test_the_check_has_no_install_section_so_only_its_timer_can_start_it() -> None:
    text = su.render_template("ondemand-check", LAYOUT)
    assert "[Install]" not in text and "Restart" not in text


def test_the_check_is_skipped_silently_while_its_checkout_is_missing() -> None:
    """The checkout is on the backups disk: without it the unit must be skipped (an unmet Condition), not fail
    every 30 s."""
    text = su.render_template("ondemand-check", LAYOUT)
    assert "ConditionPathExists=/srv/3d-cli/lib/printer_ops/ondemand.py\n" in text


@pytest.mark.parametrize("kind", su.KINDS)
def test_a_worker_unit_has_no_install_section_so_it_cannot_start_at_boot(
    kind: str,
) -> None:
    text = su.render_template(kind, LAYOUT)
    assert "[Install]" not in text and "WantedBy" not in text


def _polled(seconds_ago: float, printer: str = "idle", phase: str = "idle") -> str:
    return f"printer={printer} phase={phase} polled={int(NOW - seconds_ago)}"


def _on_demand(status_text: str, *, camera: bool = True, **over: Any) -> Systemd:
    """The supervisor timer is armed and its last check saw `status_text` (see `healthy`); the workers are
    stopped (nothing prints)."""
    systemd = healthy(camera=camera, status_text=status_text, **over)
    for kind in ("watch", "vision"):
        unit = f"3d-printer-{kind}@p1.service"
        if unit in systemd.units:
            systemd.units[unit] = unit_show(
                "inactive", sub="dead", enabled="static", args=f"printer {kind} p1"
            )
    return systemd


def test_an_idle_printer_with_a_running_supervisor_and_stopped_workers_is_ok(
    tmp_path: Path,
) -> None:
    a = assess_status(tmp_path, _on_demand(_polled(5)), state="idle")
    assert a.level == sv.OK
    assert not findings(a, sv.WARN) and not findings(a, sv.FAIL)
    assert not any("watch" in f.text or "vision" in f.text for f in a.findings)


def test_a_switched_off_printer_with_a_running_supervisor_and_stopped_workers_is_ok(
    tmp_path: Path,
) -> None:
    a = assess_status(tmp_path, _on_demand(_polled(5)), state=None)
    assert a.level == sv.OK
    assert not findings(a, sv.WARN) and not findings(a, sv.FAIL)


def test_a_print_that_started_within_one_poll_warns_not_running_yet(
    tmp_path: Path,
) -> None:
    a = assess_status(tmp_path, _on_demand(_polled(10)), state="printing")
    assert a.level == sv.WARN and not findings(a, sv.FAIL)
    assert any("not running yet" in t for t in findings(a, sv.WARN))


def test_a_printing_printer_right_after_the_timer_was_armed_warns_instead_of_failing(tmp_path: Path) -> None:
    systemd = _on_demand("")  # no check has finished yet
    systemd.units["3d-printer-ondemand@p1.timer"] = unit_show(
        "active", sub="waiting", enabled="enabled", active_mono=MONO - 2
    )
    a = assess_status(tmp_path, systemd, state="printing")
    assert a.level == sv.WARN and any("not running yet" in t for t in findings(a, sv.WARN))
    assert not findings(a, sv.FAIL)


def test_a_printing_printer_whose_supervisor_polled_long_ago_fails_for_stopped_workers(
    tmp_path: Path,
) -> None:
    a = assess_status(tmp_path, _on_demand(_polled(120)), state="printing")
    assert a.level == sv.FAIL
    assert any("is inactive" in t for t in findings(a, sv.FAIL))


def test_a_supervisor_that_tried_to_start_the_workers_and_failed_is_a_fail(
    tmp_path: Path,
) -> None:
    text = _polled(10, printer="printing", phase="active")
    a = assess_status(tmp_path, _on_demand(text), state="printing")
    assert a.level == sv.FAIL
    assert not any("not running yet" in f.text for f in a.findings)


def test_a_missing_supervisor_fails_for_a_printer_that_answers_and_warns_for_one_that_is_off(
    tmp_path: Path,
) -> None:
    missing = healthy(supervisor=False)
    on = assess_status(tmp_path, missing, state="idle")
    off = assess_status(tmp_path, missing, state=None)
    assert on.level == sv.FAIL and off.level == sv.WARN
    assert any(
        "ondemand unit" in t and "not installed" in t for t in findings(on, sv.FAIL)
    )


def test_running_workers_are_never_required_enabled_but_the_supervisor_is(
    tmp_path: Path,
) -> None:
    beats(tmp_path)
    ok = assess_status(
        tmp_path, healthy()
    )  # workers are `static`: started by the supervisor
    assert ok.level == sv.OK
    assert not any("not enabled" in f.text for f in ok.findings)
    systemd = healthy()
    systemd.units["3d-printer-ondemand@p1.timer"] = unit_show(
        sub="waiting", enabled="disabled"
    )
    bad = assess_status(tmp_path, systemd)
    assert any("ondemand" in t and "not enabled" in t for t in findings(bad, sv.WARN))


def test_no_live_status_reads_the_printer_state_from_the_supervisor_poll(
    tmp_path: Path, capsys: Any
) -> None:
    idle = env_for(tmp_path / "idle", _on_demand(_polled(10)))
    assert sv.run(["status", "--no-live"], idle) == 0
    assert "printer idle" in capsys.readouterr().out
    printing = _on_demand(_polled(100, printer="printing", phase="idle"))
    with pytest.raises(GateFailure):
        sv.run(["status", "--no-live"], env_for(tmp_path / "busy", printing))
    assert "printer printing" in capsys.readouterr().out


def test_no_live_status_with_a_printing_poll_still_requires_the_workers_to_run(
    tmp_path: Path,
) -> None:
    text = _polled(100, printer="printing", phase="idle")
    a = sv.gather(
        plan(),
        env_for(tmp_path, _on_demand(text)),
        live=False,
        layout=LAYOUT,
        linger=True,
    )
    assert a.state == "printing" and a.level == sv.FAIL


def test_a_fresh_install_writes_ondemand_then_the_workers_and_enables_only_the_supervisor(
    tmp_path: Path,
) -> None:
    _, runner = run_install(tmp_path, Systemd(), "--all")
    units = tmp_path / "units"
    check = (units / "3d-printer-ondemand@.service").read_text()
    assert su.is_managed(check) and "-m printer_ops.ondemand %i" in check
    assert sorted(p.name for p in units.iterdir()) == [
        "3d-printer-ondemand@.service",
        "3d-printer-ondemand@.timer",
        "3d-printer-vision@.service",
        "3d-printer-watch@.service",
    ]
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl"]
    assert systemctl == [
        ["daemon-reload"],
        ["enable", "--now", "3d-printer-ondemand@p1.timer"],
    ]
    assert not any(c[2] == "disable" for c in runner.calls if c[0] == "systemctl")


def test_install_plan_orders_templates_ondemand_watch_vision() -> None:
    actions = su.plan_install(
        [plan()],
        LAYOUT,
        installed={},
        dropins=[],
        linger=True,
        enabled={},
        loaded_dry_run=set(),
        now=NOW,
    )
    templates = [a.target for a in actions if a.op == "write"]
    assert templates == [
        "3d-printer-ondemand@.timer",
        "3d-printer-ondemand@.service",
        "3d-printer-watch@.service",
        "3d-printer-vision@.service",
    ]
    ops = [a.op for a in actions]
    assert ops.index("reload") > max(i for i, op in enumerate(ops) if op == "write")


def test_a_legacy_always_on_install_gets_its_workers_disabled_without_stopping_them(
    tmp_path: Path,
) -> None:
    legacy = healthy(supervisor=False, enabled="enabled")
    run_install(tmp_path, Systemd(), "--all")
    _, runner = run_install(tmp_path, legacy, "--all")
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    assert ["enable", "--now", "3d-printer-ondemand@p1.timer"] in systemctl
    assert ["disable", "3d-printer-watch@p1.service"] in systemctl
    assert ["disable", "3d-printer-vision@p1.service"] in systemctl
    assert not any(c[0] in ("stop", "disable") and "--now" in c for c in systemctl)
    assert not any(c[0] == "stop" for c in systemctl)
    assert not any(c[:2] == ["enable", "--now"] and "watch" in c[-1] for c in systemctl)


def test_uninstall_stops_the_unenabled_workers_and_disables_the_supervisor_with_now(
    tmp_path: Path,
) -> None:
    run_install(tmp_path, Systemd(), "--all")
    units = tmp_path / "units"
    runner = Runner()
    env = env_for(tmp_path, healthy(), runner)
    sv.cmd_uninstall(["--all", "--yes", "--unit-dir", str(units)], env)
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    assert ["disable", "--now", "3d-printer-ondemand@p1.timer"] in systemctl
    assert ["stop", "3d-printer-watch@p1.service"] in systemctl
    assert ["stop", "3d-printer-vision@p1.service"] in systemctl
    assert not any(
        c[:2] == ["disable", "--now"] and "watch" in c[-1] for c in systemctl
    )
    assert not list(units.glob("3d-printer-*@.service"))
    assert not list(units.glob("3d-printer-*@.timer"))


# ---- the timer supervisor (#747): what the one-shot check leaves behind, and the retired always-on process ----


def test_a_leftover_always_on_supervisor_process_is_stopped_and_disabled_before_the_timer_is_enabled(
    tmp_path: Path,
) -> None:
    old = healthy(supervisor=False)
    old.units["3d-printer-ondemand@p1.service"] = unit_show(
        enabled="enabled", args="-m printer_ops.ondemand p1"
    )
    run_install(tmp_path, Systemd(), "--all")  # the templates are already the new ones
    _, runner = run_install(tmp_path, old, "--all")
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    retire = ["disable", "--now", "3d-printer-ondemand@p1.service"]
    arm = ["enable", "--now", "3d-printer-ondemand@p1.timer"]
    assert retire in systemctl and arm in systemctl
    assert systemctl.index(retire) < systemctl.index(arm)
    # the workers it left running are not touched: the first check adopts them
    assert not any(c[0] == "stop" for c in systemctl)


def test_no_leftover_supervisor_means_no_retire_action() -> None:
    actions = su.plan_install(
        [plan()],
        LAYOUT,
        installed={},
        dropins=[],
        linger=True,
        enabled={},
        loaded_dry_run=set(),
        now=NOW,
    )
    assert not [a for a in actions if "3d-printer-ondemand@p1.service" in a.target]


def test_the_timer_status_is_the_state_file_view_plus_when_the_check_last_finished() -> (
    None
):
    systemd = healthy(status_text=_polled(7, printer="printing", phase="active"))
    u = sv.probe_supervisor("p1", systemd, systemd.last_check, NOW, MONO)
    assert u.unit == "3d-printer-ondemand@p1.timer" and u.running and u.enabled
    assert sv._status_fields(u.status_text) == {
        "printer": "printing",
        "phase": "active",
        "polled": str(int(NOW - 7)),
    }


def test_a_check_that_only_backed_off_does_not_make_an_old_observation_look_fresh() -> (
    None
):
    """The backoff tick exits 0 without asking the printer: the poll age is the age of the probe that set the
    deadline, not of the check that skipped it."""
    systemd = healthy(status_text=_polled(3, printer="unreachable"))
    systemd.last_checks["p1"]["last_probe_mono"] = MONO - 100
    u = sv.probe_supervisor("p1", systemd, systemd.last_check, NOW, MONO)
    assert sv._status_fields(u.status_text)["polled"] == str(int(NOW - 100))
    systemd.last_checks["p1"]["last_probe_mono"] = (
        MONO - 1
    )  # a probe after the last finish: never later than it
    u = sv.probe_supervisor("p1", systemd, systemd.last_check, NOW, MONO)
    assert sv._status_fields(u.status_text)["polled"] == str(int(NOW - 3))


def test_a_timer_that_never_ran_a_check_since_boot_has_no_status() -> None:
    systemd = healthy()  # armed, but no check has left a state file
    u = sv.probe_supervisor("p1", systemd, systemd.last_check, NOW, MONO)
    assert u.running and u.status_text == ""


def test_a_crashing_check_is_a_finding_not_a_fresh_poll_even_though_the_timer_keeps_firing(
    tmp_path: Path,
) -> None:
    """The timer fires every 30 s whether or not the check works: only the check service's own result and finish
    time say when the printer was last asked. An old `idle` state must not look fresh."""
    systemd = healthy(status_text=_polled(5000, printer="idle"))
    systemd.units["3d-printer-ondemand@p1.service"] = unit_show(
        "failed", sub="failed", enabled="static", inactive_mono=MONO - 2
    )
    a = assess_status(tmp_path, systemd, state="printing")
    assert a.level == sv.FAIL
    assert any("last on-demand check failed" in t for t in findings(a, sv.FAIL))
    assert (
        sv.supervisor_state(a.units[sv.SUPERVISOR], NOW) is None
    )  # the old snapshot is not trusted
    nolive = sv.gather(
        plan(), env_for(tmp_path / "x", systemd), live=False, layout=LAYOUT, linger=True
    )
    assert nolive.state is None


def test_a_check_skipped_for_a_missing_checkout_looks_old_not_fresh(
    tmp_path: Path,
) -> None:
    """An unmet Condition leaves the service inactive with its OLD finish time: the poll age grows."""
    systemd = healthy(status_text=_polled(900, printer="idle"))
    a = assess_status(tmp_path, systemd, state="printing")
    assert a.level == sv.FAIL and not any(
        "not running yet" in f.text for f in a.findings
    )


def test_the_check_time_comes_from_monotonic_stamps_whatever_format_systemd_prints_dates_in() -> (
    None
):
    """Numeric CLOCK_MONOTONIC stamps are the same on every systemd version; human date strings are not."""
    text = (
        unit_show("inactive", sub="dead", inactive_mono=MONO - 30)
        + "ActiveEnterTimestamp=Fri 2027-01-15 08:00:00 UTC\n"
    )
    u = sv.probe_unit("x", lambda _u: text)
    assert u.inactive_mono == MONO - 30 and u.since == "Fri 2027-01-15 08:00:00 UTC"


def test_an_unreadable_printer_configuration_is_a_finding_because_nothing_can_be_started(
    tmp_path: Path,
) -> None:
    systemd = healthy(status_text=_polled(5))
    systemd.last_checks["p1"] = {
        "printer": "idle",
        "phase": "idle",
        "config_error": "printers.yaml: bad indent",
    }
    a = assess_status(tmp_path, systemd, state="idle")
    assert a.level == sv.FAIL
    assert any(
        "cannot read the printer configuration" in t for t in findings(a, sv.FAIL)
    )


def test_a_printer_the_check_refuses_is_reported_as_refused_not_as_unreadable(
    tmp_path: Path,
) -> None:
    systemd = healthy(status_text=_polled(5))
    systemd.last_checks["p1"] = {
        "printer": "idle",
        "phase": "idle",
        "config_error": "p1 is not supervised",
        "refused": True,
    }
    a = assess_status(tmp_path, systemd, state="idle")
    texts = findings(a, sv.FAIL)
    assert any("does not supervise this printer" in t for t in texts)
    assert not any("cannot read" in t for t in texts)


def test_a_configuration_error_before_the_first_successful_check_still_reaches_status(
    tmp_path: Path,
) -> None:
    systemd = (
        healthy()
    )  # armed, the check service has run (inactive since MONO - 3), but no phase yet
    systemd.units["3d-printer-ondemand@p1.service"] = unit_show(
        "inactive", sub="dead", enabled="static", inactive_mono=MONO - 3
    )
    systemd.last_checks["p1"] = {"printer": "", "phase": "", "config_error": "bad yaml"}
    a = assess_status(tmp_path, systemd, state="idle")
    assert any(
        "cannot read the printer configuration" in t for t in findings(a, sv.FAIL)
    )


def test_an_uninstall_without_a_user_bus_still_attempts_the_stops_and_fails_loudly(
    tmp_path: Path,
) -> None:
    class NoBus(Systemd):
        def __call__(self, unit: str) -> str:  # `systemctl --user show` answers nothing
            return ""

    runner = Runner()
    runner.fail = {"systemctl --user disable --now 3d-printer-ondemand@p1.timer"}
    env = env_for(tmp_path, NoBus(), runner)
    with pytest.raises(GateFailure):
        sv.cmd_uninstall(["--unit-dir", str(tmp_path / "units"), "p1"], env)
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "3d-printer-ondemand@p1.timer",
    ] in runner.calls


def test_a_running_worker_whose_unit_file_was_deleted_is_still_stopped(
    tmp_path: Path,
) -> None:
    gone = healthy(supervisor=False)
    gone.units["3d-printer-watch@p1.service"] = unit_show(
        "active", sub="running", enabled="", load="not-found"
    )
    runner = Runner()
    env = env_for(tmp_path, gone, runner)
    sv.cmd_uninstall(["--unit-dir", str(tmp_path / "units"), "p1"], env)
    assert [
        "systemctl",
        "--user",
        "stop",
        "3d-printer-watch@p1.service",
    ] in runner.calls


def test_a_subset_install_also_migrates_a_peer_waiting_for_its_restart(
    tmp_path: Path,
) -> None:
    old = healthy(supervisor=False)
    old.units.update(healthy("p2", supervisor=False).units)
    old.units["3d-printer-ondemand@p1.service"] = unit_show(
        enabled="enabled", args="-m printer_ops.ondemand p1"
    )
    old.units["3d-printer-ondemand@p2.service"] = unit_show(
        "activating",
        sub="auto-restart",
        enabled="disabled",
        args="-m printer_ops.ondemand p2",
    )
    _, runner = run_install(tmp_path, old, "p1", plans=[plan("p1"), plan("p2")])
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    assert ["enable", "--now", "3d-printer-ondemand@p2.timer"] in systemctl


def test_a_second_uninstall_finds_nothing_to_stop_and_does_not_fail(
    tmp_path: Path,
) -> None:
    runner = Runner()
    env = env_for(
        tmp_path, Systemd(), runner
    )  # no unit left: the first uninstall removed them all
    code = sv.cmd_uninstall(
        ["--all", "--yes", "--unit-dir", str(tmp_path / "units")], env
    )
    assert code == 0
    assert not [
        c
        for c in runner.calls
        if c[:3]
        in (["systemctl", "--user", "stop"], ["systemctl", "--user", "disable"])
    ]


def test_a_subset_install_also_migrates_the_peers_that_share_the_replaced_template(
    tmp_path: Path,
) -> None:
    """The supervisor service template is shared: replacing it for p1 alone would leave p2, still a resident
    process of the first design, with a one-shot that runs once and then never again."""
    old = healthy(supervisor=False)
    old.units.update(healthy("p2", supervisor=False).units)
    for name in ("p1", "p2"):
        old.units[f"3d-printer-ondemand@{name}.service"] = unit_show(
            enabled="enabled", args=f"-m printer_ops.ondemand {name}"
        )
    _, runner = run_install(tmp_path, old, "p1", plans=[plan("p1"), plan("p2")])
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    for name in ("p1", "p2"):
        assert ["disable", "--now", f"3d-printer-ondemand@{name}.service"] in systemctl
        assert ["enable", "--now", f"3d-printer-ondemand@{name}.timer"] in systemctl


def test_uninstalling_a_first_design_install_skips_the_timer_it_never_had(
    tmp_path: Path,
) -> None:
    """An install of the first #747 design has a resident check process and no timer: systemctl exits 5 for
    `disable --now` of a unit it has no file for, and that must not turn the uninstall into a failure."""
    old = healthy(supervisor=False)
    old.units["3d-printer-ondemand@p1.service"] = unit_show(
        enabled="enabled", args="-m printer_ops.ondemand p1"
    )
    runner = Runner()
    runner.fail = {
        "systemctl --user disable --now 3d-printer-ondemand@p1.timer",
        "systemctl --user stop 3d-printer-ondemand@p1.timer",
    }
    env = env_for(tmp_path, old, runner)
    code = sv.cmd_uninstall(["--unit-dir", str(tmp_path / "units"), "p1"], env)
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    assert code == 0
    assert ["disable", "--now", "3d-printer-ondemand@p1.service"] in systemctl
    assert not any(c[-1].endswith(".timer") for c in systemctl)
    assert ["stop", "3d-printer-watch@p1.service"] in systemctl


def test_uninstall_stops_a_check_in_flight_before_the_workers_so_it_cannot_restart_them(
    tmp_path: Path,
) -> None:
    run_install(tmp_path, Systemd(), "--all")
    runner = Runner()
    env = env_for(tmp_path, healthy(), runner)
    sv.cmd_uninstall(["--all", "--yes", "--unit-dir", str(tmp_path / "units")], env)
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    timer = ["disable", "--now", "3d-printer-ondemand@p1.timer"]
    check = ["stop", "3d-printer-ondemand@p1.service"]
    watch = ["stop", "3d-printer-watch@p1.service"]
    assert systemctl.index(timer) < systemctl.index(check) < systemctl.index(watch)


def test_uninstall_stops_the_timer_and_the_check_even_when_the_timer_is_not_counted_enabled(
    tmp_path: Path,
) -> None:
    """An enabled-but-stopped timer, or a running one that was `disable`d without --now, reads as not enabled; the
    timer and a check in flight must still be stopped or they can restart workers after the uninstall."""
    actions = su.plan_uninstall(
        [plan()], LAYOUT, installed={}, all_units=False, enabled={}
    )
    argvs = [a.argv for a in actions]
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "3d-printer-ondemand@p1.timer",
    ] in argvs
    assert ["systemctl", "--user", "stop", "3d-printer-ondemand@p1.service"] in argvs


def test_a_timer_whose_check_service_is_missing_is_a_finding_not_a_pass(
    tmp_path: Path,
) -> None:
    systemd = healthy()
    del systemd.units[
        "3d-printer-ondemand@p1.service"
    ]  # armed timer, nothing for it to run
    for unit in ("watch", "vision"):
        systemd.units[f"3d-printer-{unit}@p1.service"] = unit_show(
            "inactive", sub="dead", enabled="static"
        )
    a = assess_status(tmp_path, systemd, state="idle")
    assert a.level == sv.FAIL
    assert any(
        "not installed" in t and "timer has nothing" in t for t in findings(a, sv.FAIL)
    )


def test_a_check_that_has_not_completed_for_ten_minutes_is_a_finding_even_with_an_armed_timer(
    tmp_path: Path,
) -> None:
    """A condition-skipped (missing checkout) or hanging check leaves the OLD finish time: the age says so."""
    systemd = _on_demand(_polled(sv.CHECK_STALE_S + 60, printer="idle"))
    a = assess_status(tmp_path, systemd, state="idle")
    assert a.level == sv.FAIL
    assert any("no check completes" in t for t in findings(a, sv.FAIL))
    fresh = assess_status(tmp_path / "f", _on_demand(_polled(40)), state="idle")
    assert fresh.level == sv.OK, [f.text for f in fresh.findings]


def test_a_resident_legacy_supervisor_is_retired_even_when_it_was_only_disabled_not_stopped(
    tmp_path: Path,
) -> None:
    old = healthy(supervisor=False)
    old.units["3d-printer-ondemand@p1.service"] = unit_show(
        "active", sub="running", enabled="disabled", args="-m printer_ops.ondemand p1"
    )
    run_install(tmp_path, Systemd(), "--all")
    _, runner = run_install(tmp_path, old, "--all")
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    assert ["disable", "--now", "3d-printer-ondemand@p1.service"] in systemctl


def test_a_check_in_flight_is_not_mistaken_for_the_resident_legacy_supervisor(
    tmp_path: Path,
) -> None:
    flight = healthy()
    flight.units["3d-printer-ondemand@p1.service"] = unit_show(
        "activating", sub="start", enabled="static"
    )
    run_install(tmp_path, Systemd(), "--all")
    _, runner = run_install(tmp_path, flight, "--all")
    assert not any(
        c[2:] == ["disable", "--now", "3d-printer-ondemand@p1.service"]
        for c in runner.calls
        if c[0] == "systemctl"
    )


def test_individual_uninstall_also_disables_a_resident_legacy_supervisor_so_it_cannot_return_at_boot(
    tmp_path: Path,
) -> None:
    old = healthy(supervisor=False)
    old.units["3d-printer-ondemand@p1.service"] = unit_show(
        "active", sub="running", enabled="enabled", args="-m printer_ops.ondemand p1"
    )
    runner = Runner()
    env = env_for(tmp_path, old, runner)
    sv.cmd_uninstall(["p1", "--unit-dir", str(tmp_path / "units")], env)
    systemctl = [c[2:] for c in runner.calls if c[0] == "systemctl" and c[2] != "show"]
    assert ["disable", "--now", "3d-printer-ondemand@p1.service"] in systemctl


def test_an_armed_timer_whose_every_check_was_skipped_is_a_finding_after_the_startup_allowance(
    tmp_path: Path,
) -> None:
    """A missing checkout skips every tick: no check ever completes, so no state file and no finish time exist."""
    systemd = _on_demand("")
    systemd.units["3d-printer-ondemand@p1.timer"] = unit_show(
        sub="waiting", active_mono=MONO - sv.CHECK_STALE_S - 60
    )
    a = assess_status(tmp_path, systemd, state="idle")
    assert a.level == sv.FAIL
    assert any("no on-demand check has completed" in t for t in findings(a, sv.FAIL))
    young = _on_demand("")
    young.units["3d-printer-ondemand@p1.timer"] = unit_show(
        sub="waiting", active_mono=MONO - 60
    )
    assert assess_status(tmp_path / "y", young, state="idle").level == sv.OK
