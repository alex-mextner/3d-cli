"""`3d printops deploy` — ship Spoolman + 3d-api to the Odroid, any ssh host, or this machine
(see printops/deploy.py for the steps; `3d init --env` is the guided front end)."""

from __future__ import annotations

import os
from pathlib import Path

from cli import paths
from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError

from printops import deploy
from printops.cli import Sub
from printops.cli._common import Parsed, parse
from printops.client import ApiError

USAGE = f"""3d printops deploy [--dry-run] [--host USER@HOST | --local] [--dir DIR] [--backup-dir DIR] [--api URL]
                       [--bind ADDR]
  Deploy services/3d-api (Spoolman v0.27.0 + 3d-api) as the separate compose project
  `printops` on the Odroid, then wait until /v1/health is "ok"
  (DB migrated, Spoolman reachable, all PrintOps extra fields registered). Never touches the
  Home Assistant compose project and never restarts HA.

  Steps: mkdir data dirs → rsync bin/3d lib fleet pyproject.toml services/3d-api to DIR/src →
  `docker compose -p printops up -d --build` → wait healthy. No backup timer is installed: Home
  Assistant schedules the backup (integrations/homeassistant/packages/printops_backup.yaml).

Options:
  --dry-run          show what rsync would change and the remote commands; change nothing
  --host USER@HOST   ssh target (default {deploy.DEFAULT_HOST}); an empty value or one starting with "-" is an error,
                     never "this machine" (that is --local)
  --local            deploy on THIS machine instead (docker compose from this checkout; no ssh/rsync).
                     --dir defaults to <data dir>/printops (~/.local/share/3d-cli/printops) and
                     --backup-dir to DIR/backup
  --dir DIR          base dir on the target (default {deploy.DEFAULT_DIR}; data in DIR/data)
  --backup-dir DIR   host dir bound as /backup in 3d-api: the BACKUP DISK (default {deploy.DEFAULT_BACKUP_DIR});
                     register it afterwards: `3d printops backup target add` (see `3d printops backup --help`).
                     The disk is remembered (<--dir>/backup-dir): a later deploy without --backup-dir keeps it
  --api URL          health URL to wait on (default http://<host>:{deploy.API_PORT}, 127.0.0.1 with --local)
  --bind ADDR        host address Spoolman (:{deploy.SPOOLMAN_PORT}) and 3d-api (:{deploy.API_PORT}) listen on: an IP address.
                     Default {deploy.ALL_INTERFACES} (all interfaces; the Odroid is behind NAT), {deploy.LOOPBACK} with --local.
                     Spoolman has no authentication: with --local, other machines and printers reach it only
                     if you pass --bind {deploy.ALL_INTERFACES} (or one LAN address)

Examples:
  3d printops deploy --dry-run
  3d printops deploy
  3d printops deploy --local --dry-run
  3d printops deploy --host ultra@odroidn2 --api http://100.116.57.66:8765
  3d printops deploy --local --bind 192.168.0.20     # reachable from the LAN on that address only"""


def _absolute(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _bind(p: Parsed, default: str | None) -> str | None:
    raw = p.opts.get("--bind")
    if raw is None:
        return default
    try:
        return deploy.normalize_bind(raw)
    except ValueError as e:
        raise InvalidArgument(
            "--bind",
            raw,
            ["an IP address such as 127.0.0.1, 192.168.0.20, 0.0.0.0 or ::1"],
            command="printops deploy",
        ) from e


def _remote_config(p: Parsed, root: Path) -> deploy.DeployConfig:
    host = p.opts.get("--host", deploy.DEFAULT_HOST)
    problem = deploy.ssh_destination_problem(host)
    if problem:
        raise UsageError(
            f"--host {host!r} {problem}",
            command="printops deploy",
            remediation=[
                'Pass USER@HOST (an empty `--host "$VAR"` does not mean this machine: use --local for that).'
            ],
        )
    return deploy.DeployConfig(
        repo_root=root,
        host=host,
        remote_dir=p.opts.get("--dir", deploy.DEFAULT_DIR),
        backup_dir=p.opts.get("--backup-dir", deploy.DEFAULT_BACKUP_DIR),
        backup_explicit="--backup-dir" in p.opts,
        api_url=p.opts.get("--api"),
        version=deploy.git_version(root),
        bind_addr=_bind(p, None),
    )


def _local_config(p: Parsed, root: Path) -> deploy.DeployConfig:
    if "--host" in p.opts:
        raise UsageError(
            "--local and --host are exclusive",
            command="printops deploy",
            remediation=[
                "Use --local for this machine, --host USER@HOST for a remote one."
            ],
        )
    # compose resolves bind sources against services/3d-api/, not the cwd: hand it absolute paths
    base = _absolute(p.opts.get("--dir") or str(paths.data_dir() / "printops"))
    return deploy.DeployConfig(
        repo_root=root,
        host="",
        local=True,
        remote_dir=base,
        backup_dir=_absolute(p.opts.get("--backup-dir") or f"{base}/backup"),
        backup_explicit="--backup-dir" in p.opts,
        api_url=p.opts.get("--api"),
        version=deploy.git_version(root),
        bind_addr=_bind(p, deploy.LOOPBACK),
        pin_host_env=True,
    )


def _config(p: Parsed, root: Path) -> deploy.DeployConfig:
    return _local_config(p, root) if "--local" in p.flags else _remote_config(p, root)


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(
        argv,
        "deploy",
        values={"--host", "--dir", "--backup-dir", "--api", "--bind"},
        flags={"--dry-run", "--local"},
    )
    root = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[3])
    cfg = _config(p, root)
    dry = "--dry-run" in p.flags
    steps = deploy.plan(cfg, dry_run=dry)
    target = (
        f"{cfg.host}:{cfg.remote_dir}" if cfg.host else f"this machine:{cfg.remote_dir}"
    )
    print(f"deploy printops {cfg.version} -> {target}" + ("  [dry run]" if dry else ""))
    try:
        deploy.run_steps(steps, cwd=root, dry_run=dry)
        if dry:
            print(f"then: wait for {cfg.health_url}/v1/health status ok")
            return 0
        print(f"waiting for {cfg.health_url}/v1/health …")
        h = deploy.wait_healthy(cfg.health_url)
    except deploy.ToolMissing as e:
        raise MissingDependency(
            e.tool,
            install=deploy.TOOL_INSTALL.get(e.tool, f"install {e.tool}"),
            degrades="`3d printops deploy` cannot run its steps",
            command="printops deploy",
        ) from e
    except (RuntimeError, ApiError) as e:
        raise ThreeDError(
            str(e),
            command="printops deploy",
            remediation=[
                f"Inspect: {'ssh ' + cfg.host + ' ' if cfg.host else ''}docker logs --tail 80 "
                f"{deploy.API_CONTAINER} (and printops-spoolman)"
            ],
        ) from e
    sp = h.get("spoolman") or {}
    print(
        f"healthy: 3d-api {h.get('version')}  spoolman v{sp.get('version')}  "
        f"extra fields {((sp.get('extra_fields') or {}).get('status'))}"
    )
    return 0


SUB = Sub(
    name="deploy",
    summary="deploy Spoolman + 3d-api to the Odroid, an ssh host or --local (compose project printops)",
    usage=USAGE,
    run=run,
)
