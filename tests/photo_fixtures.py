"""A loopback Moonraker for the `3d photo capture` e2e tests: it SIMULATES the Z axis (a `G1 Z..` script moves the
toolhead), records every G-code script, and serves a camera snapshot that changes on every request, like a live
camera. Nothing here is a real printer: the port is 127.0.0.1:<free port>."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FRAME = (
    Path(__file__).resolve().parent / "data" / "bed" / "u1_z10_clear_ref.jpg"
).read_bytes()
RESUME = """\
  resume:
    purge_macro: SM_PRINT_PREEXTRUDE_FILAMENT
    purge_mm: 30
    purge_retract_mm: 0.5
    no_tower_prime_mm: 12
    clearance_mm: 5
    tower_margin_mm: 3
    filament_diameter_mm: 1.75
    min_print_temp: 170
    default_accel: 10000
    z_max_mm: 275
    start_commands: []
"""


class ZMoonraker:
    def __init__(
        self, *, state: str = "complete", z: float = 200.0, homed: str = "xyz"
    ) -> None:
        self.state, self.z, self.homed = state, z, homed
        self.scripts: list[str] = []
        self.snapshots = 0
        self.port = 0

    def moves(self) -> list[str]:
        return [
            ln
            for s in self.scripts
            for ln in s.splitlines()
            if ln.startswith(("G0", "G1"))
        ]

    def status(self) -> dict[str, Any]:
        return {
            "webhooks": {"state": "ready"},
            "print_stats": {"state": self.state, "filename": "mole_plate.gcode"},
            "toolhead": {
                "position": [100.0, 100.0, self.z, 0.0],
                "homed_axes": self.homed,
            },
            "gcode_move": {"gcode_position": [100.0, 100.0, self.z, 0.0]},
        }

    @contextmanager
    def serve(self) -> Iterator[ZMoonraker]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def _send(
                self, code: int, body: bytes, ctype: str = "application/json"
            ) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 — http.server API
                u = urlsplit(self.path)
                if u.path == "/printer/objects/query":
                    self._send(
                        200,
                        json.dumps(
                            {"result": {"eventtime": 1.0, "status": fake.status()}}
                        ).encode(),
                    )
                elif u.path == "/cam.jpg":
                    fake.snapshots += 1
                    com = b"\xff\xfe\x00\x0a" + fake.snapshots.to_bytes(8, "big")
                    self._send(200, FRAME[:2] + com + FRAME[2:], "image/jpeg")
                else:
                    self._send(404, b'{"error": {"code": 404, "message": "not found"}}')

            def do_POST(self) -> None:  # noqa: N802 — http.server API
                u = urlsplit(self.path)
                if u.path == "/printer/gcode/script":
                    script = parse_qs(u.query)["script"][0]
                    fake.scripts.append(script)
                    for line in script.splitlines():
                        if line.startswith("G1 Z"):
                            fake.z = float(line.split()[1][1:])
                    self._send(200, b'{"result": "ok"}')
                else:
                    self._send(404, b'{"error": {"code": 404, "message": "not found"}}')

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(httpd.server_address[1])
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            yield self
        finally:
            httpd.shutdown()
            httpd.server_close()


def write_devices(config_home: Path, moon: ZMoonraker) -> Path:
    """printer-devices.yaml: `k1u` is a Moonraker printer at the fake, with a camera snapshot URL and the resume
    profile (z_max 275) that bounds the bed lift."""
    d = config_home / "3d-cli"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "printer-devices.yaml"
    path.write_text(
        "k1u:\n  adapter: moonraker\n  host: 127.0.0.1\n"
        f"  port: {moon.port}\n  model: Snapmaker U1\n"
        "  watch:\n    bed: {enabled: true}\n    vision:\n      camera: {kind: snapshot-url, url: /cam.jpg, keepalive: false}\n"
        + RESUME,
        encoding="utf-8",
    )
    return path
