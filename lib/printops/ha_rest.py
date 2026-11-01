"""ha_rest.py — a small Home Assistant REST client that can WRITE (services, config flows).

ACCESSED VIA: `printops.backup_notify` (persistent_notification on a failed backup),
`printops.ha_wire` (install the backup package, wire the Spoolman integration),
`printops.ha_detect` (read-only probes use `get`).

WHY a second HA client: `printops.homeassistant` is the READ-ONLY sensor reader of 3d-api and says so in
its contract. Everything that changes Home Assistant lives here, behind explicit callers, and is only
reached after the user confirmed (init) or from the failure path of a backup (a notification).

INVARIANTS: stdlib only; redirects are never followed (urllib would replay the bearer token to the
new host); a non-2xx answer raises HaRestError(status, message) with HA's own message.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from printops.client import NoRedirect

_OPENER = urllib.request.build_opener(NoRedirect)


class HaRestError(RuntimeError):
    """Home Assistant answered non-2xx (`status` > 0) or was unreachable (`status` 0)."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class HaRest:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:  # noqa: S310 — operator-configured URL
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise HaRestError(
                f"Home Assistant {method} {path}: HTTP {e.code} {detail}".strip(),
                status=e.code,
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise HaRestError(
                f"Home Assistant at {self.base_url} is unreachable: {getattr(e, 'reason', e)}"
            ) from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            raise HaRestError(
                f"Home Assistant {method} {path}: reply is not JSON"
            ) from e

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, {} if body is None else body)

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return self.post(f"/api/services/{domain}/{service}", data)
