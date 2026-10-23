"""printer_fw_fakes.py — in-memory fakes for the `3d printers fw` tests (no sockets, no ssh).

`FakeRunner` is the injected CommandRunner: it records every command and answers from rules.
`FakeHttp` answers `Backends.http` from a route table keyed by (method, port, path) and records every
request, so a test can assert a dry run sent nothing but GETs. `FakeClock` makes verification polls
finish instantly. `LoopbackHttp` is a REAL loopback HTTP server for the e2e tests (the CLI is a
subprocess there), recording methods, paths and upload sizes.

Nothing in here talks to a printer, Spoolman or any device (AGENTS.md).
"""

from __future__ import annotations

import io
import json
import socket
import tarfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from printer_fw.engine import Context, EventLog, IdleVerdict
from printer_fw.probes import Backends, Endpoint
from printer_fw.profiles import Profile, parse_profile, read_only_command
from printer_fw.runner import CommandResult
from printer_fw.target import Target, endpoint

Body = str | bytes | dict[str, Any] | list[Any]


def as_bytes(body: Body) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body).encode()


@dataclass
class Call:
    host: str
    user: str
    command: str | tuple[str, ...]
    port: int | None
    timeout: float


@dataclass
class FakeRunner:
    """Answers ssh commands from `rules` (first whose needle is in the command text wins)."""

    default: CommandResult = field(
        default_factory=lambda: CommandResult.text(1, "", "no rule")
    )
    rules: list[tuple[str, CommandResult, Callable[[], None] | None]] = field(
        default_factory=list
    )
    calls: list[Call] = field(default_factory=list)
    available: bool = True

    def on(
        self,
        needle: str,
        rc: int = 0,
        out: str | bytes = "",
        err: str = "",
        effect: Callable[[], None] | None = None,
    ) -> FakeRunner:
        res = CommandResult(
            rc, out if isinstance(out, bytes) else out.encode(), err.encode()
        )
        self.rules.append((needle, res, effect))
        return self

    def check_available(self) -> None:
        if not self.available:
            from errors import MissingDependency

            raise MissingDependency(
                "ssh", install="apt install openssh-client", command="printers fw"
            )

    def run(
        self,
        host: str,
        user: str,
        command: str | Sequence[str],
        *,
        timeout: float,
        port: int | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        text = command if isinstance(command, str) else " ".join(command)
        self.calls.append(
            Call(
                host,
                user,
                command if isinstance(command, str) else tuple(command),
                port,
                timeout,
            )
        )
        for needle, result, effect in self.rules:
            if needle in text:
                if effect is not None:
                    effect()
                return result
        return self.default

    def texts(self) -> list[str]:
        return [
            c.command if isinstance(c.command, str) else " ".join(c.command)
            for c in self.calls
        ]

    def mutating(self) -> list[str]:
        """Commands that are not read-only probes/pulls (a dry run must have none)."""
        out: list[str] = []
        for c in self.calls:
            if isinstance(c.command, str):
                if (
                    read_only_command(c.command.strip().splitlines()[0]) is not None
                    or "\n" in c.command.strip()
                ):
                    out.append(c.command)
            elif c.command[0] not in ("cat", "tar"):
                out.append(" ".join(c.command))
        return out


@dataclass
class FakeHttp:
    """`Backends.http`: (method, port, path-with-query) -> (status, body). A port with no route at
    all refuses the connection; an unknown path on a known port is a 404."""

    routes: dict[
        tuple[str, int, str], tuple[int, Body] | Callable[[], tuple[int, Body]]
    ] = field(default_factory=dict)
    requests: list[tuple[str, str, bytes | None]] = field(default_factory=list)

    def set(
        self, method: str, port: int, path: str, status: int = 200, body: Body = ""
    ) -> None:
        self.routes[(method, port, path)] = (status, body)

    def drop(self, port: int) -> None:
        for key in [k for k in self.routes if k[1] == port]:
            del self.routes[key]

    def __call__(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        max_bytes: int | None = None,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        port = parts.port or 80
        self.requests.append((method, url, body))
        if not any(k[1] == port for k in self.routes):
            raise ConnectionRefusedError(f"{url}: refused")
        entry = self.routes.get((method, port, path))
        if entry is None:
            return 404, b"not found"
        status, payload = entry() if callable(entry) else entry
        return status, as_bytes(payload)

    def mutations(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _ in self.requests if m != "GET"]


@dataclass
class FakeUpload:
    status: int = 200
    body: str = "SUCCESS: Completed successfully."
    effect: Callable[[], None] | None = None
    calls: list[tuple[str, str, int]] = field(default_factory=list)
    raises: OSError | None = None

    def __call__(
        self,
        url: str,
        local: Path,
        remote_name: str,
        fields: dict[str, str],
        *,
        timeout: float,
    ) -> tuple[int, bytes]:
        self.calls.append((url, remote_name, local.stat().st_size))
        if self.effect is not None:
            self.effect()
        if self.raises is not None:
            raise self.raises
        return self.status, self.body.encode()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


def make_backends(
    runner: FakeRunner | None = None,
    http: FakeHttp | None = None,
    upload: FakeUpload | None = None,
    *,
    use_ssh: bool = True,
    confirm: Callable[[str], bool] | None = None,
) -> tuple[Backends, FakeRunner, FakeHttp, FakeUpload]:
    r, h, u = runner or FakeRunner(), http or FakeHttp(), upload or FakeUpload()
    clock = FakeClock()

    def connect(host: str, port: int, timeout: float) -> None:
        if not any(k[1] == port for k in h.routes):
            raise ConnectionRefusedError(port)

    def banner(host: str, port: int, timeout: float) -> bytes:
        entry = h.routes.get(("BANNER", port, ""))
        if entry is None:
            if any(k[1] == port for k in h.routes):
                return b""
            raise ConnectionRefusedError(port)
        if callable(entry):
            entry = entry()
        return as_bytes(entry[1])

    be = Backends(
        runner=r,
        http=h,
        upload=u,
        connect=connect,
        read_banner=banner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        use_ssh=use_ssh,
        confirm=confirm,
    )
    return be, r, h, u


def target(
    name: str = "demo",
    model: str = "Test Model",
    *,
    access: str = "full",
    adapter: str = "moonraker",
    port: int = 7125,
) -> Target:
    return Target(
        name,
        model,
        access,
        "" if access == "offline" else "10.0.0.5",
        "" if access == "offline" else adapter,
        0 if access == "offline" else port,
    )


def context(
    profile: Profile,
    tgt: Target,
    be: Backends,
    *,
    ask: Callable[[str], str] | None = None,
    idle: IdleVerdict | None = None,
    log: EventLog | None = None,
) -> Context:
    verdict = idle or IdleVerdict(True, "adapter", "standby (standby)")
    return Context(
        profile,
        tgt,
        endpoint(profile, tgt, {}),
        be,
        log or EventLog(),
        idle_check=lambda *_: verdict,
        ask=ask,
    )


def ep_of(profile: Profile, tgt: Target) -> Endpoint:
    return endpoint(profile, tgt, {})


def minimal_profile_data() -> dict[str, Any]:
    """A small valid profile (one manual transition) that tests mutate into bad ones."""
    return {
        "id": "demo-profile",
        "vendor": "Acme",
        "family": "Demo",
        "models": [{"contains": "test model"}],
        "sources": ["https://example.com/docs"],
        "capabilities": {"klipper_possible": False, "notes": "demo only"},
        "services": {"web": 80},
        "states": [
            {
                "id": "stock",
                "title": "Stock",
                "detection": {
                    "all": [{"kind": "http_get", "service": "web", "path": "/"}]
                },
            },
            {
                "id": "custom",
                "title": "Custom",
                "detection": {
                    "all": [{"kind": "http_get", "service": "web", "path": "/custom"}]
                },
            },
        ],
        "transitions": [
            {
                "id": "to-custom",
                "from": "stock",
                "to": "custom",
                "title": "Go custom",
                "steps": [
                    {
                        "id": "flip-it",
                        "title": "Flip it by hand",
                        "kind": "manual",
                        "automatable": False,
                        "risk": "low",
                        "instruction": "Press the button.",
                        "verify": [
                            {"kind": "http_get", "service": "web", "path": "/custom"}
                        ],
                    }
                ],
            }
        ],
    }


def minimal_profile() -> Profile:
    return parse_profile(minimal_profile_data(), "demo.yaml")


# ---- real loopback HTTP server for the e2e tests ------------------------------------------


@dataclass
class LoopbackHttp:
    """A real HTTP server on 127.0.0.1: `routes` maps "METHOD /path?query" -> (status, body)."""

    routes: dict[str, tuple[int, Body] | Callable[[], tuple[int, Body]]] = field(
        default_factory=dict
    )
    requests: list[tuple[str, str, int]] = field(
        default_factory=list
    )  # method, path, body length
    port: int = 0
    _server: ThreadingHTTPServer | None = None

    def set(
        self,
        key: str,
        status: int = 200,
        body: Body | Callable[[], tuple[int, Body]] = "",
    ) -> None:
        self.routes[key] = body if callable(body) else (status, body)

    def mutations(self) -> list[tuple[str, str, int]]:
        return [r for r in self.requests if r[0] != "GET"]

    def start(self) -> LoopbackHttp:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                fake.requests.append((self.command, self.path, length))
                key = f"{self.command} {self.path}"
                route = fake.routes.get(key)
                if (
                    route is None
                ):  # `GET /prefix*` matches any path starting with /prefix
                    route = next(
                        (
                            v
                            for k, v in fake.routes.items()
                            if k.endswith("*") and key.startswith(k[:-1])
                        ),
                        (404, "not found"),
                    )
                status, body = route() if callable(route) else route
                data = as_bytes(body)
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _answer
            do_POST = _answer

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ---- shared K1-shaped world (rooted printer reachable over ssh) ------------------------------

PRINTER_CFG = "[printer]\n#*# [bed_mesh default]\n#*# points = 1\n"


def tgz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def k1_runner(*, cfg: str = PRINTER_CFG, tree: bytes | None = None) -> FakeRunner:
    """A rooted K1 answering the backup pulls of the creality-k1 profile."""
    if tree is None:
        tree = tgz(
            {
                "printer_data/config/printer.cfg": b"x",
                "printer_data/config/gcode_macro.cfg": b"y",
            }
        )
    return (
        FakeRunner()
        .on("cat /usr/data/printer_data/config/printer.cfg", out=cfg)
        .on("tar czf - -C /usr/data printer_data/config", out=tree)
        .on(
            "tar czf - -C /usr/data creality/userdata",
            rc=2,
            err="tar: creality/userdata: No such file or directory",
        )
    )


def k1_http() -> FakeHttp:
    http = FakeHttp()
    http.set("GET", 80, "/info", 200, {"model": "K1 SE"})
    http.set("GET", 4408, "/release_info.json", 404, "")
    return http


@dataclass
class BannerServer:
    """A loopback TCP service that sends `banner` on connect (a stand-in for an sshd)."""

    banner: bytes = b"SSH-2.0-fake_sshd\r\n"
    port: int = 0
    _sock: socket.socket | None = None

    def start(self) -> BannerServer:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])

        def serve() -> None:
            assert self._sock is not None
            while True:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    return
                with conn:
                    conn.sendall(self.banner)

        threading.Thread(target=serve, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._sock is not None:
            self._sock.close()
