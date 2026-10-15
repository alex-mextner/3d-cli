"""client.py — the HTTP client every PrintOps frontend (CLI, bot, HA tests) uses to talk to 3d-api.

ACCESSED VIA: `3d printops <sub>` (lib/printops/cli/*), projects/3d-bot, integration tests.
Domain logic runs inside 3d-api (the single Spoolman writer); frontends only speak HTTP.

CONFIG (first match wins): explicit args → `PRINTOPS_API` / `PRINTOPS_TOKEN` env →
`~/.config/3d-cli/printops.yaml` (`api: http://192.168.0.39:8765`, `token: po_…`).

INVARIANTS:
  - Stdlib + pyyaml only. The network goes through a `Transport`, so tests inject one backed by
    FastAPI's TestClient and never open a socket.
  - Non-2xx answers raise `ApiError(status, message)` with the API's `detail` text.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cli import paths

DEFAULT_TIMEOUT = 10.0
# SSE sends a comment heartbeat every 15 s; a silent socket for longer than this is dead.
STREAM_READ_TIMEOUT = 60.0


class ApiError(RuntimeError):
    """3d-api answered non-2xx (`status` > 0) or was unreachable (`status` 0)."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ApiConfigError(RuntimeError):
    """No API URL configured."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class Transport(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> Response: ...

    def stream_lines(self, url: str, headers: Mapping[str, str],
                     timeout: float) -> AbstractContextManager[Iterator[str]]: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would replay `Authorization: Bearer` to wherever a redirect points; 3d-api never
    redirects, so any 3xx is surfaced as an error instead of followed."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(NoRedirect)


class UrllibTransport:
    """Real HTTP via urllib (no third-party deps); never follows redirects."""

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read(), dict(e.headers.items()) if e.headers else {})
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"3d-api at {url} is unreachable: {getattr(e, 'reason', e)}") from e

    @contextlib.contextmanager
    def stream_lines(self, url: str, headers: Mapping[str, str], timeout: float) -> Iterator[Iterator[str]]:
        req = urllib.request.Request(url, method="GET", headers=dict(headers))
        try:
            resp = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise ApiError(_error_message(e.code, e.read()), status=e.code) from e
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"3d-api at {url} is unreachable: {getattr(e, 'reason', e)}") from e
        def lines() -> Iterator[str]:
            try:
                for raw in resp:
                    yield raw.decode("utf-8").rstrip("\r\n")
            except (OSError, http.client.HTTPException) as e:
                # 3d-api restarted (deploy) or the network dropped mid-stream
                raise ApiError(f"stream from {url} broke: {e!r}") from e

        with resp:
            yield lines()


def _error_message(status: int, body: bytes) -> str:
    try:
        data = json.loads(body)
        detail = data.get("detail", data) if isinstance(data, dict) else data
    except ValueError:
        detail = body.decode("utf-8", "replace")[:300]
    return f"HTTP {status}: {detail}"


def config_path() -> Path:
    return paths.config_dir() / "printops.yaml"


def load_config(env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """(api_url, token) from env, then ~/.config/3d-cli/printops.yaml."""
    env = os.environ if env is None else env
    api, token = env.get("PRINTOPS_API") or None, env.get("PRINTOPS_TOKEN") or None
    cfg = config_path()
    if (api is None or token is None) and cfg.is_file():
        import yaml

        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ApiConfigError(f"{cfg} must be a mapping with `api:` and `token:` keys")
        api = api or (str(data["api"]) if data.get("api") else None)
        token = token or (str(data["token"]) if data.get("token") else None)
    return api, token


def parse_sse(lines: Iterator[str]) -> Iterator[dict[str, str]]:
    """Server-Sent Events frames as {"id", "event", "data"} dicts (comments skipped)."""
    frame: dict[str, str] = {}
    data: list[str] = []
    for line in lines:
        if line == "":
            if data or frame:
                frame["data"] = "\n".join(data)
                yield frame
            frame, data = {}, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            data.append(value)
        elif name in ("id", "event", "retry"):
            frame[name] = value


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None, transport: Transport | None = None,
                 *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport: Transport = transport or UrllibTransport()
        self.timeout = timeout

    @classmethod
    def from_config(cls, *, api: str | None = None, token: str | None = None,
                    transport: Transport | None = None) -> ApiClient:
        env_api, env_token = load_config()
        url = api or env_api
        if not url:
            raise ApiConfigError(
                "no 3d-api URL: set PRINTOPS_API=http://192.168.0.39:8765 or `api:` in "
                f"{config_path()} (or pass --api)"
            )
        return cls(url, token or env_token, transport)

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        url = self.base_url + path
        clean = {k: str(v) for k, v in (params or {}).items() if v is not None}
        return url + ("?" + urllib.parse.urlencode(clean) if clean else "")

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                json_body: Any = None) -> Any:
        body = None if json_body is None else json.dumps(json_body).encode("utf-8")
        headers = self._headers({"Content-Type": "application/json"} if body is not None else None)
        resp = self.transport.request(method, self._url(path, params), headers, body, self.timeout)
        if not 200 <= resp.status < 300:
            raise ApiError(_error_message(resp.status, resp.body), status=resp.status)
        return resp.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, json_body=body if body is not None else {})

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, json_body=body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    # -- core endpoints -----------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """GET /v1/health. A degraded API answers 503 with the same body; return it either way."""
        resp = self.transport.request("GET", self._url("/v1/health"), self._headers(), None, self.timeout)
        try:
            data = resp.json() if resp.body else None
        except ValueError:  # e.g. a proxy's HTML 502 page
            data = None
        if isinstance(data, dict) and "status" in data:
            return data
        raise ApiError(_error_message(resp.status, resp.body), status=resp.status)

    def events(self, *, since: int | None = None, last: int | None = None, limit: int | None = None) -> dict[str, Any]:
        """GET /v1/events as JSON: {"events": [...], "cursor": N}."""
        return dict(self.get("/v1/events", since=since, last=last, limit=limit))

    def follow_events(self, since: int | None = None) -> Generator[dict[str, Any], None, None]:
        """Stream events (SSE) from `since` (default: only new ones) until the caller stops."""
        headers = self._headers({"Accept": "text/event-stream"})
        with self.transport.stream_lines(self._url("/v1/events", {"since": since}), headers,
                                         STREAM_READ_TIMEOUT) as lines:
            for frame in parse_sse(lines):
                if frame.get("event") == "auth_revoked":
                    raise ApiError("HTTP 401: token revoked while streaming", status=401)
                if frame.get("data"):
                    yield dict(json.loads(frame["data"]))
        # The server never ends a healthy stream; EOF means it went away (restart, proxy).
        raise ApiError("event stream ended unexpectedly (3d-api restarted?); resume with --since <last id>")
