"""remote.py — argv builders and host config for `3d remote` (rsync to an ssh box, run under its `heavy`, pull results).

WHAT: pure functions that turn a host entry + a project name into the exact `rsync` / `ssh`
  argument lists, plus the loader of `~/.config/3d-cli/remote.yaml`. `commands/remote.py` only
  runs what is built here, so the argv can be tested without a network.
WHY: the heavy box (16 cores, `~/projects/<name>-<date>/{repo,work,out,logs,cache}`, `~/projects/bin/heavy`)
  turned into hand-typed rsync/ssh lines in the Chekhov session; a typo in the quoting or the lock wrapper
  costs a retry per slow step over a ~1.7 MB/s uplink.
INVARIANTS: stdlib + yaml (lazy). Remote paths are relative to the remote $HOME and always go through
  shlex.quote; nothing here talks to the network.

Config (`~/.config/3d-cli/remote.yaml`, or `$THREED_REMOTE_CONFIG`):
    default: heavy
    hosts:
      heavy:
        ssh: ultra@65.21.40.51
        root: projects          # holds <project>/{work,out,logs}
        heavy: projects/bin/heavy
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from errors import InvalidArgument, UsageError

CONFIG_ENV = "THREED_REMOTE_CONFIG"
DEFAULT_CONFIG = "~/.config/3d-cli/remote.yaml"
RSYNC_EXCLUDES = (".git", "__pycache__", ".venv", "node_modules", ".DS_Store")


@dataclass(frozen=True)
class Host:
    """One ssh box: `ssh` is the `user@host` target, `root` the remote dir holding the projects."""

    name: str
    ssh: str
    root: str = "projects"
    heavy: str | None = "projects/bin/heavy"

    def work(self, project: str) -> str:
        return f"{self.root}/{project}/work"

    def out(self, project: str) -> str:
        return f"{self.root}/{project}/out"

    def logs(self, project: str) -> str:
        return f"{self.root}/{project}/logs"


def config_path(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    return Path(os.path.expanduser(environ.get(CONFIG_ENV, DEFAULT_CONFIG)))


def load_hosts(path: Path | None = None) -> tuple[dict[str, Host], str | None]:
    """(hosts by name, default host name) from the YAML config; empty when the file is absent."""
    cfg = path or config_path()
    if not cfg.is_file():
        return {}, None
    import yaml

    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    raw_hosts = data.get("hosts") or {}
    hosts: dict[str, Host] = {}
    for name, entry in raw_hosts.items():
        if not isinstance(entry, dict) or "ssh" not in entry:
            raise InvalidArgument(
                f"{cfg}: hosts.{name}",
                str(entry),
                ["a mapping with an `ssh:` target"],
                command="remote",
            )
        hosts[str(name)] = Host(
            name=str(name),
            ssh=str(entry["ssh"]),
            root=str(entry.get("root", "projects")).strip("/"),
            heavy=(str(entry["heavy"]).strip("/") if entry.get("heavy") else None),
        )
    default = data.get("default")
    return hosts, (str(default) if default else None)


def pick_host(hosts: dict[str, Host], default: str | None, wanted: str | None) -> Host:
    """The host named on the command line, else the config default, else the only one; `user@host` works ad hoc."""
    name = wanted or default
    if name is None and len(hosts) == 1:
        name = next(iter(hosts))
    if name is None:
        raise UsageError(
            "no remote host: pass --host NAME|user@host or set `default:` in "
            + DEFAULT_CONFIG,
            command="remote",
        )
    if name in hosts:
        return hosts[name]
    if "@" in name:
        return Host(name=name, ssh=name)
    raise InvalidArgument(
        "--host", name, sorted(hosts) or ["user@host"], command="remote"
    )


def default_project(cwd: Path | None = None, today: date | None = None) -> str:
    """`<dir>-<YYYY-MM-DD>`, the `projects/<name>-<date>` convention of the box."""
    base = (cwd or Path.cwd()).name or "job"
    return f"{base}-{(today or date.today()).isoformat()}"


def ssh_base(host: Host) -> list[str]:
    """ssh argv without the command: keys only (no password prompt), quick connect failure."""
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host.ssh]


def remote_dir_expr(rel: str) -> str:
    """Shell expression for a path relative to the remote $HOME (quoted, `$HOME` expands remotely)."""
    return '"$HOME"/' + shlex.quote(rel)


def rsync_push_argv(host: Host, project: str, sources: list[str]) -> list[str]:
    """Copy local files/dirs into `<root>/<project>/work/` (creates it)."""
    argv = ["rsync", "-az", "--partial", "--mkpath"]
    for pattern in RSYNC_EXCLUDES:
        argv += ["--exclude", pattern]
    return [*argv, *sources, f"{host.ssh}:{host.work(project)}/"]


def rsync_pull_argv(host: Host, project: str, remote_sub: str, dest: str) -> list[str]:
    """Copy `<root>/<project>/<remote_sub>/` (default `out`) into the local `dest` dir."""
    sub = remote_sub.strip("/") or "out"
    return [
        "rsync",
        "-az",
        "--partial",
        f"{host.ssh}:{host.root}/{project}/{sub}/",
        dest.rstrip("/") + "/",
    ]


def remote_command(
    host: Host, project: str, cmd: list[str], *, name: str, wait: bool, use_heavy: bool
) -> str:
    """The shell line ssh runs: cd into the work dir, make out/logs, then the command under `heavy` when asked."""
    work = remote_dir_expr(host.work(project))
    prep = f"mkdir -p {work} {remote_dir_expr(host.out(project))} {remote_dir_expr(host.logs(project))} && cd {work}"
    body = shlex.join(cmd)
    if use_heavy and host.heavy:
        flag = "--wait " if wait else ""
        body = f"{remote_dir_expr(host.heavy)} -n {shlex.quote(name)} {flag}-- {body}"
    return f"{prep} && {body}"


def ssh_run_argv(
    host: Host, project: str, cmd: list[str], *, name: str, wait: bool, use_heavy: bool
) -> list[str]:
    """Full `ssh host 'cd work && heavy -n NAME -- CMD'`."""
    return [
        *ssh_base(host),
        remote_command(host, project, cmd, name=name, wait=wait, use_heavy=use_heavy),
    ]


def ssh_status_argv(host: Host, project: str) -> list[str]:
    """Show the lock state, the newest logs and the load of the box."""
    logs = remote_dir_expr(host.logs(project))
    script = (
        f"uptime; ls -t {logs} 2>/dev/null | head -5; "
        'if flock -n "$HOME"/.cache/heavy.lock true 2>/dev/null; then echo "heavy: free"; else echo "heavy: busy"; fi'
    )
    return [*ssh_base(host), script]
