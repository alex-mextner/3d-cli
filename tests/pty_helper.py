"""Run a command on a real pseudo-terminal and read its screen back as text (#578 smoke tests).

`run_pty(argv, ...)` starts a child on a pty of a given size, feeds it keys when a pattern shows up on the screen
(`script=[(pattern, keys), ...]`; never a blind sleep), and renders everything it wrote through `pyte` (a terminal
emulator), so a test asserts on what a person would SEE: the final screen and every screen along the way."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pyte


@dataclass
class PtyRun:
    exit_code: int
    raw: bytes
    screens: list[str] = field(
        default_factory=list
    )  # the screen when each step fired (before its keys), then the end
    after: list[str] = field(
        default_factory=list
    )  # the screen `settle` seconds after each step's keys

    @property
    def final(self) -> str:
        return self.screens[-1] if self.screens else ""

    def seen(self, text: str) -> bool:
        return any(text in s for s in self.screens)


def _text(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display).rstrip("\n")


def run_pty(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    script: Sequence[tuple[str, str]] = (),
    size: tuple[int, int] = (120, 40),
    timeout: float = 60.0,
    settle: float = 0.4,
) -> PtyRun:
    """Run `argv` on a pty of `size` (columns, rows). `script` is `(pattern, keys)`: when the regex `pattern` is on
    the screen, `keys` is typed (`\\r` Enter, `\\x1b` Escape, `\\x1b[B` Down). A snapshot is taken at each step.
    The next step waits `settle` seconds after the previous keys (a lone Escape is only an Escape when nothing
    follows it within the terminal library's escape timeout). Kills the child when `timeout` passes (the test then fails on its assertions, never hangs)."""
    cols, rows = size
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    fd, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(  # noqa: S603 — the CLI under test
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    raw = b""
    snapshots: list[str] = []
    after: list[str] = []
    settled = True
    pending = list(script)
    not_before = 0.0
    deadline = time.monotonic() + timeout
    exit_code = -1
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                raw += chunk
                stream.feed(chunk)
            if not settled and time.monotonic() >= not_before:
                after.append(_text(screen))
                settled = True
            if (
                pending
                and settled
                and time.monotonic() >= not_before
                and re.search(pending[0][0], _text(screen))
            ):
                snapshots.append(_text(screen))
                os.write(fd, pending.pop(0)[1].encode())
                not_before = time.monotonic() + settle
                settled = False
        else:
            proc.kill()
        exit_code = proc.wait()
    finally:
        os.close(fd)
    snapshots.append(_text(screen))
    return PtyRun(exit_code, raw, snapshots, after)
