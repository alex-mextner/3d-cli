"""3d remote — push files to an ssh heavy box, run a command there under its `heavy` lock, pull the results.

WHAT: `push` rsyncs files into `<root>/<project>/work/`, `run` executes a command in that work dir
  under the box's `heavy` wrapper (one job at a time, memory cap, logs in `<project>/logs`), `pull`
  rsyncs `<project>/out/` back, and `run --push PATH --pull` does all three in one line.
WHY: the Chekhov session needed ~4 hand-typed rsync/ssh lines per step against a 16-core box over a slow
  uplink; the one-liner makes "run this on the big machine" a flag, not a procedure.
INVARIANTS: stdlib-only at import time; hosts come from ~/.config/3d-cli/remote.yaml (see cli/remote.py);
  `run` returns the remote command's exit code, `pull` runs even after a failed command (partial
  outputs and logs are the diagnosis) unless --no-pull-on-fail.

Examples:
  3d remote run --push cat.stl --pull -- 3d prep cat.stl --out ../out
"""

from __future__ import annotations

import subprocess
import sys

from cli import remote
from cli.registry import Command
from errors import InvalidArgument, MissingDependency, UsageError

USAGE = """3d remote hosts
3d remote push [--host H] [--project P] PATH [PATH...]
3d remote run  [--host H] [--project P] [--name N] [--wait] [--no-heavy] [--push PATH]... [--pull [SUBDIR]] [--to DIR] -- CMD [ARG...]
3d remote pull [--host H] [--project P] [--sub SUBDIR] [--to DIR]
3d remote exec [--host H] [--project P] -- CMD [ARG...]
3d remote status [--host H] [--project P]
  Work on the heavy box named in ~/.config/3d-cli/remote.yaml (or ad hoc user@host).
  Layout on the box: ~/<root>/<project>/{work,out,logs}; CMD runs in work/ under `heavy -n NAME`
  (flock: one heavy job at a time; --wait queues instead of failing). --project defaults to
  <current dir>-<YYYY-MM-DD>. `pull` copies <project>/out/ (or --sub) into --to (default ./remote-out).

Examples:
  3d remote hosts
  3d remote push --project chekhov cat.stl phones.json
  3d remote run --project chekhov --name prep --wait --push cat.stl --pull -- 3d prep cat.stl --out ../out
  3d remote run --no-heavy -- nproc
  3d remote pull --project chekhov --sub logs --to ./logs
  3d remote exec --project chekhov -- ls out
  3d remote status --project chekhov"""

_BOOL_FLAGS = {"--wait", "--no-heavy", "--no-pull-on-fail"}
_VALUE_FLAGS = {"--host", "--project", "--name", "--to", "--sub"}


class _Opts:
    def __init__(self) -> None:
        self.host: str | None = None
        self.project: str | None = None
        self.name = "job"
        self.wait = False
        self.use_heavy = True
        self.pull_on_fail = True
        self.pull: str | None = None
        self.to = "./remote-out"
        self.sub = "out"
        self.push: list[str] = []
        self.paths: list[str] = []
        self.cmd: list[str] = []


def _parse(argv: list[str]) -> _Opts:
    o = _Opts()
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            o.cmd = argv[i + 1 :]
            break
        if tok in _BOOL_FLAGS:
            if tok == "--wait":
                o.wait = True
            elif tok == "--no-heavy":
                o.use_heavy = False
            else:
                o.pull_on_fail = False
            i += 1
        elif tok == "--push":
            o.push.append(_value(argv, i, tok))
            i += 2
        elif tok == "--pull":
            nxt = argv[i + 1] if i + 1 < len(argv) else "--"
            if nxt.startswith("--"):
                o.pull = "out"
                i += 1
            else:
                o.pull = nxt
                i += 2
        elif tok in _VALUE_FLAGS:
            val = _value(argv, i, tok)
            setattr(o, tok[2:], val)
            i += 2
        elif tok.startswith("-"):
            raise InvalidArgument(
                tok,
                tok,
                sorted(_BOOL_FLAGS | _VALUE_FLAGS | {"--push", "--pull"}),
                command="remote",
            )
        else:
            o.paths.append(tok)
            i += 1
    return o


def _value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv):
        raise UsageError(f"{flag} needs a value", command="remote")
    return argv[i + 1]


def _exec(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, check=False).returncode  # noqa: S603 — argv built by cli.remote, no shell
    except FileNotFoundError as exc:
        raise MissingDependency(
            argv[0],
            install="sudo apt-get install openssh-client rsync  /  brew install rsync",
            command="remote",
        ) from exc


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    hosts, default = remote.load_hosts()
    if sub == "hosts":
        for name, h in sorted(hosts.items()):
            mark = " (default)" if name == default else ""
            print(f"{name}{mark}: {h.ssh}  root=~/{h.root}  heavy={h.heavy or '-'}")
        if not hosts:
            print(f"no hosts configured; create {remote.DEFAULT_CONFIG}")
        return 0
    if sub not in ("push", "run", "pull", "exec", "status"):
        raise InvalidArgument(
            "subcommand",
            sub,
            ["hosts", "push", "run", "pull", "exec", "status"],
            command="remote",
        )
    o = _parse(rest)
    host = remote.pick_host(hosts, default, o.host)
    project = o.project or remote.default_project()
    if sub == "push":
        if not o.paths:
            raise UsageError("push needs at least one PATH", command="remote")
        return _exec(remote.rsync_push_argv(host, project, o.paths))
    if sub == "pull":
        return _exec(remote.rsync_pull_argv(host, project, o.sub, o.to))
    if sub == "status":
        return _exec(remote.ssh_status_argv(host, project))
    if not o.cmd:
        raise UsageError(f"{sub} needs the command after `--`", command="remote")
    if sub == "exec":
        return _exec(
            remote.ssh_run_argv(
                host, project, o.cmd, name=o.name, wait=False, use_heavy=False
            )
        )
    if o.push:
        rc = _exec(remote.rsync_push_argv(host, project, o.push))
        if rc != 0:
            print(
                f"remote: push failed (rsync exit {rc}); not running the command",
                file=sys.stderr,
            )
            return rc
    rc = _exec(
        remote.ssh_run_argv(
            host, project, o.cmd, name=o.name, wait=o.wait, use_heavy=o.use_heavy
        )
    )
    if o.pull is not None and (rc == 0 or o.pull_on_fail):
        pulled = _exec(remote.rsync_pull_argv(host, project, o.pull, o.to))
        if pulled != 0:
            print(f"remote: pull failed (rsync exit {pulled})", file=sys.stderr)
            return rc or pulled
    return rc


COMMAND = Command(
    name="remote",
    group="ENVIRONMENT",
    summary="push files to an ssh heavy box, run under its heavy lock, pull results",
    usage=USAGE,
    run=run,
)
