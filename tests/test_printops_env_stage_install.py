"""printops.env_stage_install: the address Home Assistant is given, the compose-only redeploy plan and the
whole `stage_after_install` glue, with real objects and fake shells/runners (nothing reaches a host)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from printops_ha_fakes import TOKEN, FakeHa
from test_printops_ha_stage import DISCOVERY, Host

from printops import deploy, env_stage, env_stage_install
from printops.backup_discovery import Shell
from printops.env_stage_install import (
    address_for_ha,
    bind_disk_steps,
    ssh_hostname,
    stage_after_install,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_ha() -> Iterator[FakeHa]:
    ha = FakeHa()
    yield ha
    ha.close()


def cfg(host: str = "", **over: Any) -> deploy.DeployConfig:
    fields: dict[str, Any] = {
        "repo_root": REPO,
        "host": host,
        "local": not host,
        "remote_dir": "/srv/printops",
        "backup_dir": "/srv/printops/backup",
        **over,
    }
    return deploy.DeployConfig(**fields)


# ---------------------------------------------------------------------------- the address HA is given


def test_an_ssh_alias_is_resolved_to_the_address_ssh_connects_to() -> None:
    asked: list[str] = []

    def resolve(host: str) -> str:
        asked.append(host)
        return "192.168.0.39"

    assert address_for_ha(cfg("me@odroid"), resolve) == "192.168.0.39"
    assert asked == [
        "me@odroid"
    ]  # the alias `odroid` itself is something HA cannot resolve


def test_the_address_the_deploy_already_reached_wins_and_ipv6_stays_unbracketed_for_names() -> (
    None
):
    boom = lambda host: pytest.fail("resolved although the deploy knows the address")  # noqa: E731
    assert (
        address_for_ha(cfg("me@odroid", api_url="http://[fe80::1]:8765"), boom)
        == "fe80::1"
    )
    assert (
        address_for_ha(cfg("me@odroid", api_url="http://10.0.0.7:8765"), boom)
        == "10.0.0.7"
    )


def test_ssh_hostname_reads_the_hostname_of_ssh_g_and_falls_back_to_the_target_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text(
        '#!/bin/sh\n[ "$1" = "-G" ] && printf "user me\\nhostname 10.0.0.7\\nport 22\\n"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert ssh_hostname("me@odroid") == "10.0.0.7"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no ssh at all
    assert ssh_hostname("me@odroid") == "odroid"


def test_a_local_stack_gets_this_machines_lan_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sock:
        def __init__(self, *_a: object) -> None:
            pass

        def connect(self, _addr: object) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("192.168.1.20", 5555)

        def close(self) -> None:
            return None

    monkeypatch.setattr(env_stage_install.socket, "socket", Sock)
    assert address_for_ha(cfg("")) == "192.168.1.20"


@pytest.mark.parametrize(
    ("addr", "url"),
    [("10.0.0.7", "http://10.0.0.7:8765"), ("fe80::1", "http://[fe80::1]:8765")],
)
def test_urls_for_ha_bracket_ipv6(addr: str, url: str) -> None:
    assert env_stage_install._url(addr, 8765) == url


# ---------------------------------------------------------------------------- bind_disk_steps

FAKE_DOCKER = """#!{python}
import json, os, sys
a = sys.argv[1:]
log = open(os.environ["FAKE_LOG"], "a", encoding="utf-8")
log.write("docker " + " ".join(a) + "\\n")
stack = os.environ.get("FAKE_STACK", "up")
if a[0] == "inspect":
    if stack != "up":
        sys.exit(1)
    fmt = a[2] if a[1] == "-f" else ""
    name = a[-1]
    if "Destination" in fmt:
        dest = fmt.split('"')[1]
        print({{"/spoolman-data": os.environ["FAKE_ROOT"] + "/odd/data/spoolman", "/gcodes": os.environ["FAKE_ROOT"] + "/odd/gcodes"}}.get(dest, ""))
    elif ".Config.User" in fmt:
        print("1001:1002")
    elif ".Config.Env" in fmt:
        print("TZ=Europe/Paris" if name.endswith("api") else "SPOOLMAN_CORS_ORIGIN=http://fluidd.lan")
elif a[0] == "port":
    print("127.0.0.1:" + ("9876" if a[1].endswith("api") else "9912"))
elif a[0] == "compose":
    for key in sorted(os.environ):
        if key.startswith("PRINTOPS_"):
            log.write(key + "=" + os.environ[key] + "\\n")
"""


def run_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stack: str = "up"
) -> tuple[int, str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(
        FAKE_DOCKER.format(python=sys.executable), encoding="utf-8"
    )
    (bindir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "docker.log"))
    monkeypatch.setenv("FAKE_STACK", stack)
    monkeypatch.setenv("FAKE_ROOT", str(tmp_path / "srv"))
    (tmp_path / "srv" / "odd" / "data").mkdir(parents=True, exist_ok=True)
    steps = bind_disk_steps(cfg("", repo_root=tmp_path), "/mnt/backup512")
    assert len(steps) == 1 and steps[0].argv[:2] == ["bash", "-c"]
    done = subprocess.run(steps[0].argv, capture_output=True, text=True, check=False)
    log = (
        (tmp_path / "docker.log").read_text(encoding="utf-8")
        if (tmp_path / "docker.log").exists()
        else ""
    )
    return done.returncode, done.stderr, log


def test_bind_disk_recreates_only_the_api_with_the_values_of_the_running_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stack installed with another directory, ports, bind address, user, time zone and CORS list keeps them all."""
    rc, err, log = run_bind(tmp_path, monkeypatch)
    assert rc == 0, err
    assert (
        "compose -p printops -f services/3d-api/compose.yaml up -d --no-deps api" in log
    )
    assert "--build" not in log and "spoolman" not in log.split("up -d")[1]
    for expected in (
        f"PRINTOPS_DATA_DIR={tmp_path}/srv/odd/data",
        f"PRINTOPS_GCODE_HOST_DIR={tmp_path}/srv/odd/gcodes",
        "PRINTOPS_BACKUP_DIR=/mnt/backup512",
        "PRINTOPS_UID=1001",
        "PRINTOPS_GID=1002",
        "PRINTOPS_TZ=Europe/Paris",
        "PRINTOPS_SPOOLMAN_CORS_ORIGIN=http://fluidd.lan",
        "PRINTOPS_BIND_ADDR=127.0.0.1",
        "PRINTOPS_API_PORT=9876",
        "PRINTOPS_SPOOLMAN_PORT=9912",
    ):
        assert expected in log, expected
    assert not (
        tmp_path / "srv" / "odd" / "data" / "api"
    ).exists()  # no mkdirs: nothing is created
    assert (tmp_path / "srv" / "odd" / "backup-dir").read_text(
        encoding="utf-8"
    ) == "/mnt/backup512"


def test_bind_disk_refuses_when_the_stack_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, err, log = run_bind(tmp_path, monkeypatch, stack="down")
    assert (
        rc == 1
        and "is not running here" in err
        and "compose" not in log.replace("docker inspect", "")
    )


def test_bind_disk_over_ssh_runs_through_ssh_and_never_syncs_sources() -> None:
    (step,) = bind_disk_steps(cfg("me@nas"), "/mnt/backup512")
    assert step.argv[0] == "ssh" and "rsync" not in " ".join(step.argv)
    assert "/srv/printops/src" in step.argv[-1]


# ---------------------------------------------------------------------------- stage_after_install


def test_the_stage_binds_the_disk_and_gives_home_assistant_the_resolved_address(
    fake_ha: FakeHa, tmp_path: Path
) -> None:
    ha_dir = tmp_path / "ha"
    ha_dir.mkdir()
    (ha_dir / "configuration.yaml").write_text(
        "homeassistant:\n  packages: !include_dir_named packages\n", encoding="utf-8"
    )
    host = Host()
    built: list[tuple[str, float | None]] = []
    ran: list[list[deploy.Step]] = []
    said: list[str] = []

    def shells(target: str, timeout: float | None) -> Shell:
        built.append((target, timeout))
        return lambda script, stdin=None: host(script)

    opts = env_stage.parse_stage_options(
        {
            "--backup": "path:/mnt/backup512",
            "--ha": fake_ha.url,
            "--ha-wire": "yes",
            "--ha-dir": str(ha_dir),
        },
        set(),
    )
    rc = stage_after_install(
        host="me@odroid",
        label="the odroid",
        cfg=cfg("me@odroid"),
        container="printops-api",
        repo_root=REPO,
        opts=opts,
        ask=lambda _q: "",
        say=said.append,
        interactive=False,
        run_steps=ran.append,
        mint=lambda name: f"po_{name}",
        env={"HA_TOKEN": TOKEN},
        resolve=lambda _h: "192.168.0.39",
        shells=shells,
    )
    assert rc == 0, said
    assert [t for t, _ in built] == ["me@odroid", "me@odroid"]
    assert {timeout for _, timeout in built} == {
        None,
        env_stage_install.PROBE_TIMEOUT_S,
    }  # run + a bounded probe shell
    (steps,) = ran
    assert "PRINTOPS_BACKUP_DIR=/mnt/backup512" in steps[0].argv[-1] and len(steps) == 1
    package = (ha_dir / "packages" / "printops_backup.yaml").read_text(encoding="utf-8")
    assert 'url: "http://192.168.0.39:8765/v1/backup/run"' in package
    assert fake_ha.entries[0]["data"]["spoolman_url"] == "http://192.168.0.39:7912"
    assert (
        any(s.startswith("test -d") for s in host.scripts) and DISCOVERY
    )  # the disk was checked on the Spoolman host
