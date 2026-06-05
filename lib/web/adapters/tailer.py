#!/usr/bin/env python3
"""tailer.py — async line tailing for JSONL transcript files.

Shared by the claude / codex / raw adapters: open a growing file, replay existing lines,
then poll for appended lines (a portable `tail -f` — no inotify/kqueue dependency, which
keeps it cross-platform per the repo ethos). Cancellation-safe: the loop awaits an
`asyncio.sleep` between polls, so cancelling the task stops it cleanly.

Handles the realities of a file written by another process:
  - a final partial line (no trailing newline) is held back until completed;
  - truncation / rotation (file shrank) restarts from the top;
  - the file may not exist yet (waits for it).
"""
from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterator


async def tail_lines(
    path: pathlib.Path, *, from_start: bool = True, poll: float = 0.5
) -> AsyncIterator[str]:
    """Yield complete (newline-terminated) lines from `path`, following appends.

    Runs forever until the consuming task is cancelled.
    """
    pos = 0
    buf = ""
    inode: int | None = None

    if not from_start:
        try:
            pos = path.stat().st_size
        except OSError:
            pos = 0

    while True:
        try:
            st = path.stat()
        except OSError:
            # file gone / not created yet — wait and retry without crashing
            await asyncio.sleep(poll)
            continue

        # rotation / truncation: the file got smaller or its inode changed → restart
        if inode is not None and (st.st_ino != inode or st.st_size < pos):
            pos = 0
            buf = ""
        inode = st.st_ino

        if st.st_size > pos:
            # read the new bytes off the event loop's thread to stay non-blocking on
            # large transcripts.
            chunk = await asyncio.to_thread(_read_from, path, pos)
            pos += len(chunk)
            buf += chunk.decode("utf-8", errors="replace")
            while True:
                nl = buf.find("\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1 :]
                if line:
                    yield line

        await asyncio.sleep(poll)


def _read_from(path: pathlib.Path, pos: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(pos)
        return fh.read()
