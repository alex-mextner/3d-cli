"""Fixtures of `3d printer evidence`: the real Chekhov failure of 2026-10-24 03:05 UTC, trimmed, and a loopback
Moonraker that answers only what evidence reads and records every request.

tests/data/printer_evidence/:
  klippy.log.txt        real klippy.log lines of the U1 (the `pickup failed` stop, the resume attempt, the cancel);
                        the 2 s `Stats` counter lines are cut to their first 130 characters
  chekhov_layers.gcode  the real z of all 644 layers of the final4 G-code and the real first bytes of layers 454 and
                        455; every other layer keeps only its four marker lines
  job.json              the real Moonraker history record of job 000070; its file name carries the hash of the real
                        75 MB G-code, so against the 50 KB excerpt it is a MISMATCH — `job_for` renames it after
                        the excerpt, the way `3d printer upload` names a file

The real G-code is 75 MB: its layer 455 starts at byte 64061656 and the printer stopped at byte 64070964, 9308 B
into it. The fixture has the same layers at other offsets, so the log's positions are shifted by the same amount
(`shifted_log`): the distance into layer 455 and the layer numbers stay the real ones.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

DATA = Path(__file__).resolve().parent / "data" / "printer_evidence"
REAL_LAYER_455_START = 64061656  # in the 75 MB final4 G-code
REAL_STOP = 64070964  # file_position at 2026-10-24 03:05:19 UTC
REAL_FILE = "chekhov_phone_stand-final4-48b4bf31-48b4bf311a62a332.gcode"  # the file name the real log mentions
REAL_RESTOP = 64072162  # file_position at 2026-10-24 09:31:48 UTC
LAYER_455_INDEX = 454  # layers are numbered from 1, listed from 0
STOP_INTO_LAYER = REAL_STOP - REAL_LAYER_455_START  # 9308 B
READ_AT = datetime(2026, 10, 24, 10, 58, tzinfo=timezone.utc)  # when the real klippy.log copy was taken (its mtime)
LAYERS = 644


def layer_starts(gcode: Path) -> list[int]:
    out, off = [], 0
    with gcode.open("rb") as fh:
        for raw in fh:
            if raw.startswith(b";LAYER_CHANGE"):
                out.append(off)
            off += len(raw)
    return out


def fixture_gcode() -> Path:
    return DATA / "chekhov_layers.gcode"


def fixture_position(real_position: int) -> int:
    """The byte of the 50 KB excerpt that is `real_position` of the 75 MB G-code (same distance into layer 455)."""
    return layer_starts(fixture_gcode())[LAYER_455_INDEX] + real_position - REAL_LAYER_455_START


def shifted_log() -> str:
    """The real klippy.log fixture with every file position moved to the same place of the excerpt G-code."""
    delta = fixture_position(REAL_LAYER_455_START) - REAL_LAYER_455_START
    text = (
        (DATA / "klippy.log.txt").read_text(encoding="utf-8").replace(REAL_FILE, job_for(fixture_gcode())["filename"])
    )
    return re.sub(r"(file_position=|position )(\d+)", lambda m: f"{m.group(1)}{int(m.group(2)) + delta}", text)


def job_for(gcode: Path, **over: Any) -> dict[str, Any]:
    """The real job record, renamed the way `3d printer upload` names a file: <stem>-<sha8>-<sha16>.gcode of
    `gcode`, so the layer map can tell it is the printed file."""
    job: dict[str, Any] = json.loads((DATA / "job.json").read_text(encoding="utf-8"))["job"]
    sha = hashlib.sha256(gcode.read_bytes()).hexdigest()
    job["filename"] = f"chekhov_phone_stand-final4-{sha[:8]}-{sha[:16]}.gcode"
    job.update(over)
    return job


@dataclass
class FakeEvidenceMoonraker:
    """GET /server/history/job|list, /server/files/logs/klippy.log and /printer/objects/query only; anything
    else (a POST above all) is answered 405 and recorded, so a test can prove none was sent."""

    jobs: list[dict[str, Any]] = field(default_factory=list)  # newest first
    klippy: str = ""
    klippy_date: str = ""  # HTTP Date of the klippy.log reply
    klippy_last_modified: str = ""  # HTTP Last-Modified of the klippy.log reply
    job_reply: Any = None  # replaces the /server/history/job body when set
    job_status: int = 0  # the /server/history/job answer is this HTTP error status when set ...
    job_message: str = ""  # ... with this message
    print_state: str = "standby"
    klippy_status: int = 200
    requests: list[tuple[str, str]] = field(default_factory=list)  # (method, path?query)
    port: int = 0

    def methods(self) -> set[str]:
        return {m for m, _ in self.requests}

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _send(
                self,
                code: int,
                body: bytes,
                ctype: str = "application/json",
                date: str | None = None,
                last_modified: str | None = None,
            ) -> None:
                self.send_response_only(code)
                self.send_header("Date", date or formatdate(usegmt=True))
                if last_modified:
                    self.send_header("Last-Modified", last_modified)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj: Any, code: int = 200) -> None:
                self._send(code, json.dumps(obj).encode())

            def do_GET(self) -> None:
                u = urlsplit(self.path)
                fake.requests.append(("GET", self.path))
                q = parse_qs(u.query)
                if u.path == "/server/history/job":
                    uid = q.get("uid", [""])[0]
                    found = next((j for j in fake.jobs if j.get("job_id") == uid), None)
                    if fake.job_status:
                        self._json({"error": {"code": fake.job_status, "message": fake.job_message}}, fake.job_status)
                    elif fake.job_reply is not None:
                        self._json(fake.job_reply)
                    elif found is None:
                        self._json(
                            {"error": {"code": 404, "message": f"Job {uid} not found"}},
                            404,
                        )
                    else:
                        self._json({"result": {"job": found}})
                elif u.path == "/server/history/list":
                    limit = int(q.get("limit", ["50"])[0])
                    self._json({"result": {"count": len(fake.jobs), "jobs": fake.jobs[:limit]}})
                elif u.path == "/server/files/logs/klippy.log":
                    if fake.klippy_status != 200:
                        self._json({"error": {"code": fake.klippy_status}}, fake.klippy_status)
                    else:
                        self._send(
                            200,
                            fake.klippy.encode(),
                            "text/plain",
                            fake.klippy_date or None,
                            fake.klippy_last_modified or None,
                        )
                elif u.path == "/printer/objects/query":
                    self._json(
                        {
                            "result": {
                                "status": {
                                    "print_stats": {
                                        "state": fake.print_state,
                                        "message": "",
                                    }
                                }
                            }
                        }
                    )
                else:
                    self._json({"error": {"code": 404}}, 404)

            def _refuse(self) -> None:
                fake.requests.append((self.command, self.path))
                self._json({"error": {"code": 405}}, 405)

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse

        return H

    @contextmanager
    def serve(self) -> Iterator[FakeEvidenceMoonraker]:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()


def write_u1(config_home: Path, port: int) -> Path:
    """printer-devices.yaml with one Moonraker printer `u1` on 127.0.0.1:port."""
    d = config_home / "3d-cli"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "printer-devices.yaml"
    path.write_text(
        f"u1:\n  adapter: moonraker\n  host: 127.0.0.1\n  port: {port}\n  model: Snapmaker U1\n",
        encoding="utf-8",
    )
    return path
