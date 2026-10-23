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
"""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from printer_doctor.journal import JournalEntry
from printer_doctor.model import CannotSee, JsonValue

if TYPE_CHECKING:
    from printer_ops.base import Adapter, PrinterStatus
    from printer_ops.camera import CameraSpec
    from printer_ops.deploy_check import Show
    from printer_ops.devices import Device
    from printer_ops.webui import WebUiProbe
    from printer_doctor.targets import Target
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


def default_http(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    body: bytes | None,
    timeout: float,
) -> HttpReply:
    """One stdlib HTTP request; keeps the response headers (Date, Access-Control-*) that
    `printer_ops.base.http_request` drops. OSError for every transport failure."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(
        parts.hostname or "", parts.port or 80, timeout=timeout
    )
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


@dataclass(frozen=True)
class Fetched:
    url: str
    reply: HttpReply
    at: float  # the local clock when the answer arrived


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
    ) -> Fetched:
        self.requests.append((method, url))
        fn = self.services.http or default_http
        reply = fn(method, url, headers, body, self.services.timeout)
        return Fetched(url, reply, self.now())

    def external(
        self, method: str, url: str, headers: Mapping[str, str] | None = None
    ) -> Fetched:
        """A request to some other host (Spoolman, a camera streamer); not cached."""
        try:
            return self._send(method, url, headers)
        except OSError as exc:
            raise CannotSee(f"{url} unreachable from this host: {exc}") from exc

    def get(self, path: str, *, fresh: bool = False) -> Fetched:
        """GET `path` on the printer's Moonraker origin, cached (failures too) unless `fresh`."""
        url = self.base_url + path
        if not fresh and path in self._http_cache:
            hit = self._http_cache[path]
            if isinstance(hit, CannotSee):
                raise hit
            return hit
        try:
            got: Fetched | CannotSee = self._send("GET", url)
        except OSError as exc:
            got = CannotSee(f"printer unreachable: {exc} (see check `reachable`)")
        self._http_cache[path] = got
        if isinstance(got, CannotSee):
            raise got
        return got

    def post(self, path: str) -> Fetched:
        """POST (empty body) to the printer; only fixes call this. Never cached."""
        try:
            return self._send("POST", self.base_url + path, body=b"")
        except OSError as exc:
            raise CannotSee(f"POST {path} failed: {exc}", "error") from exc

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

    def job_state(self) -> str | None:
        """print_stats.state read fresh; None when Klipper cannot answer (it is down, so no job
        runs: a shutdown halts the print)."""
        try:
            body = self.result("/printer/objects/query?print_stats", fresh=True)
        except CannotSee:
            return None
        status = body.get("status") if isinstance(body, Mapping) else None
        stats = status.get("print_stats") if isinstance(status, Mapping) else None
        state = stats.get("state") if isinstance(stats, Mapping) else None
        return state if isinstance(state, str) else None

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

            try:
                self._status = self.adapter().status()
            except ThreeDError as exc:
                self._status = CannotSee(exc.message, "error")
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
                self._api_cache[key] = self.api().get(path, **params)
            except ApiError as exc:
                self._api_cache[key] = self._api_failure(path, exc.status, exc.message)
            except CannotSee as exc:
                self._api_cache[key] = exc
        hit = self._api_cache[key]
        if isinstance(hit, CannotSee):
            raise hit
        return hit

    @staticmethod
    def _api_failure(path: str, status: int, message: str) -> CannotSee:
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
            if self.services.webui is not None:
                self._webui = self.services.webui(device)
            else:
                from printer_ops.webui import probe

                self._webui = probe(device)
        return self._webui

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
        """Record an applied fix; the outcome word (journal.record). Never raises."""
        try:
            api = self.api()
        except CannotSee:
            api = None
        if self.services.journal is not None:
            return self.services.journal(entry, api)
        from printer_doctor.journal import record

        return record(entry, api)

    def invalidate(self) -> None:
        """Forget every cached read (after a fix, so the verifying re-run sees the new state)."""
        self._http_cache.clear()
        self._api_cache.clear()
        self._status = None
        self._webui = None


__all__ = [
    "Context",
    "Fetched",
    "HttpFn",
    "HttpReply",
    "Services",
    "as_list",
    "as_obj",
    "default_http",
]
