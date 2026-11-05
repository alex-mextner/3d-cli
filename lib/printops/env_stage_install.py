"""env_stage_install.py — the glue between `3d init --env` (printops.env_setup) and the backup/HA stage
(printops.env_stage): turns "a stack was just deployed to HOST" into the `StageEnv` the stage needs.

ACCESSED VIA: `printops.env_setup` (after a successful install, and for `--stage-only` on an existing one).

WHY a module of its own: env_setup knows the deploy facts (host, directories, ports, how to run a plan and
mint a token); env_stage knows backups and Home Assistant. This file is the only place that joins them, so
neither imports the other.
"""

from __future__ import annotations

import dataclasses
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from printops import backup_setup, deploy, env_stage
from printops.backup_discovery import Shell
from printops.backup_setup import BackupContext

RunSteps = Callable[[list[deploy.Step]], None]
PROBE_TIMEOUT_S = 90.0  # df / ls of a mount: a stale NFS mount hangs them for good


def _real_shell(host: str, timeout: float | None) -> Shell:
    return (
        backup_setup.ssh_shell(host, timeout=timeout)
        if host
        else backup_setup.local_shell(timeout)
    )


def ssh_hostname(host: str) -> str:
    """The name or address an HTTP client reaches the ssh target `host` on: a `Host` alias that only ssh
    knows resolves through ssh's own config (`ssh -G` connects to nothing), a plain name comes back as is."""
    name = host.rsplit("@", 1)[-1]
    try:
        r = subprocess.run(
            ["ssh", "-G", host], capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return name
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        key, _, value = line.partition(" ")
        if key == "hostname" and value.strip():
            return value.strip()
    return name


def _lan_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(
            ("192.0.2.1", 9)
        )  # a UDP "connect" sends nothing; it only picks the outgoing interface
        return str(probe.getsockname()[0])
    except OSError:
        return socket.gethostname()
    finally:
        probe.close()


def address_for_ha(
    cfg: deploy.DeployConfig, resolve: Callable[[str], str] = ssh_hostname
) -> str:
    """The host part Home Assistant should use for the stack `cfg` describes, unbracketed: the address the
    deploy itself reached (`cfg.api_url`), else what ssh resolves the target to (an alias HA could not
    resolve is never passed on), else this machine's LAN IP."""
    if cfg.api_url and (known := urlsplit(cfg.api_url).hostname):
        return known
    return resolve(cfg.host) if cfg.host else _lan_address()


def _url(addr: str, port: int) -> str:
    return f"http://[{addr}]:{port}" if ":" in addr else f"http://{addr}:{port}"


def compose_only(cfg: deploy.DeployConfig, host_dir: str) -> list[deploy.Step]:
    """The deploy plan with `host_dir` as the backup disk, minus the rsync step: just bind + recreate."""
    return [
        s
        for s in deploy.plan(dataclasses.replace(cfg, backup_dir=host_dir))
        if not s.title.startswith("sync")
    ]


def stage_after_install(
    *,
    host: str,
    label: str,
    cfg: deploy.DeployConfig,
    container: str,
    repo_root: Path,
    opts: env_stage.StageOptions,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    interactive: bool,
    run_steps: RunSteps,
    mint: Callable[[str], str],
    env: dict[str, str],
    resolve: Callable[[str], str] = ssh_hostname,
    shells: Callable[[str, float | None], Shell] | None = None,
) -> int:
    """Run the backup + Home Assistant stage for the stack `cfg` describes. `mint(name)` creates a 3d-api token.
    `shells(host, timeout)` builds the shell on the Spoolman host (default: ssh, or bash for host "")."""
    addr = address_for_ha(cfg, resolve)
    shell = shells or _real_shell
    ctx = BackupContext(
        run=shell(host, None),
        where=label,
        probe=shell(host, PROBE_TIMEOUT_S),
        data_dir=f"{cfg.remote_dir}/data",
        container=container,
        redeploy=lambda host_dir: run_steps(compose_only(cfg, host_dir)),
        ask=ask,
        say=say,
        interactive=interactive,
        hint_gb=opts.hint_gb,
    )
    stage = env_stage.StageEnv(
        ctx=ctx,
        repo_root=repo_root,
        env=env,
        api_url_for_ha=_url(addr, cfg.api_port),
        spoolman_url_for_ha=_url(addr, cfg.spoolman_port),
        mint_ha_token=lambda: mint("ha"),
        host_names=[addr],
    )
    return env_stage.run_stage(stage, opts)
