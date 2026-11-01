"""deploy.py — ship services/3d-api (Spoolman + 3d-api) to the Odroid and wait until it is healthy.

ACCESSED VIA: `3d printops deploy [--dry-run] [--local]` and `3d init --env` (printops.env_setup:
a printer's Linux host, this machine, or any ssh host — the same plan, a different `host`).

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

TARGETS: `DeployConfig.host` is `USER@HOST` (ssh + rsync) or "" for THIS machine: the same steps run
through `bash -c`, the build context is the checkout itself (no rsync, no `<dir>/src` copy) and the
timer units come from it. One path for every target — never a second deploy implementation.

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
SPOOLMAN_PORT = 7912
COMPOSE_FILE = "services/3d-api/compose.yaml"
PROJECT = "printops"
SYNC_PATHS = ("bin/3d", "lib", "fleet", "pyproject.toml", "services/3d-api")
TIMER_UNITS = ("printops-backup.service", "printops-backup.timer")


@dataclass(frozen=True)
class DeployConfig:
    repo_root: Path
    host: str = DEFAULT_HOST  # USER@HOST for ssh, "" for this machine
    remote_dir: str = DEFAULT_DIR
    backup_dir: str = DEFAULT_BACKUP_DIR
    api_url: str | None = None
    version: str = "dev"
    timer_required: bool = True  # False: a host without `systemctl --user` still gets the stack
    api_port: int = API_PORT
    spoolman_port: int = SPOOLMAN_PORT

    @property
    def local(self) -> bool:
        return not self.host

    @property
    def health_url(self) -> str:
        host = "127.0.0.1" if self.local else self.host.rsplit("@", 1)[-1]
        return self.api_url or f"http://{host}:{self.api_port}"


@dataclass(frozen=True)
class Step:
    title: str
    argv: list[str]
    script: bool = False  # argv ends with a shell script (`ssh … HOST SCRIPT` / `bash -c SCRIPT`); shown as that script
    where: str = ""  # script steps: the ssh host, or "this machine"
    read_only: bool = False  # safe to execute during --dry-run (writes nothing anywhere)


def git_version(repo_root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True,
                              check=False).stdout.strip()

    sha = git("rev-parse", "--short=12", "HEAD") or "unknown"
    return sha + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def _script_step(cfg: DeployConfig, title: str, script: str) -> Step:
    if cfg.local:
        return Step(title, ["bash", "-c", script], script=True, where="this machine")
    return Step(title, ["ssh", "-o", "BatchMode=yes", cfg.host, script], script=True, where=cfg.host)


def _mkdirs_script(cfg: DeployConfig) -> str:
    q = shlex.quote
    d, b = cfg.remote_dir, cfg.backup_dir
    dirs = [*([] if cfg.local else [d + "/src"]), d + "/data/api", d + "/data/spoolman", d + "/gcodes", b]
    return f"mkdir -p {' '.join(q(x) for x in dirs)} && printf '*\\n' > {q(b + '/.gitignore')}"


def _compose_script(cfg: DeployConfig, src: str) -> str:
    q = shlex.quote
    d, b = cfg.remote_dir, cfg.backup_dir
    ports = "".join(
        f" {name}={port}" for name, port, default in
        (("PRINTOPS_API_PORT", cfg.api_port, API_PORT), ("PRINTOPS_SPOOLMAN_PORT", cfg.spoolman_port, SPOOLMAN_PORT))
        if port != default
    )
    return (
        f"cd {q(src)} && PRINTOPS_VERSION={q(cfg.version)} PRINTOPS_DATA_DIR={q(d + '/data')}"
        f" PRINTOPS_BACKUP_DIR={q(b)} PRINTOPS_GCODE_HOST_DIR={q(d + '/gcodes')}"
        f' PRINTOPS_UID="$(id -u)" PRINTOPS_GID="$(id -g)"{ports}'
        f" docker compose -p {PROJECT} -f {COMPOSE_FILE} up -d --build --remove-orphans"
    )


def _timer_script(cfg: DeployConfig, src: str) -> str:
    units = " ".join(shlex.quote(f"{src}/services/3d-api/systemd/{u}") for u in TIMER_UNITS)
    timer = (
        f"mkdir -p ~/.config/systemd/user && cp {units} ~/.config/systemd/user/"
        " && systemctl --user daemon-reload && systemctl --user enable --now printops-backup.timer"
    )
    if cfg.timer_required:
        return timer
    return f"( {timer} ) || echo 'note: no systemd --user here; the daily backup timer was not installed'"


def _sync_step(cfg: DeployConfig, dry_run: bool) -> Step:
    sync = [p for p in SYNC_PATHS if (cfg.repo_root / p).exists()]
    rsync = ["rsync", "-az", "--relative", "--delete", "--exclude", "__pycache__", "--exclude", "*.pyc"]
    if dry_run:
        rsync += ["--dry-run", "--itemize-changes"]
    rsync += [*sync, f"{cfg.host}:{cfg.remote_dir}/src/"]
    return Step("sync build context" + (" (dry run: changes only)" if dry_run else ""), rsync, read_only=dry_run)


def plan(cfg: DeployConfig, *, dry_run: bool = False) -> list[Step]:
    src = str(cfg.repo_root) if cfg.local else cfg.remote_dir + "/src"  # the docker build context
    steps = [_script_step(cfg, "create data + backup dirs", _mkdirs_script(cfg))]
    if not cfg.local:
        steps.append(_sync_step(cfg, dry_run))
    steps += [
        _script_step(cfg, "build + start spoolman and 3d-api (compose project printops)", _compose_script(cfg, src)),
        _script_step(cfg, "install daily backup timer", _timer_script(cfg, src)),
    ]
    return steps


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


def run_steps(steps: Sequence[Step], *, cwd: Path, dry_run: bool, log: Callable[[str], None] = print,
              runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> None:
    """Print every step; execute it unless dry_run (read-only steps execute even then).
    `runner` is subprocess.run; tests inject a fake so no ssh/docker ever starts."""
    for i, step in enumerate(steps, 1):
        shown = f"{step.argv[-1]}  (on {step.where})" if step.script else shlex.join(step.argv)
        log(f"[{i}/{len(steps)}] {step.title}\n  $ {shown}")
        if dry_run and not step.read_only:
            continue
        r = runner(step.argv, cwd=str(cwd), check=False)
        if r.returncode != 0:
            raise RuntimeError(f"step {i} ({step.title}) failed with exit {r.returncode}")
