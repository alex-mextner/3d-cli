#!/usr/bin/env python3
"""claude.py — fully-working adapter for Claude Code transcripts.

Discovers two kinds of JSONL transcript:
  1. main sessions:  ~/.claude/projects/<slug>/<session-uuid>.jsonl
  2. subagent/task:  /private/tmp/claude-*/<slug>/<session>/tasks/*.output   and
                     /private/tmp/claude-*/<slug>/<session>/subagents/**/*.jsonl

Both share the same per-line message schema (verified against real files):
  { type, sessionId, cwd, gitBranch, timestamp, message:{role, content} }
content is either a str or a list of blocks:
  {type:text,text} | {type:thinking,thinking} |
  {type:tool_use,name,id,input} | {type:tool_result,tool_use_id,content,is_error}

Each parsed line becomes one (or, for assistant turns with mixed blocks, several)
`AgentEvent`s. File paths are pulled from tool_use inputs + cwd for project association.
"""
from __future__ import annotations

import glob
import json
import os
import pathlib
from collections.abc import AsyncIterator

from .base import AgentEvent, LogAdapter, SessionRef, collect_paths, dedupe
from .tailer import tail_lines

_MAX_TEXT = 4000  # truncate long blocks for the stream


class ClaudeAdapter(LogAdapter):
    id = "claude"

    #: cap discovery to the N most-recently-modified transcripts (a workspace can hold
    #: thousands; only the freshest are plausibly live). Override via the ctor.
    def __init__(self, home: pathlib.Path | None = None, *, limit: int = 200) -> None:
        self._home = home or pathlib.Path.home()
        self._limit = limit
        # cache the cwd peek keyed by (path, mtime) so an unchanged file is never reopened
        self._cwd_cache: dict[tuple[str, float], str | None] = {}

    # -- discovery -----------------------------------------------------------
    async def discover(self) -> list[SessionRef]:
        # cheap pass: stat-only, sort by mtime, then peek cwd on just the freshest N.
        cands: list[tuple[pathlib.Path, str]] = []
        proj_root = self._home / ".claude" / "projects"
        if proj_root.is_dir():
            cands += [(f, f.parent.name) for f in proj_root.glob("*/*.jsonl")]
        for base in glob.glob("/private/tmp/claude-*") + glob.glob("/tmp/claude-*"):
            bp = pathlib.Path(base)
            if not bp.is_dir():
                continue
            cands += [(f, f.stem) for f in bp.glob("*/*/tasks/*.output")]
            cands += [(f, f.stem) for f in bp.glob("*/*/subagents/**/*.jsonl")]

        def mt(f: pathlib.Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0

        cands.sort(key=lambda c: mt(c[0]), reverse=True)
        refs = [self._ref_for(f, label=lbl) for f, lbl in cands[: self._limit]]
        return refs

    def _ref_for(self, f: pathlib.Path, label: str) -> SessionRef:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = 0.0
        ckey = (str(f), mtime)
        if ckey in self._cwd_cache:
            cwd = self._cwd_cache[ckey]
        else:
            cwd = self._peek_cwd(f)
            self._cwd_cache[ckey] = cwd
        return SessionRef(
            source=self.id,
            session_id=f.stem,
            path=f,
            cwd=cwd,
            mtime=mtime,
            label=label,
        )

    @staticmethod
    def _peek_cwd(f: pathlib.Path) -> str | None:
        """Cheaply read the cwd from the first lines that carry it."""
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for _ in range(8):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(o, dict):
                        continue
                    cwd = o.get("cwd")
                    if isinstance(cwd, str):
                        return cwd
        except OSError:
            return None
        return None

    # -- tailing -------------------------------------------------------------
    async def tail(
        self, ref: SessionRef, *, from_start: bool = True, poll: float = 0.5
    ) -> AsyncIterator[AgentEvent]:
        seq = 0
        async for line in tail_lines(ref.path, from_start=from_start, poll=poll):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            for ev in self.parse_line(obj, ref.session_id):
                seq += 1
                ev.seq = seq
                yield ev

    # -- parsing (pure, unit-tested) ----------------------------------------
    @staticmethod
    def parse_line(obj: dict[str, object], session_id: str) -> list[AgentEvent]:
        """Turn one transcript object into zero or more normalized events."""
        ts = str(obj.get("timestamp") or "")
        cwd = obj.get("cwd")
        cwd_s = cwd if isinstance(cwd, str) else None
        sid = str(obj.get("sessionId") or session_id)
        typ = obj.get("type")

        msg = obj.get("message")
        if not isinstance(msg, dict):
            # attachment / queue-operation / last-prompt etc. — emit a thin meta event
            if typ in ("attachment", "queue-operation"):
                return []  # noise; skip from the stream
            return []

        role = str(msg.get("role") or (typ if isinstance(typ, str) else ""))
        content = msg.get("content")
        events: list[AgentEvent] = []

        def base_paths(extra: object = None) -> list[str]:
            acc: list[str] = []
            if cwd_s:
                acc.append(cwd_s)
            if extra is not None:
                collect_paths(extra, acc)
            return dedupe(acc)

        if isinstance(content, str):
            events.append(
                AgentEvent(
                    ts=ts, source="claude", session_id=sid, kind="text",
                    role=role, text=_trim(content), cwd=cwd_s, paths=base_paths(),
                )
            )
            return events

        if not isinstance(content, list):
            return events

        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                events.append(AgentEvent(
                    ts=ts, source="claude", session_id=sid, kind="text",
                    role=role, text=_trim(str(block.get("text", ""))),
                    cwd=cwd_s, paths=base_paths(),
                ))
            elif bt == "thinking":
                events.append(AgentEvent(
                    ts=ts, source="claude", session_id=sid, kind="thinking",
                    role=role, text=_trim(str(block.get("thinking", ""))),
                    cwd=cwd_s, paths=base_paths(),
                ))
            elif bt == "tool_use":
                name = str(block.get("name", "tool"))
                inp = block.get("input")
                events.append(AgentEvent(
                    ts=ts, source="claude", session_id=sid, kind="tool_use",
                    role=role or "assistant", text=_summarize_tool(name, inp),
                    tool_name=name, cwd=cwd_s, paths=base_paths(inp),
                ))
            elif bt == "tool_result":
                c = block.get("content")
                txt = c if isinstance(c, str) else json.dumps(c)[:_MAX_TEXT]
                events.append(AgentEvent(
                    ts=ts, source="claude", session_id=sid, kind="tool_result",
                    role="tool", text=_trim(txt), is_error=bool(block.get("is_error")),
                    cwd=cwd_s, paths=base_paths(),
                ))
        return events


def _trim(s: str) -> str:
    return s if len(s) <= _MAX_TEXT else s[:_MAX_TEXT] + " …"


def _summarize_tool(name: str, inp: object) -> str:
    """One-line human summary of a tool call for the timeline."""
    if isinstance(inp, dict):
        for key in ("file_path", "path", "pattern", "command", "url", "query"):
            v = inp.get(key)
            if isinstance(v, str) and v:
                short = v if len(v) <= 200 else v[:200] + " …"
                base = os.path.basename(short) if key in ("file_path", "path") else short
                return f"{name}: {base}"
    return name
