"""printops_ha_fakes.py — a fake Home Assistant for the backup/HA tests. Loopback only, random port.

`FakeHa` answers what `printops.ha_detect`, `printops.ha_wire`, `printops.ha_locations` and
`printops.backup_notify` send: /manifest.json, /api/, /api/config, the config-entries + config-flow API of the
community `spoolman` integration, /api/config/core/check_config, /api/services/<domain>/<service>, and the
WebSocket commands backup/agents/info, backup/config/info, supervisor/api. Every request is recorded, so a test
can assert that a declined flow sent no write (`writes()` is empty), and `tokens_received()` lists every
credential a client presented (REST Authorization header or WebSocket auth frame), so a test can prove a
token never reached an origin it should not have.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from printer_ops.ws import OP_TEXT, accept_key, encode_frame, parse_frame

TOKEN = "ha-test-token"


class FakeHa:
    def __init__(
        self,
        *,
        spoolman_installed: bool = True,
        check_valid: bool = True,
        reload_missing: tuple[str, ...] = (),
        spoolman_url_ok: bool = True,
        package_loaded: bool = True,
    ) -> None:
        self.spoolman_installed = spoolman_installed
        self.check_valid = check_valid
        self.reload_missing = reload_missing
        self.spoolman_url_ok = spoolman_url_ok
        self.package_loaded = package_loaded
        self.entries: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, Any]] = []
        self.ws_commands: list[str] = []
        self.presented: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw else None

            def _send(self, code: int, body: Any) -> None:
                raw = (
                    body.encode()
                    if isinstance(body, str)
                    else json.dumps(body).encode()
                )
                self.send_response(code)
                self.send_header(
                    "Content-Type",
                    "text/plain" if isinstance(body, str) else "application/json",
                )
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def _route(self) -> None:
                body = self._body()
                path = self.path.split("?")[0]
                outer.requests.append((self.command, self.path, body))
                if self.headers.get("Authorization"):
                    outer.presented.append(
                        self.headers["Authorization"].removeprefix("Bearer ")
                    )
                if path == "/manifest.json":
                    return self._send(200, {"name": "Home Assistant"})
                if path == "/api/websocket":
                    return self._websocket()
                if not self._authorised():
                    return self._send(401, "401: Unauthorized")
                handler = outer.routes().get(
                    (self.command, path)
                ) or outer.prefix_route(self.command, path)
                if handler is None:
                    return self._send(404, {"message": "not found"})
                code, payload = handler(body, path)
                self._send(code, payload)

            do_GET = do_POST = do_DELETE = _route  # noqa: N815

            def _websocket(self) -> None:
                key = self.headers["Sec-WebSocket-Key"]
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept_key(key))
                self.end_headers()
                outer.serve_ws(self.connection)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------------- REST routes

    def routes(self) -> dict[tuple[str, str], Any]:
        return {
            ("GET", "/api/"): lambda b, p: (200, {"message": "API running."}),
            ("GET", "/api/config"): lambda b, p: (
                200,
                {"version": "2026.9.3", "components": ["automation", "script"]},
            ),
            ("GET", "/api/config/config_entries/entry"): lambda b, p: (
                200,
                list(self.entries),
            ),
            ("POST", "/api/config/config_entries/flow"): self._start_flow,
            ("GET", "/api/states/script.printops_backup_run"): lambda b, p: (
                (200, {"entity_id": "script.printops_backup_run", "state": "off"})
                if self.package_loaded
                else (404, {"message": "Entity not found."})
            ),
            ("POST", "/api/config/core/check_config"): lambda b, p: (
                200,
                {
                    "result": "valid" if self.check_valid else "invalid",
                    "errors": None if self.check_valid else "bad",
                },
            ),
        }

    def prefix_route(self, method: str, path: str) -> Any:
        if path.startswith("/api/config/config_entries/flow/"):
            return self._flow_step if method == "POST" else (lambda b, p: (200, {}))
        if method == "POST" and path.startswith("/api/services/"):
            return self._service
        return None

    def _start_flow(self, body: Any, path: str) -> tuple[int, Any]:
        if body.get("handler") != "spoolman" or not self.spoolman_installed:
            return 404, {"message": "Invalid handler specified"}
        return 200, {
            "flow_id": "flow1",
            "type": "form",
            "step_id": "user",
            "handler": "spoolman",
        }

    def _flow_step(self, body: Any, path: str) -> tuple[int, Any]:
        if not self.spoolman_url_ok:
            return 200, {
                "type": "form",
                "errors": {"spoolman_url": "Error testing spoolman url: refused"},
            }
        self.entries.append({"domain": "spoolman", "title": "spoolman", "data": body})
        return 200, {
            "type": "create_entry",
            "title": "spoolman",
            "result": {"domain": "spoolman"},
        }

    def _service(self, body: Any, path: str) -> tuple[int, Any]:
        domain = path.split("/")[3]
        if domain in self.reload_missing:
            return 400, {"message": f"Service {domain}.reload not found"}
        return 200, []

    # ------------------------------------------------------------------------- websocket

    def serve_ws(self, conn: Any) -> None:
        def send(obj: dict[str, Any]) -> None:
            conn.sendall(encode_frame(OP_TEXT, json.dumps(obj).encode(), mask=False))

        send({"type": "auth_required"})
        buf = b""
        authed = False
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while (frame := parse_frame(buf)) is not None:
                _fin, op, data, used = frame
                buf = buf[used:]
                if op != OP_TEXT:
                    return
                msg = json.loads(data)
                if not authed:
                    self.presented.append(str(msg.get("access_token")))
                    authed = msg.get("access_token") == TOKEN
                    send({"type": "auth_ok" if authed else "auth_invalid"})
                    continue
                self.ws_commands.append(msg["type"])
                send(
                    {
                        "id": msg["id"],
                        "type": "result",
                        "success": True,
                        "result": self._ws_result(msg),
                    }
                )

    @staticmethod
    def _ws_result(msg: dict[str, Any]) -> dict[str, Any]:
        if msg["type"] == "backup/agents/info":
            return {
                "agents": [{"agent_id": "backup.local"}, {"agent_id": "hassio.nas"}]
            }
        if msg["type"] == "backup/config/info":
            return {"config": {"schedule": {"recurrence": "daily"}}}
        return {
            "mounts": [
                {
                    "name": "nas",
                    "usage": "backup",
                    "type": "nfs",
                    "server": "10.0.0.5",
                    "share": "/export/ha",
                    "state": "active",
                }
            ]
        }

    # ------------------------------------------------------------------------- assertions

    def tokens_received(self) -> list[str]:
        return list(self.presented)

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] != "GET"]
