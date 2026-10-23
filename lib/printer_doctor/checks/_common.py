"""_common.py — small helpers shared by the check modules (not discovered as checks)."""

from __future__ import annotations

from urllib.parse import urlsplit

from printer_doctor.model import JsonValue

UI_KINDS = ("Fluidd", "Mainsail")  # the web UIs a browser origin is tested for


def str_list(value: JsonValue) -> list[str]:
    """The strings of a JSON list (anything else, and non-strings, are ignored)."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def origin_of(url: str) -> str | None:
    """`scheme://host[:port]` of an http(s) URL, the form a browser sends as `Origin`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n}"
