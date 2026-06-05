"""errors.py — structured, actionable error types for the `3d` CLI (ROADMAP §1).

Every error a `3d` command raises should tell the user, in this order:

  (1) WHAT failed and WHY        — the real cause, not a bare "command failed".
  (2) HOW to fix it              — a concrete, copy-pasteable remediation (a command
                                   to run / a file to edit), step by step.
  (3) the ACCEPTED values        — when the input was invalid (e.g. "got --plane=ZZ;
                                   accepted: YZ, XZ, XY").
  (4) the INSTALL command + tier — when a dependency is missing: the exact install
                                   line for THIS OS, and which capability degrades.

The dispatcher (`lib/cli/dispatch.py`) catches `ThreeDError`, prints `.render()` to
stderr (no bare traceback) and exits with `.exit_code`. Commands raise these instead
of calling `sys.exit()` with an ad-hoc message, so the UX is uniform.

Exit-code contract (preserved from the bash era):
    127  MissingDependency   — a required tool/lib is absent.
    2    InvalidArgument / UsageError / InputNotFound — bad invocation.
    1    GateFailure         — a verification gate said FAIL (geometry/printability/…).
    0    success.
"""
from __future__ import annotations

import os
from typing import Sequence


class ThreeDError(Exception):
    """Base for every structured CLI error.

    Carries a `command` (the subcommand name, for the header), a human cause, an
    optional list of remediation steps, and an exit code. `render()` formats the
    whole thing for stderr.

    Example:
        raise ThreeDError("unexpected failure", command="render", remediation=["Check logs"])
    """

    exit_code: int = 1

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        remediation: Sequence[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.command = command
        self.remediation = list(remediation) if remediation else []

    # ---- rendering ----------------------------------------------------------
    def _header(self) -> str:
        who = self.command or "3d"
        return f"{who}: {self.message}"

    def render(self, *, color: bool | None = None) -> str:
        """Return the full multi-line error message for stderr."""
        if color is None:
            color = os.isatty(2)
        red = "\033[31m" if color else ""
        bold = "\033[1m" if color else ""
        dim = "\033[2m" if color else ""
        z = "\033[0m" if color else ""

        lines = [f"{red}{bold}{self._header()}{z}"]
        for step in self.remediation:
            lines.append(f"  {dim}->{z} {step}")
        return "\n".join(lines)


class UsageError(ThreeDError):
    """Wrong invocation form (missing positional, mode conflict). Exit 2.

    Example:
        raise UsageError("missing model file before options", command="slice")
    """

    exit_code = 2


class InputNotFound(ThreeDError):
    """A required input file does not exist. Exit 2.

    Example:
        raise InputNotFound("part.scad", command="export")
    """

    exit_code = 2

    def __init__(self, path: str, *, command: str | None = None) -> None:
        super().__init__(
            f"file not found: {path}",
            command=command,
            remediation=[
                f"Check the path exists (cwd: {os.getcwd()}). "
                "Provide an absolute path if the file is elsewhere."
            ],
        )


class InvalidArgument(ThreeDError):
    """A flag got a value outside its accepted set. Exit 2.

    Always lists the accepted values (ROADMAP §1.3) so the fix is obvious.
    If no extra remediation is provided, a default pointer to --help is added.

    Example:
        raise InvalidArgument("--plane", "ZZ", ["YZ", "XZ", "XY"], command="render")
    """

    exit_code = 2

    def __init__(
        self,
        flag: str,
        got: str,
        accepted: Sequence[str],
        *,
        command: str | None = None,
        extra: str | None = None,
    ) -> None:
        acc = ", ".join(accepted)
        msg = f"got {flag}={got!r}; accepted: {acc}"
        rem: list[str] = []
        if extra:
            rem.append(extra)
        if not rem:
            help_cmd = f"3d {command}" if command else "3d"
            rem.append(f"Run '{help_cmd} --help' for accepted values.")
        super().__init__(msg, command=command, remediation=rem)
        self.flag = flag
        self.got = got
        self.accepted = list(accepted)


class MissingDependency(ThreeDError):
    """A required external tool or python library is absent. Exit 127.

    Names the exact install command for THIS OS and which capability/tier degrades,
    per ROADMAP §1.4.

    Example:
        raise MissingDependency(
            "OpenSCAD",
            install="brew install --cask openscad",
            degrades="render / export / check cannot run",
            command="render",
        )
    """

    exit_code = 127

    def __init__(
        self,
        dependency: str,
        *,
        install: str,
        degrades: str | None = None,
        command: str | None = None,
    ) -> None:
        msg = f"{dependency} not found"
        rem = [f"Install: {install}"]
        if degrades:
            rem.append(f"Without it: {degrades}")
        else:
            rem.append("Without it: some 3d commands will be unavailable.")
        super().__init__(msg, command=command, remediation=rem)
        self.dependency = dependency
        self.install = install
        self.degrades = degrades


class GateFailure(ThreeDError):
    """A verification gate produced a FAIL verdict. Exit 1.

    The command has already printed the per-gate breakdown; this just sets the
    nonzero exit without re-printing a traceback. `silent=True` suppresses the
    extra header so the command's own report is the last thing the user sees.

    Example:
        raise GateFailure(">>> CHECK: FAIL", command="check", silent=True)
    """

    exit_code = 1

    def __init__(self, message: str, *, command: str | None = None, silent: bool = False) -> None:
        super().__init__(message, command=command)
        self.silent = silent

    def render(self, *, color: bool | None = None) -> str:
        if self.silent:
            return ""
        return super().render(color=color)
