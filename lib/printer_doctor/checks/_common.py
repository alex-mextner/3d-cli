"""_common.py — small helpers shared by the check modules (not discovered as checks)."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlsplit

from printer_doctor.model import JsonValue

UI_KINDS = ("Fluidd", "Mainsail")  # the web UIs a browser origin is tested for
DEFAULT_PORTS = {"http": 80, "https": 443}
_SAFE_PATH = re.compile(r"[A-Za-z0-9._~/%+-]*")
_UNITS = ("B", "KB", "MB", "GB", "TB")


def str_list(value: JsonValue) -> list[str]:
    """The strings of a JSON list (anything else, and non-strings, are ignored)."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _host_port(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host as written in a URL, explicit port) of an http(s) URL, else None."""
    try:
        parts = urlsplit(url.strip())
        scheme, host, port = parts.scheme, parts.hostname, parts.port
    except ValueError:
        return None
    if scheme not in DEFAULT_PORTS or not host:
        return None
    return scheme, f"[{host}]" if ":" in host else host, port


def origin_of(url: str) -> str | None:
    """`scheme://host[:port]` of an http(s) URL, the form a browser sends as `Origin`. A default
    port (http 80, https 443) is dropped: browsers never send it and CORSMiddleware compares the
    origin string exactly."""
    found = _host_port(url)
    if found is None:
        return None
    scheme, host, port = found
    return f"{scheme}://{host}" + (
        f":{port}" if port and port != DEFAULT_PORTS[scheme] else ""
    )


def safe_http_url(raw: str) -> str | None:
    """`raw` re-serialised as a plain http(s) URL, or None when it is not one worth contacting.

    A URL a printer's config supplies is fetched from this host and pasted into printed commands:
    accept only http/https with a host, no credentials (dropped), query, fragment or params, and a
    path of URL-safe characters; the result is rebuilt from the parsed parts."""
    try:
        parts = urlparse(raw.strip())
    except ValueError:
        return None
    found = _host_port(raw)
    if found is None or parts.query or parts.fragment or parts.params:
        return None
    if not _SAFE_PATH.fullmatch(parts.path):
        return None
    scheme, host, port = found
    return f"{scheme}://{host}" + (f":{port}" if port else "") + parts.path.rstrip("/")


def human_bytes(n: float) -> str:
    for unit in _UNITS[:-1]:
        if abs(n) < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} {_UNITS[-1]}"
