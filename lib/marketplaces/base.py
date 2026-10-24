"""Shared model shape, site names and errors for the marketplace clients."""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

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
    # Search-core additions (#576). `images` lists every picture URL the site gave, cover first;
    # `files_known` is True only when the site's answer carried a file list (an empty list then
    # means "no files", not "not fetched yet"), so a consumer can tell cad=false from cad=unknown.
    date: str = ""
    images: tuple[str, ...] = ()
    makes: int | None = None
    views: int | None = None
    category: str = ""
    files_known: bool = False

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["images"] = list(self.images)
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
        self.exit_code = 2 if kind == "auth" else 1

    @property
    def reason(self) -> str:
        """The message without the `site: ` prefix (derived, so it cannot drift from `message`)."""
        return self.message.removeprefix(f"{self.site}: ")

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


def collect_pages(fetch: Callable[[int, int], list[Model]], limit: int, page_size: int) -> list[Model]:
    """Up to `limit` models from an offset-paged search: `fetch(offset, size)` is called once per
    page of at most `page_size` until `limit` is reached or the site answers a short or empty
    page. Repeats across pages (a site re-ranking between requests) are dropped."""
    models: list[Model] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(-(-limit // page_size) + 1):
        want = min(page_size, limit - len(models))
        if want <= 0:
            break
        page = fetch(offset, want)
        for model in page:
            if model.id not in seen:
                seen.add(model.id)
                models.append(model)
        offset += len(page)
        if len(page) < want:
            break
    return models[:limit]


def unique_urls(*urls: str) -> tuple[str, ...]:
    """The non-empty `urls` in order without repeats."""
    return tuple(dict.fromkeys(u for u in urls if u))


class Deadline:
    """A wall-clock budget for a batch of follow-up requests: work started after it has run out
    is skipped, so a slow site delays a search by at most the budget plus requests in flight."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self._end


class DetailStore(Protocol):
    """Persistent home of per-hit follow-up answers (Thingiverse licence + file list), so a repeat CLI search,
    a separate process with no memory of the last one, does not spend the site's request allowance again.
    Implementations must be thread-safe and best effort: `get` returns None and `put` does nothing when the
    storage is unavailable."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any]) -> None: ...
