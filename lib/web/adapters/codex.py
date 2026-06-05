#!/usr/bin/env python3
"""codex.py — adapter for Codex CLI rollout transcripts (format known → working).

Codex writes JSONL "rollout" files under:
  ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl
  ~/.codex/archived_sessions/rollout-*.jsonl

Each line is { timestamp, type, payload }. Verified payload shapes:
  type=session_meta : payload.cwd, payload.id              (session start, carries cwd)
  type=event_msg    : payload.type in {agent_message(message), user_message(message),
                                       exec_command_end, web_search_end, token_count, ...}
  type=response_item: payload.type in {message(content[]), reasoning,
                                       function_call(name,arguments), function_call_output}

We normalize agent/user messages → text, function_call → tool_use (args JSON-decoded for
the cwd/cmd → paths), reasoning → thinking. Token-count/meta noise is dropped.
"""
from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator

from .base import AgentEvent, LogAdapter, SessionRef, collect_paths, dedupe
from .tailer import tail_lines

_MAX_TEXT = 4000


class CodexAdapter(LogAdapter):
    id = "codex"

    def __init__(self, home: pathlib.Path | None = None, *, limit: int = 200) -> None:
        self._root = (home or pathlib.Path.home()) / ".codex"
        self._limit = limit
        self._cwd_cache: dict[tuple[str, float], str | None] = {}

    async def discover(self) -> list[SessionRef]:
        cands: list[pathlib.Path] = []
        for sub in ("sessions", "archived_sessions"):
            d = self._root / sub
            if d.is_dir():
                cands += list(d.rglob("rollout-*.jsonl"))

        def mt(f: pathlib.Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0

        cands.sort(key=mt, reverse=True)
        refs: list[SessionRef] = []
        for f in cands[: self._limit]:
            mtime = mt(f)
            ckey = (str(f), mtime)
            if ckey not in self._cwd_cache:
                self._cwd_cache[ckey] = self._peek_cwd(f)
            refs.append(SessionRef(
                source=self.id, session_id=f.stem, path=f,
                cwd=self._cwd_cache[ckey], mtime=mtime, label=f.stem,
            ))
        return refs

    @staticmethod
    def _peek_cwd(f: pathlib.Path) -> str | None:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                line = fh.readline()
                obj = json.loads(line)
            p = obj.get("payload")
            if isinstance(p, dict) and isinstance(p.get("cwd"), str):
                return str(p["cwd"])
        except (OSError, json.JSONDecodeError):
            return None
        return None

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
            ev = self.parse_line(obj, ref.session_id, ref.cwd)
            if ev is not None:
                seq += 1
                ev.seq = seq
                yield ev

    @staticmethod
    def parse_line(
        obj: dict[str, object], session_id: str, cwd: str | None
    ) -> AgentEvent | None:
        ts = str(obj.get("timestamp") or "")
        p = obj.get("payload")
        if not isinstance(p, dict):
            return None
        ptype = p.get("type")
        sid = session_id

        def mk(kind: str, role: str, text: str, tool: str | None = None,
               paths: list[str] | None = None) -> AgentEvent:
            base = [cwd] if cwd else []
            if paths:
                base += paths
            return AgentEvent(
                ts=ts, source="codex", session_id=sid, kind=kind, role=role,
                text=_trim(text), tool_name=tool, cwd=cwd, paths=dedupe(base),
            )

        if ptype == "session_meta":
            c = p.get("cwd")
            return mk("meta", "system", f"session start (cwd={c})") if isinstance(c, str) else mk("meta", "system", "session start")
        if ptype == "agent_message":
            return mk("text", "assistant", str(p.get("message", "")))
        if ptype == "user_message":
            return mk("text", "user", str(p.get("message", "")))
        if ptype == "reasoning":
            return mk("thinking", "assistant", _reasoning_text(p))
        if ptype == "function_call":
            name = str(p.get("name", "tool"))
            args = p.get("arguments")
            paths: list[str] = []
            summary = name
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    collect_paths(parsed, paths)
                    if isinstance(parsed, dict) and isinstance(parsed.get("cmd"), str):
                        summary = f"{name}: {parsed['cmd'][:200]}"
                        if isinstance(parsed.get("workdir"), str):
                            paths.append(parsed["workdir"])
                except json.JSONDecodeError:
                    collect_paths(args, paths)
            return mk("tool_use", "assistant", summary, tool=name, paths=paths)
        if ptype == "message":
            return mk("text", str(p.get("role", "assistant")), _content_text(p.get("content")))
        # token_count, web_search_*, function_call_output, etc. → drop as noise
        return None


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return "\n".join(parts)
    return ""


def _reasoning_text(p: dict[str, object]) -> str:
    summ = p.get("summary")
    if isinstance(summ, list):
        out: list[str] = []
        for s in summ:
            if isinstance(s, dict) and isinstance(s.get("text"), str):
                out.append(s["text"])
            elif isinstance(s, str):
                out.append(s)
        if out:
            return "\n".join(out)
    return _content_text(p.get("content"))


def _trim(s: str) -> str:
    return s if len(s) <= _MAX_TEXT else s[:_MAX_TEXT] + " …"
