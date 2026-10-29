"""`3d printer supervise` through the real `bin/3d`: help, install against a temp unit directory with fake
`systemctl`/`loginctl` programs on PATH, status as JSON, quiet. The fleet is a temp file; XDG dirs are temp; the
fakes keep their state in a directory; nothing reaches systemd, tg or a printer (`--no-live`)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
THREED = REPO / "bin" / "3d"

FLEET = """
p1:
  model: Test Model
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 1
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
  watch:
    vision: {camera: {kind: snapshot-url, url: "http://127.0.0.1:1/snap"}}
n1:
  model: Test Model
  owner: neighbour
  access: read-only
  adapter: moonraker
  host: 127.0.0.1
  port: 2
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
"""

SYSTEMCTL = """#!/bin/sh
# fake `systemctl --user ...`: $FAKE_SYSTEMD/<unit> holds the unit's `show` text once it was enabled
[ "$1" = "--user" ] && shift
case "$1" in
  show)
    unit="$2"
    if [ -f "$FAKE_SYSTEMD/$unit" ]; then cat "$FAKE_SYSTEMD/$unit"; else printf 'LoadState=not-found\\nActiveState=inactive\\n'; fi ;;
  enable)
    unit="$3"; kind=$(echo "$unit" | cut -d- -f3 | cut -d@ -f1); name=$(echo "$unit" | cut -d@ -f2 | cut -d. -f1)
    printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nUnitFileState=enabled\\nNRestarts=0\\nExecStart={ path=/x ; argv[]=/x printer %s %s ; ignore_errors=no ; pid=1 }\\nEnvironmentFiles=\\n' "$kind" "$name" > "$FAKE_SYSTEMD/$unit" ;;
  *) : ;;
esac
echo "$@" >> "$FAKE_SYSTEMD/calls.log"
"""

LOGINCTL = """#!/bin/sh
if [ "$1" = "show-user" ]; then echo "Linger=yes"; fi
echo "$@" >> "$FAKE_SYSTEMD/calls.log"
"""


@dataclass(frozen=True)
class Lab:
    env: dict[str, str]
    cwd: Path
    fake: Path
    units: Path


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    bin_dir, fake = tmp_path / "bin", tmp_path / "systemd"
    bin_dir.mkdir()
    fake.mkdir()
    for name, text in (("systemctl", SYSTEMCTL), ("loginctl", LOGINCTL)):
        path = bin_dir / name
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET)
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        THREED_FLEET=str(fleet),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        FAKE_SYSTEMD=str(fake),
    )
    return Lab(env, tmp_path, fake, tmp_path / "units")


def threed(lab: Lab, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(THREED), "printer", "supervise", *args],
        env=lab.env,
        cwd=lab.cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_help_lists_every_subcommand(lab: Lab) -> None:
    """The owner reads `3d printer supervise --help` to learn install, status, uninstall, quiet and linger."""
    r = threed(lab, "--help")
    assert r.returncode == 0
    for word in (
        "status",
        "install",
        "uninstall",
        "quiet",
        "notify-failure",
        "--yes",
        "linger",
    ):
        assert word in r.stdout
    assert (
        "supervise"
        in subprocess.run(
            [sys.executable, str(THREED), "printer", "--help"],
            env=lab.env,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    )


def test_install_then_status_through_the_real_cli(lab: Lab) -> None:
    """The owner installs the always-on units for every owned printer, then asks for the status as the
    watcher and the camera loop write their heartbeats: a warning without them, a FAIL when frames stopped."""
    units = str(lab.units)
    plan = threed(lab, "install", "--all", "--plan", "--unit-dir", units)
    assert plan.returncode == 0, plan.stderr
    assert (
        "supervised printers: p1 (watch+vision)" in plan.stdout
        and "n1" not in plan.stdout
    )
    assert not Path(units).exists()

    r = threed(lab, "install", "--all", "--unit-dir", units)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        (Path(units) / "3d-printer-watch@.service")
        .read_text()
        .splitlines()[0]
        .startswith("# managed-by:")
    )
    calls = (lab.fake / "calls.log").read_text()
    assert (
        "daemon-reload" in calls
        and "enable --now 3d-printer-vision@p1.service" in calls
    )

    # units run but no heartbeat exists and the printer was not asked: a warning, never a quiet pass
    status = threed(lab, "status", "--no-live", "--json", "--unit-dir", units)
    assert status.returncode == 0, status.stderr
    doc = json.loads(status.stdout)
    assert doc[0]["printer"] == "p1" and doc[0]["level"] == "warn"
    assert any("never written a heartbeat" in f["text"] for f in doc[0]["findings"])
    assert doc[0]["units"]["watch"]["active"] == "active"

    # the watcher says the printer prints, the camera loop has not produced a frame for ten minutes: FAIL, exit 1
    state = lab.cwd / "state" / "3d-cli"
    (state / "watch").mkdir(parents=True)
    (state / "vision").mkdir(parents=True)
    now = time.time()
    (state / "watch" / "p1.json").write_text(json.dumps({"pid": 1, "updated": now, "printers": {
        "p1": {"connected": True, "state": "printing", "job": {"file": "a.gcode", "started": now - 90}}}}))
    (state / "vision" / "p1.json").write_text(json.dumps({
        "schema": 1, "printer": "p1", "ts": now, "state": "printing", "supervising": True,
        "last_frame_ts": now - 600, "interval_s": 10.0}))
    status = threed(lab, "status", "--no-live", "--unit-dir", units)
    assert status.returncode == 1 and status.stdout.startswith("FAIL p1: printer printing")
    assert "the camera gave no frame for 10 min" in status.stdout


def test_quiet_sets_and_clears_the_marker_without_touching_units(lab: Lab) -> None:
    """The owner switches the K1 off for the week: `quiet` silences the unreachable alarms and stops no unit."""
    r = threed(lab, "quiet", "p1", "--reason", "off for the week")
    marker = lab.cwd / "state" / "3d-cli" / "p1.intentional-off"
    assert json.loads(marker.read_text())["reason"] == "off for the week"
    assert "keep running" in r.stdout
    assert not (lab.fake / "calls.log").exists()
    threed(lab, "quiet", "p1", "--clear")
    assert not marker.exists()


def test_an_unsupervised_printer_name_is_a_usage_error(lab: Lab) -> None:
    """The owner asks for the status of a neighbour's printer: the command refuses and names the supervised ones."""
    r = threed(lab, "status", "n1", "--no-live")
    assert "not a supervised printer" in r.stderr and "p1" in r.stderr
    assert r.returncode == 2


def test_notify_failure_is_silent_for_a_clean_stop(lab: Lab) -> None:
    """systemd runs the hook after `systemctl restart`: no result code means a clean stop and nothing is said."""
    r = threed(lab, "notify-failure", "p1", "watch")  # SERVICE_RESULT unset: run by hand
    assert r.stdout == "" and r.stderr == ""
    assert r.returncode == 0
