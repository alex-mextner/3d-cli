"""`3d printops deploy` — ship Spoolman + 3d-api to the Odroid (see printops/deploy.py for the steps)."""
from __future__ import annotations

import os
from pathlib import Path

from errors import ThreeDError
from printops import deploy
from printops.cli import Sub
from printops.cli._common import parse
from printops.client import ApiError

USAGE = f"""3d printops deploy [--dry-run] [--host USER@HOST] [--dir DIR] [--backup-dir DIR] [--api URL]
  Deploy services/3d-api (Spoolman v0.27.0 + 3d-api) as the separate compose project
  `printops` on the Odroid, install the daily backup timer, then wait until /v1/health is "ok"
  (DB migrated, Spoolman reachable, all PrintOps extra fields registered). Never touches the
  Home Assistant compose project and never restarts HA.

  Steps: mkdir data dirs → rsync bin/3d lib fleet pyproject.toml services/3d-api to DIR/src →
  `docker compose -p printops up -d --build` → enable printops-backup.timer → wait healthy.

Options:
  --dry-run          show what rsync would change and the remote commands; change nothing
  --host USER@HOST   ssh target (default {deploy.DEFAULT_HOST})
  --dir DIR          remote base dir (default {deploy.DEFAULT_DIR}; data in DIR/data)
  --backup-dir DIR   snapshot dir inside HA's backed-up config dir (default {deploy.DEFAULT_BACKUP_DIR})
  --api URL          health URL to wait on (default http://<host>:{deploy.API_PORT})

Examples:
  3d printops deploy --dry-run
  3d printops deploy
  3d printops deploy --host ultra@odroidn2 --api http://100.116.57.66:8765"""


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    p = parse(argv, "deploy", values={"--host", "--dir", "--backup-dir", "--api"}, flags={"--dry-run"})
    root = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[3])
    cfg = deploy.DeployConfig(
        repo_root=root,
        host=p.opts.get("--host", deploy.DEFAULT_HOST),
        remote_dir=p.opts.get("--dir", deploy.DEFAULT_DIR),
        backup_dir=p.opts.get("--backup-dir", deploy.DEFAULT_BACKUP_DIR),
        api_url=p.opts.get("--api"),
        version=deploy.git_version(root),
    )
    dry = "--dry-run" in p.flags
    steps = deploy.plan(cfg, dry_run=dry)
    print(f"deploy printops {cfg.version} -> {cfg.host}:{cfg.remote_dir}" + ("  [dry run]" if dry else ""))
    try:
        deploy.run_steps(steps, cwd=root, dry_run=dry)
        if dry:
            print(f"then: wait for {cfg.health_url}/v1/health status ok")
            return 0
        print(f"waiting for {cfg.health_url}/v1/health …")
        h = deploy.wait_healthy(cfg.health_url)
    except (RuntimeError, ApiError) as e:
        raise ThreeDError(str(e), command="printops deploy",
                          remediation=[f"Inspect: ssh {cfg.host} docker logs --tail 80 printops-api "
                                       f"(and printops-spoolman)"]) from e
    sp = h.get("spoolman") or {}
    print(f"healthy: 3d-api {h.get('version')}  spoolman v{sp.get('version')}  "
          f"extra fields {((sp.get('extra_fields') or {}).get('status'))}")
    return 0


SUB = Sub(name="deploy", summary="deploy Spoolman + 3d-api to the Odroid (compose project printops)",
          usage=USAGE, run=run)
