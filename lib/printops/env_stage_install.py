"""env_stage_install.py — the glue between `3d init --env` (printops.env_setup) and the backup/HA stage
(printops.env_stage): turns "a stack was just deployed to HOST" into the `StageEnv` the stage needs.

ACCESSED VIA: `printops.env_setup` (after a successful install, and for `--stage-only` on an existing one).

WHY a module of its own: env_setup knows the deploy facts (host, directories, ports, how to run a plan and
mint a token); env_stage knows backups and Home Assistant. This file is the only place that joins them, so
neither imports the other.
"""

from __future__ import annotations

import shlex
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


def _bind_disk_script(src: str, host_dir: str) -> str:
    """Recreate ONLY the 3d-api container with `host_dir` as /backup, every other value read from the running stack
    (`docker inspect`/`docker port`): a stage never rebuilds the deploy from defaults, so Spoolman is not touched and
    a stack installed with another directory, ports, bind address, time zone or CORS list keeps them. Refuses when
    the stack is not there. The chosen disk is saved next to the data dir so a later plain deploy keeps it."""
    q = shlex.quote
    api, spoolman = (
        f"{deploy.CONTAINER_PREFIX}-api",
        f"{deploy.CONTAINER_PREFIX}-spoolman",
    )
    env_of = "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}'"
    return (
        "set -e; "
        f"api={api}; sp={spoolman}; "
        'for c in "$api" "$sp"; do docker inspect "$c" >/dev/null 2>&1 || '
        '{ echo "container $c is not running here: install Spoolman first (3d init --env --spoolman ...)" >&2; exit 1; }; done; '
        "src_of() { docker inspect -f '{{range .Mounts}}{{if eq .Destination \"'\"$2\"'\"}}{{.Source}}{{end}}{{end}}' \"$1\"; }; "
        'spdata="$(src_of "$api" /spoolman-data)"; gcodes="$(src_of "$api" /gcodes)"; '
        '[ -n "$spdata" ] && [ -n "$gcodes" ] || { echo "cannot read the data directories of $api" >&2; exit 1; }; '
        'data="$(dirname "$spdata")"; '
        'user="$(docker inspect -f \'{{.Config.User}}\' "$api")"; '
        f'tz="$({env_of} "$api" | sed -n "s/^TZ=//p")"; '
        f'cors="$({env_of} "$sp" | sed -n "s/^SPOOLMAN_CORS_ORIGIN=//p")"; '
        'apiaddr="$(docker port "$api" 8765/tcp | head -n 1)"; spaddr="$(docker port "$sp" 8000/tcp | head -n 1)"; '
        '[ -n "$apiaddr" ] && [ -n "$spaddr" ] || { echo "cannot read the published ports of the stack" >&2; exit 1; }; '
        f'printf %s {q(host_dir)} > "$(dirname "$data")/{deploy.BACKUP_BIND_FILE}"; '
        f'cd {q(src)} && PRINTOPS_DATA_DIR="$data" PRINTOPS_GCODE_HOST_DIR="$gcodes" PRINTOPS_BACKUP_DIR={q(host_dir)}'
        ' PRINTOPS_UID="${user%%:*}" PRINTOPS_GID="${user##*:}" PRINTOPS_TZ="$tz" PRINTOPS_SPOOLMAN_CORS_ORIGIN="$cors"'
        ' PRINTOPS_BIND_ADDR="${apiaddr%:*}" PRINTOPS_API_PORT="${apiaddr##*:}" PRINTOPS_SPOOLMAN_PORT="${spaddr##*:}"'
        f" PRINTOPS_CONTAINER_PREFIX={deploy.CONTAINER_PREFIX}"
        f" docker compose -p {deploy.PROJECT} -f {deploy.COMPOSE_FILE} up -d --no-deps api"
    )


def bind_disk_steps(cfg: deploy.DeployConfig, host_dir: str) -> list[deploy.Step]:
    """The one step that binds `host_dir` into the running 3d-api container (no mkdirs, no sync, no build)."""
    src = str(cfg.repo_root) if cfg.local else f"{cfg.remote_dir}/src"
    return [
        deploy.script_step(
            cfg,
            "recreate only 3d-api with the backup disk (values read from the running stack)",
            _bind_disk_script(src, host_dir),
        )
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
        redeploy=lambda host_dir: run_steps(bind_disk_steps(cfg, host_dir)),
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
