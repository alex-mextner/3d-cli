#!/usr/bin/env python3
"""Agent-log adapters: normalize Claude / Codex / opencode / raw transcripts to events."""
from __future__ import annotations

from .base import AgentEvent, LogAdapter, SessionRef, collect_paths, dedupe
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpencodeAdapter
from .raw import RawAdapter

__all__ = [
    "AgentEvent",
    "LogAdapter",
    "SessionRef",
    "collect_paths",
    "dedupe",
    "ClaudeAdapter",
    "CodexAdapter",
    "OpencodeAdapter",
    "RawAdapter",
    "ALL_ADAPTERS",
]

#: adapters the server activates by default
ALL_ADAPTERS: list[type[LogAdapter]] = [ClaudeAdapter, CodexAdapter, OpencodeAdapter]
