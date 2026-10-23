"""runner.py — the command-runner abstraction `3d printers fw` talks to printers' shells through.

WHAT: `CommandRunner` is the one seam between the firmware engine and `ssh`: it runs a read-only
  probe command, a documented install script or a file pull on a printer and returns
  `CommandResult(rc, stdout, stderr)`. `SshRunner` is the real implementation (the `ssh` binary,
  key authentication only); tests inject a recording fake, so no test ever opens a connection.

WHY: every firmware runbook ends in "ssh to the printer and run X". A runner interface keeps that
  testable (the fake records every command, so a dry run can be proven to send nothing) and keeps
  the secrets question out of the code: `BatchMode=yes` means ssh never prompts and never takes a
  password, so 3d stores none — the user's key (or agent) is the only credential. The vendor
  default password (e.g. the root password Creality documents) is only DOCUMENTED in the profile
  so the user can install their key once by hand.

ACCESSED VIA: `printer_fw.probes` (ssh probes), `printer_fw.backup` (file pulls) and
  `printer_fw.engine` (ssh steps); `printers_cli/fw.py` builds the default `SshRunner`.

INVARIANTS: stdlib only. A missing `ssh` binary is `MissingDependency` (exit 127), never a
  traceback. A script is fed to `sh -s` on stdin (no quoting of multi-line text through argv); an
  argv list is shell-quoted into one remote command. Timeouts kill the ssh process and are a
  `CommandResult` with rc 124, so callers treat them like any other failed command.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from errors import MissingDependency

TIMEOUT_RC = 124  # the `timeout(1)` convention: the command did not finish in time
SSH_ERROR_RC = 255  # ssh's own "could not connect / authenticate" status (a remote command may also exit 255)


@dataclass(frozen=True)
class CommandResult:
    rc: int
    stdout: bytes = b""
    stderr: bytes = b""

    @classmethod
    def text(cls, rc: int, stdout: str = "", stderr: str = "") -> CommandResult:
        return cls(rc, stdout.encode(), stderr.encode())

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.rc == 0


class CommandRunner(Protocol):
    """Run `command` on `user@host` and return what it printed.

    `command` is a script (str, fed to `sh -s`) or an argv list (quoted into one remote command);
    `stdin` is only valid with an argv list. Never raises for a failing command: the rc says so."""

    def run(
        self,
        host: str,
        user: str,
        command: str | Sequence[str],
        *,
        timeout: float,
        port: int | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult: ...

    def check_available(self) -> None:
        """Raise MissingDependency when ssh cannot be run at all (checked before a plan starts)."""
        ...


class SshRunner:
    """`ssh -o BatchMode=yes` — key/agent authentication only, never a password prompt."""

    def __init__(self, binary: str = "ssh") -> None:
        self.binary = binary

    def _resolve(self) -> str:
        path = shutil.which(self.binary)
        if path is None:
            raise MissingDependency(
                "ssh",
                install="apt install openssh-client  (macOS: it ships with the OS)",
                degrades="`3d printers fw` ssh probes, backups and steps cannot run",
                command="printers fw",
            )
        return path

    def check_available(self) -> None:
        self._resolve()

    def run(
        self,
        host: str,
        user: str,
        command: str | Sequence[str],
        *,
        timeout: float,
        port: int | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        ssh = self._resolve()
        argv = [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(timeout))}",
        ]
        if port is not None:
            argv += ["-p", str(port)]
        argv.append(f"{user}@{host}")
        if isinstance(command, str):
            if stdin is not None:
                raise ValueError(
                    "a script is fed on stdin; pass stdin only with an argv list"
                )
            argv += ["sh", "-s"]
            data: bytes | None = command.encode()
        else:
            argv.append(" ".join(shlex.quote(a) for a in command))
            data = stdin
        try:
            done = subprocess.run(
                argv, input=data, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                TIMEOUT_RC, exc.stdout or b"", (exc.stderr or b"") + b"ssh timed out"
            )
        except OSError as exc:
            return CommandResult(SSH_ERROR_RC, b"", str(exc).encode())
        return CommandResult(done.returncode, done.stdout, done.stderr)
