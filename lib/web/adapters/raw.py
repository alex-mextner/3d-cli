#!/usr/bin/env python3
"""raw.py — the universal fallback adapter: tail ANY text/JSONL file as raw events.

Used when a source's structured format is unknown or a user points the dashboard at an
arbitrary log. Each appended line becomes one `kind="raw"` event (JSON line objects are
flattened to a compact string; plain lines pass through). No discovery — the caller
constructs a SessionRef pointing at the file to follow.
"""
from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator

from .base import AgentEvent, LogAdapter, SessionRef, collect_paths, dedupe
from .tailer import tail_lines

_MAX_TEXT = 4000


class RawAdapter(LogAdapter):
    id = "raw"

    async def discover(self) -> list[SessionRef]:
        return []  # raw is explicitly-targeted, not discovered

    async def tail(
        self, ref: SessionRef, *, from_start: bool = True, poll: float = 0.5
    ) -> AsyncIterator[AgentEvent]:
        seq = 0
        async for line in tail_lines(ref.path, from_start=from_start, poll=poll):
            seq += 1
            paths: list[str] = []
            text = line
            try:
                obj = json.loads(line)
                collect_paths(obj, paths)
                text = json.dumps(obj)[:_MAX_TEXT]
            except json.JSONDecodeError:
                collect_paths(line, paths)
            yield AgentEvent(
                ts="", source="raw", session_id=ref.session_id, kind="raw",
                role="", text=text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + " …",
                cwd=ref.cwd, paths=dedupe(paths), seq=seq,
            )
