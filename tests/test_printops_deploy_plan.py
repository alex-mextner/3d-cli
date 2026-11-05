"""The shared deploy plan (printops.deploy) and `3d printops deploy`: what is published where.

Behaviour tests on the plan's argv/scripts and on the compose file as docker compose would
interpolate it. Plan steps that run are `bash -c` scripts on this machine against recording
`docker`/`systemctl`/`timedatectl` shims in front of PATH: no ssh, rsync or real docker.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from errors import InvalidArgument, UsageError

from printops import deploy
from printops.cli import deploy as deploy_cli

REPO = Path(__file__).resolve().parents[1]


def compose_resolved(env: Mapping[str, str]) -> dict[str, Any]:
    """services/3d-api/compose.yaml with `${VAR:-default}` interpolated like docker compose does
    (an unset or empty VAR takes the default)."""
    text = (REPO / "services" / "3d-api" / "compose.yaml").read_text(encoding="utf-8")
    resolved = re.sub(
        r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), text
    )
    return dict(yaml.safe_load(resolved))


def test_compose_defaults_are_the_odroid_values() -> None:
    services = compose_resolved({})["services"]
    spoolman, api = services["spoolman"], services["api"]
    assert spoolman["ports"] == ["0.0.0.0:7912:8000"] and api["ports"] == [
        "0.0.0.0:8765:8765"
    ]
    assert (
        spoolman["container_name"] == "printops-spoolman"
        and api["container_name"] == "printops-api"
    )
    assert str(spoolman["environment"]["PUID"]) == "1000" and api["user"] == "1000:1000"
    assert api["environment"]["TZ"] == "Europe/Belgrade"


def test_every_compose_knob_moves_what_it_names() -> None:
    knobs = {
        "PRINTOPS_BIND_ADDR": "127.0.0.1",
        "PRINTOPS_SPOOLMAN_PORT": "17912",
        "PRINTOPS_API_PORT": "18765",
        "PRINTOPS_CONTAINER_PREFIX": "pogb",
        "PRINTOPS_UID": "1234",
        "PRINTOPS_GID": "4321",
        "PRINTOPS_TZ": "UTC",
        "PRINTOPS_SPOOLMAN_CORS_ORIGIN": "http://fluidd.lan",
    }
    services = compose_resolved(knobs)["services"]
    spoolman, api = services["spoolman"], services["api"]
    assert spoolman["ports"] == ["127.0.0.1:17912:8000"] and api["ports"] == [
        "127.0.0.1:18765:8765"
    ]
    assert (
        spoolman["container_name"] == "pogb-spoolman"
        and api["container_name"] == "pogb-api"
    )
    assert api["user"] == "1234:4321" and api["environment"]["TZ"] == "UTC"
    assert spoolman["environment"]["SPOOLMAN_CORS_ORIGIN"] == "http://fluidd.lan"


def _local(tmp_path: Path, **kw: Any) -> deploy.DeployConfig:
    return deploy.DeployConfig(
        repo_root=tmp_path,
        host="",
        local=True,
        remote_dir=str(tmp_path / "p"),
        backup_dir=str(tmp_path / "p" / "backup"),
        timer_required=False,
        **kw,
    )


def _compose_script(cfg: deploy.DeployConfig) -> str:
    return next(s.argv[-1] for s in deploy.plan(cfg) if "docker compose" in s.argv[-1])


def test_the_odroid_plan_never_mentions_the_bind_address(tmp_path: Path) -> None:
    compose = _compose_script(deploy.DeployConfig(repo_root=tmp_path, version="v1"))
    assert (
        "PRINTOPS_BIND_ADDR" not in compose
    )  # its deploy output is byte-identical to before the knob


def test_a_given_bind_address_is_pinned_so_the_callers_environment_cannot_change_it(
    tmp_path: Path,
) -> None:
    assert "PRINTOPS_BIND_ADDR=127.0.0.1" in _compose_script(
        _local(tmp_path, bind_addr="127.0.0.1")
    )
    assert "PRINTOPS_BIND_ADDR=0.0.0.0" in _compose_script(
        _local(tmp_path, bind_addr="0.0.0.0")
    )
    assert "PRINTOPS_BIND_ADDR='[::1]'" in _compose_script(
        _local(tmp_path, bind_addr="[::1]")
    )


def test_the_local_health_url_follows_the_bind_address(tmp_path: Path) -> None:
    assert _local(tmp_path).health_url == "http://127.0.0.1:8765"
    assert _local(tmp_path, bind_addr="0.0.0.0").health_url == "http://127.0.0.1:8765"
    assert (
        _local(tmp_path, bind_addr="192.168.0.20").health_url
        == "http://192.168.0.20:8765"
    )
    assert _local(tmp_path, bind_addr="[::1]").health_url == "http://[::1]:8765"


def test_bind_addresses_are_ip_literals_only() -> None:
    assert deploy.normalize_bind("127.0.0.1") == "127.0.0.1"
    assert (
        deploy.normalize_bind("::1") == "[::1]"
        and deploy.normalize_bind("[::1]") == "[::1]"
    )
    for bad in ("nas.lan", "", "256.1.1.1", "127.0.0.1:80", "-p"):
        with pytest.raises(ValueError):
            deploy.normalize_bind(bad)


def test_printops_deploy_local_listens_on_loopback_unless_bind_says_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert deploy_cli.run(["--local", "--dry-run"]) == 0
    assert "PRINTOPS_BIND_ADDR=127.0.0.1" in capsys.readouterr().out
    assert deploy_cli.run(["--local", "--dry-run", "--bind", "192.168.0.20"]) == 0
    shown = capsys.readouterr().out
    assert (
        "PRINTOPS_BIND_ADDR=192.168.0.20" in shown
        and "then: wait for http://192.168.0.20:8765" in shown
    )


def test_printops_deploy_to_a_host_keeps_the_default_bind_unless_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    planned: list[deploy.Step] = []  # the plan is captured, never run: no rsync/ssh to any host
    monkeypatch.setattr(deploy, "run_steps", lambda steps, **_kw: planned.extend(steps))
    assert deploy_cli.run(["--dry-run"]) == 0
    assert "PRINTOPS_BIND_ADDR" not in " ".join(s.argv[-1] for s in planned)
    planned.clear()
    assert deploy_cli.run(["--dry-run", "--bind", "100.116.57.66"]) == 0
    assert "PRINTOPS_BIND_ADDR=100.116.57.66" in " ".join(s.argv[-1] for s in planned)
    capsys.readouterr()


def test_printops_deploy_rejects_a_bind_that_is_not_an_ip_address() -> None:
    with pytest.raises(InvalidArgument, match="--bind"):
        deploy_cli.run(["--local", "--dry-run", "--bind", "nas.lan"])


# ------------------------------------------------------------------ a host that is not the Odroid


def _step(cfg: deploy.DeployConfig, title: str) -> deploy.Step:
    return next(s for s in deploy.plan(cfg) if s.title.startswith(title))


def _shims(root: Path) -> Path:
    """`docker`/`curl` recorders plus a `timedatectl` and a `systemctl` that answer without a system."""
    from printops_env_fakes import fake_docker_bin

    bindir = fake_docker_bin(root)
    (bindir / "timedatectl").write_text("#!/bin/sh\necho Europe/Paris\n", encoding="utf-8")
    (bindir / "systemctl").write_text('#!/bin/sh\necho "systemctl $*" >> "$FAKE_LOG"\n', encoding="utf-8")
    (bindir / "loginctl").write_text('#!/bin/sh\necho "Linger=${FAKE_LINGER:-yes}"\n', encoding="utf-8")
    for name in ("timedatectl", "systemctl", "loginctl"):
        (bindir / name).chmod(0o755)
    return bindir


def _run_step(step: deploy.Step, root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        **os.environ,
        "PATH": f"{_shims(root)}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(root / "calls.log"),
        "HOME": str(root / "home"),
        **env,
    }
    (root / "home").mkdir(exist_ok=True)
    return subprocess.run(step.argv, env=full, capture_output=True, text=True, check=False)


def test_the_odroid_compose_script_leaves_prefix_tz_and_cors_to_compose(tmp_path: Path) -> None:
    compose = _compose_script(deploy.DeployConfig(repo_root=tmp_path, version="v1"))
    for knob in ("PRINTOPS_CONTAINER_PREFIX", "PRINTOPS_TZ", "PRINTOPS_SPOOLMAN_CORS_ORIGIN", "timedatectl"):
        assert knob not in compose  # the owner's Fluidd origins and Europe/Belgrade stay compose's defaults


def test_a_pinned_deploy_ignores_the_callers_container_prefix_tz_and_cors(tmp_path: Path) -> None:
    """The caller's own PRINTOPS_* environment must not rename the containers (the token mint and the
    backup unit exec into `printops-api`), pick the owner's time zone or widen Spoolman's origins."""
    done = _run_step(
        _step(_local(tmp_path, pin_host_env=True), "build + start"),
        tmp_path,
        PRINTOPS_CONTAINER_PREFIX="x",
        PRINTOPS_TZ="Mars/Olympus",
        PRINTOPS_SPOOLMAN_CORS_ORIGIN="http://evil.example",
    )
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert "PRINTOPS_CONTAINER_PREFIX=printops\n" in calls and "-p printops" in calls
    assert "PRINTOPS_TZ=Europe/Paris\n" in calls  # this host's, not the caller's and not Europe/Belgrade
    assert "PRINTOPS_SPOOLMAN_CORS_ORIGIN=http://127.0.0.1:7912,http://localhost:7912\n" in calls
    assert "evil.example" not in calls and "Mars" not in calls


def test_the_spoolman_origin_is_the_address_the_service_is_reached_on() -> None:
    nas = deploy.DeployConfig(repo_root=REPO, host="me@nas.lan", api_url="http://nas.lan:8765", pin_host_env=True)
    assert nas.spoolman_origins == "http://nas.lan:7912"
    v6 = deploy.DeployConfig(repo_root=REPO, host="me@v6", api_url="http://[2001:db8::7]:8765", pin_host_env=True)
    assert v6.spoolman_origins == "http://[2001:db8::7]:7912"
    assert _local(REPO, bind_addr="192.168.0.20").spoolman_origins == "http://192.168.0.20:7912"


def test_a_pinned_backup_unit_execs_the_docker_this_host_has(tmp_path: Path) -> None:
    """The shipped unit says /usr/bin/docker; with docker in snap or /usr/local/bin the timer would
    be enabled and fail every night unseen."""
    cfg = _local(REPO, pin_host_env=True)
    done = _run_step(_step(cfg, "install daily backup timer"), tmp_path)
    assert done.returncode == 0, done.stderr
    unit = (tmp_path / "home" / ".config" / "systemd" / "user" / "printops-backup.service").read_text()
    docker = tmp_path / "fakebin" / "docker"
    assert f"ExecStart={docker} exec {deploy.API_CONTAINER} 3d printops backup" in unit
    assert "systemctl --user enable --now printops-backup.timer" in (tmp_path / "calls.log").read_text()
    assert (tmp_path / "home" / ".config" / "systemd" / "user" / "printops-backup.timer").is_file()


def test_the_odroid_backup_unit_is_copied_as_shipped(tmp_path: Path) -> None:
    cfg = deploy.DeployConfig(
        repo_root=REPO, host="", local=True, remote_dir=str(tmp_path / "p"), backup_dir=str(tmp_path / "b")
    )
    done = _run_step(_step(cfg, "install daily backup timer"), tmp_path)
    assert done.returncode == 0, done.stderr
    copied = tmp_path / "home" / ".config" / "systemd" / "user" / "printops-backup.service"
    shipped = REPO / "services" / "3d-api" / "systemd" / "printops-backup.service"
    assert copied.read_bytes() == shipped.read_bytes()


def test_the_shipped_backup_unit_and_the_token_mint_name_the_same_container() -> None:
    from printops.cli import token

    shipped = (REPO / "services" / "3d-api" / "systemd" / "printops-backup.service").read_text(encoding="utf-8")
    exec_start = next(ln for ln in shipped.splitlines() if ln.startswith("ExecStart="))
    assert f" exec {deploy.API_CONTAINER} " in exec_start and token.CONTAINER == deploy.API_CONTAINER


# ------------------------------------------------------------------ "this machine" is never a default


def test_an_empty_host_is_not_this_machine() -> None:
    """`--host "$ODROID"` with the variable unset must not quietly deploy here as `ultra`."""
    with pytest.raises(ValueError, match="empty"):
        deploy.DeployConfig(repo_root=REPO, host="")
    for bad in ("-oProxyCommand=x", "me@nas lan"):
        with pytest.raises(ValueError, match="deploy host"):
            deploy.DeployConfig(repo_root=REPO, host=bad)
    with pytest.raises(ValueError, match="no ssh host"):
        deploy.DeployConfig(repo_root=REPO, host="me@nas", local=True)
    assert deploy.DeployConfig(repo_root=REPO, host="", local=True).local


def test_printops_deploy_refuses_an_empty_or_option_like_host_before_running_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(deploy, "run_steps", lambda steps, **_kw: ran.append(steps))
    for host in ("", "-oProxyCommand=sh", "a b"):
        with pytest.raises(UsageError, match="--host") as err:
            deploy_cli.run(["--host", host, "--dry-run"])
        assert err.value.exit_code == 2 and "--local" in err.value.render()
    assert not ran  # nothing was planned, let alone executed


def test_a_host_without_lingering_is_told_the_backup_timer_stops_with_the_session(tmp_path: Path) -> None:
    cfg = _local(REPO, pin_host_env=True)
    off = _run_step(_step(cfg, "install daily backup timer"), tmp_path, FAKE_LINGER="no")
    assert off.returncode == 0, off.stderr
    assert "sudo loginctl enable-linger" in off.stdout and "only runs while you are logged in" in off.stdout
    on = _run_step(_step(cfg, "install daily backup timer"), tmp_path / "on", FAKE_LINGER="yes")
    assert on.returncode == 0, on.stderr
    assert "linger" not in on.stdout


def test_the_odroid_timer_step_has_no_linger_check(tmp_path: Path) -> None:
    """Its plan output stays byte-identical, and the Odroid's linger is already on."""
    assert "loginctl" not in _step(deploy.DeployConfig(repo_root=tmp_path), "install daily backup timer").argv[-1]
