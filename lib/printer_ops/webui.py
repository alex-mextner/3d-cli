"""webui.py — where a printer's web interface actually answers (read-only probe for `3d printers show`).

WHAT: GETs the places a printer's UI normally lives and reports only those that answered, each
  identified by its page: Moonraker printers — `http://host/`, `:4408` (Fluidd's default),
  `:4409` (Mainsail's default) and the Moonraker API root (`/server/info` on the device port:
  version + Klippy state); creality-stock — its built-in UI at `http://host:<port>/`.
WHY a probe and not a table: the same machine can run Fluidd on 80 (MainsailOS/FluiddPi) or on
  4408 (Creality Helper Script), so the only honest answer is what replies. The page <title>
  tells Fluidd from Mainsail; anything else is listed as "web page" with its title.
INVARIANTS: stdlib only; GET only; one short-timeout request per candidate, run concurrently,
  so a dead host costs one timeout, not one per URL. URLs are printed credential-free
  (`redact_url`). `THREED_WEBUI_PORTS=80,4408,4409` overrides the probed ports (a printer behind
  a port-forward; the test suite points it at loopback fakes).
"""
from __future__ import annotations

import http.client
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from printer_ops.base import http_json, http_request

if TYPE_CHECKING:
    from printer_ops.devices import Device

ENV_PORTS = "THREED_WEBUI_PORTS"
DEFAULT_PORTS = (80, 4408, 4409)
PROBE_TIMEOUT_S = 2.0
MAX_PAGE_BYTES = 64 * 1024
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SECRET_QUERY = re.compile(r"token|key|pass|pwd|secret|auth|sig|user|usr|login|cred|session|sid", re.IGNORECASE)
MAX_PORTS = 8
_KNOWN_UIS = ("Fluidd", "Mainsail", "OctoPrint", "Creality")


@dataclass(frozen=True)
class WebUi:
    url: str
    kind: str            # Fluidd | Mainsail | OctoPrint | Creality | Moonraker API | web page
    title: str | None
    http_status: int
    note: str | None = None


@dataclass
class WebUiProbe:
    found: list[WebUi] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # candidates that did not answer at all
    tried: int = 0


def redact_url(url: str) -> str:
    """`url` without user:password@ and with credential-looking query values masked. A URL that
    cannot be parsed is not echoed at all (it may still carry a secret)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return "(invalid URL)"
    query = urlencode([(k, "***" if _SECRET_QUERY.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)], safe="*")
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def web_ports() -> tuple[int, ...]:
    """The ports probed for a web UI: THREED_WEBUI_PORTS (comma separated, 1-65535, at most
    MAX_PORTS) or 80, 4408, 4409. A bad value is an InvalidArgument, not a silent default."""
    raw = os.environ.get(ENV_PORTS, "").strip()
    if not raw:
        return DEFAULT_PORTS
    try:
        ports = tuple(dict.fromkeys(int(p) for p in raw.split(",") if p.strip()))
    except ValueError:
        ports = ()
    if not ports or len(ports) > MAX_PORTS or not all(0 < p < 65536 for p in ports):
        from errors import InvalidArgument

        raise InvalidArgument(ENV_PORTS, raw, [f"1 to {MAX_PORTS} comma-separated ports 1-65535, e.g. 80,4408,4409"],
                              command="printers")
    return ports


def page_title(html: str) -> str | None:
    m = _TITLE.search(html)
    title = " ".join(m.group(1).split()) if m else ""
    return title[:80] or None


def identify(title: str | None, body: str) -> str:
    """Which web UI a page is: Fluidd/Mainsail/OctoPrint/Creality by <title>, then by body text."""
    lowered = (title or "").lower()
    for kind in _KNOWN_UIS:
        if kind.lower() in lowered:
            return kind
    head = body[:MAX_PAGE_BYTES].lower()
    found = [(head.index(k.lower()), k) for k in _KNOWN_UIS if k.lower() in head]
    return min(found)[1] if found else "web page"


def _base_url(host: str, port: int) -> str:
    return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"


def _page(url: str, timeout: float) -> WebUi | None:
    """The UI at `url`, or None when the server answered but has no page there; OSError when
    nothing answered."""
    status, data = http_request("GET", url, timeout=timeout, max_bytes=MAX_PAGE_BYTES)
    if not (200 <= status < 400 or status in (401, 403)):
        return None
    body = data.decode("utf-8", "replace")
    title = page_title(body)
    note = "login required" if status in (401, 403) else None
    return WebUi(redact_url(url), identify(title, body), title, status, note)


def _moonraker_api(device: Device, timeout: float) -> WebUi | None:
    url = _base_url(device.host, device.port)
    body = http_json("GET", url + "server/info", timeout=timeout)
    info = body.get("result") if isinstance(body, dict) else None
    if not isinstance(info, dict):
        return None
    version = info.get("moonraker_version")
    klippy = info.get("klippy_state")
    note = ", ".join(x for x in (f"Moonraker {version}" if version else None,
                                 f"klippy {klippy}" if klippy else None) if x)
    return WebUi(redact_url(url), "Moonraker API", None, 200, note or None)


def probe(device: Device, *, ports: tuple[int, ...] | None = None, timeout: float = PROBE_TIMEOUT_S) -> WebUiProbe:
    """Probe the web UI places of `device` (`ports`: default `web_ports()`); only what answered
    lands in `found`, in probe order."""
    jobs: list[tuple[str, Callable[[], WebUi | None]]] = []
    if device.adapter == "moonraker":
        for port in ports if ports is not None else web_ports():
            url = _base_url(device.host, port)
            jobs.append((url, partial(_page, url, timeout)))
        jobs.append((_base_url(device.host, device.port), partial(_moonraker_api, device, timeout)))
    else:
        url = _base_url(device.host, device.port)
        jobs.append((url, partial(_page, url, timeout)))
    result = WebUiProbe(tried=len(jobs))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [(url, pool.submit(fn)) for url, fn in jobs]
        for url, future in futures:
            try:
                hit = future.result()
            except (OSError, ValueError, http.client.HTTPException) as exc:  # nothing answered / not JSON / not HTTP
                result.errors.append(f"{redact_url(url)}: {exc}")
                continue
            if hit is not None:
                result.found.append(hit)
    return result
