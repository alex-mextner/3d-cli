"""Server smoke test (`/`, `/api/projects`, `/api/agents`) + agent-manager streaming."""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from web import webconfig
from web.adapters import SessionRef
from web.adapters.base import AgentEvent, LogAdapter
from web.agent_manager import AgentManager, TrackedSession


def _make_project(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "model.scad").write_text("cube(1);")
    (tmp_path / "p1" / "SPEC.md").write_text("# Title\n\nhello")
    return tmp_path


def test_server_smoke(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from web.server import create_app

    _make_project(tmp_path)
    cfg = webconfig.WebConfig(project_root=str(tmp_path), port=8799)
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "3d" in r.text.lower()

        r = c.get("/api/projects")
        assert r.status_code == 200
        projs = r.json()["projects"]
        assert any(p["name"] == "p1" for p in projs)

        p1 = next(p for p in projs if p["name"] == "p1")
        r = c.get("/api/spec", params={"path": p1["spec"]})
        assert r.status_code == 200 and "Title" in r.text

        r = c.get("/api/constants", params={"path": p1["primary_scad"]})
        assert r.status_code == 200  # model.scad has no constants → empty list ok

        r = c.get("/api/agents")
        assert r.status_code == 200 and "sessions" in r.json()

        # path-confinement: outside-root path is rejected
        r = c.get("/api/spec", params={"path": "/etc/hosts"})
        assert r.status_code == 403


def test_associate_by_cwd(tmp_path: pathlib.Path) -> None:
    proj = str((tmp_path / "p1"))
    (tmp_path / "p1").mkdir()
    mgr = AgentManager([], [proj])
    assert mgr.associate([], proj + "/sub/file.scad") == proj
    assert mgr.associate(["/elsewhere/x"], None) is None


class _StubAdapter(LogAdapter):
    id = "stub"

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    async def discover(self) -> list[SessionRef]:
        return [SessionRef(source="stub", session_id="s", path=pathlib.Path("/dev/null"))]

    async def tail(self, ref, *, from_start=True, poll=0.5):  # type: ignore[no-untyped-def]
        for e in self._events:
            yield e
        # then idle so the task doesn't complete and drop the stream
        while True:
            await asyncio.sleep(0.05)


def test_manager_streams_events_to_subscriber() -> None:
    """subscribe() BEFORE start_tail() → subscriber receives the replayed events."""
    evs = [
        AgentEvent(ts="t", source="stub", session_id="s", kind="text", role="user", text="hi"),
        AgentEvent(ts="t", source="stub", session_id="s", kind="tool_use", role="assistant",
                   text="Read foo", tool_name="Read", paths=["/proj/p1/foo"]),
    ]

    async def run() -> list[AgentEvent]:
        mgr = AgentManager([_StubAdapter(evs)], ["/proj/p1"])
        await mgr.refresh()
        agen = mgr.subscribe()  # queue created first
        await mgr.start_tail("stub:s", from_start=True)
        got: list[AgentEvent] = []
        for _ in range(2):
            got.append(await asyncio.wait_for(agen.__anext__(), timeout=3))
        await agen.aclose()
        # association happened from the tool_use path
        ts: TrackedSession = mgr._sessions["stub:s"]
        assert ts.project_path == "/proj/p1"
        await mgr.stop()
        return got

    got = asyncio.run(run())
    assert [e.kind for e in got] == ["text", "tool_use"]
