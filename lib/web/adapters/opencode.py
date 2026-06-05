#!/usr/bin/env python3
"""opencode.py — BEST-EFFORT adapter for opencode sessions.

opencode does NOT write a single appendable JSONL transcript. It keeps a SQLite db plus a
`storage/` tree of per-session JSON files:
  ~/.local/share/opencode/storage/session/<...>/<ses_id>.json   (session metadata)
  ~/.local/share/opencode/storage/message/<ses_id>/<msg_id>.json
  ~/.local/share/opencode/storage/part/<ses_id>/<...>.json

Verified message shape: { id, sessionID, role, time:{created}, agent, model:{...} }.
The message *text* lives in the part files, whose schema we have not fully reverse-
engineered. So this adapter is intentionally best-effort:

  - discover():  enumerate message-dir sessions (newest by mtime).
  - tail():      poll the session's message dir for NEW message json files and emit a
                 normalized event per message (role + best-effort text from any "text"
                 field found in the message or its parts). New files only → behaves like
                 a tail.

TODO(opencode): fully parse storage/part/<ses>/*.json for the assistant/tool text and
tool-call file paths once the part schema is pinned down. Until then `text` may be a
role/agent summary rather than the full message body. A consumer that needs the raw bytes
can fall back to the RawAdapter on the db/json files.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import AsyncIterator

from .base import AgentEvent, LogAdapter, SessionRef, collect_paths, dedupe

_MAX_TEXT = 4000


class OpencodeAdapter(LogAdapter):
    id = "opencode"

    def __init__(self, home: pathlib.Path | None = None, *, limit: int = 200) -> None:
        h = home or pathlib.Path.home()
        # cross-platform: respect XDG if set, else the default share dir
        self._root = h / ".local" / "share" / "opencode" / "storage"
        self._limit = limit

    async def discover(self) -> list[SessionRef]:
        refs: list[SessionRef] = []
        msg_root = self._root / "message"
        if not msg_root.is_dir():
            return refs
        for sdir in msg_root.iterdir():
            if not sdir.is_dir():
                continue
            try:
                mtime = max((f.stat().st_mtime for f in sdir.glob("*.json")), default=0.0)
            except OSError:
                mtime = 0.0
            refs.append(SessionRef(
                source=self.id, session_id=sdir.name, path=sdir,
                cwd=None, mtime=mtime, label=sdir.name,
            ))
        refs.sort(key=lambda r: r.mtime, reverse=True)
        return refs[: self._limit]

    async def tail(
        self, ref: SessionRef, *, from_start: bool = True, poll: float = 0.5
    ) -> AsyncIterator[AgentEvent]:
        sdir = ref.path
        seen: set[str] = set()
        seq = 0
        first = True
        while True:
            try:
                files = sorted(sdir.glob("*.json"), key=lambda f: f.stat().st_mtime)
            except OSError:
                files = []
            for f in files:
                if f.name in seen:
                    continue
                seen.add(f.name)
                if first and not from_start:
                    continue  # skip pre-existing on follow-from-EOF
                ev = self._parse_message_file(f, ref.session_id)
                if ev is not None:
                    seq += 1
                    ev.seq = seq
                    yield ev
            first = False
            await asyncio.sleep(poll)

    def _parse_message_file(
        self, f: pathlib.Path, session_id: str
    ) -> AgentEvent | None:
        try:
            obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        role = str(obj.get("role", ""))
        created = obj.get("time", {})
        ts = ""
        if isinstance(created, dict) and isinstance(created.get("created"), (int, float)):
            ts = _epoch_ms_to_iso(float(created["created"]))
        # best-effort text: any 'text' anywhere + part files for this message
        text = _find_text(obj) or f"{role} message ({obj.get('agent', '')})".strip()
        paths: list[str] = []
        collect_paths(obj, paths)
        self._merge_parts(f, obj, paths, text_acc := [text])
        return AgentEvent(
            ts=ts, source="opencode", session_id=session_id, kind="text",
            role=role or "assistant", text=_trim("\n".join(t for t in text_acc if t)),
            cwd=None, paths=dedupe(paths),
        )

    def _merge_parts(
        self, msg_file: pathlib.Path, obj: dict[str, object],
        paths: list[str], text_acc: list[str]
    ) -> None:
        """Pull text/paths from the message's part files if the part dir exists."""
        ses = str(obj.get("sessionID") or msg_file.parent.name)
        mid = str(obj.get("id") or msg_file.stem)
        part_dir = self._root / "part" / ses
        if not part_dir.is_dir():
            return
        for pf in part_dir.glob(f"{mid}*.json"):
            try:
                pobj = json.loads(pf.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            t = _find_text(pobj)
            if t:
                text_acc.append(t)
            collect_paths(pobj, paths)


def _find_text(obj: object) -> str | None:
    if isinstance(obj, dict):
        v = obj.get("text")
        if isinstance(v, str) and v.strip():
            return v
        for vv in obj.values():
            r = _find_text(vv)
            if r:
                return r
    elif isinstance(obj, list):
        for vv in obj:
            r = _find_text(vv)
            if r:
                return r
    return None


def _epoch_ms_to_iso(ms: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).isoformat()


def _trim(s: str) -> str:
    return s if len(s) <= _MAX_TEXT else s[:_MAX_TEXT] + " …"
