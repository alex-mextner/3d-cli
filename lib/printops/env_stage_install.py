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
from collections.abc import Callable
from pathlib import Path

from printops import backup_setup, deploy, env_stage
from printops.backup_setup import BackupContext

RunSteps = Callable[[list[deploy.Step]], None]


def address_for_ha(host: str) -> str:
    """The address Home Assistant should use for the Spoolman host: its ssh host, or this machine's LAN IP."""
    if host:
        return host.rsplit("@", 1)[-1]
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
) -> int:
    """Run the backup + Home Assistant stage for the stack `cfg` describes. `mint(name)` creates a 3d-api token."""
    addr = address_for_ha(host)
    ctx = BackupContext(
        run=backup_setup.ssh_shell(host) if host else backup_setup.local_shell(),
        where=label,
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
        api_url_for_ha=f"http://{addr}:{cfg.api_port}",
        spoolman_url_for_ha=f"http://{addr}:{cfg.spoolman_port}",
        mint_ha_token=lambda: mint("ha"),
        host_names=[addr],
    )
    return env_stage.run_stage(stage, opts)
