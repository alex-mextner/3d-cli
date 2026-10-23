"""printer_doctor_fakes.py — loopback fakes and builders for the `3d printers doctor` tests.

Everything here is in-process: a Spoolman with real CORS behaviour, a camera snapshot server, a
fake 3d-api (as a `printops.client.Transport` and as a loopback HTTP server), a manual clock, and
builders for `Target` / `Device` / `FleetPrinter` so a test states only what it cares about.
Nothing contacts a real printer, Spoolman or 3d-api.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from printer_doctor.context import Context, Services
from printer_doctor.model import CheckResult
from printer_doctor.targets import Target
from printer_ops.devices import Device
from printops.client import ApiClient, Response
from printops.fleet import FleetPrinter, Head

FLEET_FILE = Path("fleet-under-test.yaml")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def ev(result: CheckResult) -> dict[str, Any]:
    """A result's evidence for assertions (JSON-shaped, so indexing it is untyped on purpose)."""
    return dict(result.evidence)


class ManualClock:
    """A clock that only moves when the code sleeps: restart polls finish instantly."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def device(
    name: str = "p1",
    *,
    adapter: str = "moonraker",
    host: str = "127.0.0.1",
    port: int = 7125,
    source: Path = FLEET_FILE,
    access: str = "full",
    watch: dict[str, Any] | None = None,
    model: str | None = None,
    ws_port: int | None = None,
) -> Device:
    return Device(
        name,
        adapter,
        host,
        port,
        model,
        source,
        ws_port,
        watch=watch or {},
        access=access,
    )


def fleet_printer(
    name: str = "p1",
    *,
    heads: int = 1,
    unusable: tuple[int, ...] = (),
    access: str = "full",
    adapter: str = "moonraker",
) -> FleetPrinter:
    rows = tuple(
        Head(
            f"T{i + 1}",
            i,
            f"extruder{i or ''}",
            None,
            i not in unusable,
            "broken" if i in unusable else None,
        )
        for i in range(heads)
    )
    return FleetPrinter(
        name, "Test Model", "own", access, rows, None, None, None, None, None, (),
        {"adapter": adapter} if access != "offline" else {}, FLEET_FILE,
    )  # fmt: skip


def target(
    name: str = "p1",
    *,
    adapter: str = "moonraker",
    port: int = 7125,
    ws_port: int | None = None,
    heads: int = 1,
    unusable: tuple[int, ...] = (),
    access: str = "full",
    watch: dict[str, Any] | None = None,
    override: Path | None = None,
    override_host: str | None = None,
    with_fleet: bool = True,
) -> Target:
    """A Target on loopback. `override` makes the effective device come from that file."""
    base = device(
        name, adapter=adapter, port=port, ws_port=ws_port, access=access, watch=watch
    )
    fleet = (
        fleet_printer(
            name, heads=heads, unusable=unusable, access=access, adapter=adapter
        )
        if with_fleet
        else None
    )
    if access == "offline":
        return Target(name, fleet, None, None)
    effective = (
        device(name, adapter=adapter, host=override_host or base.host, port=port, ws_port=ws_port,
               source=override, access=access, watch=watch)
        if override is not None
        else base
    )  # fmt: skip
    return Target(name, fleet, effective, base if with_fleet else None)


@dataclass
class Rig:
    """A Context plus the seams a test inspects."""

    ctx: Context
    clock: ManualClock
    journal: list[Any] = field(default_factory=list)


def rig(
    tgt: Target,
    *,
    api: ApiClient | None = None,
    journal_outcome: str = "buffered",
    real_clock: bool = False,
    **services: Any,
) -> Rig:
    """A Context whose clock/sleep are manual, 3d-api is `api` (None: not configured) and whose
    journal records into `Rig.journal`; other seams via keyword (systemctl_show=, webui=, ...)."""
    clock = ManualClock()
    entries: list[Any] = []

    def journal(entry: Any, _api: ApiClient | None) -> str:
        entries.append(entry)
        return journal_outcome

    svc = Services(
        clock=time.time if real_clock else clock,
        sleep=clock.sleep,
        api=lambda: api,
        journal=journal,
        timeout=3.0,
        poll_s=5.0,
        **services,
    )
    return Rig(Context(tgt, svc), clock, entries)


# ---- Spoolman with CORS ---------------------------------------------------------------------


@dataclass
class FakeSpoolmanCors:
    """Spoolman's `/api/v1/info` behind FastAPI's CORSMiddleware: `Access-Control-Allow-Origin`
    only for an allowed origin (or `*`), none for any other."""

    allowed: tuple[str, ...] = ()
    requests: list[tuple[str, str, str | None]] = field(
        default_factory=list
    )  # method, path, Origin
    port: int = 0

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                origin = self.headers.get("Origin")
                fake.requests.append(("GET", self.path, origin))
                data = json.dumps({"version": "0.0-fake"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if origin and ("*" in fake.allowed or origin in fake.allowed):
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H

    @contextmanager
    def serve(self) -> Iterator[FakeSpoolmanCors]:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# ---- camera snapshot ------------------------------------------------------------------------


@dataclass
class FakeCameraServer:
    """A streamer answering GET `?action=snapshot` with `body` (a JPEG by default). `wake_after`
    answers 503 until that many hits happened (a camera that sleeps until it is woken)."""

    body: bytes = JPEG
    status: int = 200
    wake_after: int = 0
    hits: int = 0
    port: int = 0

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.hits += 1
                code = 503 if fake.hits <= fake.wake_after else fake.status
                self.send_response(code)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                self.wfile.write(fake.body)

        return H

    @contextmanager
    def serve(self) -> Iterator[FakeCameraServer]:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()


# ---- 3d-api ---------------------------------------------------------------------------------

Route = Callable[[str, dict[str, list[str]], Any], tuple[int, Any]]


def printer_view(
    name: str,
    *,
    heads: int = 1,
    bound: Mapping[int, list[int]] | None = None,
    live: Mapping[int, dict[str, Any] | None] | None = None,
    polled: bool = True,
    spools_error: str | None = None,
) -> dict[str, Any]:
    """`GET /v1/printers/{name}` (routes_reconcile.printer_view): per head the bound spools and the
    printer's live reading (None = no reading)."""
    slots = []
    for i in range(heads):
        label = f"T{i + 1}"
        slots.append(
            {
                "slot": f"{name}/{label}",
                "head": label,
                "index": i,
                "usable": True,
                "reason": None,
                "printer": (live or {}).get(
                    i, {"present": True, "material": "PLA", "color": "#FFFFFF"}
                ),
                "spools": None
                if spools_error
                else [
                    {"spool_id": sid, "slot": f"{name}/{label}"}
                    for sid in (bound or {}).get(i, [])
                ],
                "anomalies": [],
            }
        )
    return {
        "name": name,
        "polled": polled,
        "live": {"ok": True},
        "slots": slots,
        "spools_error": spools_error,
        "anomalies_open": 0,
    }


@dataclass
class FakeApi:
    """A fake 3d-api: `routes` maps a path to a JSON body or a callable(method, query, body) ->
    (status, body). Implements printops.client.Transport; `serve()` exposes the same over HTTP."""

    routes: dict[str, Any] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    imported: list[str] = field(
        default_factory=list
    )  # lines POSTed to /v1/settings/import
    import_refuses: bool = False
    port: int = 0

    def handle(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any]:
        self.requests.append((method, path))
        if path == "/v1/settings/import" and method == "POST":
            self.imported.extend(body["lines"])
            refused = 1 if self.import_refuses else 0
            return 200, {
                "counts": {"imported": 1 - refused, "skipped": 0, "refused": refused},
                "results": [],
            }
        route = self.routes.get(path)
        if route is None:
            return 404, {"detail": f"no route {path}"}
        if callable(route):
            reply: tuple[int, Any] = route(method, query, body)
            return reply
        return 200, route

    # printops.client.Transport
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        parts = urlsplit(url)
        status, data = self.handle(
            method,
            parts.path,
            parse_qs(parts.query),
            json.loads(body) if body else None,
        )
        return Response(status, json.dumps(data).encode(), {})

    def stream_lines(self, url: str, headers: Mapping[str, str], timeout: float) -> Any:
        raise NotImplementedError("the doctor never streams")

    def client(self) -> ApiClient:
        return ApiClient("http://fake-3d-api", "po_test", self)

    @contextmanager
    def serve(self) -> Iterator[FakeApi]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _do(self, method: str) -> None:
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                status, data = fake.handle(
                    method,
                    parts.path,
                    parse_qs(parts.query),
                    json.loads(raw) if raw else None,
                )
                out = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self) -> None:
                self._do("GET")

            def do_POST(self) -> None:
                self._do("POST")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
