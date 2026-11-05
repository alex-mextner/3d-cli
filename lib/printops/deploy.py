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
  4. ssh: retire the legacy `printops-backup.timer` systemd --user units an older deploy installed (idempotent;
     nothing to do on a host that never had them).
  5. poll http://<host>:8765/v1/health until status "ok" (DB migrated, Spoolman reachable,
     every extra field registered by 3d-api's startup task).

NO BACKUP TIMER: the legacy timer ran `3d printops backup` with no subcommand, which now exits 1 without
backing anything up, so a redeploy removes it instead of leaving a schedule that fails every night. The daily backup is scheduled by Home Assistant (04:00, integrations/homeassistant/packages/
printops_backup.yaml -> POST /v1/backup/run); the backup disk is `backup_dir`, registered with
`3d printops backup target add`.

TARGETS: `DeployConfig.host` is `USER@HOST` (ssh + rsync) or "" for THIS machine: the same steps run
through `bash -c`, the build context is the checkout itself (no rsync, no `<dir>/src` copy).
One path for every target — never a second deploy implementation.

`--dry-run` runs step 2 as `rsync --dry-run --itemize-changes` (shows what would change, writes
nothing) and only prints the other steps.
"""

from __future__ import annotations

import ipaddress
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from printops.client import ApiClient, ApiError

DEFAULT_HOST = "ultra@192.168.0.39"
DEFAULT_DIR = "/home/ultra/printops"
DEFAULT_BACKUP_DIR = "/home/ultra/homeassistant/printops-backup"
API_PORT = 8765
SPOOLMAN_PORT = 7912
COMPOSE_FILE = "services/3d-api/compose.yaml"
PROJECT = "printops"
LEGACY_BACKUP_UNITS = ("printops-backup.timer", "printops-backup.service")
SYNC_PATHS = ("bin/3d", "lib", "fleet", "pyproject.toml", "services/3d-api")
CONTAINER_PREFIX = (
    "printops"  # compose's default PRINTOPS_CONTAINER_PREFIX, and the unit's container
)
API_CONTAINER = f"{CONTAINER_PREFIX}-api"
# the host's own time zone, as the standalone script finds it; PRINTOPS_TZ="${tz:-UTC}" follows
TZ_PRELUDE = (
    'tz="$(timedatectl show -p Timezone --value 2>/dev/null)"; '
    '[ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null)"; '
)
ALL_INTERFACES = (
    "0.0.0.0"  # compose's default for PRINTOPS_BIND_ADDR: the Odroid is behind NAT
)
LOOPBACK = "127.0.0.1"


def normalize_bind(raw: str) -> str:
    """`raw` as compose's PRINTOPS_BIND_ADDR wants it (an IPv6 literal in brackets); ValueError
    when it is not an IP address — a hostname would let docker publish on whatever it resolves to."""
    addr = ipaddress.ip_address(raw.strip().removeprefix("[").removesuffix("]"))
    return f"[{addr}]" if addr.version == 6 else str(addr)


def ssh_destination_problem(dest: str) -> str | None:
    """Why `dest` cannot be handed to ssh/rsync as the host argument, None when it can. A leading
    "-" would be read as an option (`-oProxyCommand=…` runs a local command), whitespace splits it."""
    if not dest:
        return "is empty"
    if dest.startswith("-"):
        return "starts with '-', which ssh would read as an option"
    if any(ch.isspace() for ch in dest):
        return "contains whitespace"
    return None


@dataclass(frozen=True)
class DeployConfig:
    repo_root: Path
    host: str = DEFAULT_HOST  # USER@HOST for ssh; "" with local=True
    remote_dir: str = DEFAULT_DIR
    backup_dir: str = DEFAULT_BACKUP_DIR
    api_url: str | None = None
    version: str = "dev"
    api_port: int = API_PORT
    spoolman_port: int = SPOOLMAN_PORT
    bind_addr: str | None = (
        None  # normalize_bind form; None leaves compose's default (ALL_INTERFACES)
    )
    # A host that is not the Odroid: pin the container prefix, time zone and Spoolman's browser origin
    # to this deploy's own values. Left to compose they would be the Odroid's (Europe/Belgrade, the
    # owner's LAN allowlist) or whatever the caller's own PRINTOPS_* environment says.
    pin_host_env: bool = False
    # THIS machine, said explicitly: an empty host (an unset `--host "$VAR"`) is never "here".
    local: bool = False
    # `backup_dir` was named by the user (the stage's chosen disk, or --backup-dir), not defaulted: it is
    # saved host-side so a later plain deploy keeps binding it instead of going back to the default.
    backup_explicit: bool = False

    def __post_init__(self) -> None:
        if self.local and self.host:
            raise ValueError(f"a local deploy has no ssh host (got {self.host!r})")
        problem = None if self.local else ssh_destination_problem(self.host)
        if problem:
            raise ValueError(f"deploy host {self.host!r} {problem}")

    @property
    def health_url(self) -> str:
        host = self.host.rsplit("@", 1)[-1]
        if (
            self.local
        ):  # a wildcard bind is reached through loopback, a specific one through itself
            bind = self.bind_addr or ALL_INTERFACES
            host = LOOPBACK if bind in (ALL_INTERFACES, "[::]") else bind
        return self.api_url or f"http://{host}:{self.api_port}"

    @property
    def spoolman_origins(self) -> str:
        """Spoolman's own address as a browser origin (comma-separated): all a pinned deploy allows."""
        host = urlsplit(self.health_url).hostname or LOOPBACK
        origins = [
            f"http://{f'[{host}]' if ':' in host else host}:{self.spoolman_port}"
        ]
        if host == LOOPBACK:
            origins.append(f"http://localhost:{self.spoolman_port}")
        return ",".join(origins)


@dataclass(frozen=True)
class Step:
    title: str
    argv: list[str]
    script: bool = False  # argv ends with a shell script (`ssh … HOST SCRIPT` / `bash -c SCRIPT`); shown as that script
    where: str = ""  # script steps: the ssh host, or "this machine"
    read_only: bool = (
        False  # safe to execute during --dry-run (writes nothing anywhere)
    )


def git_version(repo_root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

    sha = git("rev-parse", "--short=12", "HEAD") or "unknown"
    return sha + (
        "-dirty" if git("status", "--porcelain", "--untracked-files=no") else ""
    )


def script_step(cfg: DeployConfig, title: str, script: str) -> Step:
    if cfg.local:
        return Step(title, ["bash", "-c", script], script=True, where="this machine")
    return Step(
        title,
        ["ssh", "-o", "BatchMode=yes", cfg.host, script],
        script=True,
        where=cfg.host,
    )


BACKUP_BIND_FILE = "backup-dir"  # <remote_dir>/backup-dir: the host directory bound as /backup, remembered


def _mkdirs_script(cfg: DeployConfig) -> str:
    q = shlex.quote
    d, b = cfg.remote_dir, cfg.backup_dir
    dirs = [
        *([] if cfg.local else [d + "/src"]),
        d + "/data/api",
        d + "/data/spoolman",
        d + "/gcodes",
        b,
    ]
    # an existing .gitignore (a directory the user picked) is never overwritten
    gitignore = q(b + "/.gitignore")
    return f"mkdir -p {' '.join(q(x) for x in dirs)} && {{ [ -e {gitignore} ] || printf '*\\n' > {gitignore}; }}"


def _backup_bind_prelude(cfg: DeployConfig) -> str:
    """Shell that sets `bd`, the directory compose binds as /backup: the one the user named (and saves it), else
    the one saved by an earlier deploy or stage, else this deploy's default."""
    q = shlex.quote
    saved = q(f"{cfg.remote_dir}/{BACKUP_BIND_FILE}")
    if cfg.backup_explicit:
        return f'bd={q(cfg.backup_dir)}; printf %s "$bd" > {saved}; '
    return f'bd="$(cat {saved} 2>/dev/null)"; [ -n "$bd" ] || bd={q(cfg.backup_dir)}; '


def _pinned_env(cfg: DeployConfig) -> str:
    """Compose's host-specific knobs as `VAR=value` words for a pinned (non-Odroid) deploy."""
    if not cfg.pin_host_env:
        return ""
    return (
        f' PRINTOPS_CONTAINER_PREFIX={CONTAINER_PREFIX} PRINTOPS_TZ="${{tz:-UTC}}"'
        f" PRINTOPS_SPOOLMAN_CORS_ORIGIN={shlex.quote(cfg.spoolman_origins)}"
    )


def _compose_script(cfg: DeployConfig, src: str) -> str:
    q = shlex.quote
    d = cfg.remote_dir
    # Always explicit: a default left to compose would be overridden by the caller's own
    # PRINTOPS_*_PORT environment while cfg.health_url still polls cfg.api_port.
    ports = (
        f" PRINTOPS_API_PORT={cfg.api_port} PRINTOPS_SPOOLMAN_PORT={cfg.spoolman_port}"
    )
    if (
        cfg.bind_addr is not None
    ):  # an unset bind keeps the Odroid's script exactly as it was
        ports += f" PRINTOPS_BIND_ADDR={q(cfg.bind_addr)}"
    tz = TZ_PRELUDE if cfg.pin_host_env else ""
    return (
        f"{tz}{_backup_bind_prelude(cfg)}cd {q(src)} && PRINTOPS_VERSION={q(cfg.version)} PRINTOPS_DATA_DIR={q(d + '/data')}"
        f' PRINTOPS_BACKUP_DIR="$bd" PRINTOPS_GCODE_HOST_DIR={q(d + "/gcodes")}'
        f' PRINTOPS_UID="$(id -u)" PRINTOPS_GID="$(id -g)"{ports}{_pinned_env(cfg)}'
        f" docker compose -p {PROJECT} -f {COMPOSE_FILE} up -d --build --remove-orphans"
    )


def _rsync_host(host: str) -> str:
    """`USER@HOST` as rsync wants it before the `:path`: an IPv6 literal in brackets (rsync splits
    `me@2001:db8::7:/dir` at the first colon and syncs to the host `2001`). ssh takes it bare."""
    user, at, name = host.rpartition("@")
    return f"{user}{at}[{name}]" if ":" in name and not name.startswith("[") else host


def _retire_legacy_timer_script() -> str:
    """Remove the `printops-backup.{timer,service}` units an older deploy installed (systemd --user)."""
    timer, service = LEGACY_BACKUP_UNITS
    systemctl = "command -v systemctl >/dev/null 2>&1"
    return (
        'u="$HOME/.config/systemd/user"; '
        f'if [ -e "$u/{timer}" ] || [ -e "$u/{service}" ]; then '
        f"if {systemctl}; then systemctl --user disable --now {timer} >/dev/null 2>&1 || true; fi; "
        f'rm -f "$u/{timer}" "$u/{service}"; '
        f"if {systemctl}; then systemctl --user daemon-reload >/dev/null 2>&1 || true; fi; "
        "echo 'note: removed the legacy printops-backup.timer (it ran a command that no longer backs anything up);"
        " Home Assistant schedules backups now: run `3d init --env --stage-only --spoolman <local|ssh:USER@HOST>` to set the backup disk up'; "
        "fi"
    )


def _sync_step(cfg: DeployConfig, dry_run: bool) -> Step:
    sync = [p for p in SYNC_PATHS if (cfg.repo_root / p).exists()]
    rsync = [
        "rsync",
        "-az",
        "--relative",
        "--delete",
        "--exclude",
        "__pycache__",
        "--exclude",
        "*.pyc",
    ]
    if dry_run:
        rsync += ["--dry-run", "--itemize-changes"]
    rsync += [*sync, f"{_rsync_host(cfg.host)}:{cfg.remote_dir}/src/"]
    return Step(
        "sync build context" + (" (dry run: changes only)" if dry_run else ""),
        rsync,
        read_only=dry_run,
    )


def plan(cfg: DeployConfig, *, dry_run: bool = False) -> list[Step]:
    src = (
        str(cfg.repo_root) if cfg.local else cfg.remote_dir + "/src"
    )  # the docker build context
    steps = [script_step(cfg, "create data + backup dirs", _mkdirs_script(cfg))]
    if not cfg.local:
        steps.append(_sync_step(cfg, dry_run))
    steps += [
        script_step(
            cfg,
            "build + start spoolman and 3d-api (compose project printops)",
            _compose_script(cfg, src),
        ),
        script_step(
            cfg,
            "retire the legacy backup timer (Home Assistant schedules backups)",
            _retire_legacy_timer_script(),
        ),
    ]
    return steps


def degraded_reason(body: dict[str, Any]) -> str:
    """Why a /v1/health body that is not "ok" is not: the DB error, Spoolman's error, the missing extra
    fields, or the one-line hint of what is only a warning."""
    db, sp = body.get("db") or {}, body.get("spoolman") or {}
    if db.get("ok") is False:
        return f"db: {db.get('error') or 'not ok'}"
    if sp.get("error"):
        return f"spoolman: {sp['error']}"
    fields = sp.get("extra_fields") or {}
    if fields.get("missing"):
        return f"spoolman extra fields missing: {', '.join(fields['missing'])}"
    if fields.get("error"):
        return f"spoolman extra fields: {fields['error']}"
    return str(sp.get("message") or f"spoolman extra fields {fields.get('status') or 'not ok'}")


def wait_healthy(
    url: str,
    *,
    timeout_s: float = 300.0,
    interval_s: float = 3.0,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll /v1/health until status "ok"; returns the last body or raises ApiError on timeout."""
    api = ApiClient(url)
    deadline = clock() + timeout_s
    last = ""
    while True:
        try:
            h = api.health()
            if h.get("status") == "ok":
                return h
            msg = f"degraded: {degraded_reason(h)}"
        except ApiError as e:
            msg = e.message
        if msg != last:
            log(f"  waiting: {msg}")
            last = msg
        if clock() >= deadline:
            raise ApiError(f"3d-api at {url} not healthy after {timeout_s:.0f}s: {msg}")
        sleep(interval_s)


class ToolMissing(RuntimeError):
    """A program a step runs (ssh, rsync, bash) is not installed on THIS machine."""

    def __init__(self, tool: str, step: str) -> None:
        super().__init__(f"{step}: `{tool}` is not installed on this machine")
        self.tool = tool


TOOL_INSTALL = {
    "rsync": "sudo apt install rsync  (macOS: brew install rsync)",
    "ssh": "sudo apt install openssh-client  (macOS: ssh ships with the system)",
}


def run_steps(
    steps: Sequence[Step],
    *,
    cwd: Path,
    dry_run: bool,
    log: Callable[[str], None] = print,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Print every step; execute it unless dry_run (read-only steps execute even then).
    `runner` is subprocess.run; tests inject a fake so no ssh/docker ever starts."""
    for i, step in enumerate(steps, 1):
        shown = (
            f"{step.argv[-1]}  (on {step.where})"
            if step.script
            else shlex.join(step.argv)
        )
        log(f"[{i}/{len(steps)}] {step.title}\n  $ {shown}")
        if dry_run and not step.read_only:
            continue
        try:
            r = runner(step.argv, cwd=str(cwd), check=False)
        except FileNotFoundError as e:
            raise ToolMissing(step.argv[0], f"step {i} ({step.title})") from e
        if r.returncode != 0:
            raise RuntimeError(
                f"step {i} ({step.title}) failed with exit {r.returncode}"
            )
