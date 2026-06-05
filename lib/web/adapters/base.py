#!/usr/bin/env python3
"""base.py — the LogAdapter interface + the normalized agent-activity event.

The agent-activity panel ("the key feature") streams a SINGLE normalized event shape
regardless of which AI coding tool produced it. Every concrete adapter (claude, codex,
opencode, raw) discovers its own session files and tails them, parsing whatever native
format into `AgentEvent`s.

Design notes (the schema is derived from REAL transcripts, not recall):
  - Claude Code writes JSONL: one object per line with top-level `type`
    (user/assistant/attachment/...), `sessionId`, `cwd`, `gitBranch`, `timestamp`, and a
    `message` { role, content } where content is a str or a list of blocks
    (thinking / text / tool_use{name,id,input} / tool_result{tool_use_id,content,is_error}).
    Subagent transcripts live as `*.output` JSONL with the same message schema.
  - Codex writes JSONL rollouts: { timestamp, type, payload } where payload.type is
    session_meta (carries cwd) / event_msg{agent_message,exec_command_end,...} /
    response_item{message,function_call{name,arguments},...}.
  - opencode writes a SQLite db + a storage/ tree of per-session JSON files; no clean
    tail, so its adapter is best-effort (poll storage dir) with a raw-tail fallback.

`paths` is the gold field for AUTO-ASSOCIATING an agent session with a project: it
collects every filesystem path the event references (tool inputs, cwd, commands). The
server matches those against the scanned project roots.
"""
from __future__ import annotations

import abc
import dataclasses
import pathlib
from collections.abc import AsyncIterator


@dataclasses.dataclass(slots=True)
class AgentEvent:
    """One normalized agent-activity event, JSON-serializable for SSE."""

    ts: str                       # ISO-8601 timestamp (best-effort; "" if unknown)
    source: str                   # adapter id: "claude" | "codex" | "opencode" | "raw"
    session_id: str               # adapter-local session identifier
    kind: str                     # text | thinking | tool_use | tool_result | meta | raw
    role: str                     # user | assistant | system | tool | ""
    text: str                     # human-readable content / summary (may be truncated)
    tool_name: str | None = None  # for kind == tool_use / tool_result
    is_error: bool = False        # for tool_result
    paths: list[str] = dataclasses.field(default_factory=list)  # referenced fs paths
    cwd: str | None = None        # the agent's working dir, when known
    seq: int = 0                  # monotonic per-stream sequence (assigned by the tailer)

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(slots=True)
class SessionRef:
    """A discovered agent session: where its log lives + what we know about it."""

    source: str                   # adapter id
    session_id: str               # adapter-local id (often the file stem)
    path: pathlib.Path            # the log file (or representative file) to tail
    cwd: str | None = None        # working dir, if cheaply known from the file
    mtime: float = 0.0            # last-modified epoch seconds (for activity/sorting)
    label: str = ""               # short human label (branch, task id, ...)


class LogAdapter(abc.ABC):
    """Interface every agent-log source implements.

    Lifecycle: the server calls `discover()` to enumerate sessions, then `tail()` on a
    chosen session to get an async stream of normalized events (existing lines first,
    then new ones as they are appended). Adapters never raise on a missing source — an
    absent log dir yields an empty `discover()`.
    """

    #: stable adapter id, used as AgentEvent.source and in the API
    id: str = "base"

    @abc.abstractmethod
    async def discover(self) -> list[SessionRef]:
        """Enumerate currently-known sessions for this source (newest first)."""

    @abc.abstractmethod
    async def tail(
        self, ref: SessionRef, *, from_start: bool = True, poll: float = 0.5
    ) -> AsyncIterator[AgentEvent]:
        """Yield normalized events for one session.

        from_start=True replays the whole file then follows; False starts at EOF.
        The iterator runs until cancelled. Implementations must be cancellation-safe.
        """
        raise NotImplementedError
        yield  # pragma: no cover  (make this an async generator for typing)


def collect_paths(value: object, out: list[str]) -> None:
    """Recursively pull anything that looks like an absolute filesystem path out of a
    tool-input blob. Conservative: only keeps strings that start with '/' (POSIX abs) so
    we don't mistake flags/ids for paths. Deduped by the caller."""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("/") and len(s) > 1 and "\n" not in s:
            # tool args sometimes embed a path inside a longer command string; take the
            # leading token if it is path-like, else the whole string if it is a path.
            out.append(s.split()[0] if " " in s and not s[1:].split()[0].endswith("/") else s)
    elif isinstance(value, dict):
        for v in value.values():
            collect_paths(v, out)
    elif isinstance(value, list):
        for v in value:
            collect_paths(v, out)


def dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
