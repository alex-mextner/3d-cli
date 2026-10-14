"""`3d printops token` — mint, list and revoke 3d-api bearer tokens (sha256 in the tokens file).

Runs where the tokens file is: inside the 3d-api container on the Odroid
(`docker exec printops-api 3d printops token …`), or from the workstation with `--ssh HOST`,
which runs exactly that over ssh. The API re-reads the file on change; no restart needed.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from errors import ThreeDError, UsageError
from printops import tokens
from printops.cli import Sub
from printops.cli._common import parse
from printops.settings import Settings

CONTAINER = "printops-api"

USAGE = f"""3d printops token create NAME --scopes SCOPES [--file PATH | --ssh HOST]
3d printops token list [--file PATH | --ssh HOST]
3d printops token revoke NAME [--file PATH | --ssh HOST]
  Manage 3d-api bearer tokens. Only sha256(token) is stored; `create` prints the token ONCE.
  Scopes: {', '.join(tokens.SCOPES)} (independent; give every one the client needs).
  The API picks up changes immediately (it re-reads the file when it changes).

Options:
  --scopes LIST  comma-separated scopes for `create`, e.g. read or read,write
  --file PATH    tokens file (default: $PRINTOPS_TOKENS_FILE, else tokens.json next to $PRINTOPS_DB;
                 on the Odroid: /home/ultra/printops/data/api/tokens.json = /data/tokens.json in the container)
  --ssh HOST     run the same command in the `{CONTAINER}` container on HOST over ssh
                 (e.g. ultra@192.168.0.39); the token is printed here, never stored locally

Examples:
  3d printops token create bot --scopes read,write --ssh ultra@192.168.0.39
  3d printops token create ha --scopes read --ssh ultra@192.168.0.39
  3d printops token list --ssh ultra@192.168.0.39
  3d printops token revoke bot --ssh ultra@192.168.0.39
  3d printops token create cli --scopes read --file ./tokens.json"""


def _remote(host: str, argv: list[str]) -> int:
    if not host or host.startswith("-"):
        raise UsageError(f"--ssh needs USER@HOST, got {host!r}", command="printops token")
    remote = ["docker", "exec", CONTAINER, "3d", "printops", "token", *argv]
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", host, shlex.join(remote)], check=False).returncode
    except FileNotFoundError as e:
        raise ThreeDError("ssh is not installed", command="printops token") from e


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    if action not in ("create", "list", "revoke"):
        raise UsageError(f"unknown token action {action!r}; accepted: create, list, revoke",
                         command="printops token", remediation=["See `3d printops token --help`."])
    n_pos = 0 if action == "list" else 1
    values = {"--file", "--ssh"} | ({"--scopes"} if action == "create" else set())
    p = parse(rest, f"token {action}", values=values, n_pos=n_pos)
    if "--ssh" in p.opts:
        if "--file" in p.opts:
            raise UsageError("--file and --ssh are exclusive (the remote side uses its container's file)",
                             command="printops token")
        forwarded = [action, *p.pos] + (["--scopes", p.opts["--scopes"]] if "--scopes" in p.opts else [])
        return _remote(p.opts["--ssh"], forwarded)

    path = Path(p.opts["--file"]).expanduser() if "--file" in p.opts else Settings.from_env().tokens_file
    try:
        if action == "create":
            if "--scopes" not in p.opts:
                raise UsageError("create needs --scopes (e.g. --scopes read,write)", command="printops token")
            plaintext, rec = tokens.create(path, p.pos[0], tokens.parse_scopes(p.opts["--scopes"]))
            print(plaintext)
            print(f"token {rec.name!r} scopes {','.join(rec.scopes)} stored as sha256 in {path}; "
                  "this is the only time it is shown", file=sys.stderr)
        elif action == "list":
            records = tokens.load(path)
            if not records:
                print(f"no tokens in {path}")
            for rec in records:
                print(f"{rec.name:<20} {','.join(rec.scopes):<28} {rec.created_at}  sha256 {rec.sha256[:12]}…")
        elif not tokens.revoke(path, p.pos[0]):
            raise UsageError(f"no token named {p.pos[0]!r} in {path}", command="printops token")
        else:
            print(f"revoked {p.pos[0]!r} in {path}")
    except tokens.TokenError as e:
        raise UsageError(str(e), command="printops token") from e
    return 0


SUB = Sub(name="token", summary="create/list/revoke 3d-api bearer tokens", usage=USAGE, run=run)
