#!/usr/bin/env python3
"""agent_manager.py — orchestrates the log adapters into a live, project-associated feed.

Responsibilities (feature 7, "the key feature"):
  - run every enabled adapter's discover() and keep a cache of known SessionRefs;
  - AUTO-ASSOCIATE a session with a scanned project by matching the paths/cwd its events
    reference against the project roots;
  - TAIL the active session(s) and fan their normalized events out to SSE subscribers;
  - track ACTIVITY: a session with no new lines for `inactive_after` seconds is marked
    inactive, and discover() is re-run to pick up a newer session for that project/cwd.

Adapters do the format-specific work; this layer is format-agnostic.
"""
from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import time
from collections.abc import AsyncGenerator

from .adapters import AgentEvent, LogAdapter, SessionRef


@dataclasses.dataclass(slots=True)
class TrackedSession:
    ref: SessionRef
    project_path: str | None = None   # associated project dir, if matched
    last_event: float = 0.0           # epoch of last emitted event
    active: bool = False
    event_count: int = 0
    task: asyncio.Task[None] | None = None


class AgentManager:
    def __init__(
        self,
        adapters: list[LogAdapter],
        project_paths: list[str],
        *,
        inactive_after: float = 30.0,
    ) -> None:
        self._adapters = adapters
        self._project_paths = [str(pathlib.Path(p).resolve()) for p in project_paths]
        self.inactive_after = inactive_after
        self._sessions: dict[str, TrackedSession] = {}  # key: f"{source}:{session_id}"
        self._subscribers: set[asyncio.Queue[AgentEvent]] = set()
        self._lock = asyncio.Lock()
        self._started = False

    # -- project association -------------------------------------------------
    def set_projects(self, project_paths: list[str]) -> None:
        self._project_paths = [str(pathlib.Path(p).resolve()) for p in project_paths]

    def associate(self, paths: list[str], cwd: str | None) -> str | None:
        """Return the project dir whose path is a prefix of (or contains) any referenced
        path or the cwd. Longest match wins."""
        candidates = list(paths)
        if cwd:
            candidates.append(cwd)
        best: str | None = None
        for proj in self._project_paths:
            for c in candidates:
                cr = c
                if cr == proj or cr.startswith(proj + "/") or proj.startswith(cr + "/"):
                    if best is None or len(proj) > len(best):
                        best = proj
        return best

    # -- discovery + lifecycle ----------------------------------------------
    async def refresh(self) -> list[TrackedSession]:
        """(Re)discover sessions across all adapters; start tailing newly-seen ones."""
        async with self._lock:
            for ad in self._adapters:
                try:
                    refs = await ad.discover()
                except Exception:
                    refs = []
                for ref in refs:
                    key = f"{ref.source}:{ref.session_id}"
                    existing = self._sessions.get(key)
                    if existing is None:
                        ts = TrackedSession(ref=ref)
                        if ref.cwd:
                            ts.project_path = self.associate([], ref.cwd)
                        self._sessions[key] = ts
                    else:
                        # a known session whose file grew/resumed: refresh mtime (and cwd
                        # if it only just became readable) so the monitor's freshness
                        # check can re-tail a resumed session.
                        if ref.mtime > existing.ref.mtime:
                            existing.ref.mtime = ref.mtime
                        if existing.ref.cwd is None and ref.cwd:
                            existing.ref.cwd = ref.cwd
                            if existing.project_path is None:
                                existing.project_path = self.associate([], ref.cwd)
            return list(self._sessions.values())

    def sessions(self) -> list[TrackedSession]:
        now = time.time()
        for ts in self._sessions.values():
            ts.active = ts.last_event > 0 and (now - ts.last_event) < self.inactive_after
        return list(self._sessions.values())

    def _adapter_for(self, source: str) -> LogAdapter | None:
        return next((a for a in self._adapters if a.id == source), None)

    async def start_tail(self, key: str, *, from_start: bool = False) -> bool:
        """Begin following one session (idempotent). Events flow to all subscribers."""
        ts = self._sessions.get(key)
        if ts is None or (ts.task is not None and not ts.task.done()):
            return ts is not None
        ad = self._adapter_for(ts.ref.source)
        if ad is None:
            return False
        ts.task = asyncio.create_task(self._pump(ad, ts, from_start))
        return True

    async def _pump(
        self, ad: LogAdapter, ts: TrackedSession, from_start: bool
    ) -> None:
        try:
            async for ev in ad.tail(ts.ref, from_start=from_start):
                ts.last_event = time.time()
                ts.event_count += 1
                if ts.project_path is None:
                    ts.project_path = self.associate(ev.paths, ev.cwd or ts.ref.cwd)
                await self._fanout(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _fanout(self, ev: AgentEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    # -- subscription (SSE) --------------------------------------------------
    def subscribe(self) -> AsyncGenerator[AgentEvent, None]:
        """Register a subscriber queue EAGERLY (before any await), then return an async
        generator draining it. Registering up-front (not lazily inside the generator body)
        means events published between subscribe() and the first __anext__() are not lost —
        critical for the 'subscribe then start_tail(from_start)' replay path."""
        q: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)

        async def _drain() -> AsyncGenerator[AgentEvent, None]:
            try:
                while True:
                    yield await q.get()
            finally:
                self._subscribers.discard(q)

        return _drain()

    # -- background activity monitor ----------------------------------------
    def _newer_session_for(
        self, ts: TrackedSession, now: float
    ) -> TrackedSession | None:
        """Find a more-recently-modified session associated with the same project/cwd as
        an inactive one — the spec's 'detect inactive → search for a newer one'."""
        target = ts.project_path or ts.ref.cwd
        if not target:
            return None
        best: TrackedSession | None = None
        for other in self._sessions.values():
            if other is ts:
                continue
            same = (other.project_path and other.project_path == ts.project_path) or (
                other.ref.cwd and other.ref.cwd == ts.ref.cwd
            )
            if not same:
                continue
            if other.ref.mtime > ts.ref.mtime and (best is None or other.ref.mtime > best.ref.mtime):
                best = other
        return best

    async def monitor(self, interval: float = 5.0) -> None:
        """Periodically re-discover so new sessions (and newer ones replacing inactive
        ones) get picked up and tailed. Runs until cancelled."""
        while True:
            await self.refresh()
            now = time.time()
            # auto-tail recently-active sessions so the feed stays live without the UI
            # having to opt into each one.
            for key, ts in list(self._sessions.items()):
                fresh = ts.ref.mtime > 0 and (now - ts.ref.mtime) < self.inactive_after * 4
                if fresh and (ts.task is None or ts.task.done()):
                    await self.start_tail(key, from_start=False)
                # inactive (was live, now quiet) → look for a successor and tail it
                gone_quiet = ts.last_event > 0 and (now - ts.last_event) >= self.inactive_after
                if gone_quiet:
                    succ = self._newer_session_for(ts, now)
                    if succ is not None:
                        skey = f"{succ.ref.source}:{succ.ref.session_id}"
                        if succ.task is None or succ.task.done():
                            await self.start_tail(skey, from_start=False)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        for ts in self._sessions.values():
            if ts.task and not ts.task.done():
                ts.task.cancel()
        for ts in self._sessions.values():
            if ts.task:
                try:
                    await ts.task
                except (asyncio.CancelledError, Exception):
                    pass
