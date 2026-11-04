"""The shared deploy plan (printops.deploy) and `3d printops deploy`: what is published where.

Behaviour tests on the plan's argv/scripts and on the compose file as docker compose would
interpolate it. Nothing is executed: no ssh, rsync or docker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from errors import InvalidArgument

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
