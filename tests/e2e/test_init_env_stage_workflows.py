"""`3d init --env --stage-only`: the backup disk + Home Assistant stage, one story per flag, through the real `bin/3d`.

The Spoolman host is "this machine" with a fake `docker` first on PATH: `compose` calls are logged (with the PRINTOPS_*
environment they got), `exec printops-api 3d printops ...` runs the REAL CLI with the container's /backup mapped to the
backup disk the story chose. Home Assistant is the loopback FakeHa; `ssh` (for --ha-ssh) is a shim that runs the
script locally and logs its argv. Nothing here reaches a real host, printer, docker daemon or Home Assistant.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from printops_ha_fakes import TOKEN, FakeHa

from .test_printops_backup_workflows import make_world
from .workflow_helper import run_cli

FAKE_DOCKER = """#!{python}
import os, sys
a = sys.argv[1:]
log = open(os.environ["FAKE_LOG"], "a", encoding="utf-8")
log.write("docker " + " ".join(a) + "\\n")
if a[:1] == ["--version"]:
    print("Docker version 27.0.0, build fake")
elif a[:2] == ["compose", "version"]:
    print("2.29.0")
elif a[:1] == ["inspect"]:
    if os.environ.get("FAKE_STACK") == "down":
        sys.exit(1)
    fmt = a[2] if a[1] == "-f" else ""
    if "Destination" in fmt:
        dest = fmt.split('"')[1]
        print({{"/spoolman-data": os.environ["FAKE_DATA"] + "/spoolman", "/gcodes": os.environ["FAKE_DATA"] + "/../gcodes"}}.get(dest, ""))
    elif ".Config.User" in fmt:
        print("1000:1000")
    elif ".Config.Env" in fmt:
        print("TZ=UTC")
elif a[:1] == ["port"]:
    print("127.0.0.1:" + ("8765" if a[1].endswith("api") else "7912"))
elif a[:1] == ["compose"]:
    for key, value in sorted(os.environ.items()):
        if key.startswith("PRINTOPS_"):
            log.write(key + "=" + value + "\\n")
elif a[:1] == ["exec"]:
    rest = [os.environ["FAKE_BACKUP_DIR"] if x == "/backup" else x for x in a[3:]]  # exec CONTAINER 3d ...
    log.close()
    os.execv(sys.executable, [sys.executable, os.environ["THREED"], *rest])
"""
FAKE_SSH = """#!/bin/sh
echo "ssh $*" >> "$FAKE_LOG"
if [ "$1" = "-G" ]; then echo "hostname 127.0.0.1"; exit 0; fi
for last; do :; done
exec bash -c "$last"
"""


@dataclass
class Stack:
    tmp: Path
    disk: Path
    env: dict[str, str]
    ha: FakeHa
    ha_dir: Path

    def init(self, *flags: str, with_ha: bool = False, backup: bool = True):  # type: ignore[no-untyped-def]
        argv = ["init", "--env", "--stage-only", "--spoolman", "local", "--no-input"]
        if backup:
            argv += ["--backup", f"path:{self.disk}", "--allow-same-disk"]
        argv += [
            "--ha",
            self.ha.url if with_ha else "skip",
        ]  # never let a test multicast the LAN for a real HA
        return run_cli(self.tmp, *argv, *flags, env_extra=self.env, timeout=300)

    @property
    def log(self) -> str:
        path = self.tmp / "fake.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def package(self) -> str:
        return (self.ha_dir / "packages" / "printops_backup.yaml").read_text(
            encoding="utf-8"
        )


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    env = make_world(tmp_path)
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "docker").write_text(
        FAKE_DOCKER.format(python=sys.executable), encoding="utf-8"
    )
    (bindir / "ssh").write_text(FAKE_SSH, encoding="utf-8")
    for tool in ("docker", "ssh"):
        (bindir / tool).chmod(0o755)
    disk = tmp_path / "backup-disk"
    disk.mkdir()
    # the Spoolman data dir `--stage-only --spoolman local` derives: on the same filesystem as `disk`
    (tmp_path / "xdg-data" / "3d-cli" / "printops" / "data").mkdir(parents=True)
    ha_dir = tmp_path / "ha-config"
    ha_dir.mkdir()
    (ha_dir / "configuration.yaml").write_text(
        "homeassistant:\n  packages: !include_dir_named packages\n", encoding="utf-8"
    )
    ha = FakeHa()
    env.update(
        {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(tmp_path / "fake.log"),
            "FAKE_BACKUP_DIR": str(disk),
            "FAKE_DATA": str(tmp_path / "xdg-data" / "3d-cli" / "printops" / "data"),
            "HA_TOKEN": TOKEN,
        }
    )
    yield Stack(tmp_path, disk, env, ha, ha_dir)
    ha.close()


def test_stage_only_with_backup_path_binds_the_disk_and_proves_a_restore(
    stack: Stack,
) -> None:
    """Story: the stack already runs; the operator names a mounted disk. It sits on the data disk, so it is refused
    until --allow-same-disk; then the disk is bound, registered, a real backup is written and restored."""
    refused = run_cli(
        stack.tmp,
        "init",
        "--env",
        "--stage-only",
        "--spoolman",
        "local",
        "--no-input",
        "--backup",
        f"path:{stack.disk}",
        "--ha",
        "skip",  # never let a test multicast the LAN looking for a real Home Assistant
        env_extra=stack.env,
    )
    assert refused.returncode == 1, refused.stdout
    assert (
        "same disk as the Spoolman data" in refused.stdout
        and "NOT configured" in refused.stdout
    )
    assert (
        "compose -p" not in stack.log and not (stack.disk / ".3d-backup-disk").exists()
    )

    ok = stack.init()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "backups: configured" in ok.stdout and "selftest ok" in ok.stdout
    assert (
        f"PRINTOPS_BACKUP_DIR={stack.disk}" in stack.log
    )  # the 3d-api container got the disk bound
    assert (stack.disk / ".3d-backup-disk").is_file()
    assert list((stack.disk / "3d-printops").glob("3d-backup-*"))
    assert str(stack.disk) in Path(stack.env["PRINTOPS_BACKUP_CONFIG"]).read_text(
        encoding="utf-8"
    )


def test_backup_skip_writes_nothing_and_exits_zero(stack: Stack) -> None:
    """A user running `3d init --env --stage-only --backup skip` is told backups were skipped; no container is touched and no disk marker is written."""
    r = run_cli(
        stack.tmp,
        "init",
        "--env",
        "--stage-only",
        "--spoolman",
        "local",
        "--no-input",
        "--backup",
        "skip",
        "--ha",
        "skip",
        env_extra=stack.env,
    )
    assert r.returncode == 0
    assert "backups: skipped" in r.stdout
    assert (
        "compose -p" not in stack.log and not (stack.disk / ".3d-backup-disk").exists()
    )


def test_backup_agent_prints_a_hand_off_prompt_sized_by_backup_disk_gb(
    stack: Stack,
) -> None:
    """Story: the operator hands the disk work to an agent; --backup-disk-gb 250 tells it which size to expect."""
    r = run_cli(
        stack.tmp,
        "init",
        "--env",
        "--stage-only",
        "--spoolman",
        "local",
        "--no-input",
        "--backup",
        "agent",
        "--backup-disk-gb",
        "250",
        "--ha",
        "skip",
        env_extra=stack.env,
    )
    assert r.returncode == 0, r.stderr
    assert "hand this to an AI agent" in r.stdout and "about 250 GB" in r.stdout
    assert (
        "compose -p" not in stack.log and not (stack.disk / ".3d-backup-disk").exists()
    )


def test_backup_auto_with_no_input_chooses_nothing(stack: Stack) -> None:
    """An agent running the stage with `--backup auto --no-input` is shown what was found and told that nothing is chosen for it; nothing is bound or written."""
    r = run_cli(
        stack.tmp,
        "init",
        "--env",
        "--stage-only",
        "--spoolman",
        "local",
        "--no-input",
        "--backup",
        "auto",
        "--ha",
        "skip",
        env_extra=stack.env,
    )
    assert r.returncode == 0
    assert "--no-input chooses nothing" in r.stdout
    assert "compose -p" not in stack.log


def test_ha_wire_yes_installs_the_package_with_keep_and_backup_at_and_wires_spoolman(
    stack: Stack,
) -> None:
    """Story: --ha names the Home Assistant, --ha-wire yes answers the question, --ha-dir is its config dir; --keep and
    --backup-at end up in the package; the 3d-api token is minted for HA's secrets.yaml."""
    r = stack.init(
        "--ha-wire",
        "yes",
        "--ha-dir",
        str(stack.ha_dir),
        "--keep",
        "daily=3,weekly=2,monthly=1",
        "--backup-at",
        "03:15",
        with_ha=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    package = stack.package()
    assert (
        "keep_daily: 3" in package
        and "keep_weekly: 2" in package
        and "keep_monthly: 1" in package
    )
    assert 'at: "03:15:00"' in package
    assert (
        'target: "default"' in package and "api_url" not in package.split("script:")[1]
    )
    url_line = next(
        ln for ln in package.splitlines() if ln.strip().startswith('url: "http://')
    )
    assert url_line.strip().endswith(
        ':8765/v1/backup/run"'
    )  # the stack's own address, written into the rest_command
    assert 'printops_api_auth: "Bearer po_' in (
        stack.ha_dir / "secrets.yaml"
    ).read_text(encoding="utf-8")
    assert stack.ha.entries and stack.ha.entries[0]["data"]["spoolman_url"].endswith(
        ":7912"
    )
    posted = [r[1] for r in stack.ha.requests if r[0] == "POST"]
    assert (
        "/api/config/core/check_config" in posted
        and "/api/services/script/reload" in posted
    )
    assert "backup package: done" in r.stdout


def test_ha_wire_no_leaves_home_assistant_alone(stack: Stack) -> None:
    """A user who passes `--ha-wire no` has Home Assistant left alone: no write request is sent and no package file appears."""
    r = stack.init("--ha-wire", "no", "--ha-dir", str(stack.ha_dir), with_ha=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "left alone" in r.stdout
    assert stack.ha.writes() == [] and not (stack.ha_dir / "packages").exists()


def test_ha_ssh_writes_the_package_through_ssh_to_the_ha_host(stack: Stack) -> None:
    """Story: Home Assistant lives on another host: the package and secret are written there over ssh."""
    r = stack.init(
        "--ha-wire",
        "yes",
        "--ha-dir",
        str(stack.ha_dir),
        "--ha-ssh",
        "ha@hassbox",
        with_ha=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    ssh_calls = [
        ln
        for ln in stack.log.splitlines()
        if ln.startswith("ssh ") and "ha@hassbox" in ln
    ]
    assert ssh_calls and all("BatchMode=yes" in ln for ln in ssh_calls)
    assert (stack.ha_dir / "packages" / "printops_backup.yaml").is_file()


@pytest.mark.parametrize(
    ("flags", "needle"),
    [
        (("--ha", "yes"), "--ha"),
        (("--ha", "host:8123"), "--ha"),
        (("--ha-wire", "maybe"), "--ha-wire"),
        (("--backup-at", "25:00"), "--backup-at"),
        (("--backup-disk-gb", "0"), "--backup-disk-gb"),
        (("--backup", "everything"), "--backup"),
        (("--keep", "daily=x"), "--keep"),
    ],
)
def test_stage_flag_mistakes_exit_2_naming_the_flag(
    stack: Stack, flags: tuple[str, ...], needle: str
) -> None:
    """A user who mistypes a stage flag gets exit 2 with the flag named on stderr, no traceback, and no container is touched."""
    r = run_cli(
        stack.tmp,
        "init",
        "--env",
        "--stage-only",
        "--spoolman",
        "local",
        "--no-input",
        *flags,
        env_extra=stack.env,
    )
    assert r.returncode == 2
    assert needle in r.stderr
    assert "Traceback" not in r.stderr
    assert "compose -p" not in stack.log


def test_stage_only_refuses_when_the_stack_is_not_running_and_changes_nothing(
    stack: Stack,
) -> None:
    """Story: `--stage-only` on a machine whose containers are gone must not rebuild the stack from defaults."""
    stack.env["FAKE_STACK"] = "down"
    r = stack.init()
    assert r.returncode == 1, r.stdout
    assert "is not running here" in r.stdout + r.stderr and "NOT configured" in r.stdout
    assert (
        "compose -p" not in stack.log and not (stack.disk / ".3d-backup-disk").exists()
    )


def test_the_chosen_disk_is_remembered_next_to_the_data_dir(stack: Stack) -> None:
    """A user who chose a backup disk in the stage finds it saved beside the data dir, so a later plain `3d printops deploy` binds the same disk."""
    ok = stack.init()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    saved = stack.tmp / "xdg-data" / "3d-cli" / "printops" / "backup-dir"
    assert saved.read_text(encoding="utf-8") == str(
        stack.disk
    )  # a later plain `3d printops deploy` binds it again
