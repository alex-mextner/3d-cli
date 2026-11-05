"""printops.env_stage_install: the address Home Assistant is given, the compose-only redeploy plan and the
whole `stage_after_install` glue, with real objects and fake shells/runners (nothing reaches a host)."""

from __future__ import annotations

import subprocess
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
    compose_only,
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
    return deploy.DeployConfig(
        repo_root=REPO,
        host=host,
        remote_dir="/srv/printops",
        backup_dir="/srv/printops/backup",
        **over,
    )


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


# ---------------------------------------------------------------------------- compose_only


def test_compose_only_binds_the_chosen_disk_and_never_syncs_sources() -> None:
    """A real DeployConfig through the real planner and run_steps, with a runner that records instead of running."""
    steps = compose_only(cfg("me@nas"), "/mnt/backup512")
    assert not any("sync" in s.title for s in steps)
    seen: list[list[str]] = []

    def runner(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    deploy.run_steps(steps, cwd=REPO, dry_run=False, log=lambda _l: None, runner=runner)
    compose = next(a[-1] for a in seen if "docker compose" in a[-1])
    assert "PRINTOPS_BACKUP_DIR=/mnt/backup512" in compose and "up -d" in compose
    assert all(a[0] == "ssh" for a in seen)  # an ssh host stays an ssh host
    assert not any("rsync" in " ".join(a) for a in seen)


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
    assert "PRINTOPS_BACKUP_DIR=/mnt/backup512" in next(
        s.argv[-1] for s in steps if "compose" in s.argv[-1]
    )
    package = (ha_dir / "packages" / "printops_backup.yaml").read_text(encoding="utf-8")
    assert 'url: "http://192.168.0.39:8765/v1/backup/run"' in package
    assert fake_ha.entries[0]["data"]["spoolman_url"] == "http://192.168.0.39:7912"
    assert (
        any(s.startswith("test -d") for s in host.scripts) and DISCOVERY
    )  # the disk was checked on the Spoolman host
