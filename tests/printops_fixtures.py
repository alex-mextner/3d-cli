"""printops_fixtures.py — shared pytest fixtures for every PrintOps slice (tests/test_printops_*.py).

Registered for the whole suite via `pytest_plugins` in tests/conftest.py, so tests just name them:

  printops_settings   Settings on a tmp DB + tokens file (Spoolman injected, no URL)
  fake_spoolman       a fresh FakeSpoolman (tests/printops_fakes.py)
  printops_app        create_app(printops_settings, spoolman=fake_spoolman)
  printops_client     starlette TestClient (lifespan ran, extra fields registered)
  printops_tokens     {"read": po_…, "write": po_…, "rw": po_…, "all": po_…} minted in the tokens file
  api_client          printops.client.ApiClient (token "rw") whose transport is the TestClient
  printops_db         an open, migrated sqlite3 connection to the same DB as the app
  live_api            a real uvicorn 3d-api on 127.0.0.1:<port> backed by FakeSpoolman over HTTP
                      (for SSE/streaming and `bin/3d` e2e) — LiveApi(url, settings, tokens, spoolman)
"""
from __future__ import annotations

import contextlib
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from printops_fakes import FakeSpoolman

from printops import tokens
from printops.client import ApiClient, ApiError, Response
from printops.db import migrate, open_db
from printops.settings import Settings


class TestClientTransport:
    """printops.client.Transport over a starlette TestClient (no sockets). Streams must be
    finite: the TestClient buffers a response, so use `live_api` for endless SSE."""

    __test__ = False  # not a pytest test class

    def __init__(self, client: Any) -> None:
        self.client = client

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> Response:
        r = self.client.request(method, url, headers=dict(headers), content=body)
        return Response(r.status_code, r.content, dict(r.headers))

    @contextlib.contextmanager
    def stream_lines(self, url: str, headers: Mapping[str, str], timeout: float) -> Iterator[Iterator[str]]:
        with self.client.stream("GET", url, headers=dict(headers)) as r:
            if r.status_code >= 400:
                r.read()
                raise ApiError(f"HTTP {r.status_code}: {r.text}", status=r.status_code)
            yield iter(r.iter_lines())


def mint_tokens(path: Path) -> dict[str, str]:
    scopes = {"read": ("read",), "write": ("write",), "rw": ("read", "write"),
              "all": ("read", "write", "printer-control")}
    return {name: tokens.create(path, f"test-{name}", s)[0] for name, s in scopes.items()}


def wait_fields(app: Any, timeout: float = 5.0) -> None:
    """Block until the app's background task has registered the extra fields."""
    deadline = time.monotonic() + timeout
    while app.state.printops.fields.get("status") == "pending":
        if time.monotonic() > deadline:
            raise AssertionError(f"extra fields not registered: {app.state.printops.fields}")
        time.sleep(0.01)


@pytest.fixture
def printops_settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "printops.db", tokens_file=tmp_path / "tokens.json", version="test")


@pytest.fixture
def fake_spoolman() -> FakeSpoolman:
    return FakeSpoolman()


@pytest.fixture
def printops_app(printops_settings: Settings, fake_spoolman: FakeSpoolman) -> Any:
    from printops.api.app import create_app

    return create_app(printops_settings, spoolman=fake_spoolman)


@pytest.fixture
def printops_client(printops_app: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(printops_app) as client:
        wait_fields(printops_app)
        yield client


@pytest.fixture
def printops_tokens(printops_settings: Settings) -> dict[str, str]:
    return mint_tokens(printops_settings.tokens_file)


@pytest.fixture
def api_client(printops_client: Any, printops_tokens: dict[str, str]) -> ApiClient:
    return ApiClient("http://testserver", printops_tokens["rw"], TestClientTransport(printops_client))


@pytest.fixture
def printops_db(printops_settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = open_db(printops_settings.db_path)
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class LiveApi:
    url: str
    settings: Settings
    tokens: dict[str, str]
    spoolman: FakeSpoolman


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def serve_api(tmp_path: Path, fake: FakeSpoolman) -> Iterator[LiveApi]:
    """A real uvicorn 3d-api in a thread, talking HTTP to `fake.serve()`."""
    import uvicorn

    from printops.api.app import create_app

    with fake.serve() as spoolman_url:
        settings = Settings(db_path=tmp_path / "live" / "printops.db", tokens_file=tmp_path / "live" / "tokens.json",
                            spoolman_url=spoolman_url, version="live-test")
        app = create_app(settings)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                               lifespan="on", timeout_graceful_shutdown=1))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        wait_fields(app)
        try:
            yield LiveApi(f"http://127.0.0.1:{port}", settings, mint_tokens(settings.tokens_file), fake)
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture
def live_api(tmp_path: Path, fake_spoolman: FakeSpoolman) -> Iterator[LiveApi]:
    with serve_api(tmp_path, fake_spoolman) as live:
        yield live
