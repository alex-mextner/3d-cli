"""Shared model shape, site names and errors for the marketplace clients."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from errors import ThreeDError

SITES: tuple[str, ...] = ("printables", "makerworld", "thingiverse", "cults")


@dataclass(frozen=True)
class ModelFile:
    """One downloadable file of a model. `ref` is the site's handle the download step needs
    (a Printables file id + type, a Thingiverse download URL, a Cults blueprint URL)."""

    name: str
    size: int | None = None
    ref: str = ""
    kind: str = ""


@dataclass(frozen=True)
class Model:
    """A marketplace listing, normalised across sites. `price` is a display string such as
    "4.99 EUR"; `paid` is True whenever the site sells the files (download refuses those)."""

    site: str
    id: str
    title: str
    url: str
    author: str = ""
    author_url: str = ""
    likes: int | None = None
    downloads: int | None = None
    license: str = ""
    price: str = ""
    paid: bool = False
    summary: str = ""
    tags: tuple[str, ...] = ()
    image: str = ""
    files: tuple[ModelFile, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data


# (file, URL to GET, extra request headers) for each file a download step fetches.
DownloadLinks = list[tuple[ModelFile, str, dict[str, str]]]


class MarketplaceError(ThreeDError):
    """A site call failed: blocked by a WAF, needs a login/token, the model is paid, or the
    site answered with an error. `kind` is one of KINDS so scripts can tell them apart in
    `--json` output; exit 2 for missing credentials (a setup problem), 1 otherwise."""

    KINDS = ("auth", "blocked", "paid", "not-found", "http", "network", "parse")

    def __init__(
        self,
        site: str,
        kind: str,
        message: str,
        *,
        remediation: Sequence[str] | None = None,
    ) -> None:
        super().__init__(f"{site}: {message}", command="models", remediation=remediation)
        self.site = site
        self.kind = kind
        self.reason = message
        self.exit_code = 2 if kind == "auth" else 1

    def to_json(self) -> dict[str, Any]:
        return {"site": self.site, "kind": self.kind, "message": self.reason,
                "remediation": list(self.remediation)}


def as_int(value: object) -> int | None:
    """Counts arrive as int, numeric string or null depending on the site."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        return int(value.strip())
    return None


def as_str(value: object) -> str:
    """A string field, stripped; lone surrogates (valid JSON escapes, invalid text) become
    U+FFFD so printing and file names cannot crash on them."""
    if not isinstance(value, str):
        return ""
    if value.isascii():
        return value.strip()
    # UTF-16 round trip: each lone surrogate decodes back as exactly one U+FFFD.
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").strip()


_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)
# NAME_MAX is 255 *bytes*; leave room for `_unique`'s "-N" and the staging dir's affixes.
_MAX_NAME_BYTES = 200


def safe_name(name: str, fallback: str = "file") -> str:
    """A file/dir name from site data that is valid on Linux, macOS and Windows: no path
    separators or characters Windows rejects, no leading dots, no trailing dots/spaces, and
    no reserved device names (CON, NUL, COM1, ...)."""
    cleaned = _clip(re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", name).strip().lstrip(".")).rstrip(". ")
    if _WINDOWS_RESERVED.match(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned or fallback


def _clip(name: str) -> str:
    """Cut `name` to _MAX_NAME_BYTES of UTF-8 (a CJK title hits NAME_MAX at ~85 chars),
    keeping a short extension so `.stl`/`.3mf` survive."""
    if len(name.encode("utf-8")) <= _MAX_NAME_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    suffix = f".{ext}" if dot and stem and len(ext.encode("utf-8")) <= 16 else ""
    base = stem if suffix else name
    budget = _MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    return base.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + suffix


def require_dict(site: str, value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketplaceError(site, "parse", f"unexpected response: {what} is not an object")
    return value


def dict_list(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def child(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """parent[key] when it is an object, else {} — sites send null for absent sub-objects."""
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


def str_list(value: object) -> tuple[str, ...]:
    return tuple(v.strip() for v in value if isinstance(v, str)) if isinstance(value, list) else ()
