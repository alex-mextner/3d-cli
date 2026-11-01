"""printops_env_fakes.py — local stand-ins for the services `3d init --env` talks to.

`FakeService` serves 3d-api's /v1/health and /v1/events (bearer token) and Spoolman's
/api/v1/health on 127.0.0.1:<random port>, and records every request, so a test can assert that a
flow made no request, or no write. Nothing here can reach a real host.
`fake_docker_bin` writes `docker`/`curl` shims for running the generated install script itself.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GOOD_TOKEN = "po_goodtoken"


class FakeService:
    def __init__(
        self, *, api: bool = True, spoolman: bool = True, status: str = "ok"
    ) -> None:
        self.api = api  # serve /v1/health + /v1/events
        self.spoolman = spoolman  # serve /api/v1/health
        self.status = status  # "ok" | "degraded"
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def _send(self, code: int, body: object) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self) -> None:
                outer.requests.append((self.command, self.path))
                path = self.path.split("?")[0]
                if self.command != "GET":
                    self._send(405, {"detail": "read-only fake"})
                elif path == "/v1/health" and outer.api:
                    body = {
                        "status": outer.status,
                        "version": "fake",
                        "db": {"ok": True},
                        "spoolman": {"ok": outer.status == "ok", "version": "0.27.0"},
                    }
                    self._send(200 if outer.status == "ok" else 503, body)
                elif path == "/v1/events" and outer.api:
                    if self.headers.get("Authorization") != f"Bearer {GOOD_TOKEN}":
                        self._send(401, {"detail": "invalid token"})
                    else:
                        self._send(200, {"events": [], "cursor": 0})
                elif path == "/api/v1/health" and outer.spoolman:
                    self._send(200, {"status": "healthy"})
                else:
                    self._send(404, {"detail": "not found"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


FAKE_DOCKER = """#!/bin/sh
# records every call; `docker compose ... up` also records the PRINTOPS_* environment it got
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  info) exit 0 ;;
  compose)
    if [ "$2" = "version" ]; then echo "v2.fake"; exit 0; fi
    env | grep '^PRINTOPS_' | sort >> "$FAKE_LOG"
    exit 0 ;;
  exec)
    # docker exec CONTAINER 3d printops token list|create NAME --scopes S
    if [ "$6" = "list" ]; then cat "$FAKE_TOKENS" 2>/dev/null; exit 0; fi
    if [ "$6" = "create" ]; then echo "$7        read,write  now  sha256 abcdef" >> "$FAKE_TOKENS"; echo po_fakeminted; exit 0; fi
    exit 1 ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/sh
case "$*" in
  *"/api/v1/health"*) echo '{"status": "healthy"}' ;;
  *"/v1/health"*) echo '{"status": "ok"}' ;;
  *) exit 22 ;;
esac
"""


def fake_docker_bin(root: Path) -> Path:
    """A dir with `docker` and `curl` shims (put it first on PATH); calls go to root/calls.log."""
    bindir = root / "fakebin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        shim = bindir / name
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)
    return bindir
