"""Adapter parsing tests: a sample transcript line → normalized AgentEvent."""
from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import AsyncGenerator
from typing import cast

import pytest

from web.adapters import ClaudeAdapter, CodexAdapter, RawAdapter, SessionRef
from web.adapters.base import AgentEvent, collect_paths, dedupe


# --- Claude --------------------------------------------------------------
def test_claude_user_string_message() -> None:
    line: dict[str, object] = {
        "type": "user", "sessionId": "S1", "cwd": "/proj/x",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": "do the thing"},
    }
    evs = ClaudeAdapter.parse_line(line, "S1")
    assert len(evs) == 1
    e = evs[0]
    assert e.source == "claude" and e.kind == "text" and e.role == "user"
    assert e.text == "do the thing"
    assert "/proj/x" in e.paths  # cwd captured for association


def test_claude_assistant_tooluse_and_thinking() -> None:
    line: dict[str, object] = {
        "type": "assistant", "sessionId": "S1", "cwd": "/proj/x",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "I will read"},
            {"type": "tool_use", "name": "Read", "id": "t1",
             "input": {"file_path": "/proj/x/model.scad"}},
        ]},
    }
    evs = ClaudeAdapter.parse_line(line, "S1")
    kinds = [e.kind for e in evs]
    assert kinds == ["thinking", "text", "tool_use"]
    tu = evs[2]
    assert tu.tool_name == "Read"
    assert "/proj/x/model.scad" in tu.paths
    assert "model.scad" in tu.text


def test_claude_tool_result_error() -> None:
    line: dict[str, object] = {
        "type": "user", "sessionId": "S1",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "boom", "is_error": True},
        ]},
    }
    evs = ClaudeAdapter.parse_line(line, "S1")
    assert len(evs) == 1 and evs[0].kind == "tool_result" and evs[0].is_error


def test_claude_ignores_nonmessage_types() -> None:
    assert ClaudeAdapter.parse_line({"type": "attachment"}, "S1") == []


# --- Codex ---------------------------------------------------------------
def test_codex_function_call_extracts_workdir() -> None:
    line: dict[str, object] = {
        "timestamp": "2026-01-01T00:00:00Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "exec_command",
                    "arguments": json.dumps({"cmd": "ls /proj/y", "workdir": "/proj/y"})},
    }
    ev = CodexAdapter.parse_line(line, "C1", None)
    assert ev is not None
    assert ev.kind == "tool_use" and ev.tool_name == "exec_command"
    assert "/proj/y" in ev.paths


def test_codex_agent_message_and_noise() -> None:
    msg: dict[str, object] = {"timestamp": "t", "type": "event_msg",
                              "payload": {"type": "agent_message", "message": "done"}}
    ev = CodexAdapter.parse_line(msg, "C1", "/cwd")
    assert ev is not None and ev.kind == "text" and ev.role == "assistant"
    noise: dict[str, object] = {"type": "event_msg", "payload": {"type": "token_count"}}
    assert CodexAdapter.parse_line(noise, "C1", None) is None


def test_codex_session_meta_cwd() -> None:
    line: dict[str, object] = {
        "type": "session_meta",
        "payload": {"type": "session_meta", "cwd": "/proj/z", "id": "abc"},
    }
    ev = CodexAdapter.parse_line(line, "C1", "/proj/z")
    assert ev is not None and ev.kind == "meta"


# --- helpers + raw -------------------------------------------------------
def test_collect_paths_only_absolute() -> None:
    out: list[str] = []
    collect_paths({"a": "/abs/path", "b": "rel/no", "c": ["/x", 3, True]}, out)
    assert "/abs/path" in out and "/x" in out
    assert "rel/no" not in out


def test_dedupe_preserves_order() -> None:
    assert dedupe(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_raw_adapter_tails_lines(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text('{"file_path": "/p/a"}\nplain line\n')

    async def run() -> list[str]:
        ad = RawAdapter()
        ref = SessionRef(source="raw", session_id="r", path=f)
        out: list[str] = []
        # tail() is typed as the broad AsyncIterator; the concrete impl is an async
        # generator, so cast to call aclose() for deterministic cleanup.
        agen = cast(AsyncGenerator[AgentEvent, None], ad.tail(ref, from_start=True, poll=0.02))
        for _ in range(2):
            ev = await asyncio.wait_for(agen.__anext__(), timeout=3)
            out.append(ev.text)
        await agen.aclose()
        return out

    texts = asyncio.run(run())
    assert len(texts) == 2
