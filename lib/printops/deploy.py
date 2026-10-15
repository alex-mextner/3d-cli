"""deploy.py — ship services/3d-api (Spoolman + 3d-api) to the Odroid and wait until it is healthy.

ACCESSED VIA: `3d printops deploy [--dry-run]`.

WHAT IT DOES (each step is a plain ssh/rsync argv, printed before it runs):
  1. ssh: create /home/ultra/printops/{src,data/api,data/spoolman} and the backup dir inside the
     HA config dir (with a `*` .gitignore so the HA git checkout stays clean). The data dirs
     MUST exist, owned by `ultra`, before compose starts — otherwise docker creates them as root
     and the uid-1000 containers cannot write.
  2. rsync bin/3d, lib/, fleet/, pyproject.toml and services/3d-api/ to <dir>/src (the docker
     build context; only these paths, `--delete` inside them).
  3. ssh: `docker compose -p printops -f services/3d-api/compose.yaml up -d --build` with
     PRINTOPS_VERSION=<git sha>. Separate compose project — the HA compose project is never
     touched, and nothing here restarts Home Assistant.
  4. ssh: install + enable the `printops-backup.timer` systemd --user unit.
  5. poll http://<host>:8765/v1/health until status "ok" (DB migrated, Spoolman reachable,
     every extra field registered by 3d-api's startup task).

`--dry-run` runs step 2 as `rsync --dry-run --itemize-changes` (shows what would change, writes
nothing) and only prints the other steps.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from printops.client import ApiClient, ApiError

DEFAULT_HOST = "ultra@192.168.0.39"
DEFAULT_DIR = "/home/ultra/printops"
DEFAULT_BACKUP_DIR = "/home/ultra/homeassistant/printops-backup"
API_PORT = 8765
COMPOSE_FILE = "services/3d-api/compose.yaml"
PROJECT = "printops"
SYNC_PATHS = ("bin/3d", "lib", "fleet", "pyproject.toml", "services/3d-api")
TIMER_UNITS = ("printops-backup.service", "printops-backup.timer")


@dataclass(frozen=True)
class DeployConfig:
    repo_root: Path
    host: str = DEFAULT_HOST
    remote_dir: str = DEFAULT_DIR
    backup_dir: str = DEFAULT_BACKUP_DIR
    api_url: str | None = None
    version: str = "dev"

    @property
    def health_url(self) -> str:
        return self.api_url or f"http://{self.host.rsplit('@', 1)[-1]}:{API_PORT}"


@dataclass(frozen=True)
class Step:
    title: str
    argv: list[str]
    remote: bool = False  # argv is `ssh … HOST SCRIPT`; shown as the remote script
    read_only: bool = False  # safe to execute during --dry-run (writes nothing anywhere)


def git_version(repo_root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True,
                              check=False).stdout.strip()

    sha = git("rev-parse", "--short=12", "HEAD") or "unknown"
    return sha + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def _ssh(host: str, script: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", host, script]


def plan(cfg: DeployConfig, *, dry_run: bool = False) -> list[Step]:
    d, b = cfg.remote_dir, cfg.backup_dir
    q = shlex.quote
    sync = [p for p in SYNC_PATHS if (cfg.repo_root / p).exists()]
    mkdirs = (
        f"mkdir -p {q(d + '/src')} {q(d + '/data/api')} {q(d + '/data/spoolman')} {q(b)}"
        f" && printf '*\\n' > {q(b + '/.gitignore')}"
    )
    rsync = ["rsync", "-az", "--relative", "--delete", "--exclude", "__pycache__", "--exclude", "*.pyc"]
    if dry_run:
        rsync += ["--dry-run", "--itemize-changes"]
    rsync += [*sync, f"{cfg.host}:{d}/src/"]
    compose = (
        f"cd {q(d + '/src')} && PRINTOPS_VERSION={q(cfg.version)} PRINTOPS_DATA_DIR={q(d + '/data')}"
        f" PRINTOPS_BACKUP_DIR={q(b)} docker compose -p {PROJECT} -f {COMPOSE_FILE} up -d --build --remove-orphans"
    )
    units = " ".join(q(f"{d}/src/services/3d-api/systemd/{u}") for u in TIMER_UNITS)
    timer = (
        f"mkdir -p ~/.config/systemd/user && cp {units} ~/.config/systemd/user/"
        " && systemctl --user daemon-reload && systemctl --user enable --now printops-backup.timer"
    )
    return [
        Step("create data + backup dirs", _ssh(cfg.host, mkdirs), remote=True),
        Step("sync build context" + (" (dry run: changes only)" if dry_run else ""), rsync, read_only=dry_run),
        Step("build + start spoolman and 3d-api (compose project printops)", _ssh(cfg.host, compose), remote=True),
        Step("install daily backup timer", _ssh(cfg.host, timer), remote=True),
    ]


def wait_healthy(url: str, *, timeout_s: float = 300.0, interval_s: float = 3.0,
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Poll /v1/health until status "ok"; returns the last body or raises ApiError on timeout."""
    api = ApiClient(url)
    deadline = clock() + timeout_s
    last = ""
    while True:
        try:
            h = api.health()
            if h.get("status") == "ok":
                return h
            sp = h.get("spoolman") or {}
            msg = f"degraded: spoolman {sp.get('error') or (sp.get('extra_fields') or {}).get('status')}"
        except ApiError as e:
            msg = e.message
        if msg != last:
            log(f"  waiting: {msg}")
            last = msg
        if clock() >= deadline:
            raise ApiError(f"3d-api at {url} not healthy after {timeout_s:.0f}s: {msg}")
        sleep(interval_s)


def run_steps(steps: Sequence[Step], *, cwd: Path, dry_run: bool, log: Callable[[str], None] = print) -> None:
    """Print every step; execute it unless dry_run (read-only steps execute even then)."""
    for i, step in enumerate(steps, 1):
        shown = f"{step.argv[-1]}  (on {step.argv[-2]})" if step.remote else shlex.join(step.argv)
        log(f"[{i}/{len(steps)}] {step.title}\n  $ {shown}")
        if dry_run and not step.read_only:
            continue
        r = subprocess.run(step.argv, cwd=str(cwd), check=False)
        if r.returncode != 0:
            raise RuntimeError(f"step {i} ({step.title}) failed with exit {r.returncode}")
