"""context.py — what a check may look at: one printer, lazily and with a per-run cache.

WHAT: `Context` wraps a `Target` (fleet entry + device) and gives checks cached, injectable access
  to everything they read: Moonraker GETs (`get`/`result`; 12 checks share ONE `/server/info`),
  the adapter and its status, 3d-api (`api_get`), the web-UI probe, `systemctl --user show`, the
  clock and plain HTTP to other hosts (Spoolman).
WHY: checks stay pure functions of a Context, so tests inject fakes for every seam (HTTP, clock,
  systemd, 3d-api, journal) and a run never asks the same question twice.
INVARIANTS: stdlib at import time; anything heavier is imported inside the method that needs it.
  GET results (and failures) are cached until `invalidate()` (called after a fix, so verification
  re-reads). POSTs are never cached and are logged in `requests` like every other call, so a test
  can prove that a read-only run sent none. Transport failures become `CannotSee`: the check
  reports skip/error with the reason, never a pass.
  A dead host costs ONE timeout per run: the first transport failure is remembered in
  `Services.down` (shared by every Context of the run, thread-safe) and every later request to
  that host fails at once with the first error. A timeout / no route marks the whole host (any
  port: a powered-off printer), a refusal or reset only host:port (Moonraker down, camera up).
  `fresh=True` reads and `invalidate()` bypass and clear the mark: a restart fix must be able to
  watch the printer come back. 3d-api is tracked the same way (`Services.down.api`).
"""

from __future__ import annotations

import errno
import http.client
import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from printer_doctor.journal import JournalEntry
from printer_doctor.model import CannotSee, JsonValue, clean

if TYPE_CHECKING:
    from printer_ops.base import Adapter, PrinterStatus
    from printer_ops.camera import CameraSpec
    from printer_ops.deploy_check import Show
    from printer_ops.devices import Device
    from printer_ops.webui import WebUiProbe
    from printer_doctor.targets import Target
    from printer_ops.printer_agent import PrinterAgents
    from printer_ops.subscriptions import DeliveryState, Health, OmpLive, Registry
    from printer_ops.watch_sinks import Runner
    from printops.client import ApiClient

MAX_BODY = 4 * 1024 * 1024


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]  # keys lower-cased
    body: bytes

    def json(self) -> JsonValue:
        value: JsonValue = json.loads(self.body or b"null")
        return value


HttpFn = Callable[[str, str, Mapping[str, str] | None, bytes | None, float], HttpReply]


def endpoint(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of an http(s) URL; OSError for anything a request cannot be sent to."""
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme, parts.hostname, parts.port
    except ValueError as exc:
        raise OSError(f"invalid URL: {exc}") from exc
    if scheme not in ("http", "https"):
        raise OSError(
            f"unsupported URL scheme {scheme!r} (only http and https are contacted)"
        )
    if not host:
        raise OSError("URL has no host")
    return scheme, host.lower(), port or (443 if scheme == "https" else 80)


def default_http(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    body: bytes | None,
    timeout: float,
) -> HttpReply:
    """One stdlib HTTP(S) request; keeps the response headers (Date, Access-Control-*) that
    `printer_ops.base.http_request` drops. The URL scheme is honoured (https: default port 443,
    certificate verified); OSError for every transport failure and for any other scheme."""
    scheme, host, port = endpoint(url)
    parts = urlsplit(url)
    conn: http.client.HTTPConnection
    if scheme == "https":
        import ssl

        conn = http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request(method, path, body=body, headers=dict(headers or {}))
        reply = conn.getresponse()
        return HttpReply(
            reply.status,
            {k.lower(): v for k, v in reply.getheaders()},
            reply.read(MAX_BODY),
        )
    except http.client.HTTPException as exc:
        raise OSError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


_HOST_LEVEL_ERRNOS = frozenset(
    {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}
)


def _host_level(exc: OSError) -> bool:
    """A failure that says the HOST is gone (timeout, no route, name does not resolve), not just
    one port (refused / reset: something else on that host may still answer)."""
    import socket

    return (
        isinstance(exc, (TimeoutError, socket.gaierror))
        or exc.errno in _HOST_LEVEL_ERRNOS
    )


class DownTracker:
    """Hosts (and 3d-api) that already failed during this run; shared by every Context."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, str] = {}
        self._endpoints: dict[tuple[str, int], str] = {}
        self.api: str | None = None

    def mark(self, host: str, port: int, exc: OSError) -> None:
        with self._lock:
            if _host_level(exc):
                self._hosts.setdefault(host, str(exc))
            else:
                self._endpoints.setdefault((host, port), str(exc))

    def reason(self, host: str, port: int) -> str | None:
        with self._lock:
            return self._hosts.get(host) or self._endpoints.get((host, port))

    def forget(self, host: str, port: int) -> None:
        with self._lock:
            self._hosts.pop(host, None)
            self._endpoints.pop((host, port), None)

    def mark_api(self, reason: str) -> None:
        with self._lock:
            self.api = self.api or reason


@dataclass(frozen=True)
class Fetched:
    url: str
    reply: HttpReply
    at: float  # the local clock when the answer arrived


@dataclass
class SubscriptionSources:
    """Where the alert-subscription data comes from; None fields are the real registry file, delivery
    state file and `tg-ctl omp-live` (tests inject temp files and a fake runner)."""

    registry: Registry | None = None
    state: DeliveryState | None = None
    live: OmpLive | None = None


@dataclass
class AgentSources:
    """Where the temporary-printer-agent data comes from; None fields are the real thing (the user's
    state dir, `tmux` / `omp` found on PATH, the real runner). Tests inject a temp state dir, fake tool
    paths (`tools`: name -> path, None = not installed) and a fake tmux runner."""

    state_dir: Path | None = None
    state: DeliveryState | None = None
    tools: Mapping[str, str | None] | None = None
    runner: Runner | None = None


@dataclass
class Services:
    """Every seam a test may replace; None = the real implementation."""

    http: HttpFn | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    systemctl_show: Show | None = None
    api: Callable[[], ApiClient | None] | None = None
    adapter: Callable[[Device], Adapter] | None = None
    webui: Callable[[Device], WebUiProbe] | None = None
    wake_camera: Callable[[Device, CameraSpec], None] | None = None
    journal: Callable[[JournalEntry, ApiClient | None], str] | None = None
    timeout: float = 8.0  # per request
    listen_s: float | None = None  # Creality ws listen window (--secs)
    poll_s: float = 45.0  # how long a restart fix waits for Klipper to come back
    subscriptions: SubscriptionSources = field(default_factory=SubscriptionSources)
    agents: AgentSources = field(default_factory=AgentSources)
    down: DownTracker = field(
        default_factory=DownTracker
    )  # shared by every Context of the run


def as_obj(value: JsonValue, what: str) -> Mapping[str, JsonValue]:
    if isinstance(value, Mapping):
        return value
    raise CannotSee(
        f"{what}: expected a JSON object, got {type(value).__name__}", "error"
    )


def as_list(value: JsonValue, what: str) -> Sequence[JsonValue]:
    if isinstance(value, list):
        return value
    raise CannotSee(
        f"{what}: expected a JSON list, got {type(value).__name__}", "error"
    )


class Context:
    def __init__(self, target: Target, services: Services | None = None) -> None:
        self.target = target
        self.services = services or Services()
        self.requests: list[
            tuple[str, str]
        ] = []  # (method, url) of every call, in order
        self._http_cache: dict[str, Fetched | CannotSee] = {}
        self._adapter: Adapter | None = None
        self._status: PrinterStatus | CannotSee | None = None
        self._api: tuple[ApiClient | None] | None = None
        self._api_cache: dict[str, JsonValue | CannotSee] = {}
        self._webui: WebUiProbe | None = None
        self._sub_health: list[Health] | None = None

    # ---- identity -------------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.target.name

    @property
    def device(self) -> Device:
        """The device to contact; an offline printer has none (the engine never gets here)."""
        if self.target.device is None:
            raise CannotSee("never contacted: access offline")
        return self.target.device

    @property
    def is_moonraker(self) -> bool:
        return self.target.adapter == "moonraker"

    @property
    def base_url(self) -> str:
        d = self.device
        return f"http://{d.host}:{d.port}"

    def moonraker_only(self) -> str | None:
        """`applies` helper: None for a Moonraker-family printer, else the skip reason."""
        if self.is_moonraker:
            return None
        return f"adapter {self.target.adapter or 'none'} is not Moonraker"

    # ---- clock / transport ----------------------------------------------------------------
    def now(self) -> float:
        return self.services.clock()

    def sleep(self, seconds: float) -> None:
        self.services.sleep(seconds)

    def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        *,
        fresh: bool = False,
    ) -> Fetched:
        """One request. Without `fresh`, a host that already failed raises its first error at once
        (and nothing is sent or logged); a success clears the mark."""
        _, host, port = endpoint(url)
        down = self.services.down
        if not fresh and (cached := down.reason(host, port)) is not None:
            raise OSError(cached)
        self.requests.append((method, url))
        fn = self.services.http or default_http
        try:
            reply = fn(method, url, headers, body, self.services.timeout)
        except OSError as exc:
            down.mark(host, port, exc)
            raise
        down.forget(host, port)
        return Fetched(url, reply, self.now())

    def external(
        self, method: str, url: str, headers: Mapping[str, str] | None = None
    ) -> Fetched:
        """A request to some other host (Spoolman, a camera streamer); not cached. Errors name the
        URL with credentials and secret query values redacted."""
        from printer_ops.webui import redact_url

        try:
            return self._send(method, url, headers)
        except OSError as exc:
            raise CannotSee(
                f"{redact_url(url)} unreachable from this host: {clean(str(exc))}"
            ) from exc

    def get(self, path: str, *, fresh: bool = False) -> Fetched:
        """GET `path` on the printer's Moonraker origin, cached (failures too) unless `fresh`."""
        url = self.base_url + path
        if not fresh and path in self._http_cache:
            hit = self._http_cache[path]
            if isinstance(hit, CannotSee):
                raise hit
            return hit
        try:
            got: Fetched | CannotSee = self._send("GET", url, fresh=fresh)
        except OSError as exc:
            got = CannotSee(
                f"printer unreachable: {clean(str(exc))} (see check `reachable`)"
            )
        self._http_cache[path] = got
        if isinstance(got, CannotSee):
            raise got
        return got

    def post(
        self, path: str, payload: Mapping[str, JsonValue] | None = None
    ) -> Fetched:
        """POST to the printer (JSON body when `payload`); only fixes call this. Never cached and
        never short-circuited by the down mark: the answer to a write must come from the printer."""
        headers = {"Content-Type": "application/json"} if payload is not None else None
        data = json.dumps(payload).encode() if payload is not None else b""
        try:
            return self._send("POST", self.base_url + path, headers, data, fresh=True)
        except OSError as exc:
            raise CannotSee(f"POST {path} failed: {clean(str(exc))}", "error") from exc

    def result(self, path: str, *, fresh: bool = False) -> JsonValue:
        """The `result` of a Moonraker JSON answer. 404 -> skip (not served), 401/403 -> skip
        (auth), other HTTP errors / non-JSON -> error."""
        got = self.get(path, fresh=fresh)
        status = got.reply.status
        if status == 404:
            raise CannotSee(f"GET {path} is not served by this printer (HTTP 404)")
        if status in (401, 403):
            raise CannotSee(
                f"GET {path}: HTTP {status}, Moonraker wants authentication"
            )
        if status == 503:
            raise CannotSee(f"GET {path}: HTTP 503 (Klipper is probably not ready)")
        if status >= 400:
            raise CannotSee(f"GET {path}: HTTP {status}", "error")
        try:
            body = got.reply.json()
        except ValueError as exc:
            raise CannotSee(f"GET {path}: not JSON ({exc})", "error") from exc
        if isinstance(body, Mapping) and "result" in body:
            return body["result"]
        return body

    def result_obj(self, path: str, *, fresh: bool = False) -> Mapping[str, JsonValue]:
        return as_obj(self.result(path, fresh=fresh), f"GET {path}")

    # ---- Moonraker shortcuts ----------------------------------------------------------------
    def server_info(self, *, fresh: bool = False) -> Mapping[str, JsonValue]:
        return self.result_obj("/server/info", fresh=fresh)

    def components(self) -> set[str]:
        raw = self.server_info().get("components")
        return (
            {c for c in raw if isinstance(c, str)} if isinstance(raw, list) else set()
        )

    def config(self) -> Mapping[str, JsonValue]:
        """`GET /server/config` -> the `config` mapping (section -> options)."""
        return as_obj(
            self.result_obj("/server/config").get("config"), "/server/config config"
        )

    def config_section(self, name: str) -> Mapping[str, JsonValue] | None:
        section = self.config().get(name)
        return section if isinstance(section, Mapping) else None

    def job_state(self) -> str:
        """print_stats.state, read fresh. CannotSee when it cannot be read (timeout, 5xx, 401,
        missing): callers that must never act mid-print treat that as "busy"."""
        body = self.result("/printer/objects/query?print_stats", fresh=True)
        status = body.get("status") if isinstance(body, Mapping) else None
        stats = status.get("print_stats") if isinstance(status, Mapping) else None
        state = stats.get("state") if isinstance(stats, Mapping) else None
        if not isinstance(state, str):
            raise CannotSee("print_stats.state is missing from objects/query", "error")
        return state

    def device_down(self) -> str | None:
        """Why the printer's host is already known to be unreachable this run, else None."""
        device = self.target.device
        if device is None:
            return None
        return self.services.down.reason(device.host.lower(), device.port)

    # ---- adapter ----------------------------------------------------------------------------
    def adapter(self) -> Adapter:
        if self._adapter is None:
            device = self.device
            if self.services.adapter is not None:
                self._adapter = self.services.adapter(device)
            else:
                from printer_ops import make_adapter

                options: dict[str, float] = {"timeout": self.services.timeout}
                if self.services.listen_s is not None:
                    options["listen_s"] = self.services.listen_s
                self._adapter = make_adapter(device, **options)
        return self._adapter

    def adapter_status(self) -> PrinterStatus:
        """The adapter's normalized status (cached); a failure is a `CannotSee(error)` reason."""
        if self._status is None:
            from errors import ThreeDError

            device = self.device
            cached = self.device_down()
            if cached is not None:
                self._status = CannotSee(
                    f"printer unreachable: {clean(cached)}", "error"
                )
            else:
                try:
                    self._status = self.adapter().status()
                    self.services.down.forget(device.host.lower(), device.port)
                except ThreeDError as exc:
                    cause = exc.__cause__
                    if isinstance(cause, OSError):
                        self.services.down.mark(device.host.lower(), device.port, cause)
                    self._status = CannotSee(clean(exc.message), "error")
        if isinstance(self._status, CannotSee):
            raise self._status
        return self._status

    # ---- 3d-api -----------------------------------------------------------------------------
    def api(self) -> ApiClient:
        if self._api is None:
            if self.services.api is not None:
                self._api = (self.services.api(),)
            else:
                from printops.client import ApiClient, ApiConfigError

                try:
                    self._api = (ApiClient.from_config(),)
                except ApiConfigError:
                    self._api = (None,)
        client = self._api[0]
        if client is None:
            raise CannotSee(
                "3d-api is not configured (set PRINTOPS_API / PRINTOPS_TOKEN or "
                "~/.config/3d-cli/printops.yaml)"
            )
        return client

    def api_configured(self) -> str | None:
        """`applies` helper: None when a 3d-api client exists, else the skip reason."""
        try:
            self.api()
        except CannotSee as exc:
            return exc.reason
        return None

    def api_get(self, path: str, **params: str) -> JsonValue:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if key not in self._api_cache:
            from printops.client import ApiError

            try:
                if self.services.down.api is not None:
                    raise CannotSee(f"3d-api unreachable: {self.services.down.api}")
                self._api_cache[key] = self.api().get(path, **params)
            except ApiError as exc:
                if exc.status == 0:
                    self.services.down.mark_api(exc.message)
                self._api_cache[key] = self._api_failure(path, exc.status, exc.message)
            except CannotSee as exc:
                self._api_cache[key] = exc
        hit = self._api_cache[key]
        if isinstance(hit, CannotSee):
            raise hit
        return hit

    @staticmethod
    def _api_failure(path: str, status: int, message: str) -> CannotSee:
        message = clean(message)
        if status == 0:
            return CannotSee(f"3d-api unreachable: {message}")
        if status in (401, 403):
            return CannotSee(
                f"3d-api refused GET {path} ({message}); the token needs the read scope"
            )
        if status == 404:
            return CannotSee(f"3d-api does not know {path} ({message})")
        return CannotSee(f"3d-api GET {path}: {message}", "error")

    # ---- other seams ------------------------------------------------------------------------
    def webui(self) -> WebUiProbe:
        if self._webui is None:
            device = self.device
            cached = self.device_down()
            if cached is not None:
                raise CannotSee(
                    f"printer unreachable: {clean(cached)} (see check `reachable`)"
                )
            if self.services.webui is not None:
                self._webui = self.services.webui(device)
            else:
                from printer_ops.webui import probe

                self._webui = probe(device)
        return self._webui

    def subscription_registry(self) -> Registry:
        from printer_ops.subscriptions import Registry

        return self.services.subscriptions.registry or Registry()

    def subscription_health(self, *, fresh: bool = False) -> list[Health]:
        """Every alert subscription judged now (session liveness from `tg-ctl omp-live`, delivery
        state from the state file); cached until `invalidate()` (`fresh` re-reads: a fix must act on
        what is true now, not on what the check saw). An unreadable registry is an error."""
        if fresh:
            self._sub_health = None
        if self._sub_health is None:
            from errors import ThreeDError
            from printer_ops.subscriptions import DeliveryState, OmpLive, read_health

            src = self.services.subscriptions
            try:
                self._sub_health = read_health(
                    registry=self.subscription_registry(),
                    state=src.state or DeliveryState(),
                    live=src.live or OmpLive(),
                )
            except ThreeDError as exc:
                raise CannotSee(clean(exc.message), "error") from exc
        return self._sub_health

    def state_dir(self) -> Path:
        """The directory the watchers and agents keep their state in (`<state>/watch`, `<state>/vision`,
        `<state>/printer-agents`)."""
        if self.services.agents.state_dir is not None:
            return self.services.agents.state_dir
        from cli.paths import state_dir

        return state_dir()

    def tool(self, name: str) -> str | None:
        """Where `name` (tmux, omp) is installed, the way the watcher finds it (PATH, then the usual dirs)."""
        tools = self.services.agents.tools
        if tools is not None:
            return tools.get(name)
        from printer_ops.printer_agent import find_tool

        return find_tool(name)

    def printer_agents(self) -> PrinterAgents:
        from printer_ops.printer_agent import PrinterAgents
        from printer_ops.subscriptions import DeliveryState

        src = self.services.agents
        return PrinterAgents(
            runner=src.runner or subprocess.run,
            state=src.state or DeliveryState(),
            tmux=self.tool("tmux"),
            omp=self.tool("omp"),
        )

    def systemctl_show(self, unit: str) -> str:
        if self.services.systemctl_show is not None:
            return self.services.systemctl_show(unit)
        from printer_ops.deploy_check import systemctl_show

        return systemctl_show(unit)

    def wake_camera(self, spec: CameraSpec) -> None:
        if self.services.wake_camera is not None:
            self.services.wake_camera(self.device, spec)
            return
        from printer_ops.camera import open_camera

        open_camera(self.device, spec).keepalive()

    def journal(self, entry: JournalEntry) -> str:
        """Record an applied fix; the outcome word (journal.record). Never raises: whatever goes
        wrong becomes `failed: ...` (the fix already happened and must still be reported)."""
        try:
            try:
                api = None if self.services.down.api is not None else self.api()
            except (
                Exception
            ):  # not configured, or a config that cannot be read: buffer only
                api = None
            if self.services.journal is not None:
                return self.services.journal(entry, api)
            from printer_doctor.journal import record

            return record(entry, api)
        except Exception as exc:
            return clean(f"failed: {type(exc).__name__}: {exc}")

    def invalidate(self) -> None:
        """Forget every cached read (after a fix, so the verifying re-run sees the new state)."""
        self._http_cache.clear()
        self._api_cache.clear()
        self._status = None
        self._webui = None
        self._sub_health = None
        device = self.target.device
        if device is not None:
            self.services.down.forget(device.host.lower(), device.port)


__all__ = [
    "Context",
    "DownTracker",
    "Fetched",
    "HttpFn",
    "HttpReply",
    "AgentSources",
    "Services",
    "SubscriptionSources",
    "as_list",
    "as_obj",
    "default_http",
    "endpoint",
]
