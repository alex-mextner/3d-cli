"""env_setup.py — `3d init --env`: the machine stage that gets Spoolman + 3d-api running and
`~/.config/3d-cli/printops.yaml` (`api:` + `token:`) written.

ACCESSED VIA: `3d init --env [...]` (lib/commands/init.py hands everything after `--env` to run()).
`3d init [path]` without `--env` stays the per-project scaffold; the two stages never mix.

FLOW (each arrow is one function):
  status()      the configured `api:` answers /v1/health (3d-api) or /api/v1/health (Spoolman)
                -> "already configured", exit 0, NOTHING changes (not even the file's mtime).
  choose        --spoolman auto|skip|local|printer:NAME|ssh:USER@HOST|script, or the interactive
                menu. `--no-input` NEVER prompts and NEVER installs unless an explicit target is
                named; with `auto` it prints the exact commands instead.
  probe()       read-only look at the picked host (home, arch, cores, RAM, load, docker). Only AFTER
                the pick: a host is never contacted to decorate the menu, and nothing is ever
                auto-selected (a busy host is not a silent default).
  install()     printops.deploy.plan() for that host — THE deploy path of `3d printops deploy`, not a
                copy — then wait for health and mint a token (`docker exec … token create`).
  write_config  only after the health check (and the token check) passed; atomic, mode 600.
  script        printops.standalone_script: the same stack as a copy-me bash script (no install).

WHY printers are candidates at all: a Klipper/Moonraker box is already an always-on Linux host on the
  LAN. HOST_LIMITS records, from docs/printers/*, which models are poor hosts (RAM, firmware, CPU
  architecture). Docker is never installed for the user; a host without it gets the install link.

INVARIANTS:
  - No request to any real host in tests: `runner`, `ask`, `say` are injected; the HTTP checks go
    through printops.client (its Transport is injectable) against a local fake.
  - config is written only after a successful check; an existing different config is kept as
    `printops.yaml.bak` (mode 600) first.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli import paths
from errors import GateFailure, InvalidArgument, ThreeDError, UsageError
from printops import deploy
from printops.cli._common import parse
from printops.client import (
    ApiClient,
    ApiConfigError,
    ApiError,
    config_path,
    load_config,
)

CLI_NAME = "init --env"
TARGET_HELP = "auto|skip|local|printer:NAME|ssh:USER@HOST|script"
DEFAULT_SCRIPT_OUT = "spoolman-setup.sh"
DOCKER_INSTALL = "https://docs.docker.com/engine/install/"
TOKEN_SCOPES = "read,write"
CONTAINER = "printops-api"
SSH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=40",
)  # the K1's dropbear needs ~28 s to greet

# (regex on the registry model name, level, reason). Levels: discouraged = possible, not recommended
# (needs --allow-discouraged / an explicit y); unsupported = cannot run the stack, listed, not selectable.
HOST_LIMITS: tuple[tuple[str, str, str], ...] = (
    (
        r"snapmaker u1",
        "discouraged",
        "aarch64 with 1 GB RAM running Klipper and the print, Buildroot firmware without a package manager; "
        "ssh needs the paxx12 firmware and /etc plus ssh keys are wiped on every firmware upgrade "
        "(docs/printers/u1-paxx12-upgrade.md)",
    ),
    (
        r"k1 se",
        "unsupported",
        "MIPS SoC (mipsel Entware, docs/printers/k1se-root-upgrade.md); the Spoolman image is built for "
        "amd64, arm64 and armv7 only (services/3d-api/compose.yaml)",
    ),
)

USAGE = f"""3d init --env [--no-input] [--spoolman TARGET] [--script-out PATH] [--api URL] [--token TOKEN]
                [--ssh-user USER] [--dry-run] [--allow-discouraged]
  The MACHINE stage of `3d init` (plain `3d init [path]` still scaffolds a project): make Spoolman +
  the 3d-api that `3d printops` talks to run somewhere, and write {config_path()} (api: + token:,
  mode 600) once the service answered its health check. Safe to re-run: when the configured api
  already answers /v1/health (or Spoolman's /api/v1/health) it says "already configured" and
  changes nothing. Exit 0 = configured / skipped / already configured; nonzero = failed, with the fix.

  Without --no-input (and on a terminal) it asks where to install:
    1) a printer of the registry (Moonraker boxes; U1: possible but NOT recommended, K1 SE: not possible)
    2) this computer (docker compose)   3) a server over ssh   4) a standalone script   0) skip
  Nothing is pre-selected and no host is contacted before you pick it; then its load/RAM/docker are shown.

Options:
  --spoolman TARGET  {TARGET_HELP}
                       auto           ask (terminal) or, with --no-input, report status + the exact commands (default)
                       skip           do nothing
                       local          docker compose on this computer (data in ~/.local/share/3d-cli/printops)
                       printer:NAME   a registry printer's host over ssh (see `3d printer list`); add --ssh-user
                       ssh:USER@HOST  any server with docker compose you can ssh into without a password prompt
                       script         write a self-contained bash script (docker compose + Spoolman + 3d-api,
                                      idempotent, no 3d-cli needed) to copy to a server and run; installs nothing here
  --script-out PATH  where `script` writes (default ./{DEFAULT_SCRIPT_OUT}; asked for on a terminal)
  --api URL          connect to a Spoolman/3d-api that already runs (e.g. the URL the script printed): check
                     it, then write the config. With --token TOKEN (checked against the API before writing)
  --token TOKEN      3d-api bearer token for --api (an install target mints its own)
  --ssh-user USER    ssh user for printer:NAME (default: your ssh config's)
  --dry-run          install targets: print the steps (rsync dry run), change nothing
  --allow-discouraged  --no-input only: accept a "possible but not recommended" printer host
  --no-input, --yes, -y  never prompt, never install unless an explicit target is named

Examples:
  3d init --env                                             # interactive menu
  3d init --env --no-input --spoolman auto                  # CI/agent: status + what to run
  3d init --env --no-input --spoolman local
  3d init --env --no-input --spoolman ssh:me@nas.lan --dry-run
  3d init --env --no-input --spoolman script --script-out ~/spoolman-setup.sh
  3d init --env --no-input --api http://nas.lan:8765 --token po_..."""


@dataclass(frozen=True)
class Target:
    kind: str  # auto | skip | local | printer | ssh | script
    name: str = ""  # printer name / USER@HOST


@dataclass(frozen=True)
class Candidate:
    """A registry printer that may host the stack."""

    name: str
    model: str
    host: str
    level: str  # ok | discouraged | unsupported
    reason: str = ""


@dataclass(frozen=True)
class HostProbe:
    home: str
    arch: str
    cores: int
    mem_mb: int
    load1: float
    docker: str
    compose: str
    os_name: str

    @property
    def busy(self) -> bool:
        return self.cores > 0 and self.load1 >= self.cores * 0.9

    def describe(self) -> str:
        return (
            f"{self.os_name or 'Linux'} {self.arch}, {self.cores} cores, {self.mem_mb} MB RAM, "
            f"load {self.load1:.2f}"
            + (" (BUSY)" if self.busy else "")
            + f", docker {self.docker or 'MISSING'}, compose {self.compose or 'MISSING'}"
        )


@dataclass(frozen=True)
class Health:
    state: str  # ok | degraded | unreachable | token-rejected
    kind: str = ""  # 3d-api | spoolman
    detail: str = ""


@dataclass
class Options:
    spoolman: str = "auto"
    script_out: str | None = None
    api: str | None = None
    token: str | None = None
    ssh_user: str | None = None
    no_input: bool = False
    dry_run: bool = False
    allow_discouraged: bool = False


@dataclass
class Env:
    """Everything with a side effect, injectable: tests run the whole flow with fakes."""

    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
    deploy_runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
    is_tty: bool = field(default_factory=lambda: sys.stdin.isatty())
    repo_root: Path | None = None


# ----------------------------------------------------------------------------------- options


def parse_target(raw: str) -> Target:
    if raw in ("auto", "skip", "local", "script"):
        return Target(raw)
    kind, _, name = raw.partition(":")
    if kind in ("printer", "ssh") and name:
        if kind == "ssh" and (name.startswith("-") or any(ch.isspace() for ch in name)):
            raise InvalidArgument(
                "--spoolman",
                raw,
                [TARGET_HELP],
                command=CLI_NAME,
                extra="ssh:USER@HOST takes one host, e.g. ssh:ultra@server.lan",
            )
        return Target(kind, name)
    raise InvalidArgument(
        "--spoolman",
        raw,
        [TARGET_HELP],
        command=CLI_NAME,
        extra="Examples: --spoolman local, --spoolman printer:myprinter, "
        "--spoolman ssh:me@nas.lan, --spoolman script",
    )


def parse_options(argv: Sequence[str]) -> Options:
    args = ["--no-input" if a == "-y" else a for a in argv]
    p = parse(
        args,
        "--env",
        prefix="init",
        values={"--spoolman", "--script-out", "--api", "--token", "--ssh-user"},
        flags={"--no-input", "--yes", "--dry-run", "--allow-discouraged"},
    )
    o = Options(
        spoolman=p.opts.get("--spoolman", "auto"),
        script_out=p.opts.get("--script-out"),
        api=p.opts.get("--api"),
        token=p.opts.get("--token"),
        ssh_user=p.opts.get("--ssh-user"),
        no_input=bool(p.flags & {"--no-input", "--yes"}),
        dry_run="--dry-run" in p.flags,
        allow_discouraged="--allow-discouraged" in p.flags,
    )
    target = parse_target(o.spoolman)
    if o.api and target.kind not in ("auto", "skip"):
        raise UsageError(
            "--api connects to a service that already runs; it cannot be combined with "
            f"--spoolman {o.spoolman}",
            command=CLI_NAME,
            remediation=["Install first (drop --api), or connect (drop --spoolman)."],
        )
    if o.token and not o.api:
        raise UsageError("--token needs --api URL (an install target mints its own token)", command=CLI_NAME)
    if o.script_out and target.kind != "script":
        raise UsageError(
            "--script-out only goes with --spoolman script", command=CLI_NAME
        )
    return o


# ----------------------------------------------------------------------------------- health


def check_health(api: str, token: str | None) -> Health:
    """3d-api first (/v1/health, then the token against /v1/events); a bare Spoolman answers
    /api/v1/health. Network errors become state "unreachable", never an exception."""
    client = ApiClient(api, token, timeout=5.0)
    try:
        body = client.health()
    except ApiError as e:
        if e.status == 0:
            return Health("unreachable", detail=e.message)
        return _spoolman_health(client, e.message)
    if body.get("status") != "ok":
        sp = body.get("spoolman") or {}
        return Health(
            "degraded",
            "3d-api",
            f"status {body.get('status')}: spoolman "
            f"{sp.get('error') or (sp.get('extra_fields') or {}).get('status') or 'not ok'}",
        )
    if token:
        try:
            client.events(last=1)
        except ApiError as e:
            if e.status in (401, 403):
                return Health("token-rejected", "3d-api", e.message)
            return Health("unreachable", "3d-api", e.message)
    return Health("ok", "3d-api", f"version {body.get('version', '?')}")


def _spoolman_health(client: ApiClient, first_error: str) -> Health:
    try:
        body = client.get("/api/v1/health")
    except ApiError as e:
        return Health("unreachable", detail=f"{first_error}; {e.message}")
    if isinstance(body, dict) and body.get("status") == "healthy":
        return Health(
            "ok",
            "spoolman",
            "answers /api/v1/health (a bare Spoolman: the 3d printops commands need 3d-api)",
        )
    return Health(
        "unreachable",
        detail=f"{client.base_url} is neither 3d-api nor Spoolman: {first_error}",
    )


def _write_private(path: Path, text: str) -> None:
    """Atomically write `text` to `path`, mode 0600 from the moment the file exists (mkstemp)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_config(api: str, token: str | None, say: Callable[[str], None]) -> Path:
    """Merge api/token into printops.yaml (atomic, 0600); keep a different old one as .bak.

    A token belongs to the endpoint it was issued for: moving to another api without a
    replacement token drops the old one instead of sending it to the new host."""
    import yaml

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        old = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(old) or {}
        data = dict(loaded) if isinstance(loaded, dict) else {}
        old_api = str(data.get("api") or "").rstrip("/")
        if old_api != api.rstrip("/") and not token:
            data.pop("token", None)
        if old_api != api.rstrip("/") or (token and data.get("token") != token):
            bak = path.with_name(path.name + ".bak")
            _write_private(bak, old)
            say(f"kept the previous config as {bak}")
    data["api"] = api
    if token:
        data["token"] = token
    _write_private(path, yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    return path


# ----------------------------------------------------------------------------------- candidates


def classify(model: str) -> tuple[str, str]:
    for pattern, level, reason in HOST_LIMITS:
        if re.search(pattern, model, re.IGNORECASE):
            return level, reason
    return "ok", ""


def candidates() -> list[Candidate]:
    """Registry printers that are ours to use (full access) and speak Moonraker, i.e. Linux boxes
    on the LAN. Static: nothing is contacted."""
    from printer_ops.devices import load_devices

    try:
        devices = load_devices()
    except ThreeDError:
        return []
    out = []
    for name, dev in sorted(devices.items()):
        if dev.adapter != "moonraker" or dev.access != "full" or not dev.host:
            continue
        level, reason = classify(dev.model or "")
        out.append(Candidate(name, dev.model or "?", dev.host, level, reason))
    return out


# ----------------------------------------------------------------------------------- probe

PROBE_SCRIPT = (
    'printf "home=%s\\n" "$HOME"; printf "arch=%s\\n" "$(uname -m)";'
    ' printf "cores=%s\\n" "$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)";'
    ' printf "load=%s\\n" "$(cut -d" " -f1 /proc/loadavg)";'
    ' printf "mem_mb=%s\\n" "$(awk \'/MemTotal/ {print int($2/1024)}\' /proc/meminfo)";'
    ' printf "docker=%s\\n" "$(docker --version 2>/dev/null)";'
    ' printf "compose=%s\\n" "$(docker compose version --short 2>/dev/null)";'
    ' printf "os=%s\\n" "$(. /etc/os-release 2>/dev/null && printf %s "$PRETTY_NAME")"'
)


def parse_probe(text: str) -> HostProbe:
    kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def num(key: str, cast: Callable[[str], Any], default: Any) -> Any:
        try:
            return cast(kv.get(key, ""))
        except ValueError:
            return default

    return HostProbe(
        home=kv.get("home", ""),
        arch=kv.get("arch", "?"),
        cores=num("cores", int, 0),
        mem_mb=num("mem_mb", int, 0),
        load1=num("load", float, 0.0),
        docker=kv.get("docker", "").removeprefix("Docker version ").split(",")[0],
        compose=kv.get("compose", ""),
        os_name=kv.get("os", ""),
    )


def probe(host: str, env: Env) -> HostProbe:
    """Read-only commands only. `host` "" = this machine."""
    argv = (
        ["bash", "-c", PROBE_SCRIPT]
        if not host
        else ["ssh", *SSH_OPTS, host, PROBE_SCRIPT]
    )
    try:
        r = env.runner(argv, capture_output=True, text=True, check=False, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ThreeDError(
            f"cannot probe {host or 'this machine'}: {e}",
            command=CLI_NAME,
            remediation=[f"Check `ssh {host}` works without a password prompt."],
        ) from e
    if r.returncode != 0:
        raise ThreeDError(
            f"cannot probe {host}: ssh exited {r.returncode}: {(r.stderr or '').strip()[:300]}",
            command=CLI_NAME,
            remediation=[
                f"Check `ssh {host} true` works with your key (BatchMode: no password prompt)."
            ],
        )
    return parse_probe(r.stdout)


# ----------------------------------------------------------------------------------- install


def _repo_root(env: Env) -> Path:
    return env.repo_root or Path(
        os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2]
    )


TOKEN_NAME_MAX = 64  # printops.tokens.NAME_RE: [a-z0-9][a-z0-9_.-]{0,63}
TOKEN_STAMP_LEN = len("20260101000000")


def _token_name() -> str:
    import socket

    host = (
        re.sub(r"[^a-z0-9._-]", "-", socket.gethostname().split(".")[0].lower())
        or "host"
    )
    room = TOKEN_NAME_MAX - len("cli--") - TOKEN_STAMP_LEN  # a 63-char hostname must still mint
    return f"cli-{host[:room]}-{time.strftime('%Y%m%d%H%M%S')}"


def mint_token(host: str, env: Env) -> str:
    """`docker exec printops-api 3d printops token create …` here or over ssh; the plaintext is
    printed once by the service and kept only in the config file we write next."""
    remote = shlex.join(
        [
            "docker",
            "exec",
            CONTAINER,
            "3d",
            "printops",
            "token",
            "create",
            _token_name(),
            "--scopes",
            TOKEN_SCOPES,
        ]
    )
    argv = ["bash", "-c", remote] if not host else ["ssh", *SSH_OPTS, host, remote]
    r = env.runner(argv, capture_output=True, text=True, check=False, timeout=120)
    lines = [
        ln.strip()
        for ln in (r.stdout or "").splitlines()
        if ln.strip().startswith("po_")
    ]
    if r.returncode != 0 or not lines:
        raise ThreeDError(
            f"could not mint a 3d-api token (exit {r.returncode}): {(r.stderr or '').strip()[:300]}",
            command=CLI_NAME,
            remediation=[
                f"Run it by hand: {remote}",
                "then `3d init --env --api URL --token TOKEN`.",
            ],
        )
    return lines[0]


def _require_docker(pr: HostProbe, label: str) -> None:
    if pr.docker and pr.compose:
        return
    raise ThreeDError(
        f"{label} has no {'docker' if not pr.docker else 'docker compose plugin'}", command=CLI_NAME,
        remediation=[f"Install Docker Engine with the compose plugin there: {DOCKER_INSTALL}",
                     "then run this again. (3d never installs docker for you.)"])


def _warn_if_busy(pr: HostProbe, label: str, env: Env, opts: Options) -> None:
    if not pr.busy:
        return
    env.say(f"warning: {label} is busy (load {pr.load1:.2f} on {pr.cores} cores); building the image will be slow")
    if _interactive(env, opts) and not _confirm(env, f"{label} is busy. Install anyway?"):
        raise GateFailure(f"cancelled: {label} is busy", command=CLI_NAME,
                          remediation=["Pick another target, or run again when its load is lower."])


def _http_host(host: str, env: Env) -> str:
    """The address an HTTP client reaches the ssh target `host` on: a `Host` alias that only ssh
    knows resolves through ssh's own config (`ssh -G`), a plain name or address comes back as is."""
    name = host.rsplit("@", 1)[-1]
    try:
        r = env.runner(["ssh", "-G", host], capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return name
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        key, _, value = line.partition(" ")
        if key == "hostname" and value.strip():
            return value.strip()
    return name


def install(host: str, label: str, env: Env, opts: Options) -> tuple[str, str | None]:
    """Deploy to `host` ("" = this machine); returns (api_url, token or None on --dry-run)."""
    pr = probe(host, env)
    env.say(f"{label}: {pr.describe()}")
    _require_docker(pr, label)
    _warn_if_busy(pr, label, env, opts)
    root = _repo_root(env)
    base = str(paths.data_dir() / "printops") if not host else f"{pr.home}/printops"
    api_url = None
    if host:
        http_host = _http_host(host, env)
        api_url = f"http://{f'[{http_host}]' if ':' in http_host else http_host}:{deploy.API_PORT}"
    cfg = deploy.DeployConfig(repo_root=root, host=host, remote_dir=base, backup_dir=f"{base}/backup",
                              api_url=api_url, version=deploy.git_version(root), timer_required=False)
    steps = deploy.plan(cfg, dry_run=opts.dry_run)
    env.say(f"deploy printops {cfg.version} -> {host or 'this machine'}:{base}" + ("  [dry run]" if opts.dry_run else ""))
    try:
        deploy.run_steps(steps, cwd=root, dry_run=opts.dry_run, log=env.say, runner=env.deploy_runner)
        if opts.dry_run:
            env.say(f"then: wait for {cfg.health_url}/v1/health status ok, mint a token, write {config_path()}")
            return cfg.health_url, None
        env.say(f"waiting for {cfg.health_url}/v1/health …")
        deploy.wait_healthy(cfg.health_url, log=env.say)
    except (RuntimeError, ApiError) as e:
        where = f"ssh {host} " if host else ""
        raise ThreeDError(str(e), command=CLI_NAME,
                          remediation=[f"Inspect: {where}docker logs --tail 80 {CONTAINER} (and printops-spoolman)"]) from e
    return cfg.health_url, mint_token(host, env)


def _interactive(env: Env, opts: Options) -> bool:
    return env.is_tty and not opts.no_input


def _confirm(env: Env, question: str) -> bool:
    return env.ask(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# ----------------------------------------------------------------------------------- targets


def resolve_host(
    target: Target, cands: Sequence[Candidate], opts: Options
) -> tuple[str, str]:
    """(ssh host or "" for local, label) for an install target."""
    if target.kind == "local":
        return "", "this machine"
    if target.kind == "ssh":
        return target.name, target.name
    by_name = {c.name: c for c in cands}
    cand = by_name.get(target.name)
    if cand is None:
        raise InvalidArgument(
            "--spoolman printer:",
            target.name,
            sorted(by_name) or ["(no Moonraker printer is configured)"],
            command=CLI_NAME,
            extra="Printers come from `3d printer list` (fleet/printers.yaml, "
            "~/.config/3d-cli/printer-devices.yaml).",
        )
    if cand.level == "unsupported":
        raise ThreeDError(
            f"{cand.name} ({cand.model}) cannot host Spoolman: {cand.reason}",
            command=CLI_NAME,
            remediation=[
                "Pick another printer, this machine (`local`) or an ssh host."
            ],
        )
    if cand.level == "discouraged" and not opts.allow_discouraged:
        raise ThreeDError(
            f"{cand.name} ({cand.model}) is possible but not recommended as a host: {cand.reason}",
            command=CLI_NAME,
            remediation=[
                "If you really want it, add --allow-discouraged.",
                "Otherwise use `local`, `ssh:USER@HOST` or `script`.",
            ],
        )
    user = f"{opts.ssh_user}@" if opts.ssh_user else ""
    return f"{user}{cand.host}", f"{cand.name} ({cand.host})"


SIMPLE_CHOICES = {"0": "skip", "skip": "skip", "2": "local", "4": "script"}


def menu(cands: Sequence[Candidate], env: Env, opts: Options) -> Target:
    """Interactive target menu. Nothing is pre-selected: an empty answer asks again."""
    say = env.say
    say("\nWhere should Spoolman (+ the 3d-api that 3d talks to) run?")
    say("  1) a printer of the registry (an always-on Linux box on the LAN)")
    say("  2) this computer (docker compose)")
    say("  3) a server over ssh")
    say("  4) generate a standalone install script to copy to any server and run there")
    say("  0) skip (configure later: 3d init --env)")
    while True:
        picked = _menu_answer(env.ask("Choice: ").strip(), cands, env, opts)
        if picked is not None:
            return picked


def _menu_answer(ans: str, cands: Sequence[Candidate], env: Env, opts: Options) -> Target | None:
    """One answer -> a Target, or None to ask again."""
    if ans in SIMPLE_CHOICES:
        return Target(SIMPLE_CHOICES[ans])
    if ans == "1":
        return _menu_printer(cands, env, opts)
    if ans == "3":
        host = env.ask("ssh target (USER@HOST): ").strip()
        return Target("ssh", host) if host and not host.startswith("-") else None
    env.say("Enter 1, 2, 3, 4 or 0.")
    return None


def _menu_printer(cands: Sequence[Candidate], env: Env, opts: Options) -> Target | None:
    say = env.say
    if not cands:
        say("No Moonraker printer with full access is configured (`3d printer list`).")
        return None
    for i, c in enumerate(cands, 1):
        tag = {
            "ok": "",
            "discouraged": "  [possible but NOT RECOMMENDED]",
            "unsupported": "  [not possible]",
        }[c.level]
        say(f"  {i}) {c.name}  {c.model}  {c.host}{tag}")
        if c.reason:
            say(f"       {c.reason}")
    ans = env.ask("Printer number (empty = back): ").strip()
    if not ans.isdigit() or not 1 <= int(ans) <= len(cands):
        return None
    c = cands[int(ans) - 1]
    if c.level == "unsupported":
        say(f"{c.name} cannot host it: {c.reason}")
        return None
    if c.level == "discouraged" and not _confirm(
        env, f"{c.name} is not a recommended host. Use it anyway?"
    ):
        return None
    opts.allow_discouraged = True
    if opts.ssh_user is None:
        user = env.ask("ssh user on the printer (empty = your ssh default): ").strip()
        opts.ssh_user = user or None
    return Target("printer", c.name)


def unconfigured_hints(cands: Sequence[Candidate]) -> list[str]:
    base = "3d init --env --no-input"
    lines = [
        f"  {base} --spoolman local                          # docker compose on this computer",
        f"  {base} --spoolman ssh:USER@HOST                  # any server you can ssh into (docker there)",
        f"  {base} --spoolman script --script-out ./{DEFAULT_SCRIPT_OUT}   # copy-me script, installs nothing here",
    ]
    for c in cands:
        if c.level == "unsupported":
            continue
        note = (
            "   # possible but NOT recommended (add --allow-discouraged): " + c.reason
            if c.level == "discouraged"
            else ""
        )
        lines.append(f"  {base} --spoolman printer:{c.name} [--ssh-user USER]{note}")
    lines.append(
        f"  {base} --api URL --token TOKEN                   # a Spoolman/3d-api that already runs"
    )
    return lines


# ----------------------------------------------------------------------------------- script


def make_script(opts: Options, env: Env) -> int:
    from printops import standalone_script

    root = _repo_root(env)
    out = Path(opts.script_out or DEFAULT_SCRIPT_OUT).expanduser()
    if env.is_tty and not opts.no_input and opts.script_out is None:
        ans = env.ask(f"Write the script to [{out}]: ").strip()
        out = Path(ans).expanduser() if ans else out
    try:
        written = standalone_script.write(root, deploy.git_version(root), out)
    except (OSError, FileNotFoundError, ValueError) as e:
        raise ThreeDError(
            f"cannot write the install script: {e}",
            command=CLI_NAME,
            remediation=["Pass a writable --script-out PATH."],
        ) from e
    env.say(
        f"wrote {written} ({written.stat().st_size // 1024} KiB, bash, idempotent, needs only docker compose)"
    )
    env.say("On the server (as the user that should own the data):")
    env.say(f"  scp {written} USER@SERVER:  &&  ssh USER@SERVER bash {written.name}")
    env.say("It prints the Spoolman and 3d-api URLs and a token. Back here:")
    env.say("  3d init --env --no-input --api http://SERVER:8765 --token TOKEN")
    return 0


# ----------------------------------------------------------------------------------- entry


def _finish(api: str, token: str | None, env: Env) -> int:
    """Check, then write: the config never points at something that did not answer."""
    h = check_health(api, token)
    if h.state != "ok":
        raise ThreeDError(
            f"{api} is not healthy: {h.state} {h.detail}",
            command=CLI_NAME,
            remediation=[
                "Nothing was written to the config.",
                "Check the URL/token, then `3d printops health --api URL`.",
            ],
        )
    path = write_config(api, token, env.say)
    env.say(f"configured: {api} ({h.kind}, {h.detail}); wrote {path} (mode 600)")
    return 0


INSTALL_KINDS = ("local", "printer", "ssh")


def _current_config() -> tuple[str | None, str | None]:
    try:
        return load_config()
    except ApiConfigError as e:
        raise UsageError(str(e), command=CLI_NAME, remediation=[f"Fix or remove {config_path()}."]) from e


def _connect(opts: Options, cur_api: str | None, cur_token: str | None, env: Env) -> int:
    """`--api URL [--token T]`: a service that already runs (the script's printed line)."""
    api = str(opts.api).rstrip("/")
    same = bool(cur_api) and str(cur_api).rstrip("/") == api
    token = opts.token or (cur_token if same else None)
    h = check_health(api, token)
    if h.state == "ok" and same and token == cur_token:
        env.say(f"already configured: {api} ({h.kind}, {h.detail})")
        return 0
    return _finish(api, token, env)


def _settle_existing(health: Health, cur_api: str, cur_token: str | None, target: Target, opts: Options,
                     env: Env) -> int | None:
    """What the configured api's health decides: an exit code, or None to go on setting up."""
    if health.state == "ok":
        if target.kind == "script":  # an explicit artifact request: generating it changes nothing here
            return None
        env.say(f"already configured: {cur_api} ({health.kind}, {health.detail}); nothing changed")
        if not cur_token and health.kind == "3d-api":
            env.say(f"note: no token in {config_path()}; mint one: 3d printops token create cli --scopes "
                    f"{TOKEN_SCOPES} --ssh USER@HOST")
        return 0
    if target.kind == "auto" and not _interactive(env, opts):
        raise ThreeDError(
            f"configured {cur_api} is {health.state}: {health.detail}", command=CLI_NAME,
            remediation=["Fix the service, or point 3d at a new one: 3d init --env --api URL --token TOKEN",
                         f"or install a new one: 3d init --env --spoolman {TARGET_HELP}"])
    if target.kind in INSTALL_KINDS:
        env.say(f"note: configured {cur_api} is {health.state}; a new install replaces the config")
    return None


def _choose_interactively(cands: Sequence[Candidate], health: Health | None, cur_api: str | None,
                          opts: Options, env: Env) -> Target | None:
    """The menu, after a word about a dead configured api; None = the user declined to replace it."""
    if health is not None and health.state != "ok":
        env.say(f"configured {cur_api} is {health.state}: {health.detail}")
        if not _confirm(env, "Set up a new Spoolman/PrintOps instead?"):
            return None
    return menu(cands, env, opts)


def _confirm_discouraged(target: Target, cands: Sequence[Candidate], opts: Options, env: Env) -> None:
    """`--spoolman printer:u1` on a terminal: an explicit y for a not-recommended host."""
    if target.kind != "printer" or opts.allow_discouraged or not _interactive(env, opts):
        return
    cand = next((c for c in cands if c.name == target.name), None)
    if cand is not None and cand.level == "discouraged" and _confirm(
            env, f"{cand.name} is possible but NOT recommended as a host ({cand.reason}). Use it anyway?"):
        opts.allow_discouraged = True


def _setup(target: Target, health: Health | None, cur_api: str | None, opts: Options, env: Env) -> int:
    cands = candidates()
    if target.kind == "auto":
        if not _interactive(env, opts):
            env.say("Spoolman/PrintOps is not configured; --no-input installs nothing. Choose a target:")
            for line in unconfigured_hints(cands):
                env.say(line)
            return 0
        picked = _choose_interactively(cands, health, cur_api, opts, env)
        if picked is None:
            return 1
        target = picked
    if target.kind == "skip":
        env.say("skipped: Spoolman/PrintOps not configured (3d init --env when you want it)")
        return 0
    if target.kind == "script":
        return make_script(opts, env)
    _confirm_discouraged(target, cands, opts, env)
    host, label = resolve_host(target, cands, opts)
    if target.kind == "ssh" and _interactive(env, opts):
        env.say(f"Installing over ssh on {label}: it will run docker compose there (project printops).")
    api, token = install(host, label, env, opts)
    return 0 if opts.dry_run else _finish(api, token, env)


def run(argv: Sequence[str], env: Env | None = None) -> int:
    env = env or Env()
    opts = parse_options(argv)
    target = parse_target(opts.spoolman)
    cur_api, cur_token = _current_config()
    if opts.api:
        return _connect(opts, cur_api, cur_token, env)
    health = check_health(cur_api, cur_token) if cur_api else None
    if health is not None and cur_api:
        done = _settle_existing(health, cur_api, cur_token, target, opts, env)
        if done is not None:
            return done
    return _setup(target, health, cur_api, opts, env)
